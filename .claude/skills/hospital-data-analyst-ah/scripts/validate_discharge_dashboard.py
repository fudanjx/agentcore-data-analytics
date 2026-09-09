#!/usr/bin/env python3
"""Validate a complete AH discharge month/ward/type dashboard export.

Same shape and gates as validate_admission_dashboard.py (month-coverage gate,
strict integer parsing, mapping-completeness gate, arithmetic-identity checks,
fail-closed audit JSON), adapted to discharge.md:
  - Ward exclusion applies directly to Nrs_OU (discharge.md: "no derived-ward
    step needed here, unlike admission.Adm_Ward").
  - Adm_Type is the same inpatient-only code set as admission (discharge
    carries the admission's Adm_Type on the same row).
  - Adds a deaths count (Death_Date IS NOT NULL, per discharge.md's guidance
    to prefer Death_Date over parsing Discharge_Type_Text) with a sanity
    check that deaths never exceed discharges. No locked death benchmark yet
    (TABLE 6.3 in the yearly reports is a dept x class breakdown, not a
    single total) -- deaths is tracked and reported, not benchmark-checked.
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

# Adm_Type codes — same inpatient-only filter as admission.md, discharge.md
# confirms discharge carries the same code set on Adm_Type.
ADM_TYPES = {"EM", "EL", "SD", "DI", "TA", "RA"}

# Ward codes excluded from discharge reporting — discharge.md "Ward exclusions",
# applied directly to Nrs_OU. Their presence here means the SQL export leaked
# rows that should never have been included.
WARD_EXCLUSIONS = {"LWEDTU", "LWASW", "LWDSW", "LWVOTU", "LOMOT", "LCUCC"}

# Same paying-status split as admission.md's Adm_Cls convention, applied to
# the discharge-resolved class_abc.
SUBSIDISED_CLASSES = {"B2", "C"}

REQUIRED_COLUMNS = {"month_date", "disch_ward", "adm_type", "class_abc", "discharges", "deaths"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Complete SQL CSV export")
    parser.add_argument(
        "--class-mapping",
        required=True,
        type=Path,
        help="references/pt-class-lookup.json — raw code -> Class_abc/Class_abc_MOH/"
        "Resident_Type/Resident_MOH — used to validate the mapping is complete and "
        "that the SQL's own class_abc output only uses known values",
    )
    parser.add_argument("--start-month", required=True, help="Inclusive YYYY-MM")
    parser.add_argument("--end-month", required=True, help="Inclusive YYYY-MM")
    parser.add_argument("--output", required=True, type=Path, help="Classified aggregate CSV")
    parser.add_argument("--audit", required=True, type=Path, help="QC audit JSON")
    parser.add_argument(
        "--benchmark",
        action="append",
        default=[],
        metavar="YEAR=TOTAL",
        help="Expected annual discharges total; repeat as needed.",
    )
    parser.add_argument(
        "--benchmark-file",
        type=Path,
        default=None,
        help="references/ah-yearly-benchmarks.json — the 'discharge' section is "
        "{year: {discharges}} extracted from the official yearly inpatient reports "
        "(TABLE 1.6). discharges merges into --benchmark; only checked for years "
        "within the requested month range.",
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


def parse_benchmarks(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        try:
            year, total = value.split("=", 1)
            if len(year) != 4:
                raise ValueError
            result[year] = int(total.replace(",", ""))
        except ValueError as exc:
            raise ValueError(f"invalid benchmark {value!r}; use YEAR=TOTAL") from exc
    return result


def load_benchmark_file(path: Path, table: str) -> dict[str, dict[str, int]]:
    """Pull one table's section out of the consolidated ah-yearly-benchmarks.json
    (sectioned {table: {year: {...}}} so admission/discharge/etc. share one file)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    table_data = data.get(table, {})
    for year, metrics in table_data.items():
        if len(year) != 4 or not isinstance(metrics, dict):
            raise ValueError(f"{path}: malformed entry for {table}.{year!r}")
    return table_data


def integral_count(raw: str, row_number: int, column: str) -> int:
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValueError(f"row {row_number}: invalid {column} value {raw!r}") from exc
    if value < 0 or value != value.to_integral_value():
        raise ValueError(f"row {row_number}: {column} must be a non-negative integer")
    return int(value)


