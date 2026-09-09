"""Bounded, value-free review work run only inside the large Fargate worker.

The functions in this module intentionally mirror the v1 raw key semantics:
null/blank key components use a sentinel, exact duplicates retain one row, and
same-key/different-row groups are excluded.  They never write raw values to
logs or session records.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from .ingest_contract import compare_schema, profile_table
from .sanitization import detect_nric_columns, sanitised_schema


SUPPORTED_UPLOAD_SUFFIXES = (".parquet", ".parquet.gzip", ".xlsx", ".xls", ".csv", ".tsv")


def normalise_names(names: list[str]) -> list[str]:
    used: set[str] = set(); result: list[str] = []
    for source in names:
        base = re.sub(r"_+", "_", re.sub(r"[ /()\-]", "_", source)).strip("_").lower()
        if not base:
            raise ValueError(f"Column name normalises to an empty value: {source!r}")
        candidate = base; index = 1
        while candidate in used:
            candidate = f"{base}_{index:02d}"; index += 1
        used.add(candidate); result.append(candidate)
    return result


def read_upload_table(path: Path, filename: str) -> pa.Table:
    lower = filename.lower()
    if lower.endswith((".parquet", ".parquet.gzip")):
        return pq.read_table(path)
    if lower.endswith(".csv"):
        frame = pd.read_csv(path)
    elif lower.endswith(".tsv"):
        frame = pd.read_csv(path, sep="\t")
    elif lower.endswith((".xlsx", ".xls")):
        frame = pd.read_excel(path, engine="xlrd" if lower.endswith(".xls") else "openpyxl")
    else:
        raise ValueError(f"Unsupported file type: {filename}")
    for name in frame.columns:
        if pd.api.types.is_object_dtype(frame[name].dtype):
            try:
                pa.array(frame[name], from_pandas=True)
            except (pa.ArrowInvalid, pa.ArrowTypeError):
                frame[name] = frame[name].map(lambda value: None if pd.isna(value) else str(value)).astype("string")
    return pa.Table.from_pandas(frame, preserve_index=False)


def _raw_analysis_lazy_frame(path: Path, filename: str) -> pl.LazyFrame:
    lower = filename.lower()
    if lower.endswith((".parquet", ".parquet.gzip")):
        return pl.scan_parquet(path)
    if lower.endswith((".csv", ".tsv")):
        separator = "\t" if lower.endswith(".tsv") else ","
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            header = next(csv.reader(stream, delimiter=separator), None)
        if not header:
            raise ValueError("The selected file has no header row for key analysis")
        return pl.scan_csv(path, separator=separator, schema_overrides={name: pl.String for name in header}, infer_schema=False, try_parse_dates=False)
    return pl.from_arrow(read_upload_table(path, filename)).lazy()


def raw_key_impact_metrics(paths: list[tuple[Path, str]], key_columns: list[str]) -> dict[str, int]:
    """Calculate exact v1 composite-key impact without exposing values."""
    frames: list[tuple[pl.LazyFrame, dict[str, str]]] = []
    all_columns: set[str] = set()
    for path, filename in paths:
        frame = _raw_analysis_lazy_frame(path, filename)
        source_names = list(frame.collect_schema().names())
        lookup = dict(zip(normalise_names(source_names), source_names))
        frames.append((frame, lookup)); all_columns.update(lookup)
    missing = sorted(set(key_columns) - all_columns)
    if missing:
        raise ValueError(f"The selected key columns are not present in the upload: {', '.join(missing)}")
    if not all_columns:
        raise ValueError("The selected file has no columns for key analysis")
    columns = sorted(all_columns)
    incoming = pl.concat([
        frame.select([pl.col(lookup[name]).cast(pl.String, strict=False).alias(name) if name in lookup else pl.lit(None, dtype=pl.String).alias(name) for name in columns])
        for frame, lookup in frames
    ], how="vertical_relaxed")
    components = [
        pl.when(pl.col(name).is_null() | (pl.col(name).str.strip_chars() == "")).then(pl.lit("~")).otherwise(pl.col(name)).alias(name)
        for name in key_columns
    ]
    grouped = incoming.with_columns(pl.struct(components).alias("__uploader_composite_key")).group_by("__uploader_composite_key").agg(
        pl.len().alias("rows"), pl.struct([pl.col(name) for name in columns]).n_unique().alias("variants")
    )
    values = grouped.select(
        pl.col("rows").sum().alias("incoming_rows"), pl.len().alias("unique_composite_keys"),
        pl.when(pl.col("variants") == 1).then(pl.col("rows") - 1).otherwise(0).sum().alias("exact_duplicate_rows"),
        (pl.col("variants") > 1).sum().alias("conflicting_key_groups"),
        pl.when(pl.col("variants") > 1).then(pl.col("rows")).otherwise(0).sum().alias("rows_in_conflicting_key_groups"),
        (pl.col("variants") == 1).sum().alias("expected_retained_rows"),
    ).collect().row(0, named=True)
    total = int(values["incoming_rows"] or 0); retained = int(values["expected_retained_rows"] or 0)
    return {
        "incoming_rows": total, "unique_composite_keys": int(values["unique_composite_keys"] or 0),
        "exact_duplicate_rows": int(values["exact_duplicate_rows"] or 0),
        "conflicting_key_groups": int(values["conflicting_key_groups"] or 0),
        "rows_in_conflicting_key_groups": int(values["rows_in_conflicting_key_groups"] or 0),
        "expected_retained_rows": retained, "expected_skipped_rows": total - retained,
    }


def raw_key_row_selection(paths: list[tuple[Path, str]], key_columns: list[str]) -> tuple[dict[int, list[int]], dict[str, int]]:
    """Return retained raw row offsets using the exact V1 keyed contract.

    This is deliberately separate from :func:`raw_key_impact_metrics`: the
    latter is review-only, whereas this function makes the acknowledged review
    enforceable.  It runs before sanitisation, so redaction/encryption cannot
    turn otherwise distinct raw rows into apparent duplicates in Glue.
    """
    frames: list[tuple[pl.LazyFrame, dict[str, str]]] = []
    all_columns: set[str] = set()
    for path, filename in paths:
        frame = _raw_analysis_lazy_frame(path, filename)
        source_names = list(frame.collect_schema().names())
        lookup = dict(zip(normalise_names(source_names), source_names))
        frames.append((frame, lookup))
        all_columns.update(lookup)
    missing = sorted(set(key_columns) - all_columns)
    if missing:
        raise ValueError(f"The selected key columns are not present in the upload: {', '.join(missing)}")
    if not all_columns:
        raise ValueError("The selected file has no columns for key analysis")

    columns = sorted(all_columns)
    projected = []
    for file_number, (frame, lookup) in enumerate(frames):
        projected.append(
            frame.select([
                pl.col(lookup[name]).cast(pl.String, strict=False).alias(name)
                if name in lookup else pl.lit(None, dtype=pl.String).alias(name)
                for name in columns
            ])
            .with_row_index("__source_row")
            .with_columns(pl.lit(file_number, dtype=pl.Int32).alias("__source_file"))
        )
    incoming = pl.concat(projected, how="vertical_relaxed")
    components = [
        pl.when(pl.col(name).is_null() | (pl.col(name).str.strip_chars() == ""))
        .then(pl.lit("~"))
        .otherwise(pl.col(name))
        .alias(name)
        for name in key_columns
    ]
    classified_source = incoming.with_columns(pl.struct(components).alias("__uploader_composite_key"))
    grouped = classified_source.group_by("__uploader_composite_key").agg(
        pl.len().alias("rows"),
        pl.struct([pl.col(name) for name in columns]).n_unique().alias("variants"),
    )
    classified = classified_source.join(grouped, on="__uploader_composite_key", how="left")
    selected = (
        classified.filter(pl.col("variants") == 1)
        .sort("__source_file", "__source_row")
        .unique(subset=["__uploader_composite_key"], keep="first", maintain_order=True)
        .select("__source_file", "__source_row")
    )
    summary = grouped.select(
        pl.col("rows").sum().alias("incoming_rows"),
        pl.len().alias("unique_composite_keys"),
        pl.when(pl.col("variants") == 1).then(pl.col("rows") - 1).otherwise(0).sum().alias("exact_duplicate_rows"),
        (pl.col("variants") > 1).sum().alias("conflicting_key_groups"),
        pl.when(pl.col("variants") > 1).then(pl.col("rows")).otherwise(0).sum().alias("rows_in_conflicting_key_groups"),
        (pl.col("variants") == 1).sum().alias("expected_retained_rows"),
    )
    summary_frame, selected_frame = pl.collect_all([summary, selected])
    values = summary_frame.row(0, named=True)
    total = int(values["incoming_rows"] or 0)
    retained = int(values["expected_retained_rows"] or 0)
    selections: dict[int, list[int]] = {number: [] for number in range(len(paths))}
    for file_number, source_row in selected_frame.iter_rows():
        selections[int(file_number)].append(int(source_row))
    return selections, {
        "incoming_rows": total,
        "unique_composite_keys": int(values["unique_composite_keys"] or 0),
        "duplicate_rows_within_upload": int(values["exact_duplicate_rows"] or 0),
        "within_upload_key_conflicts": int(values["rows_in_conflicting_key_groups"] or 0),
        "within_upload_conflict_keys": int(values["conflicting_key_groups"] or 0),
        "rows_retained_after_local_deduplication": retained,
        "expected_skipped_rows": total - retained,
    }


def profile_files(paths: list[tuple[Path, str, str]], mode: str, table_bucket_arn: str, namespace: str, table: str, existing_contract: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run v1's raw inspection and sanitisation review in the worker.

    Create-mode type profiling uses every value in the first source file, just
    as v1 does.  Append-contract comparison is performed later in the worker
    ingestion path against the durable table contract.
    """
    if not paths:
        raise ValueError("Choose at least one supported file")
    tables = [read_upload_table(path, name) for path, name, _ in paths]
    target_schema, plan = sanitised_schema(tables[0].schema)
    forced = set(plan.identifier_columns) | set(plan.postal_columns) | set(plan.age_columns)
    if mode == "append":
        if not existing_contract or not existing_contract.get("schema"):
            raise ValueError("No uploader schema contract is available for the selected append table")
        target, warnings = existing_contract["schema"], []
    else:
        target, warnings, _ = profile_table(tables[0], target_schema, forced)
    target_names = {field["name"] for field in target}
    results = []
    for (path, name, digest), source in zip(paths, tables):
        source_schema, source_plan = sanitised_schema(source.schema)
        comparison = compare_schema(source_schema, target)
        matching = comparison["matching_column_count"]; percent = comparison["matching_percentage"]
        accepted = mode == "create" or percent >= 50.0
        nric_columns, nric_details = detect_nric_columns(source, digest)
        sanitized = sorted(set(source_plan.drop_columns + source_plan.identifier_columns + source_plan.postal_columns + source_plan.age_columns + nric_columns))
        results.append({
            "filename": name, "target_column_count": len(target), "source_column_count": comparison["source_column_count"],
            "matching_column_count": matching, "matching_percentage": percent,
            "extra_columns": comparison["extra_columns"], "missing_columns": comparison["missing_columns"], "type_conversions": comparison["type_conversions"], "warnings": comparison["warnings"],
            "sanitization": {"dropped_columns": list(source_plan.drop_columns), "encrypted_columns": list(source_plan.identifier_columns), "postal_columns": list(source_plan.postal_columns), "age_banded_columns": list(source_plan.age_columns)},
            "nric_detection": nric_details, "nric_detected_columns": list(nric_columns), "sanitized_columns": sanitized,
            "sanitized_column_count": len(sanitized), "unsafe_casts": [], "temporal_coercions": [], "accepted": accepted,
            "rejection_reasons": [] if accepted else [f"{name}: only {percent:.1f}% of the initial table schema matches; at least 50.0% is required for an append."],
        })
    candidates = [{"column": field["name"], "target_type": field["type"], "source_type": field["type"], "sample_values": [], "samples_masked": True, "non_null_count": 0, "deduplication_eligible": True} for field in target]
    automatic_encrypted = sorted({
        normalise_names([column])[0]
        for result in results
        for column in result["sanitization"]["encrypted_columns"] + result["nric_detected_columns"]
    })
    transformed = {
        normalise_names([column])[0]
        for result in results
        for column in result["sanitization"]["dropped_columns"] + result["sanitization"]["encrypted_columns"]
        + result["sanitization"]["postal_columns"] + result["sanitization"]["age_banded_columns"] + result["nric_detected_columns"]
    }
    manual_candidates = [
        {"column": item["column"], "sample_values": [], "samples_masked": True}
        for item in candidates if item["column"] not in transformed
    ]
    return {
        "mode": mode, "table_bucket_arn": table_bucket_arn, "namespace": namespace, "table": table,
        "target_schema": target, "creation_warnings": warnings, "initial_table_column_count": len(target),
        "minimum_append_schema_match_percent": 50.0, "files": results, "type_selections": [],
        "deduplication_candidates": candidates, "deduplication_columns": [], "deduplication_policy": "none",
        "contract_fingerprint": None, "temporal_policy_adoption": None, "phase_timings_ms": {},
        "incompatible_sensitive_columns": [], "accepted": all(item["accepted"] for item in results),
        "rejection_reasons": [reason for item in results for reason in item["rejection_reasons"]],
        "sensitive_column_scan": "Sanitization is enforced in the isolated worker before temporary S3 staging.",
        "sanitization_review": {"automatic_encrypted_columns": automatic_encrypted, "manual_encryption_candidates": manual_candidates, "nric_detection_policy": {"sample_size": 5, "match_threshold": 3, "kind": "sampled-heuristic-v1"}},
    }
