"""Schema normalisation and preflight helpers for the local pilot web UI."""

from __future__ import annotations

import re
from typing import Any

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
    """Return the supported SQL type and whether the UI must ask for consent."""
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
    # Nested and other uncommon Arrow types are retained losslessly as strings
    # only after the caller confirms this explicit fallback.
    return "STRING", True


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


def compare_schema(source: pa.Schema, target_fields: list[dict[str, str]]) -> dict[str, Any]:
    """Describe extra/missing fields and type casts without inspecting values."""
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
    return {"extra_columns": extra, "missing_columns": missing, "type_conversions": casts, "warnings": warnings}
