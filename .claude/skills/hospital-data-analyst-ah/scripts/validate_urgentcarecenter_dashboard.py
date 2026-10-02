#!/usr/bin/env python3
"""Validate a complete AH urgentcarecenter (UCC / A&E attendance) month/case-end-type/
acuity export.

Same shape and gates as the other five validators (month-coverage gate, strict integer
parsing, mapping-completeness gate, arithmetic-identity check, fail-closed audit JSON),
adapted to urgentcarecenter.md / data-ontology.yaml's urgentcarecenter entry:

  - No ward-style exclusion list applies to this table (confirmed against
    data-ontology.yaml: urgentcarecenter's only filters are
    `case_end_type <> 'Cancelled'` and `att_phy_name <> 'CANCELLATION'` -- there is no
    equivalent of admission/discharge/inflight's 6-code ward exclusion set here).
    `Case_End_Type = 'Cancelled'` leaking into the export is checked below as a hard
    error, the same way a leaked excluded ward is treated elsewhere. This script cannot
    see the raw `Att_Phy_Name` column from an aggregated export (it isn't part of the
    month/case_end_type/acuity grain), so an `Att_Phy_Name = 'CANCELLATION'` leak is not
    detectable here -- same class of limitation as inflight's same-day top-up union,
    enforced instead by the SKILL.md pre-query workflow forcing urgentcarecenter.md to be
    read before any SQL is written.
  - `case_end_type` in the export is the POST-mapping (relabeled) value, produced by the
    CASE WHEN in urgentcarecenter.md's "Case_End_Type -- full mapping". Validated against
    `case-end-type-lookup.json` (36 known raw values -> final labels, mirrors the .md
    1:1). Two distinct problems are told apart:
      * a value that is one of the 9 raw codes the mapping is supposed to rename (e.g.
        "Admitted" instead of "Admit") means the SQL's CASE WHEN was not applied --
        that's a hard error, not a warning.
      * a value outside the full known-output catalog entirely is a genuinely
        undocumented code the source system started emitting -- flagged as a warning
        (escalate with --fail-on-unmapped), same idiom as unmapped Class_abc/Sub-Specialty
        values in the other validators.
      * null/blank case_end_type is allowed (documented in the .md) and tracked
        separately, not an error.
  - `acuity` is `COALESCE(NULLIF("CONSULT_ACUITY", ''), "TRIAGE_ACUITY", "PACS")` (Consult
    Acuity, derived) per urgentcarecenter.md -- a free-text field with no closed
    vocabulary to validate against (unlike Class_abc),
    so this script only checks it resolves to a non-blank value for effectively every row
    (PACS alone covers ~99.8% of real rows per the .md) and warns on any blank.
  - Production's primary acuity field changed from TRIAGE_ACUITY to CONSULT_ACUITY on
    2026-07-01 (see urgentcarecenter.md). This script cannot tell which raw field the SQL
    actually used, so it only checks whether the *requested* month range straddles that
    boundary and emits an informational warning reminding the caller to confirm the SQL
    uses the current-production fallback chain for the post-2026-07 portion -- never a
    hard error, since a request entirely on one side of the boundary is unaffected.
  - Benchmark is a single annual attendance total (TABLE 3.1 "Attendance by PACS and
    Arrival Mode", cross-validated against TABLE 3.2 "Attendance by PACS and Case End
    Type" -- both tables' grand-total rows matched exactly for all 36 months across
    CY2023-25).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

# urgentcarecenter has no ward-style exclusion list (see data-ontology.yaml) -- the only
# structural exclusion is Case_End_Type = 'Cancelled', which is supposed to be filtered
# out of the SQL entirely before this export is built. Its presence here means the
# exclusion filter leaked.
EXCLUDED_CASE_END_TYPE = "Cancelled"

# Production's primary acuity field switched from TRIAGE_ACUITY to CONSULT_ACUITY on this
# date (urgentcarecenter.md "Production changed its primary acuity field on 2026-07-01").
ACUITY_FIELD_SWITCH_MONTH = "2026-07"

REQUIRED_COLUMNS = {"month_date", "case_end_type", "acuity", "attendances"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Complete SQL CSV export")
    parser.add_argument(
        "--case-end-type-mapping",
        required=True,
        type=Path,
        help="references/case-end-type-lookup.json -- raw Case_End_Type -> relabeled "
        "value -- used to confirm the export's case_end_type column only contains "
        "already-relabeled values, never a raw code that should have been renamed",
    )
    parser.add_argument("--start-month", required=True, help="Inclusive YYYY-MM")
    parser.add_argument("--end-month", required=True, help="Inclusive YYYY-MM")
    parser.add_argument("--output", required=True, type=Path, help="Classified aggregate CSV")
    parser.add_argument("--audit", required=True, type=Path, help="QC audit JSON")
    parser.add_argument(
        "--benchmark-file",
        type=Path,
        default=None,
        help="references/ah-yearly-benchmarks.json -- the 'urgentcarecenter' section's "
        "'monthly' block is {metric: {'YYYY-MM': value}} for attendances, extracted "
        "from the official HIM report (TABLE 3.1, cross-checked against TABLE 3.2). The "
        "metric is checked by summing its monthly series over exactly the requested "
        "--start-month..--end-month range -- works for any range (a partial year, a "
        "full year, or one spanning a year boundary), not just a full calendar year. "
        "Missing monthly coverage for the full requested range is skipped with a "
        "warning, not an error.",
    )
    parser.add_argument("--fail-on-unmapped", action="store_true")
    return parser.parse_args()


def month_sequence(start: str, end: str) -> list[str]:
    try:
        current = datetime.strptime(start, "%Y-%m")
        finish = datetime.strptime(end, "%Y-%m")
    except ValueError as exc:
        raise ValueError("start-month and end-month must use YYYY-MM") from exc
    if current > finish:
        raise ValueError("start-month must not be after end-month")
    result = []
    while current <= finish:
        result.append(current.strftime("%Y-%m"))
        year = current.year + (current.month == 12)
        month = 1 if current.month == 12 else current.month + 1
        current = current.replace(year=year, month=month)
    return result


def load_monthly_benchmarks(path: Path, table: str) -> dict[str, dict[str, int]]:
    """Pull one table's "monthly" benchmark series out of the consolidated
    ah-yearly-benchmarks.json: {metric: {"YYYY-MM": value}}. The per-year annual
    totals alongside it in the file are a human-readable summary only -- the
    validator only reads "monthly", since summing the right months covers the
    full-year case too plus anything narrower or spanning a year boundary.
    Missing/absent monthly data means every check against it is skipped with a
    warning, never an error."""
    data = json.loads(path.read_text(encoding="utf-8"))
    monthly = data.get(table, {}).get("monthly", {})
    for metric, series in monthly.items():
        if not isinstance(series, dict):
            raise ValueError(f"{path}: {table}.monthly.{metric!r} must be an object")
    return monthly


def monthly_benchmark_total(
    monthly: dict[str, dict[str, int]], metric: str, months: list[str]
) -> int | None:
    """Sum a benchmark metric's monthly series over exactly the requested months.
    Returns None (never 0) if the metric has no monthly series at all, or is
    missing any one of the requested months -- the caller skips the check with
    a warning rather than comparing actual against a partial/wrong sum."""
    series = monthly.get(metric)
    if series is None:
        return None
    total = 0
    for m in months:
        if m not in series:
            return None
        total += series[m]
    return total


def integral_count(raw: str, row_number: int, column: str) -> int:
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValueError(f"row {row_number}: invalid {column} value {raw!r}") from exc
    if value < 0 or value != value.to_integral_value():
        raise ValueError(f"row {row_number}: {column} must be a non-negative integer")
    return int(value)


def load_case_end_type_mapping(path: Path) -> tuple[int, set[str], set[str]]:
    """Load references/case-end-type-lookup.json. Returns (raw-code count, the set of
    raw codes the mapping is supposed to rename, the set of valid final output values).
    A code whose export value equals one of the "needs renaming" raw codes means the
    SQL's CASE WHEN never ran for that row."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if len(data) != 36:
        raise ValueError(
            f"{path}: completeness failed -- expected 36 known raw Case_End_Type "
            f"values, found {len(data)}"
        )
    needs_relabel = {raw for raw, final in data.items() if raw != final}
    valid_outputs = set(data.values())
    return len(data), needs_relabel, valid_outputs


