#!/usr/bin/env python3
"""Validate a complete AH procedure month/adm-type-bucket/class export.

Same shape and gates as the other AH validators (month-coverage gate, strict
integer parsing, mapping-completeness gate, arithmetic-identity checks,
fail-closed audit JSON), adapted to procedure.md and data-ontology.yaml:

  - No ward or Trt_Cat style inclusion/exclusion filter exists for this table in
    data-ontology.yaml (its "procedure" entry has no `filters:` key at all, only
    a counting-grain caution) -- so there is nothing here analogous to
    admission/discharge's ward exclusion or outpatient's Visit_Type/Trt_Cat
    leak checks. Adm_Type is a pure classification, not an inclusion/exclusion
    filter (see next point), so nothing is flagged as a "leak."
  - Adm_Type is NOT a closed exclusion/inclusion set the way it is for
    admission/discharge -- procedure.md splits it into two buckets (day-surgery
    {DS,ES,DO} vs inpatient {DI,SD,EM,EL}) and is explicit that some rows
    legitimately have no value or a code in neither bucket (e.g. RA/TA are named
    as falling into neither). A row outside both buckets is tracked as
    "Unclassified" and reported for visibility -- it is NOT an error, unlike a
    ward/visit-type leak in the other validators. Confirmed against the real
    1000-row sample: Adm_Type splits cleanly into DS/ES/DO (865), DI/SD/EM/EL
    (127), and blank (8) with none left over.
  - Episode-vs-procedure-row counting (data-ontology.yaml's own caution) is
    resolved here as procedure-ROW counting (COUNT(*)) for the primary grain,
    matching both benchmark sources below and procedure.md's own default SQL
    (plain COUNT(*), no DISTINCT). Day-surgery EPISODE counting is a SEPARATE,
    optional check: if the input CSV also carries an "episodes" column (SQL:
    COUNT(DISTINCT COALESCE("Adm_CSN","case_no")) per procedure.md, day-surgery
    rows only), it's summed and checked against the benchmark file's monthly
    day_surgery_episodes series (TABLE 1.4), summed over whatever months were
    actually requested -- not just a full CY2023/24/25. Absent that column,
    the episode check is silently skipped -- it's genuinely optional, not
    every caller will compute it.
  - class_abc collapse is identical to validate_outpatient_dashboard.py's
    Private/Subsidised split ({A1,B1,Private}->Private, {B2,C,Subsidized}
    ->Subsidised) -- procedure.md's own CASE expression is written with an ELSE
    passthrough for the already-named Private/Subsidized values but resolves to
    the exact same two buckets. Same completeness self-check reused verbatim.
  - Two independent benchmark identities, not one, because the two source
    tables cover different populations with different available splits:
      * TABLE 1.3 "Day Surgery Procedures by Specialty, Clinic and Patient
        Class" -- day-surgery bucket, with a Private/Subsidised split.
      * TABLE 2.3 "Inpatient Procedures by Specialty and OP Table" -- inpatient
        bucket, grand total only, no class split available.
    Both reproduced exactly by independently summing their own subtotals against
    each sheet's own "Total" row before trusting the numbers (10237/11516/12114
    day-surgery for CY2023-25; 1253/1349/1643 inpatient).
  - Sub-Specialty_Final harmonization and the OpTable transform are display-
    grouping concerns for the dashboards themselves, not this identity/
    benchmark-grain aggregate (same scoping reasoning as outpatient's Trt_OU
    relabeling) -- out of scope here, not silently assumed correct.
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

# procedure.md "Adm_Type segmentation" -- two classification buckets, not an
# inclusion/exclusion filter. Anything outside both is "Unclassified" and is
# expected (includes RA/TA), not an error.
DAY_SURGERY_TYPES = {"DS", "ES", "DO"}
INPATIENT_TYPES = {"DI", "SD", "EM", "EL"}

# procedure.md "Patient class" collapse -- identical buckets to outpatient's.
PRIVATE_CLASSES = {"A1", "B1", "Private"}
SUBSIDISED_CLASSES = {"B2", "C", "Subsidized"}

REQUIRED_COLUMNS = {"month_date", "adm_type", "class_abc", "procedures"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Complete SQL CSV export")
    parser.add_argument(
        "--class-mapping",
        required=True,
        type=Path,
        help="references/pt-class-lookup.json -- raw code -> Class_abc/Class_abc_MOH/"
        "Resident_Type/Resident_MOH -- used to validate the mapping is complete and "
        "that the SQL's own class_abc output only uses known values",
    )
    parser.add_argument("--start-month", required=True, help="Inclusive YYYY-MM")
    parser.add_argument("--end-month", required=True, help="Inclusive YYYY-MM")
    parser.add_argument("--output", required=True, type=Path, help="Classified aggregate CSV")
    parser.add_argument("--audit", required=True, type=Path, help="QC audit JSON")
    parser.add_argument(
        "--benchmark-file",
        type=Path,
        default=None,
        help="references/ah-yearly-benchmarks.json -- the 'procedure' section's "
        "'monthly' block is {metric: {'YYYY-MM': value}} for day_surgery_procedures, "
        "day_surgery_private, day_surgery_subsidised, inpatient_procedures, and "
        "day_surgery_episodes, extracted from the official yearly HIM report "
        "workbooks (TABLE 1.3 day-surgery, TABLE 2.3 inpatient, TABLE 1.4 day-surgery "
        "episodes). Each metric is checked by summing its monthly series over exactly "
        "the requested --start-month..--end-month range -- works for any range (a "
        "partial year, a full year, or one spanning a year boundary), not just a full "
        "calendar year. A metric missing monthly coverage for the full requested range "
        "is skipped with a warning, not an error; day_surgery_episodes is further only "
        "checked when the input CSV provides an 'episodes' column.",
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
    totals alongside it in the file are a human-readable summary only (and the
    original CY2023-25 numbers this monthly series was derived from and
    cross-checked against) -- the validator only reads "monthly", since summing
    the right months covers the full-year case too plus anything narrower or
    spanning a year boundary. Missing/absent monthly data means every check
    against it is skipped with a warning, never an error."""
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


