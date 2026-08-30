"""Schema normalisation and preflight helpers for the local pilot web UI."""

from __future__ import annotations

import re
from typing import Any, Iterable

import pandas as pd
import pyarrow as pa


def normalise_name(name: str) -> str:
    """Return the S3 Tables-safe base name, without collision handling."""
    return re.sub(r"_+", "_", re.sub(r"[ /()\-]", "_", name)).strip("_").lower()


def normalise_names(names: list[str]) -> list[str]:
    """Keep first use of a base and suffix later collisions as ``_01``, ``_02``."""
    used: set[str] = set()
    result: list[str] = []
    for source_name in names:
        base = normalise_name(source_name)
        if not base:
            raise ValueError(f"Column name normalises to an empty value: {source_name!r}")
        candidate = base
        number = 1
        while candidate in used:
            candidate = f"{base}_{number:02d}"
            number += 1
        used.add(candidate)
        result.append(candidate)
    return result


def iceberg_type(field: pa.Field) -> tuple[str, bool]:
    """Return the supported SQL type and whether preflight must show a warning."""
    value = field.type
    if pa.types.is_boolean(value):
        return "BOOLEAN", False
    if pa.types.is_integer(value):
        return "BIGINT", False
    if pa.types.is_floating(value):
        return "DOUBLE", False
    if pa.types.is_decimal(value):
        return f"DECIMAL({value.precision},{value.scale})", False
    if pa.types.is_timestamp(value) or pa.types.is_date(value):
        return "TIMESTAMP", False
    if pa.types.is_string(value) or pa.types.is_large_string(value) or pa.types.is_binary(value) or pa.types.is_large_binary(value):
        return "STRING", False
    # Nested and other uncommon Arrow types are stored as strings and surfaced
    # in preflight; there is no browser-side approval override.
    return "STRING", True


_STRING_NAME_TOKENS = {
    "name", "surgeon", "clinician", "specialty", "ward", "department",
    "mcr", "code", "id", "ou", "location", "room", "type", "class",
    "description", "diagnosis", "consultant", "assistant", "nationality",
    "race", "gender", "disposition", "reason", "source", "status", "mode",
}
_TIMESTAMP_NAME_TOKENS = {"date", "time", "instant", "datetime", "timestamp"}


def _name_tokens(name: str) -> set[str]:
    return set(normalise_name(name).split("_"))


def _non_empty_values(column: pa.ChunkedArray) -> pd.Series:
    """Return only values that a user would regard as populated."""
    series = column.to_pandas()
    present = series.notna()
    if pd.api.types.is_object_dtype(series.dtype) or pd.api.types.is_string_dtype(series.dtype):
        text = series.astype("string").str.strip()
        present &= ~text.isin({"", "nan", "none", "nat"})
    return series[present]


def profiled_iceberg_type(field: pa.Field, values: pa.ChunkedArray | None) -> tuple[str, bool]:
    """Infer a conservative table type from all populated values.

    This is used only when the first upload creates a new table.  It avoids the
    Excel all-empty-column ``float64`` trap and protects known healthcare text
    fields such as surgeon, ward, specialty, MCR, codes, and identifiers.
    """
    tokens = _name_tokens(field.name)
    if tokens & _STRING_NAME_TOKENS:
        return "STRING", False
    if values is None:
        return "STRING", False
    populated = _non_empty_values(values)
    if populated.empty:
        return "STRING", False
    if tokens & _TIMESTAMP_NAME_TOKENS:
        converted = pd.to_datetime(populated, errors="coerce")
        return ("TIMESTAMP", False) if converted.notna().all() else ("STRING", True)
    if pa.types.is_timestamp(field.type) or pa.types.is_date(field.type):
        return "TIMESTAMP", False
    if pa.types.is_boolean(field.type):
        return "BOOLEAN", False
    # Strings that are truly all numeric may represent a measure.  Any text
    # value makes the column STRING; this scans every populated value, not just
    # the first visible Excel row.
    numeric = pd.to_numeric(populated, errors="coerce")
    if numeric.notna().all():
        as_float = numeric.astype(float)
        return ("BIGINT", False) if (as_float % 1 == 0).all() else ("DOUBLE", False)
    return "STRING", False


def schema_from_arrow(schema: pa.Schema) -> tuple[list[dict[str, str]], list[str]]:
    fields: list[dict[str, str]] = []
    warnings: list[str] = []
    names = normalise_names([field.name for field in schema])
    for field, name in zip(schema, names):
        data_type, confirmation = iceberg_type(field)
        fields.append({"name": name, "type": data_type, "source_name": field.name})
        if pa.types.is_timestamp(field.type) and field.type.unit == "ns":
            warnings.append(f"{field.name} uses nanosecond timestamps and will be converted to microseconds for Glue compatibility")
        if confirmation:
            warnings.append(f"{field.name} ({field.type}) will be stored as STRING")
    return fields, warnings


def schema_from_table(
    table: pa.Table,
    schema: pa.Schema | None = None,
    force_string_columns: Iterable[str] = (),
) -> tuple[list[dict[str, str]], list[str]]:
    """Create a new-table contract from full-column values, not row order.

    ``schema`` may be the sanitised schema. ``force_string_columns`` is used
    for fields transformed by sanitization (encrypted IDs, postal codes, and
    age bands); that mandated type wins over profiling raw numeric-looking
    source values.
    """
    active_schema = schema or table.schema
    forced = set(force_string_columns)
    fields: list[dict[str, str]] = []
    warnings: list[str] = []
    names = normalise_names([field.name for field in active_schema])
    for field, name in zip(active_schema, names):
        raw_field = table.schema.field(field.name) if field.name in table.schema.names else None
        raw_values = table[field.name] if raw_field is not None else None
        if field.name in forced or (raw_field is not None and raw_field.type != field.type):
            data_type, warning = "STRING", False
        else:
            data_type, warning = profiled_iceberg_type(field, raw_values)
        fields.append({"name": name, "type": data_type, "source_name": field.name})
        if warning:
            warnings.append(f"{field.name} has date/time-like name but non-date values; it will be stored as STRING")
    return fields, warnings


def compare_schema(source: pa.Schema, target_fields: list[dict[str, str]]) -> dict[str, Any]:
    """Describe canonical-name schema overlap and type casts without values.

    The caller must apply policy to this result.  In particular, append
    requests use ``matching_columns / target_column_count`` after all source
    names have been canonicalised and duplicate names have received their
    deterministic suffixes.
    """
    source_fields, warnings = schema_from_arrow(source)
    source_by_name = {field["name"]: field for field in source_fields}
    target_by_name = {field["name"]: field for field in target_fields}
    extra = sorted(set(source_by_name) - set(target_by_name))
    missing = sorted(set(target_by_name) - set(source_by_name))
    casts = [
        {"column": name, "source_type": source_by_name[name]["type"], "target_type": target_by_name[name]["type"]}
        for name in sorted(set(source_by_name) & set(target_by_name))
        if source_by_name[name]["type"] != target_by_name[name]["type"]
    ]
    matching = sorted(set(source_by_name) & set(target_by_name))
    target_count = len(target_by_name)
    return {
        "source_column_count": len(source_by_name),
        "target_column_count": target_count,
        "matching_columns": matching,
        "matching_column_count": len(matching),
        "matching_percentage": (len(matching) / target_count * 100) if target_count else 0.0,
        "extra_columns": extra,
        "missing_columns": missing,
        "type_conversions": casts,
        "warnings": warnings,
    }
