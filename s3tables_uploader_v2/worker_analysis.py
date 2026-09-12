"""Bounded review work run only inside the large Fargate worker.

The functions in this module intentionally mirror the v1 raw key semantics:
null/blank key components use a sentinel, exact duplicates retain one row, and
same-key/different-row groups are excluded.  They never write raw values to
logs, staging manifests, QC reports, or Glue arguments.  The preflight result
contains at most five short examples for a non-protected review field, matching
the V1 UI contract.  Protected healthcare fields are always represented by a
masked notice instead of source values.
"""

from __future__ import annotations

import csv
import random
import re
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
import pyarrow as pa
import pyarrow.compute as pc
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


def _strict_source_schema(table: pa.Table) -> list[dict[str, str]]:
    """Return the exact cross-file schema contract before sanitisation."""
    names = normalise_names(table.schema.names)
    return [{"name": name, "type": str(field.type)} for name, field in zip(names, table.schema)]


def _column_name_difference(expected: list[dict[str, str]], actual: list[dict[str, str]]) -> str | None:
    expected_names = {field["name"] for field in expected}
    actual_names = {field["name"] for field in actual}
    if expected_names == actual_names:
        return None
    missing = sorted(expected_names - actual_names)
    extra = sorted(actual_names - expected_names)
    details = []
    if missing:
        details.append(f"missing {missing}")
    if extra:
        details.append(f"unexpected {extra}")
    return "; ".join(details)


def _cross_file_type_mismatches(schemas: list[list[dict[str, str]]]) -> dict[str, list[str]]:
    observed: dict[str, set[str]] = {}
    for schema in schemas:
        for field in schema:
            observed.setdefault(field["name"], set()).add(field["type"])
    return {name: sorted(types) for name, types in observed.items() if len(types) > 1}


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


def _deduplication_candidates(
    source: pa.Table,
    target: list[dict[str, str]],
    protected_columns: set[str],
) -> list[dict[str, Any]]:
    """Return V1-style, bounded examples for first-upload key selection.

    Values are sampled only from the first source file, just as V1 did.  They
    are intended for the review response only; automatically protected fields
    never expose source values or quality counts.
    """
    source_by_canonical = dict(zip(normalise_names(source.schema.names), source.schema.names))
    candidates: list[dict[str, Any]] = []
    for field in target:
        source_name = source_by_canonical.get(field["name"])
        masked = source_name in protected_columns
        values: list[str] = []
        non_null_count = 0
        if source_name and not masked:
            column = source[source_name]
            non_null_count = int(pc.count(column).as_py())
            for index in random.SystemRandom().sample(range(len(column)), min(1024, len(column))):
                value = column[index].as_py()
                if value is not None and str(value).strip().lower() not in {"", "nan", "none", "nat"}:
                    values.append(str(value)[:160])
                    if len(values) == 5:
                        break
        candidates.append({
            "column": field["name"],
            "target_type": field["type"],
            "source_type": str(source.schema.field(source_name).type) if source_name else "MISSING",
            "sample_values": values,
            "samples_masked": bool(masked),
            "deduplication_eligible": True,
            "deduplication_ineligible_reason": None,
            "non_null_count": non_null_count if not masked else None,
            "distinct_non_null_count": None,
        })
    return candidates