def load_class_mapping(path: Path) -> tuple[int, set[str]]:
    """Load references/pt-class-lookup.json -- shared with the other AH validators."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if len(data) != 18 or "" in data:
        raise ValueError(
            f"{path}: completeness failed -- expected 18 unique raw codes, found {len(data)}"
        )
    valid_class_abc: set[str] = set()
    for raw_code, record in data.items():
        class_abc = str(record.get("class_abc", "")).strip()
        if not class_abc:
            raise ValueError(f"{path}: {raw_code!r} has a blank class_abc")
        valid_class_abc.add(class_abc)
    uncovered = valid_class_abc - (PRIVATE_CLASSES | SUBSIDISED_CLASSES)
    if uncovered:
        raise ValueError(
            f"{path}: class_abc value(s) {sorted(uncovered)} are not covered by the "
            "Private/Subsidised Pat_Class collapse -- update PRIVATE_CLASSES/"
            "SUBSIDISED_CLASSES in this script to match pt-class-lookup.md"
        )
    return len(data), valid_class_abc


def bucket_of(adm_type: str) -> str:
    if adm_type in DAY_SURGERY_TYPES:
        return "DaySurgery"
    if adm_type in INPATIENT_TYPES:
        return "Inpatient"
    return "Unclassified"


def main() -> int:
    args = parse_args()
    errors: list[str] = []
    warnings: list[str] = []

    try:
        expected_months = month_sequence(args.start_month, args.end_month)
        monthly_benchmarks = (
            load_monthly_benchmarks(args.benchmark_file, "procedure") if args.benchmark_file else {}
        )
        mapping_count, valid_class_abc = load_class_mapping(args.class_mapping)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"QC FAILED: {exc}", file=sys.stderr)
        return 1

    rows: list[dict[str, object]] = []
    input_rows = 0
    has_episodes_column = False
    try:
        with args.input.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing_columns = REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing_columns:
                raise ValueError(f"missing CSV columns: {sorted(missing_columns)}")
            # "episodes" is optional -- COUNT(DISTINCT COALESCE("Adm_CSN","case_no"))
            # per procedure.md, day-surgery rows only. Absent callers just get the
            # procedure-row grain; present callers also get the TABLE 1.4 check below.
            has_episodes_column = "episodes" in (reader.fieldnames or [])
            for row_number, row in enumerate(reader, start=2):
                input_rows += 1
                month = str(row["month_date"] or "").strip()[:7]
                datetime.strptime(month, "%Y-%m")
                adm_type = str(row["adm_type"] or "").strip()
                class_abc = str(row["class_abc"] or "").strip()
                procedures = integral_count(row["procedures"], row_number, "procedures")
                episodes = (
                    integral_count(row["episodes"], row_number, "episodes")
                    if has_episodes_column
                    else 0
                )
                rows.append(
                    {
                        "month": month,
                        "adm_type": adm_type,
                        "class_abc": class_abc,
                        "procedures": procedures,
                        "episodes": episodes,
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

    unmapped_classes: set[str] = set()
    day_surgery_annual: dict[str, int] = defaultdict(int)
    inpatient_annual: dict[str, int] = defaultdict(int)
    unclassified_annual: dict[str, int] = defaultdict(int)
    day_surgery_episodes_annual: dict[str, int] = defaultdict(int)
    day_surgery_private = 0
    day_surgery_subsidised = 0
    day_surgery_unclassified_class_total = 0
    output_rows: list[dict[str, object]] = []

    for row in rows:
        adm_type = row["adm_type"]
        class_abc = row["class_abc"]
        procedures = row["procedures"]
        episodes = row["episodes"]
        month = row["month"]
        year = month[:4]

        bucket = bucket_of(adm_type)
        if bucket == "DaySurgery":
            day_surgery_annual[year] += procedures
            day_surgery_episodes_annual[year] += episodes
        elif bucket == "Inpatient":
            inpatient_annual[year] += procedures
        else:
            unclassified_annual[year] += procedures

        if class_abc not in valid_class_abc:
            pat_class = "Unmapped"
            unmapped_classes.add(class_abc)
            if bucket == "DaySurgery":
                day_surgery_unclassified_class_total += procedures
        elif class_abc in SUBSIDISED_CLASSES:
            pat_class = "Subsidised"
            if bucket == "DaySurgery":
                day_surgery_subsidised += procedures
        else:
            pat_class = "Private"
            if bucket == "DaySurgery":
                day_surgery_private += procedures

        output_rows.append({**row, "adm_type_bucket": bucket, "pat_class": pat_class})

    day_surgery_source_total = sum(day_surgery_annual.values())
    if (
        day_surgery_private + day_surgery_subsidised + day_surgery_unclassified_class_total
        != day_surgery_source_total
    ):
        errors.append(
            "day-surgery private + subsidised + unclassified-class does not equal "
            "day-surgery source total"
        )

    # The month-coverage gate above already forces the CSV to contain exactly
    # the requested months, so each metric's grand total is directly comparable
    # to the benchmark's monthly series summed over that same range -- no more
    # per-year bucketing or "is this a full calendar year" guesswork needed.
    day_surgery_episodes_source_total = sum(day_surgery_episodes_annual.values())
    for metric, actual, applicable in (
        ("day_surgery_procedures", day_surgery_source_total, True),
        ("inpatient_procedures", sum(inpatient_annual.values()), True),
        ("day_surgery_private", day_surgery_private, True),
        ("day_surgery_subsidised", day_surgery_subsidised, True),
        ("day_surgery_episodes", day_surgery_episodes_source_total, has_episodes_column),
    ):
        if not applicable:
            continue
        expected = monthly_benchmark_total(monthly_benchmarks, metric, expected_months)
        if expected is None:
            if monthly_benchmarks:
                warnings.append(
                    f"no monthly benchmark data for {metric} covering the full "
                    f"requested range {expected_months[0]}..{expected_months[-1]} -- skipped"
                )
            continue
        if actual != expected:
            errors.append(
                f"{metric} total {actual} != benchmark {expected} "
                f"(summed {expected_months[0]}..{expected_months[-1]})"
            )

    if unmapped_classes:
        warnings.append(f"{len(unmapped_classes)} class_abc values are unmapped/blank")
        if args.fail_on_unmapped:
            errors.append("unmapped class_abc values present and --fail-on-unmapped was requested")

    unclassified_adm_type_total = sum(unclassified_annual.values())
    if unclassified_adm_type_total:
        warnings.append(
            f"{unclassified_adm_type_total} procedures have an Adm_Type outside both "
            "the day-surgery and inpatient buckets -- expected at low volume, not an error"
        )
    if not has_episodes_column:
        warnings.append(
            "no 'episodes' column in input -- day-surgery episode count (TABLE 1.4) "
            "was not checked against the benchmark"
        )

    audit = {
        "qc_status": "PASSED" if not errors else "FAILED",
        "input_rows": input_rows,
        "requested_months": expected_months,
        "observed_months": observed_months,
        "missing_months": missing_months,
        "extra_months": extra_months,
        "class_mapping_records": mapping_count,
        "day_surgery_source_total": day_surgery_source_total,
        "day_surgery_private": day_surgery_private,
        "day_surgery_subsidised": day_surgery_subsidised,
        "day_surgery_unclassified_class_total": day_surgery_unclassified_class_total,
        "inpatient_source_total": sum(inpatient_annual.values()),
        "unclassified_adm_type_total": unclassified_adm_type_total,
        "unmapped_class_values": sorted(unmapped_classes),
        "day_surgery_annual_totals": dict(sorted(day_surgery_annual.items())),
        "inpatient_annual_totals": dict(sorted(inpatient_annual.items())),
        "unclassified_annual_totals": dict(sorted(unclassified_annual.items())),
        "episodes_column_provided": has_episodes_column,
        "day_surgery_episodes_source_total": day_surgery_episodes_source_total,
        "day_surgery_episodes_annual_totals": dict(sorted(day_surgery_episodes_annual.items())),
        "warnings": warnings,
        "errors": errors,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "month",
        "adm_type",
        "adm_type_bucket",
        "class_abc",
        "procedures",
        "episodes",
        "pat_class",
    ]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)
    args.audit.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "qc_status": audit["qc_status"],
                "day_surgery_source_total": day_surgery_source_total,
                "inpatient_source_total": audit["inpatient_source_total"],
                "errors": errors,
                "warnings": warnings,
            },
            ensure_ascii=False,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
