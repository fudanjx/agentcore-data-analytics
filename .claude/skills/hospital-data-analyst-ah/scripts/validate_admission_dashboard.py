#!/usr/bin/env python3
"""Validate a complete AH admission month/ward/type dashboard export.

Mirrors the shape of hospital-data-analyst-nuh/scripts/validate_inpatient_dashboard.py
(month-coverage gate, strict integer parsing, mapping-completeness gate,
arithmetic-identity checks, fail-closed audit JSON) but uses AH's own closed
code sets from references/admission.md and references/pt-class-lookup.json
instead of NUH's 277-OU subspecialty mapping.
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

# Adm_Type codes — references/admission.md "Adm_Type codes"
ADM_TYPES = {"EM", "EL", "SD", "DI", "TA", "RA"}

# Ward codes that must already be excluded upstream — references/admission.md
# "Ward exclusions" (LCUCC added since the script was first written). Their
# presence here means the SQL export leaked rows that should never have been included.
WARD_EXCLUSIONS = {"LWEDTU", "LWASW", "LWDSW", "LWVOTU", "LOMOT", "LCUCC"}

# admission.md "quick paying-status split"
SUBSIDISED_CLASSES = {"B2", "C"}

REQUIRED_COLUMNS = {"month_date", "adm_ward", "adm_type", "class_abc", "admissions"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Complete SQL CSV export")
    parser.add_argument(
        "--class-mapping",
        required=True,
        type=Path,
        help="references/pt-class-lookup.json — raw code -> Class_abc/Class_abc_MOH/"
        "Resident_Type/Resident_MOH — used to validate the mapping is complete and "
        "that the SQL's own Class_abc output only uses known values",
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
        help="Expected annual admissions total; repeat as needed.",
    )
    parser.add_argument(
        "--benchmark-file",
        type=Path,
        default=None,
        help="references/ah-yearly-benchmarks.json — the 'admission' section is "
        "{year: {admissions, paying, subsidised}} extracted from the official yearly "
        "inpatient reports. admissions merges into --benchmark; paying/subsidised are "
        "only checked when the requested month range is exactly one calendar year.",
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
    """Load references/pt-class-lookup.json (raw code -> class_abc/class_abc_moh/
    resident_type/resident_moh). This is a machine-readable mirror of
    pt-class-lookup.md's table — keep both in sync if the mapping ever changes.
    """
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
            load_benchmark_file(args.benchmark_file, "admission") if args.benchmark_file else {}
        )
        requested_years = {m[:4] for m in expected_months}
        for year, metrics in file_benchmarks.items():
            if year in requested_years and "admissions" in metrics:
                benchmarks.setdefault(year, metrics["admissions"])
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
                ward = str(row["adm_ward"] or "").strip()
                adm_type = str(row["adm_type"] or "").strip()
                class_abc = str(row["class_abc"] or "").strip()
                if not ward:
                    raise ValueError(f"row {row_number}: blank adm_ward")
                admissions = integral_count(row["admissions"], row_number, "admissions")
                rows.append(
                    {
                        "month": month,
                        "adm_ward": ward,
                        "adm_type": adm_type,
                        "class_abc": class_abc,
                        "admissions": admissions,
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
    output_rows: list[dict[str, object]] = []

    for row in rows:
        ward = row["adm_ward"]
        adm_type = row["adm_type"]
        class_abc = row["class_abc"]
        admissions = row["admissions"]
        month = row["month"]

        if ward in WARD_EXCLUSIONS:
            leaked_wards.add(ward)
            errors.append(f"{month}/{ward}: excluded ward present in export ({admissions} admissions)")
        if adm_type not in ADM_TYPES:
            unclassified_types.add(adm_type)
            errors.append(f"{month}/{ward}: unclassified Adm_Type {adm_type!r} ({admissions} admissions)")

        if class_abc not in valid_class_abc:
            paying_status = "Unmapped"
            unmapped_classes.add(class_abc)
            unclassified_class_total += admissions
        elif class_abc in SUBSIDISED_CLASSES:
            paying_status = "Subsidised"
            subsidised_total += admissions
        else:
            paying_status = "Paying"
            paying_total += admissions

        annual_total[month[:4]] += admissions
        monthly_total[month] += admissions
        output_rows.append({**row, "paying_status": paying_status})

    source_total = sum(monthly_total.values())
    if paying_total + subsidised_total + unclassified_class_total != source_total:
        errors.append("paying + subsidised + unclassified-class does not equal source total")
    for year, expected in benchmarks.items():
        actual = annual_total.get(year, 0)
        if actual != expected:
            errors.append(f"{year} admissions total {actual} != benchmark {expected}")

    # paying/subsidised totals are running totals across the whole requested range,
    # so they only line up with a benchmark year when that range is exactly one CY.
    if expected_months and expected_months[0][:4] == expected_months[-1][:4]:
        metrics = file_benchmarks.get(expected_months[0][:4], {})
        year = expected_months[0][:4]
        if "paying" in metrics and paying_total != metrics["paying"]:
            errors.append(f"{year} paying total {paying_total} != benchmark {metrics['paying']}")
        if "subsidised" in metrics and subsidised_total != metrics["subsidised"]:
            errors.append(f"{year} subsidised total {subsidised_total} != benchmark {metrics['subsidised']}")

    if unmapped_classes:
        warnings.append(
            f"{len(unmapped_classes)} class_abc values are unmapped/blank "
            f"({unclassified_class_total} admissions)"
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
    fieldnames = ["month", "adm_ward", "adm_type", "class_abc", "admissions", "paying_status"]
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
                "errors": errors,
                "warnings": warnings,
            },
            ensure_ascii=False,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
