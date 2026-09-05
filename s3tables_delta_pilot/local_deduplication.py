"""Vectorized, raw-value within-upload de-duplication for keyed v2 uploads."""

from __future__ import annotations

from typing import Any

import polars as pl


def keyed_deduplicate(frame: pl.DataFrame, key_columns: list[str]) -> tuple[pl.DataFrame, dict[str, int]]:
    """Keep one exact duplicate and discard every conflicting key group.

    Values are compared before sanitization. Empty/null key components use the
    same explicit sentinel semantics as the key-impact preview and Glue job.
    The returned frame has exactly the original columns, preserving a staged
    schema for the subsequent sanitization and typed Parquet preparation.
    """
    if not key_columns:
        raise ValueError("Keyed local de-duplication requires at least one key column")
    missing = sorted(set(key_columns) - set(frame.columns))
    if missing:
        raise ValueError(f"Key columns are missing from local frame: {', '.join(missing)}")
    columns = frame.columns
    components = [
        pl.when(pl.col(column).is_null() | (pl.col(column).cast(pl.String).str.strip_chars() == ""))
        .then(pl.lit("~"))
        .otherwise(pl.col(column).cast(pl.String))
        .alias(column)
        for column in key_columns
    ]
    classified = (
        frame.with_columns(pl.struct(components).alias("__uploader_composite_key"))
        .with_columns(pl.struct([pl.col(column) for column in columns]).alias("__uploader_row_payload"))
        .join(
            frame.with_columns(pl.struct(components).alias("__uploader_composite_key"))
            .with_columns(pl.struct([pl.col(column) for column in columns]).alias("__uploader_row_payload"))
            .group_by("__uploader_composite_key")
            .agg(pl.len().alias("__uploader_key_rows"), pl.col("__uploader_row_payload").n_unique().alias("__uploader_key_variants")),
            on="__uploader_composite_key", how="left",
        )
    )
    conflict = classified.filter(pl.col("__uploader_key_variants") > 1)
    exact = classified.filter(pl.col("__uploader_key_variants") == 1)
    retained = exact.unique(subset=["__uploader_composite_key"], keep="first", maintain_order=True).select(columns)
    metrics = {
        "duplicate_rows_within_upload": exact.height - exact.select("__uploader_composite_key").n_unique(),
        "within_upload_key_conflicts": conflict.height,
        "within_upload_conflict_keys": conflict.select("__uploader_composite_key").n_unique(),
        "rows_retained_after_local_deduplication": retained.height,
    }
    return retained, {key: int(value) for key, value in metrics.items()}
