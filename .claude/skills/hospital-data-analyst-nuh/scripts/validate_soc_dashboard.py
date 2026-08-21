#!/usr/bin/env python3
"""Map and validate complete SOC month-by-OU aggregates before dashboard use."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Map SOC month-by-OU SQL output and emit a fail-closed QC audit."
    )
    parser.add_argument("--input", required=True, type=Path, help="Complete SQL CSV export")
    parser.add_argument("--mapping", required=True, type=Path, help="subspec-mapping.json")
    parser.add_argument("--start-month", required=True, help="Inclusive YYYY-MM")
    parser.add_argument("--end-month", required=True, help="Inclusive YYYY-MM")
    parser.add_argument("--output", required=True, type=Path, help="Mapped aggregate CSV")
    parser.add_argument("--audit", required=True, type=Path, help="QC audit JSON")
    parser.add_argument("--month-column", default="month_date")
    parser.add_argument("--ou-column", default="source_ou")
    parser.add_argument("--count-column", default="visit_count")
    parser.add_argument(
        "--benchmark",
        action="append",
        default=[],
        metavar="YEAR=TOTAL",
        help="Expected unfiltered annual total; repeat as needed",
    )
    parser.add_argument("--expected-total", type=int)
    parser.add_argument("--clinical-only", action="store_true")
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


def integral_count(raw: str, row_number: int) -> int:
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValueError(f"row {row_number}: invalid visit count {raw!r}") from exc
    if value < 0 or value != value.to_integral_value():
        raise ValueError(f"row {row_number}: visit count must be a non-negative integer")
    return int(value)


def main() -> int:
    args = parse_args()
    errors: list[str] = []
    warnings: list[str] = []

    try:
        expected_months = month_sequence(args.start_month, args.end_month)
        benchmarks = parse_benchmarks(args.benchmark)
        mapping_doc = json.loads(args.mapping.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"QC FAILED: {exc}", file=sys.stderr)
        return 1

    records = mapping_doc.get("records")
    declared_count = mapping_doc.get("source", {}).get("record_count")
    if not isinstance(records, list):
        print("QC FAILED: mapping JSON must contain a records array", file=sys.stderr)
        return 1

    mapping: dict[str, dict] = {}
    duplicate_mapping_ous: list[str] = []
    for record in records:
        ou = str(record.get("organizational_unit") or "").strip()
        if not ou:
            errors.append("mapping contains a blank organizational_unit")
            continue
        if ou in mapping:
            duplicate_mapping_ous.append(ou)
        mapping[ou] = record
    if declared_count != 277 or len(records) != 277 or len(mapping) != 277:
        errors.append(
            "mapping completeness failed: declared, record, and unique-OU counts must all be 277"
        )
    if duplicate_mapping_ous:
        errors.append(f"duplicate mapping OUs: {sorted(set(duplicate_mapping_ous))}")

    aggregates: dict[tuple[str, str], int] = defaultdict(int)
    input_rows = 0
    try:
        with args.input.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            required = {args.month_column, args.ou_column, args.count_column}
            missing_columns = required - set(reader.fieldnames or [])
            if missing_columns:
                raise ValueError(f"missing CSV columns: {sorted(missing_columns)}")
            for row_number, row in enumerate(reader, start=2):
                input_rows += 1
                month = str(row[args.month_column] or "").strip()[:7]
                ou = str(row[args.ou_column] or "").strip()
                if not ou:
                    raise ValueError(f"row {row_number}: blank source OU")
                datetime.strptime(month, "%Y-%m")
                aggregates[(month, ou)] += integral_count(row[args.count_column], row_number)
    except (OSError, ValueError) as exc:
        print(f"QC FAILED: {exc}", file=sys.stderr)
        return 1

    observed_months = sorted({month for month, _ in aggregates})
    missing_months = sorted(set(expected_months) - set(observed_months))
    extra_months = sorted(set(observed_months) - set(expected_months))
    if missing_months:
        errors.append(f"missing requested months: {missing_months}")
    if extra_months:
        errors.append(f"months outside requested range: {extra_months}")

    annual_source: dict[str, int] = defaultdict(int)
    annual_chart: dict[str, int] = defaultdict(int)
    month_source: dict[str, int] = defaultdict(int)
    month_chart: dict[str, int] = defaultdict(int)
    unmapped_visits = 0
    excluded_visits = 0
    unmapped_ous: set[str] = set()
    output_rows: list[dict[str, object]] = []

    for (month, ou), count in sorted(aggregates.items()):
        record = mapping.get(ou)
        mapped = record is not None
        department = record.get("department_grouping") if mapped else "Unmapped"
        cluster = record.get("cluster_grouping") if mapped else "Unmapped"
        excluded = bool(
            args.clinical_only
            and mapped
            and (cluster == "XX Cluster" or department == "xx Dept")
        )
        year = month[:4]
        month_source[month] += count
        annual_source[year] += count
        if not mapped:
            unmapped_ous.add(ou)
            unmapped_visits += count
        if excluded:
            excluded_visits += count
        else:
            month_chart[month] += count
            annual_chart[year] += count
        output_rows.append(
            {
                "month": month,
                "source_ou": ou,
                "department_grouping": department,
                "cluster_grouping": cluster,
                "visit_count": count,
                "mapping_status": "Mapped" if mapped else "Unmapped",
                "excluded_nonclinical": "Yes" if excluded else "No",
            }
        )

    source_total = sum(month_source.values())
    chart_total = sum(month_chart.values())
    if chart_total + excluded_visits != source_total:
        errors.append("chart total plus exclusions does not equal source total")
    if args.expected_total is not None and source_total != args.expected_total:
        errors.append(f"source total {source_total} != expected total {args.expected_total}")
    for year, expected in benchmarks.items():
        actual = annual_source.get(year, 0)
        if actual != expected:
            errors.append(f"{year} source total {actual} != benchmark {expected}")
    if unmapped_ous:
        warnings.append(
            f"{len(unmapped_ous)} source OUs are unmapped ({unmapped_visits} visits)"
        )
        if args.fail_on_unmapped:
            errors.append("unmapped OUs present and --fail-on-unmapped was requested")

    audit = {
        "qc_status": "PASSED" if not errors else "FAILED",
        "input_rows": input_rows,
        "aggregated_month_ou_rows": len(aggregates),
        "requested_months": expected_months,
        "observed_months": observed_months,
        "missing_months": missing_months,
        "extra_months": extra_months,
        "mapping_declared_records": declared_count,
        "mapping_records": len(records),
        "mapping_unique_ous": len(mapping),
        "unique_source_ous": len({ou for _, ou in aggregates}),
        "unmapped_ous": sorted(unmapped_ous),
        "unmapped_visits": unmapped_visits,
        "excluded_nonclinical_visits": excluded_visits,
        "source_total": source_total,
        "chart_total": chart_total,
        "monthly_source_totals": dict(sorted(month_source.items())),
        "monthly_chart_totals": dict(sorted(month_chart.items())),
        "annual_source_totals": dict(sorted(annual_source.items())),
        "annual_chart_totals": dict(sorted(annual_chart.items())),
        "benchmarks": benchmarks,
        "warnings": warnings,
        "errors": errors,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0].keys()) if output_rows else [
            "month", "source_ou", "department_grouping", "cluster_grouping",
            "visit_count", "mapping_status", "excluded_nonclinical"
        ])
        writer.writeheader()
        writer.writerows(output_rows)
    args.audit.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(json.dumps({
        "qc_status": audit["qc_status"],
        "source_total": source_total,
        "chart_total": chart_total,
        "mapping_unique_ous": len(mapping),
        "observed_month_count": len(observed_months),
        "unmapped_ou_count": len(unmapped_ous),
        "errors": errors,
        "warnings": warnings,
    }, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
