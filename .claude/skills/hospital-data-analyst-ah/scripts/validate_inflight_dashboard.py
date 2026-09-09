#!/usr/bin/env python3
"""Validate a complete AH inflight (daily census / patient-days) month/ward/class export.

Same shape and gates as validate_admission_dashboard.py / validate_discharge_dashboard.py
(month-coverage gate, strict integer parsing, mapping-completeness gate, arithmetic-identity
checks, fail-closed audit JSON), adapted to inflight.md:

  - Ward exclusion applies to `ward`, same 6-code set as admission/discharge.
  - No Adm_Type filter -- inflight.md / data-ontology.yaml document only the ward exclusion
    for this table; Adm_Type is not part of the inflight inclusion criteria.
  - `effective_class` is NOT raw pt_class_abc's Class_abc -- it's the ICU/HD/ISO override
    chain from inflight.md ("Critical: the ICU/HD/ISO override chain"): Accom_Category='ISO'
    -> 'ISO', Trt_Cat prefix 'CCU' -> 'ICU', Trt_Cat prefix 'HD' -> 'HD', else the looked-up
    Class_abc. Valid values are therefore pt-class-lookup's Class_abc set plus {ISO, ICU, HD}.
    Discharge's own ISO override (Nrs_OU ward prefix) does NOT apply here -- don't reuse it.
  - Counts patient-days (SUM(cnt), always 1 per row), not admissions/discharges.
  - MANDATORY precondition, not an edge case: raw `inflight` can never structurally contain
    a same-day admission+discharge case -- a daily census snapshot has no row for a patient
    who was never present at snapshot time. Every single patient-days query against this
    table therefore requires the same-day top-up union documented in inflight.md ("Conceptual
    union to replicate production"), for every ward and every period, not just high-turnover
    ones. This script cannot detect a missing top-up from the aggregate CSV alone (a topped-up
    row and a raw row are indistinguishable after grouping) -- the requirement is enforced by
    the SKILL.md pre-query workflow forcing inflight.md to be read before any SQL is written,
    not by this script.
  - Benchmark is a single annual patient-days total (TABLE 1.7 "Patient Days by Ward" -- a
    ward-level breakdown, so the locked figure is the grand total across all wards for the
    year). No paying/subsidised benchmark yet -- no clean single-total source table for that
    split exists in the yearly reports.
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

# Ward codes excluded from inflight reporting -- same set as admission.md / discharge.md
# "Ward exclusions", applied directly to `Ward`. Their presence here means the SQL export
# leaked rows that should never have been included.
WARD_EXCLUSIONS = {"LWEDTU", "LWASW", "LWDSW", "LWVOTU", "LOMOT", "LCUCC"}

# inflight.md "Critical: the ICU/HD/ISO override chain" -- accommodation overrides that sit
# on top of (not instead of) the pt_class_abc-resolved Class_abc value.
CLASS_OVERRIDES = {"ISO", "ICU", "HD"}

# Same paying-status split as admission.md/discharge.md, applied only to rows whose
# effective_class is a genuine Class_abc value (not an ISO/ICU/HD override).
SUBSIDISED_CLASSES = {"B2", "C"}

REQUIRED_COLUMNS = {"month_date", "ward", "effective_class", "patient_days"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Complete SQL CSV export")
    parser.add_argument(
        "--class-mapping",
        required=True,
        type=Path,
        help="references/pt-class-lookup.json -- raw code -> Class_abc/Class_abc_MOH/"
        "Resident_Type/Resident_MOH -- used to validate the mapping is complete and "
        "that the SQL's own effective_class output only uses known Class_abc values "
        "(plus the ISO/ICU/HD overrides, checked separately)",
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
        help="Expected annual patient-days total; repeat as needed.",
    )
    parser.add_argument(
        "--benchmark-file",
        type=Path,
        default=None,
        help="references/ah-yearly-benchmarks.json -- the 'inflight' section is "
        "{year: {patient_days}} extracted from the official yearly inpatient reports "
        "(TABLE 1.7). patient_days merges into --benchmark; only checked for years "
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
    (sectioned {table: {year: {...}}} so admission/discharge/inflight/etc. share one file)."""
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
    """Load references/pt-class-lookup.json -- shared with the admission/discharge
    validators. Returns the base Class_abc set; CLASS_OVERRIDES (ISO/ICU/HD) are added
    separately since they aren't part of pt_class_abc."""
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
    return len(data), valid_class_abc


