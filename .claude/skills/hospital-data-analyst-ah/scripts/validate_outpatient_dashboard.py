#!/usr/bin/env python3
"""Validate a complete AH outpatient (SOC visit) month/visit-type/class export.

Same shape and gates as validate_admission_dashboard.py / validate_discharge_dashboard.py
/ validate_inflight_dashboard.py (month-coverage gate, strict integer parsing,
mapping-completeness gate, arithmetic-identity checks, fail-closed audit JSON),
adapted to outpatient.md and the outpatient filters in data-ontology.yaml:

  - No ward exclusion for this table -- outpatient.md / data-ontology.yaml document
    only a Visit_Type inclusion filter and a Trt_Cat exclusion (see below), not a
    ward list. Ward/Trt_OU leak-detection is out of scope for this identity-level
    aggregate (it operates on a month/visit_type/class_abc grain, same level as the
    TABLE 1.1 benchmark it checks against) -- add it if a Trt_OU-grained export is
    ever validated here.
  - VALID_VISIT_TYPES is a closed INCLUSION set (data-ontology.yaml's
    "visit_type IN (...)"), the mirror image of admission/discharge's ward
    EXCLUSION set. The raw data legitimately contains other real Visit_Type codes
    (AF, AR, TT, EN, PA, FS, TS, XP, FT, TR, NR, NF, RT, ...) that belong to other
    report sections (Allied Health, staff clinic, anaesthesia pre-assessment, etc.)
    -- outpatient.md is explicit that these are not noise. Their presence here means
    the SQL export's WHERE visit_type IN (...) filter leaked, not that the mapping
    is incomplete.
  - Trt_Cat <> 'NC' is enforced with the Dental exception from data-ontology.yaml /
    outpatient.md: a row with Trt_Cat == 'NC' is only valid if Sub_Specialty_ID is
    one of the 4 Dental codes (LSHAPROS, LSHADEN, LSHAGDEN, LSHAGDGD) documented
    there -- no separate Sub-Specialty_ID mapping file exists in references/ (unlike
    NUH's subspec-mapping.json), so this closed set is hardcoded here the same way
    it's hardcoded in data-ontology.yaml's filter and outpatient.md's SQL. Confirmed
    against the real sample that both sides of this rule are exercised: 15 genuine
    NC+Dental rows that must be kept, 439 NC+non-Dental rows that must be filtered
    out upstream.
  - class_abc is resolved the same way as admission/discharge (pt-class-lookup.json,
    18 raw codes -> 6 class_abc values), then collapsed to Pat_Class per
    outpatient.md "Patient class" step 3: {A1,B1,Private} -> Private,
    {B2,C,Subsidized} -> Subsidised. A completeness self-check confirms those two
    sets fully partition the 6 known class_abc values before any row is processed,
    so a future 7th class_abc value fails closed instead of silently falling
    through the collapse.
  - Benchmark source: TABLE 1.1 "SOC ATTENDANCES BY NEW/RETURNING, DEPARTMENT,
    SPECIALTY, CLINIC AND PATIENT CLASS" in the yearly HIM report workbooks -- its
    single "Total" row is the grand total of the New+Repeat sections above it
    (reproduced exactly by summing Pat_Class subtotals independently: 70822 /
    82482 / 92581 for CY2023-2025). TABLE 4.1 ("... EXCL. TELEHEALTH") was
    considered as a cross-check but measures a different population (adds Allied
    Health visits, excludes telehealth) so its total is not comparable to this
    table's -- not used as a benchmark here.
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

# outpatient.md "Visit_Type codes" -- the 8-code SOC doctor-consult workload.
# Inclusion set: data-ontology.yaml's outpatient filter is "visit_type IN (...)",
# not an exclusion list, so any other value present is a filter leak.
VALID_VISIT_TYPES = {"FV", "RV", "FW", "RW", "DF", "DR", "FD", "RD"}

# data-ontology.yaml / outpatient.md: "trt_cat <> 'NC' OR sub_specialty_id IN (...)".
# Hardcoded here the same way it's hardcoded at both those source locations --
# there is no separate Sub-Specialty_ID mapping file in references/ to load instead.
TRT_CAT_EXCLUDE = "NC"
DENTAL_SUBSPECIALTY_IDS = {"LSHAPROS", "LSHADEN", "LSHAGDEN", "LSHAGDGD"}

# outpatient.md "Patient class" step 3 collapse.
PRIVATE_CLASSES = {"A1", "B1", "Private"}
SUBSIDISED_CLASSES = {"B2", "C", "Subsidized"}

REQUIRED_COLUMNS = {"month_date", "visit_type", "trt_cat", "sub_specialty_id", "class_abc", "visits"}


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
        help="references/ah-yearly-benchmarks.json -- the 'outpatient' section's "
        "'monthly' block is {metric: {'YYYY-MM': value}} for visits, private, and "
        "subsidised, extracted from the official yearly HIM report workbooks "
        "(TABLE 1.1). Each metric is checked by summing its monthly series over "
        "exactly the requested --start-month..--end-month range -- works for any "
        "range (a partial year, a full year, or one spanning a year boundary), not "
        "just a full calendar year. A metric missing monthly coverage for the full "
        "requested range is skipped with a warning, not an error.",
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


def main() -> int:
    args = parse_args()
    errors: list[str] = []
    warnings: list[str] = []

    try:
        expected_months = month_sequence(args.start_month, args.end_month)
        monthly_benchmarks = (
            load_monthly_benchmarks(args.benchmark_file, "outpatient") if args.benchmark_file else {}
        )
        mapping_count, valid_class_abc = load_class_mapping(args.class_mapping)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"QC FAILED: {exc}", file=sys.stderr)
        return 1

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
                visit_type = str(row["visit_type"] or "").strip()
                trt_cat = str(row["trt_cat"] or "").strip()
                sub_specialty_id = str(row["sub_specialty_id"] or "").strip()
                class_abc = str(row["class_abc"] or "").strip()
                if not visit_type:
                    raise ValueError(f"row {row_number}: blank visit_type")
                visits = integral_count(row["visits"], row_number, "visits")
                rows.append(
                    {
                        "month": month,
                        "visit_type": visit_type,
                        "trt_cat": trt_cat,
                        "sub_specialty_id": sub_specialty_id,
                        "class_abc": class_abc,
                        "visits": visits,
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

    leaked_visit_types: set[str] = set()
    leaked_nc_subspecialties: set[str] = set()
    leaked_nc_total = 0
    unmapped_classes: set[str] = set()
    annual_total: dict[str, int] = defaultdict(int)
    monthly_total: dict[str, int] = defaultdict(int)
    private_total = 0
    subsidised_total = 0
    unclassified_class_total = 0
    output_rows: list[dict[str, object]] = []

    for row in rows:
        visit_type = row["visit_type"]
        trt_cat = row["trt_cat"]
        sub_specialty_id = row["sub_specialty_id"]
        class_abc = row["class_abc"]
        visits = row["visits"]
        month = row["month"]

        if visit_type not in VALID_VISIT_TYPES:
            leaked_visit_types.add(visit_type)
            errors.append(f"{month}: unfiltered Visit_Type {visit_type!r} present ({visits} visits)")

        if trt_cat == TRT_CAT_EXCLUDE and sub_specialty_id not in DENTAL_SUBSPECIALTY_IDS:
            leaked_nc_subspecialties.add(sub_specialty_id)
            leaked_nc_total += visits
            errors.append(
                f"{month}: Trt_Cat 'NC' row with non-Dental Sub_Specialty_ID "
                f"{sub_specialty_id!r} present ({visits} visits)"
            )

        if class_abc not in valid_class_abc:
            pat_class = "Unmapped"
            unmapped_classes.add(class_abc)
            unclassified_class_total += visits
        elif class_abc in SUBSIDISED_CLASSES:
            pat_class = "Subsidised"
            subsidised_total += visits
        else:
            pat_class = "Private"
            private_total += visits

        annual_total[month[:4]] += visits
        monthly_total[month] += visits
        output_rows.append({**row, "pat_class": pat_class})

    source_total = sum(monthly_total.values())
    if private_total + subsidised_total + unclassified_class_total != source_total:
        errors.append("private + subsidised + unclassified-class does not equal source total")

    # The month-coverage gate above already forces the CSV to contain exactly
    # the requested months, so each metric's grand total is directly comparable
    # to the benchmark's monthly series summed over that same range -- no more
    # per-year bucketing or "is this a full calendar year" guesswork needed.
    for metric, actual in (
        ("visits", source_total),
        ("private", private_total),
        ("subsidised", subsidised_total),
    ):
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
        warnings.append(
            f"{len(unmapped_classes)} class_abc values are unmapped/blank "
            f"({unclassified_class_total} visits)"
        )
        if args.fail_on_unmapped:
            errors.append("unmapped class_abc values present and --fail-on-unmapped was requested")

    audit = {
        "qc_status": "PASSED" if not errors else "FAILED",
        "input_rows": input_rows,
        "requested_months": expected_months,
        "observed_months": observed_months,
        "missing_months": missing_months,
        "extra_months": extra_months,
        "class_mapping_records": mapping_count,
        "source_total": source_total,
        "private_total": private_total,
        "subsidised_total": subsidised_total,
        "unclassified_class_total": unclassified_class_total,
        "unmapped_class_values": sorted(unmapped_classes),
        "leaked_visit_types": sorted(leaked_visit_types),
        "leaked_nc_subspecialties": sorted(leaked_nc_subspecialties),
        "leaked_nc_total": leaked_nc_total,
        "monthly_totals": dict(sorted(monthly_total.items())),
        "annual_totals": dict(sorted(annual_total.items())),
        "warnings": warnings,
        "errors": errors,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "month",
        "visit_type",
        "trt_cat",
        "sub_specialty_id",
        "class_abc",
        "visits",
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
                "source_total": source_total,
                "private_total": private_total,
                "subsidised_total": subsidised_total,
                "errors": errors,
                "warnings": warnings,
            },
            ensure_ascii=False,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
