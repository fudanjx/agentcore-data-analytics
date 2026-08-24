#!/usr/bin/env python3
"""Validate and flatten compact monthly NUH mapped-dashboard SQL output."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--start-month", required=True)
    parser.add_argument("--end-month", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--expected-total", type=int)
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
        current = current.replace(year=current.year + (current.month == 12), month=1 if current.month == 12 else current.month + 1)
    return result


def integral(raw: object, label: str) -> int:
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if value < 0 or value != value.to_integral_value():
        raise ValueError(f"{label} must be a non-negative integer")
    return int(value)


def mapping_identity(path: Path) -> tuple[int, str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    records = document.get("records")
    declared = document.get("source", {}).get("record_count")
    if not isinstance(records, list):
        raise ValueError("mapping JSON must contain a records array")
    fields = ("organizational_unit", "organizational_unit_name", "department_grouping", "cluster_grouping", "moh_specialty_code", "moh_specialty_description")
    selected = [{field: record.get(field) for field in fields} for record in records]
    selected.sort(key=lambda record: str(record["organizational_unit"] or ""))
    ous = [str(record["organizational_unit"] or "").strip() for record in selected]
    if declared != 277 or len(selected) != 277 or len(set(ous)) != 277 or "" in ous:
        raise ValueError("mapping completeness failed")
    canonical = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return len(selected), hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def main() -> int:
    args = arguments()
    errors: list[str] = []
    try:
        expected_months = month_sequence(args.start_month, args.end_month)
        mapping_count, mapping_sha256 = mapping_identity(args.mapping)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"QC FAILED: {exc}", file=sys.stderr)
        return 1

    required = {"month_date", "department_payload", "source_total", "mapped_total", "unmapped_total", "excluded_total", "plotted_total", "mapping_count", "mapping_sha256"}
    rows: dict[str, dict] = {}
    flattened: list[dict[str, object]] = []
    try:
        with args.input.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"missing CSV columns: {sorted(missing)}")
            for row_number, row in enumerate(reader, start=2):
                month = str(row["month_date"] or "").strip()[:7]
                datetime.strptime(month, "%Y-%m")
                if month in rows:
                    raise ValueError(f"row {row_number}: duplicate month {month}")
                payload = json.loads(row["department_payload"])
                if not isinstance(payload, dict) or not payload:
                    raise ValueError(f"row {row_number}: department_payload must be a non-empty JSON object")
                totals = {name: integral(row[name], f"row {row_number} {name}") for name in ("source_total", "mapped_total", "unmapped_total", "excluded_total", "plotted_total")}
                payload_total = 0
                for department, raw_value in sorted(payload.items()):
                    if not str(department).strip():
                        raise ValueError(f"row {row_number}: blank department name")
                    value = integral(raw_value, f"row {row_number} payload {department}")
                    payload_total += value
                    flattened.append({"month": month, "department_grouping": department, "workload": value})
                row_mapping_count = integral(row["mapping_count"], f"row {row_number} mapping_count")
                row_checksum = str(row["mapping_sha256"] or "").strip()
                if row_mapping_count != mapping_count or row_checksum != mapping_sha256:
                    errors.append(f"{month}: mapping identity does not match bundled JSON")
                if totals["mapped_total"] + totals["unmapped_total"] != totals["source_total"]:
                    errors.append(f"{month}: mapped plus unmapped does not equal source")
                if totals["plotted_total"] + totals["excluded_total"] != totals["source_total"]:
                    errors.append(f"{month}: plotted plus exclusions does not equal source")
                if payload_total != totals["plotted_total"]:
                    errors.append(f"{month}: JSON payload does not equal plotted_total")
                if totals["unmapped_total"] and integral(payload.get("Unmapped", 0), f"row {row_number} Unmapped") != totals["unmapped_total"]:
                    errors.append(f"{month}: Unmapped payload does not equal unmapped_total")
                rows[month] = {**totals, "payload_total": payload_total}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"QC FAILED: {exc}", file=sys.stderr)
        return 1

    observed_months = sorted(rows)
    missing_months = sorted(set(expected_months) - set(observed_months))
    extra_months = sorted(set(observed_months) - set(expected_months))
    if missing_months:
        errors.append(f"missing requested months: {missing_months}")
    if extra_months:
        errors.append(f"months outside requested range: {extra_months}")
    totals = {name: sum(row[name] for row in rows.values()) for name in ("source_total", "mapped_total", "unmapped_total", "excluded_total", "plotted_total", "payload_total")}
    if totals["mapped_total"] + totals["unmapped_total"] != totals["source_total"]:
        errors.append("full-period mapped plus unmapped does not equal source")
    if totals["plotted_total"] + totals["excluded_total"] != totals["source_total"]:
        errors.append("full-period plotted plus exclusions does not equal source")
    if totals["payload_total"] != totals["plotted_total"]:
        errors.append("full-period JSON payload does not equal plotted total")
    if args.expected_total is not None and totals["source_total"] != args.expected_total:
        errors.append(f"source total {totals['source_total']} != expected total {args.expected_total}")
    if args.fail_on_unmapped and totals["unmapped_total"]:
        errors.append("unmapped workload present and --fail-on-unmapped was requested")

    audit = {"qc_status": "PASSED" if not errors else "FAILED", "requested_months": expected_months, "observed_months": observed_months, "missing_months": missing_months, "extra_months": extra_months, "mapping_count": mapping_count, "mapping_sha256": mapping_sha256, "monthly_totals": rows, "full_period_totals": totals, "errors": errors}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["month", "department_grouping", "workload"])
        writer.writeheader()
        writer.writerows(flattened)
    args.audit.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"qc_status": audit["qc_status"], "month_count": len(observed_months), "source_total": totals["source_total"], "plotted_total": totals["plotted_total"], "mapping_count": mapping_count, "errors": errors}, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