def main() -> int:
    args = parse_args()
    errors: list[str] = []
    warnings: list[str] = []

    try:
        expected_months = month_sequence(args.start_month, args.end_month)
        benchmarks = parse_benchmarks(args.benchmark)
        file_benchmarks = (
            load_benchmark_file(args.benchmark_file, "inflight") if args.benchmark_file else {}
        )
        requested_years = {m[:4] for m in expected_months}
        for year, metrics in file_benchmarks.items():
            if year in requested_years and "patient_days" in metrics:
                benchmarks.setdefault(year, metrics["patient_days"])
        mapping_count, valid_class_abc = load_class_mapping(args.class_mapping)
        valid_effective_classes = valid_class_abc | CLASS_OVERRIDES
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
                ward = str(row["ward"] or "").strip()
                effective_class = str(row["effective_class"] or "").strip()
                if not ward:
                    raise ValueError(f"row {row_number}: blank ward")
                patient_days = integral_count(row["patient_days"], row_number, "patient_days")
                rows.append(
                    {
                        "month": month,
                        "ward": ward,
                        "effective_class": effective_class,
                        "patient_days": patient_days,
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
    unmapped_classes: set[str] = set()
    annual_total: dict[str, int] = defaultdict(int)
    monthly_total: dict[str, int] = defaultdict(int)
    paying_total = 0
    subsidised_total = 0
    iso_total = 0
    icu_total = 0
    hd_total = 0
    unclassified_class_total = 0
    output_rows: list[dict[str, object]] = []

    for row in rows:
        ward = row["ward"]
        effective_class = row["effective_class"]
        patient_days = row["patient_days"]
        month = row["month"]

        if ward in WARD_EXCLUSIONS:
            leaked_wards.add(ward)
            errors.append(
                f"{month}/{ward}: excluded ward present in export ({patient_days} patient-days)"
            )

        if effective_class not in valid_effective_classes:
            paying_status = "Unmapped"
            unmapped_classes.add(effective_class)
            unclassified_class_total += patient_days
        elif effective_class == "ISO":
            paying_status = "ISO"
            iso_total += patient_days
        elif effective_class == "ICU":
            paying_status = "ICU"
            icu_total += patient_days
        elif effective_class == "HD":
            paying_status = "HD"
            hd_total += patient_days
        elif effective_class in SUBSIDISED_CLASSES:
            paying_status = "Subsidised"
            subsidised_total += patient_days
        else:
            paying_status = "Paying"
            paying_total += patient_days

        annual_total[month[:4]] += patient_days
        monthly_total[month] += patient_days
        output_rows.append({**row, "paying_status": paying_status})

    source_total = sum(monthly_total.values())
    classified_total = (
        paying_total + subsidised_total + iso_total + icu_total + hd_total + unclassified_class_total
    )
    if classified_total != source_total:
        errors.append(
            "paying + subsidised + ISO + ICU + HD + unclassified-class does not equal source total"
        )
    for year, expected in benchmarks.items():
        actual = annual_total.get(year, 0)
        if actual != expected:
            errors.append(f"{year} patient-days total {actual} != benchmark {expected}")

    if unmapped_classes:
        warnings.append(
            f"{len(unmapped_classes)} effective_class values are unmapped/blank "
            f"({unclassified_class_total} patient-days)"
        )
        if args.fail_on_unmapped:
            errors.append("unmapped effective_class values present and --fail-on-unmapped was requested")

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
        "iso_total": iso_total,
        "icu_total": icu_total,
        "hd_total": hd_total,
        "unclassified_class_total": unclassified_class_total,
        "unmapped_class_values": sorted(unmapped_classes),
        "leaked_excluded_wards": sorted(leaked_wards),
        "monthly_totals": dict(sorted(monthly_total.items())),
        "annual_totals": dict(sorted(annual_total.items())),
        "benchmarks": benchmarks,
        "warnings": warnings,
        "errors": errors,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["month", "ward", "effective_class", "patient_days", "paying_status"]
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
                "iso_total": iso_total,
                "icu_total": icu_total,
                "hd_total": hd_total,
                "errors": errors,
                "warnings": warnings,
            },
            ensure_ascii=False,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