def main() -> int:
    args = parse_args()
    errors: list[str] = []
    warnings: list[str] = []

    try:
        expected_months = month_sequence(args.start_month, args.end_month)
        monthly_benchmarks = (
            load_monthly_benchmarks(args.benchmark_file, "urgentcarecenter")
            if args.benchmark_file
            else {}
        )
        mapping_count, needs_relabel, valid_outputs = load_case_end_type_mapping(
            args.case_end_type_mapping
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"QC FAILED: {exc}", file=sys.stderr)
        return 1

    if expected_months[0] < ACUITY_FIELD_SWITCH_MONTH <= expected_months[-1]:
        warnings.append(
            f"requested range {expected_months[0]}..{expected_months[-1]} spans the "
            f"{ACUITY_FIELD_SWITCH_MONTH}-01 acuity field switch (TRIAGE_ACUITY -> "
            "CONSULT_ACUITY as primary) -- confirm the SQL used the current-production "
            "fallback chain, not an assumption carried over from before the switch"
        )

    rows: list[dict[str, object]] = []
    input_rows = 0
    try:
        with args.input.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing_columns = REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing_columns:
                raise ValueError(f"missing CSV columns: {sorted(missing_columns)}")
            for row_number, row in enumerate(reader, start=2):
                input_rows += 1
                month = str(row["month_date"] or "").strip()[:7]
                datetime.strptime(month, "%Y-%m")
                case_end_type = str(row["case_end_type"] or "").strip()
                acuity = str(row["acuity"] or "").strip()
                attendances = integral_count(row["attendances"], row_number, "attendances")
                rows.append(
                    {
                        "month": month,
                        "case_end_type": case_end_type,
                        "acuity": acuity,
                        "attendances": attendances,
                    }
                )
    except (OSError, ValueError) as exc:
        print(f"QC FAILED: {exc}", file=sys.stderr)
        return 1

    observed_months = sorted({r["month"] for r in rows})
    missing_months = sorted(set(expected_months) - set(observed_months))
    extra_months = sorted(set(observed_months) - set(expected_months))
    if missing_months:
        errors.append(f"missing requested months: {missing_months}")
    if extra_months:
        errors.append(f"months outside requested range: {extra_months}")

    leaked_cancelled_total = 0
    unrelabeled_leak_total = 0
    unmapped_total = 0
    unspecified_total = 0
    valid_total = 0
    blank_acuity_total = 0
    unrelabeled_values: set[str] = set()
    unmapped_values: set[str] = set()
    monthly_total: dict[str, int] = defaultdict(int)
    output_rows: list[dict[str, object]] = []

    for row in rows:
        case_end_type = row["case_end_type"]
        acuity = row["acuity"]
        attendances = row["attendances"]
        month = row["month"]

        if not acuity:
            blank_acuity_total += attendances

        if case_end_type == EXCLUDED_CASE_END_TYPE:
            leaked_cancelled_total += attendances
            errors.append(
                f"{month}: excluded case_end_type 'Cancelled' present in export "
                f"({attendances} attendances)"
            )
            classification = "Leaked-Cancelled"
        elif case_end_type in needs_relabel:
            unrelabeled_values.add(case_end_type)
            unrelabeled_leak_total += attendances
            errors.append(
                f"{month}: un-relabeled raw case_end_type {case_end_type!r} present in "
                f"export -- the mapping CASE WHEN did not run ({attendances} attendances)"
            )
            classification = "Leaked-unrelabeled"
        elif not case_end_type:
            unspecified_total += attendances
            classification = "Unspecified"
        elif case_end_type not in valid_outputs:
            unmapped_values.add(case_end_type)
            unmapped_total += attendances
            classification = "Unmapped"
        else:
            valid_total += attendances
            classification = "Valid"

        monthly_total[month] += attendances
        output_rows.append({**row, "classification": classification})

    source_total = sum(monthly_total.values())
    classified_total = (
        valid_total
        + unspecified_total
        + unmapped_total
        + unrelabeled_leak_total
        + leaked_cancelled_total
    )
    if classified_total != source_total:
        errors.append(
            "valid + unspecified + unmapped + unrelabeled-leak + cancelled-leak does "
            "not equal source total"
        )

    expected = monthly_benchmark_total(monthly_benchmarks, "attendances", expected_months)
    if expected is None:
        if monthly_benchmarks:
            warnings.append(
                f"no monthly benchmark data for attendances covering the full "
                f"requested range {expected_months[0]}..{expected_months[-1]} -- skipped"
            )
    elif source_total != expected:
        errors.append(
            f"attendances total {source_total} != benchmark {expected} "
            f"(summed {expected_months[0]}..{expected_months[-1]})"
        )

    if unmapped_values:
        warnings.append(
            f"{len(unmapped_values)} case_end_type values are outside the known "
            f"36-code catalog ({unmapped_total} attendances) -- may be a genuinely new "
            "source-system code not yet documented in urgentcarecenter.md"
        )
        if args.fail_on_unmapped:
            errors.append(
                "unmapped case_end_type values present and --fail-on-unmapped was requested"
            )

    if blank_acuity_total:
        warnings.append(
            f"{blank_acuity_total} rows have a blank acuity after the "
            "CONSULT_ACUITY/PACS fallback -- both source fields were empty"
        )

    audit = {
        "qc_status": "PASSED" if not errors else "FAILED",
        "input_rows": input_rows,
        "requested_months": expected_months,
        "observed_months": observed_months,
        "missing_months": missing_months,
        "extra_months": extra_months,
        "case_end_type_mapping_records": mapping_count,
        "source_total": source_total,
        "valid_total": valid_total,
        "unspecified_total": unspecified_total,
        "unmapped_total": unmapped_total,
        "unrelabeled_leak_total": unrelabeled_leak_total,
        "leaked_cancelled_total": leaked_cancelled_total,
        "blank_acuity_total": blank_acuity_total,
        "unmapped_case_end_type_values": sorted(unmapped_values),
        "unrelabeled_case_end_type_values": sorted(unrelabeled_values),
        "monthly_totals": dict(sorted(monthly_total.items())),
        "warnings": warnings,
        "errors": errors,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["month", "case_end_type", "acuity", "attendances", "classification"]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)
    args.audit.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "qc_status": audit["qc_status"],
                "source_total": source_total,
                "valid_total": valid_total,
                "unmapped_total": unmapped_total,
                "unrelabeled_leak_total": unrelabeled_leak_total,
                "leaked_cancelled_total": leaked_cancelled_total,
                "errors": errors,
                "warnings": warnings,
            },
            ensure_ascii=False,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