def profile_files(paths: list[tuple[Path, str, str]], mode: str, table_bucket_arn: str, namespace: str, table: str, existing_contract: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run v1's raw inspection and sanitisation review in the worker.

    Create-mode type profiling uses every value in the first source file, just
    as v1 does.  Append-contract comparison is performed later in the worker
    ingestion path against the durable table contract.
    """
    if not paths:
        raise ValueError("Choose at least one supported file")
    tables = [read_upload_table(path, name) for path, name, _ in paths]
    source_schemas = [_strict_source_schema(source) for source in tables]
    baseline_schema = source_schemas[0]
    schema_differences = [_column_name_difference(baseline_schema, schema) for schema in source_schemas]
    type_mismatches = _cross_file_type_mismatches(source_schemas)
    target_schema, plan = sanitised_schema(tables[0].schema)
    forced = set(plan.identifier_columns) | set(plan.postal_columns) | set(plan.age_columns)
    if mode == "append":
        if not existing_contract or not existing_contract.get("schema"):
            raise ValueError("No uploader schema contract is available for the selected append table")
        target, warnings = existing_contract["schema"], []
    else:
        target, warnings, _ = profile_table(tables[0], target_schema, forced)
        if type_mismatches:
            target = [
                {**field, "type": "STRING"} if field["name"] in type_mismatches else field
                for field in target
            ]
            warnings.extend(
                f"{column} has different source types across selected files and will be stored as STRING."
                for column in sorted(type_mismatches)
            )
    target_names = {field["name"] for field in target}
    results = []
    for number, ((path, name, digest), source) in enumerate(zip(paths, tables)):
        source_schema, source_plan = sanitised_schema(source.schema)
        comparison = compare_schema(source_schema, target)
        matching = comparison["matching_column_count"]; percent = comparison["matching_percentage"]
        schema_error = schema_differences[number]
        accepted = (mode == "create" or percent >= 50.0) and schema_error is None
        nric_columns, nric_details = detect_nric_columns(source, digest)
        sanitized = sorted(set(source_plan.drop_columns + source_plan.identifier_columns + source_plan.postal_columns + source_plan.age_columns + nric_columns))
        results.append({
            "filename": name, "target_column_count": len(target), "source_column_count": comparison["source_column_count"],
            "matching_column_count": matching, "matching_percentage": percent,
            "extra_columns": comparison["extra_columns"], "missing_columns": comparison["missing_columns"], "type_conversions": comparison["type_conversions"], "warnings": comparison["warnings"],
            "sanitization": {"dropped_columns": list(source_plan.drop_columns), "encrypted_columns": list(source_plan.identifier_columns), "postal_columns": list(source_plan.postal_columns), "age_banded_columns": list(source_plan.age_columns)},
            "nric_detection": nric_details, "nric_detected_columns": list(nric_columns), "sanitized_columns": sanitized,
            "sanitized_column_count": len(sanitized), "unsafe_casts": [], "temporal_coercions": [], "accepted": accepted,
            "rejection_reasons": ([] if mode == "create" or percent >= 50.0 else [f"{name}: only {percent:.1f}% of the initial table schema matches; at least 50.0% is required for an append."])
            + ([] if schema_error is None else [f"{name}: multi-file upload column mismatch: {schema_error}. Submit this file as a separate upload."]),
        })
    configured_key = list((existing_contract or {}).get("deduplication_columns") or [])
    source_column_sets = [set(normalise_names(source.schema.names)) for source in tables]
    available_columns = set.intersection(*source_column_sets) if source_column_sets else set()
    active_key = [column for column in configured_key if column in available_columns]
    protected_columns = {
        column
        for result in results
        for column in (
            result["sanitization"]["dropped_columns"]
            + result["sanitization"]["encrypted_columns"]
            + result["sanitization"]["postal_columns"]
            + result["sanitization"]["age_banded_columns"]
            + result["nric_detected_columns"]
        )
    }
    candidates = [] if configured_key else _deduplication_candidates(tables[0], target, protected_columns)
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
        {"column": item["column"], "sample_values": item["sample_values"], "samples_masked": item["samples_masked"]}
        for item in candidates if item["column"] not in transformed
    ]
    return {
        "mode": mode, "table_bucket_arn": table_bucket_arn, "namespace": namespace, "table": table,
        "target_schema": target, "creation_warnings": warnings, "initial_table_column_count": len(target),
        "minimum_append_schema_match_percent": 50.0, "files": results, "type_selections": [],
        "deduplication_candidates": candidates, "deduplication_columns": active_key,
        "deduplication_locked_columns": configured_key,
        "deduplication_policy": "derived-locked-key-v3" if configured_key else "none",
        "contract_fingerprint": None, "temporal_policy_adoption": None, "phase_timings_ms": {},
        "incompatible_sensitive_columns": [], "accepted": all(item["accepted"] for item in results),
        "rejection_reasons": [reason for item in results for reason in item["rejection_reasons"]],
        "multi_file_schema": {
            "enforced": len(tables) > 1, "reference_file": paths[0][1], "reference_schema": baseline_schema,
            "type_conflicts_stored_as_string": type_mismatches if mode == "create" else {},
        },
        "sensitive_column_scan": "Sanitization is enforced in the isolated worker before temporary S3 staging.",
        "sanitization_review": {"automatic_encrypted_columns": automatic_encrypted, "manual_encryption_candidates": manual_candidates, "nric_detection_policy": {"sample_size": 5, "match_threshold": 3, "kind": "sampled-heuristic-v1"}},
    }