def load_class_mapping(path: Path) -> tuple[int, set[str]]:
    """Load references/pt-class-lookup.json — shared with validate_admission_dashboard.py,
    resolves Disch_Class/Adm_Class the same way Adm_Cls is resolved for admissions."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if len(data) != 18 or "" in data:
        raise ValueError(
            f"{path}: completeness failed — expected 18 unique raw codes, found {len(data)}"
        )
    valid_class_abc: set[str] = set()
    for raw_code, record in data.items():
        class_abc = str(record.get("class_abc", "")).strip()
        if not class_abc:
            raise ValueError(f"{path}: {raw_code!r} has a blank class_abc")
        valid_class_abc.add(class_abc)
    return len(data), valid_class_abc


def main() -> int:
    args = parse_args()
    errors: list[str] = []
    warnings: list[str] = []

    try:
        expected_months = month_sequence(args.start_month, args.end_month)
        benchmarks = parse_benchmarks(args.benchmark)
        file_benchmarks = (
            load_benchmark_file(args.benchmark_file, "discharge") if args.benchmark_file else {}
        )
        requested_years = {m[:4] for m in expected_months}
        for year, metrics in file_benchmarks.items():
            if year in requested_years and "discharges" in metrics:
                benchmarks.setdefault(year, metrics["discharges"])
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
                ward = str(row["disch_ward"] or "").strip()
                adm_type = str(row["adm_type"] or "").strip()
                class_abc = str(row["class_abc"] or "").strip()
                if not ward:
                    raise ValueError(f"row {row_number}: blank disch_ward")
                discharges = integral_count(row["discharges"], row_number, "discharges")
                deaths = integral_count(row["deaths"], row_number, "deaths")
                if deaths > discharges:
                    raise ValueError(
                        f"row {row_number}: deaths ({deaths}) exceeds discharges ({discharges})"
                    )
                rows.append(
                    {
                        "month": month,
                        "disch_ward": ward,
                        "adm_type": adm_type,
                        "class_abc": class_abc,
                        "discharges": discharges,
                        "deaths": deaths,
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

    leaked_wards: set[str] = set()
    unclassified_types: set[str] = set()
    unmapped_classes: set[str] = set()
    annual_total: dict[str, int] = defaultdict(int)
    monthly_total: dict[str, int] = defaultdict(int)
    paying_total = 0
    subsidised_total = 0
    unclassified_class_total = 0
    death_total = 0
    output_rows: list[dict[str, object]] = []

    for row in rows:
        ward = row["disch_ward"]
        adm_type = row["adm_type"]
        class_abc = row["class_abc"]
        discharges = row["discharges"]
        month = row["month"]

        if ward in WARD_EXCLUSIONS:
            leaked_wards.add(ward)
            errors.append(f"{month}/{ward}: excluded ward present in export ({discharges} discharges)")
        if adm_type not in ADM_TYPES:
            unclassified_types.add(adm_type)
            errors.append(f"{month}/{ward}: unclassified Adm_Type {adm_type!r} ({discharges} discharges)")

        if class_abc not in valid_class_abc:
            paying_status = "Unmapped"
            unmapped_classes.add(class_abc)
            unclassified_class_total += discharges
        elif class_abc in SUBSIDISED_CLASSES:
            paying_status = "Subsidised"
            subsidised_total += discharges
        else:
            paying_status = "Paying"
            paying_total += discharges

        annual_total[month[:4]] += discharges
        monthly_total[month] += discharges
        death_total += row["deaths"]
        output_rows.append({**row, "paying_status": paying_status})

    source_total = sum(monthly_total.values())
    if paying_total + subsidised_total + unclassified_class_total != source_total:
        errors.append("paying + subsidised + unclassified-class does not equal source total")
    for year, expected in benchmarks.items():
        actual = annual_total.get(year, 0)
        if actual != expected:
            errors.append(f"{year} discharges total {actual} != benchmark {expected}")

    if unmapped_classes:
        warnings.append(
            f"{len(unmapped_classes)} class_abc values are unmapped/blank "
            f"({unclassified_class_total} discharges)"
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
        "paying_total": paying_total,
        "subsidised_total": subsidised_total,
        "death_total": death_total,
        "unclassified_class_total": unclassified_class_total,
        "unmapped_class_values": sorted(unmapped_classes),
        "leaked_excluded_wards": sorted(leaked_wards),
        "unclassified_adm_types": sorted(unclassified_types),
        "monthly_totals": dict(sorted(monthly_total.items())),
        "annual_totals": dict(sorted(annual_total.items())),
        "benchmarks": benchmarks,
        "warnings": warnings,
        "errors": errors,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["month", "disch_ward", "adm_type", "class_abc", "discharges", "deaths", "paying_status"]
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
                "paying_total": paying_total,
                "subsidised_total": subsidised_total,
                "death_total": death_total,
                "errors": errors,
                "warnings": warnings,
            },
            ensure_ascii=False,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
