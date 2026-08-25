#!/usr/bin/env python3
"""Validate a complete NUH surgery month-by-OU dashboard export."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

METRICS = ("procedure_total", "day_surgery", "normal_delivery", "inpatient_surgery", "unclassified", "emergency", "elective", "unexpected_emerg_ind")
LOCKED = {
    "2023": {"months": [f"2023-{m:02d}" for m in range(1, 13)], "day_surgery": 69886, "normal_delivery": 2831, "inpatient_surgery": 38237, "procedure_total": 110954, "emergency": 16416},
    "2024": {"months": [f"2024-{m:02d}" for m in range(1, 13)], "day_surgery": 73061, "normal_delivery": 2730, "inpatient_surgery": 39112, "procedure_total": 114903, "emergency": 14137},
    "2025": {"months": [f"2025-{m:02d}" for m in range(1, 13)], "day_surgery": 83467, "normal_delivery": 2537, "inpatient_surgery": 39945, "procedure_total": 125949, "emergency": 13804, "elective": 112145},
    "H1 2026": {"months": [f"2026-{m:02d}" for m in range(1, 7)], "day_surgery": 42210, "normal_delivery": 1146, "inpatient_surgery": 20716, "procedure_total": 64072, "emergency": 7560},
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
        current = current.replace(year=current.year + (current.month == 12), month=1 if current.month == 12 else current.month + 1)
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
    by_period = defaultdict(lambda: defaultdict(int))
    monthly = defaultdict(lambda: defaultdict(int))
    unmapped_ous: set[str] = set()
    unmapped_procedures = 0
    output_rows = []
    for (month, ou), row_values in sorted(values.items()):
        if row_values["day_surgery"] + row_values["normal_delivery"] + row_values["inpatient_surgery"] + row_values["unclassified"] != row_values["procedure_total"]:
            errors.append(f"{month}/{ou}: surgical categories do not equal procedure_total")
        if row_values["emergency"] + row_values["elective"] + row_values["unexpected_emerg_ind"] != row_values["procedure_total"]:
            errors.append(f"{month}/{ou}: emergency classes do not equal procedure_total")
        if row_values["unclassified"]:
            errors.append(f"{month}/{ou}: unclassified procedures = {row_values['unclassified']}")
        if row_values["unexpected_emerg_ind"]:
            errors.append(f"{month}/{ou}: unexpected EMERG_IND procedures = {row_values['unexpected_emerg_ind']}")
        record = mapping.get(ou)
        mapped = record is not None
        department = record.get("department_grouping") if mapped else "Unmapped"
        is_excluded = bool(args.clinical_only and mapped and department == "xx Dept")
        if not mapped:
            unmapped_ous.add(ou)
            unmapped_procedures += row_values["procedure_total"]
        periods = [month[:4]]
        if month.startswith("2026-") and int(month[5:7]) <= 6:
            periods.append("H1 2026")
        for metric, value in row_values.items():
            source[metric] += value
            monthly[month][metric] += value
            for period in periods:
                by_period[period][metric] += value
            if is_excluded:
                excluded[metric] += value
            else:
                chart[metric] += value
        output_rows.append({"month": month, "source_ou": ou, "department_grouping": department, **row_values, "mapping_status": "Mapped" if mapped else "Unmapped", "excluded_nonclinical": "Yes" if is_excluded else "No"})

    for metric in METRICS:
        if chart[metric] + excluded[metric] != source[metric]:
            errors.append(f"{metric}: chart plus exclusions does not equal source")
    for month in (f"2024-{number:02d}" for number in range(2, 10)):
        if month in observed and monthly[month]["procedure_total"] < 7000:
            errors.append(f"{month} procedure_total below 7,000; likely source-filter error")
    for period, benchmark in LOCKED.items():
        if set(benchmark["months"]).issubset(expected_months):
            for metric, expected in benchmark.items():
                if metric == "months":
                    continue
                actual = by_period[period][metric]
                if actual != expected:
                    errors.append(f"{period} {metric} {actual} != locked benchmark {expected}")
    if unmapped_ous:
        warnings.append(f"{len(unmapped_ous)} source OUs are unmapped ({unmapped_procedures} procedures)")
        if args.fail_on_unmapped:
            errors.append("unmapped OUs present and --fail-on-unmapped was requested")

    audit = {"qc_status": "PASSED" if not errors else "FAILED", "input_rows": input_rows, "requested_months": expected_months, "observed_months": observed, "missing_months": missing_months, "extra_months": extra_months, "mapping_declared_records": declared, "mapping_records": len(records), "mapping_unique_ous": len(mapping), "unique_source_ous": len({ou for _, ou in values}), "unmapped_ous": sorted(unmapped_ous), "source_totals": dict(source), "chart_totals": dict(chart), "excluded_nonclinical_totals": dict(excluded), "monthly_source_totals": {month: dict(metrics) for month, metrics in sorted(monthly.items())}, "period_source_totals": {period: dict(metrics) for period, metrics in sorted(by_period.items())}, "warnings": warnings, "errors": errors}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    fields = ["month", "source_ou", "department_grouping", *METRICS, "mapping_status", "excluded_nonclinical"]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output_rows)
    args.audit.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"qc_status": audit["qc_status"], "source_totals": dict(source), "mapping_unique_ous": len(mapping), "errors": errors, "warnings": warnings}, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
