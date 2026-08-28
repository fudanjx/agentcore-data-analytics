#!/usr/bin/env python3
"""Validate a complete NUH inpatient month-by-OU dashboard export."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

METRICS = (
    "admissions", "discharges", "patient_days", "paying_discharges",
    "subsidised_discharges", "unclassified_discharges",
)
CY2025 = {
    "admissions": 74461,
    "discharges": 75037,
    "patient_days": 389331,
    "paying_discharges": 18548,
    "subsidised_discharges": 56489,
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--start-month", required=True)
    parser.add_argument("--end-month", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--clinical-only", action="store_true")
    parser.add_argument("--fail-on-unmapped", action="store_true")
    return parser.parse_args()


def months(start: str, end: str) -> list[str]:
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
        current = current.replace(
            year=current.year + (current.month == 12),
            month=1 if current.month == 12 else current.month + 1,
        )
    return result


def count(raw: str, row: int, column: str) -> int:
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValueError(f"row {row}: invalid {column} value {raw!r}") from exc
    if value < 0 or value != value.to_integral_value():
        raise ValueError(f"row {row}: {column} must be a non-negative integer")
    return int(value)


def main() -> int:
    args = arguments()
    errors: list[str] = []
    warnings: list[str] = []
    try:
        expected_months = months(args.start_month, args.end_month)
        mapping_doc = json.loads(args.mapping.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"QC FAILED: {exc}", file=sys.stderr)
        return 1

    records = mapping_doc.get("records")
    declared = mapping_doc.get("source", {}).get("record_count")
    if not isinstance(records, list):
        print("QC FAILED: mapping JSON must contain a records array", file=sys.stderr)
        return 1
    mapping = {}
    for record in records:
        ou = str(record.get("organizational_unit") or "").strip()
        if not ou or ou in mapping:
            errors.append(f"blank or duplicate mapping OU: {ou!r}")
        mapping[ou] = record
    if declared != 277 or len(records) != 277 or len(mapping) != 277:
        errors.append("mapping completeness failed: declared, record, and unique-OU counts must all be 277")

    values: dict[tuple[str, str], dict[str, int]] = {}
    input_rows = 0
    try:
        with args.input.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            required = {"month_date", "source_ou", *METRICS}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"missing CSV columns: {sorted(missing)}")
            for row_number, row in enumerate(reader, start=2):
                input_rows += 1
                month = str(row["month_date"] or "").strip()[:7]
                datetime.strptime(month, "%Y-%m")
                ou = str(row["source_ou"] or "").strip()
                if not ou:
                    raise ValueError(f"row {row_number}: blank source_ou")
                key = (month, ou)
                if key in values:
                    raise ValueError(f"row {row_number}: duplicate month/source_ou {key}")
                values[key] = {metric: count(row[metric], row_number, metric) for metric in METRICS}
    except (OSError, ValueError) as exc:
        print(f"QC FAILED: {exc}", file=sys.stderr)
        return 1

    observed = sorted({month for month, _ in values})
    missing_months = sorted(set(expected_months) - set(observed))
    extra_months = sorted(set(observed) - set(expected_months))
    if missing_months:
        errors.append(f"missing requested months: {missing_months}")
    if extra_months:
        errors.append(f"months outside requested range: {extra_months}")

    source = defaultdict(int)
    chart = defaultdict(int)
    excluded = defaultdict(int)
    annual = defaultdict(lambda: defaultdict(int))
    monthly = defaultdict(lambda: defaultdict(int))
    unmapped_ous: set[str] = set()
    unmapped_discharges = 0
    output_rows = []
    for (month, ou), row_values in sorted(values.items()):
        if (row_values["paying_discharges"] + row_values["subsidised_discharges"]
                != row_values["discharges"]):
            errors.append(f"{month}/{ou}: paying plus subsidised does not equal discharges")
        if row_values["unclassified_discharges"] != 0:
            errors.append(f"{month}/{ou}: unclassified discharges must be zero")
        record = mapping.get(ou)
        mapped = record is not None
        department = record.get("department_grouping") if mapped else "Unmapped"
        cluster = record.get("cluster_grouping") if mapped else "Unmapped"
        is_excluded = bool(args.clinical_only and mapped and (cluster == "XX Cluster" or department == "xx Dept"))
        if not mapped:
            unmapped_ous.add(ou)
            unmapped_discharges += row_values["discharges"]
        for metric, value in row_values.items():
            source[metric] += value
            annual[month[:4]][metric] += value
            monthly[month][metric] += value
            if is_excluded:
                excluded[metric] += value
            else:
                chart[metric] += value
        output_rows.append({
            "month": month, "source_ou": ou,
            "department_grouping": department, "cluster_grouping": cluster,
            **row_values, "mapping_status": "Mapped" if mapped else "Unmapped",
            "excluded_nonclinical": "Yes" if is_excluded else "No",
        })

    for metric in METRICS:
        if chart[metric] + excluded[metric] != source[metric]:
            errors.append(f"{metric}: chart plus exclusions does not equal source")
    if set(f"2025-{month:02d}" for month in range(1, 13)).issubset(expected_months):
        for metric, benchmark in CY2025.items():
            if annual["2025"][metric] != benchmark:
                errors.append(f"CY2025 {metric} {annual['2025'][metric]} != locked benchmark {benchmark}")
        if annual["2025"]["discharges"]:
            alos = annual["2025"]["patient_days"] / annual["2025"]["discharges"]
            if round(alos, 2) != 5.19:
                errors.append(f"CY2025 snapshot ALOS {alos:.2f} != locked benchmark 5.19")
    if "2025-04" in observed and "2025-05" in observed:
        for metric in ("admissions", "discharges", "patient_days"):
            april = monthly["2025-04"][metric]
            may = monthly["2025-05"][metric]
            if april and abs(may - april) / april > 0.05:
                warnings.append(f"{metric}: April-to-May 2025 change exceeds 5%; investigate the SAP/Epic transition")
    if unmapped_ous:
        warnings.append(f"{len(unmapped_ous)} source OUs are unmapped ({unmapped_discharges} discharges)")
        if args.fail_on_unmapped:
            errors.append("unmapped OUs present and --fail-on-unmapped was requested")

    audit = {
        "qc_status": "PASSED" if not errors else "FAILED",
        "input_rows": input_rows, "requested_months": expected_months,
        "observed_months": observed, "missing_months": missing_months,
        "extra_months": extra_months, "mapping_declared_records": declared,
        "mapping_records": len(records), "mapping_unique_ous": len(mapping),
        "unique_source_ous": len({ou for _, ou in values}),
        "unmapped_ous": sorted(unmapped_ous),
        "source_totals": dict(source), "chart_totals": dict(chart),
        "excluded_nonclinical_totals": dict(excluded),
        "monthly_source_totals": {month: dict(metrics) for month, metrics in sorted(monthly.items())},
        "annual_source_totals": {year: dict(metrics) for year, metrics in sorted(annual.items())},
        "warnings": warnings, "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    fields = ["month", "source_ou", "department_grouping", "cluster_grouping", *METRICS, "mapping_status", "excluded_nonclinical"]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output_rows)
    args.audit.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"qc_status": audit["qc_status"], "source_totals": dict(source), "mapping_unique_ous": len(mapping), "errors": errors, "warnings": warnings}, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
