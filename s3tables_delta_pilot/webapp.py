"""Local-only web UI for the isolated S3 Tables pilot namespace."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .contract import TARGET_COLUMNS, TIMESTAMP_TARGET_COLUMNS
from .ingest_contract import compare_schema, normalise_names, schema_from_arrow, schema_from_table
from .pilot import NAMESPACE, QC_PREFIX, REGION, ROLE_NAME, SOURCE_BUCKET, SOURCE_PREFIX, TABLE_BUCKET_ARN
from .sanitization import encryption_key, sanitise_table, sanitised_schema

WEB_JOB_NAME = "ah-soc-delta-pilot-web-ingest"
WEB_SCRIPT_KEY = f"{SOURCE_PREFIX}/_pilot_assets/generic_glue_job.py"
WEB_CONTRACT_PREFIX = f"{SOURCE_PREFIX}/web_ingest/table_contracts"
WEB_UPLOAD_PREFIX = f"{SOURCE_PREFIX}/web_ingest/uploads"
WEB_HISTORY_PREFIX = f"{SOURCE_PREFIX}/web_ingest/upload_history"
UPLOAD_HISTORY_TABLE = "uploader_upload_history"
SNAPSHOT_RETENTION = {"minSnapshotsToKeep": 12, "maxSnapshotAgeHours": 365 * 24}
ROOT = Path(__file__).parent
s3 = boto3.client("s3", region_name=REGION)
s3tables = boto3.client("s3tables", region_name=REGION)
glue = boto3.client("glue", region_name=REGION)
iam = boto3.client("iam")
app = FastAPI(title="AH S3 Tables Pilot", docs_url=None, redoc_url=None)
SUPPORTED_UPLOAD_SUFFIXES = (".parquet", ".parquet.gzip", ".xlsx", ".xls", ".csv", ".tsv")
MIN_APPEND_SCHEMA_MATCH_PERCENT = 50.0
SELECTABLE_ICEBERG_TYPES = ("STRING", "BIGINT", "DOUBLE", "TIMESTAMP", "BOOLEAN")


@dataclass(frozen=True)
class BucketScope:
    table_bucket_arn: str
    namespace: str
    label: str


@dataclass(frozen=True)
class PilotUser:
    user_id: str
    is_admin: bool
    buckets: tuple[BucketScope, ...]


def _configured_users() -> dict:
    """Placeholder for the future frontend's authenticated user/bucket relationship.

    Set PILOT_USER_ACCESS_JSON to a mapping of user IDs to ``is_admin`` and a
    ``buckets`` list. Until that integration exists, local-admin is deliberately
    scoped only to this pilot bucket and namespace.
    """
    default = {
        "local-admin": {
            "is_admin": True,
            "buckets": [{"table_bucket_arn": TABLE_BUCKET_ARN, "namespace": NAMESPACE, "label": "AH SOC delta pilot"}],
        }
    }
    raw = os.environ.get("PILOT_USER_ACCESS_JSON")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("PILOT_USER_ACCESS_JSON must contain valid JSON") from error


def _current_user(x_pilot_user_id: str | None = Header(default=None, alias="X-Pilot-User-Id")) -> PilotUser:
    # TRUST PLACEHOLDER: replace this lookup with verified frontend identity claims.
    user_id = x_pilot_user_id or os.environ.get("PILOT_LOCAL_USER_ID", "local-admin")
    definition = _configured_users().get(user_id)
    if not definition:
        raise HTTPException(403, "This user has no S3 Tables bucket assignment")
    buckets = tuple(
        BucketScope(
            table_bucket_arn=item["table_bucket_arn"],
            namespace=item.get("namespace", NAMESPACE),
            label=item.get("label", item["table_bucket_arn"].rsplit("/", 1)[-1]),
        )
        for item in definition.get("buckets", [])
    )
    if not buckets:
        raise HTTPException(403, "This user has no S3 Tables bucket assignment")
    return PilotUser(user_id=user_id, is_admin=bool(definition.get("is_admin", False)), buckets=buckets)


def _require_scope(user: PilotUser, table_bucket_arn: str, namespace: str) -> BucketScope:
    scope = next((item for item in user.buckets if item.table_bucket_arn == table_bucket_arn and item.namespace == namespace), None)
    if not scope:
        raise HTTPException(403, "The selected S3 Tables bucket or namespace is not assigned to this user")
    return scope


def _canonical_table_name(value: str) -> str:
    """Translate UI-friendly hyphens to the S3 Tables identifier convention."""
    return value.strip().replace("-", "_")


class IngestionRequest(BaseModel):
    mode: Literal["create", "append"]
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    request_id: str
    # Backward-compatible audit field name.  It is now a free-form user tag,
    # not a calendar month.
    reporting_month: str = Field(min_length=1, max_length=256)
    type_overrides: dict[str, str] = Field(default_factory=dict)
    deduplication_columns: list[str] = Field(default_factory=list)
    @field_validator("table", mode="before")
    @classmethod
    def canonicalise_table(cls, value: str) -> str:
        return _canonical_table_name(value)


class DeleteTableRequest(BaseModel):
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")


class RollbackRequest(BaseModel):
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    upload_id: str = Field(min_length=1, max_length=128)
    confirm: bool = False


def _soc_schema() -> list[dict[str, str]]:
    return [{"name": column, "type": "TIMESTAMP" if column in TIMESTAMP_TARGET_COLUMNS else "BIGINT" if column == "cnt" else "STRING"} for column in TARGET_COLUMNS]


def _contract_key(table_bucket_arn: str, namespace: str, table: str) -> str:
    scope = hashlib.sha256(f"{table_bucket_arn}|{namespace}".encode()).hexdigest()[:16]
    return f"{WEB_CONTRACT_PREFIX}/{scope}/{table}.json"


def _history_prefix(table_bucket_arn: str, namespace: str, table: str) -> str:
    """Return the request-scoped, value-free upload-history projection prefix."""
    scope = hashlib.sha256(f"{table_bucket_arn}|{namespace}".encode()).hexdigest()[:16]
    return f"{WEB_HISTORY_PREFIX}/{scope}/{table}/"


def _history_entries(table_bucket_arn: str, namespace: str, table: str) -> list[dict]:
    """Load the latest immutable audit projection for each upload; admin only."""
    entries: list[dict] = []
    paginator = s3.get_paginator("list_objects_v2")
    prefix = _history_prefix(table_bucket_arn, namespace, table)
    for page in paginator.paginate(Bucket=SOURCE_BUCKET, Prefix=prefix):
        for item in page.get("Contents", []):
            try:
                entry = json.loads(s3.get_object(Bucket=SOURCE_BUCKET, Key=item["Key"])["Body"].read())
            except Exception:
                continue
            if (
                entry.get("table_bucket_arn") == table_bucket_arn
                and entry.get("namespace") == namespace
                and entry.get("target_table") == table
            ):
                entries.append(entry)
    return sorted(entries, key=lambda item: item.get("uploaded_at") or "", reverse=True)


def _upload_id() -> str:
    """Technical identifier deliberately independent of free-form user tags."""
    return f"UPLOAD-{uuid.uuid4().hex[:12].upper()}"


def _configure_snapshot_retention(table_bucket_arn: str, namespace: str, table: str) -> None:
    """Configure S3 Tables retention from the local service's current boto3.

    Glue 5's bundled boto3 may not yet include the S3 Tables service model, so
    this control-plane operation must not run inside the Glue data-plane job.
    """
    s3tables.put_table_maintenance_configuration(
        tableBucketARN=table_bucket_arn,
        namespace=namespace,
        name=table,
        type="icebergSnapshotManagement",
        value={"status": "enabled", "settings": {"icebergSnapshotManagement": SNAPSHOT_RETENTION}},
    )


def _load_contract_record(table_bucket_arn: str, namespace: str, table: str) -> dict:
    """Read a table contract, retaining legacy full-row de-duplication."""
    if table == "soc":
        return {"schema": _soc_schema(), "deduplication_columns": [], "deduplication_policy": "legacy-full-row-v1"}
    try:
        response = s3.get_object(Bucket=SOURCE_BUCKET, Key=_contract_key(table_bucket_arn, namespace, table))
    except s3.exceptions.NoSuchKey as error:
        # Backward-compatible lookup for contracts created by the first local UI.
        if table_bucket_arn == TABLE_BUCKET_ARN and namespace == NAMESPACE:
            try:
                response = s3.get_object(Bucket=SOURCE_BUCKET, Key=f"{WEB_CONTRACT_PREFIX}/{table}.json")
            except s3.exceptions.NoSuchKey:
                raise HTTPException(400, f"No local pilot schema contract is available for table {table!r}") from error
        else:
            raise HTTPException(400, f"No local pilot schema contract is available for table {table!r}") from error
    record = json.loads(response["Body"].read())
    if not record.get("schema"):
        raise HTTPException(400, f"The stored table contract for {table!r} has no schema")
    # Contracts created before key selection deliberately retain their original
    # full-row behavior rather than changing existing tables unexpectedly.
    record.setdefault("deduplication_columns", [])
    record.setdefault("deduplication_policy", "legacy-full-row-v1")
    return record


def _load_contract(table_bucket_arn: str, namespace: str, table: str) -> list[dict[str, str]]:
    return _load_contract_record(table_bucket_arn, namespace, table)["schema"]


def _temporary_suffix(filename: str) -> str:
    return ".parquet" if filename.lower().endswith((".parquet", ".parquet.gzip")) else Path(filename).suffix


def _read_upload_table(path: Path, filename: str) -> pa.Table:
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
    # Excel and CSV object columns can legitimately contain a mix such as
    # 671 and "672A". PyArrow otherwise chooses int64 from the first value and
    # rejects the later text value. Keep native types where Arrow can infer
    # them, but use a string column whenever the source is genuinely mixed.
    for name in frame.columns:
        series = frame[name]
        if not pd.api.types.is_object_dtype(series.dtype):
            continue
        try:
            pa.array(series, from_pandas=True)
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            frame[name] = series.map(lambda value: None if pd.isna(value) else str(value)).astype("string")
    return pa.Table.from_pandas(frame, preserve_index=False)


def _sanitization_details(schema: pa.Schema) -> tuple[pa.Schema, dict]:
    sanitized, plan = sanitised_schema(schema)
    return sanitized, {
        "dropped_columns": list(plan.drop_columns),
        "encrypted_columns": list(plan.identifier_columns),
        "postal_columns": list(plan.postal_columns),
        "age_banded_columns": list(plan.age_columns),
    }


def _read_schemas(files: list[UploadFile]) -> tuple[list[pa.Schema], list[dict]]:
    schemas, sanitization = [], []
    for upload in files:
        if not upload.filename or not upload.filename.lower().endswith(SUPPORTED_UPLOAD_SUFFIXES):
            raise HTTPException(400, f"Supported files are Parquet, XLSX, XLS, CSV, and TSV: {upload.filename or '<unnamed>'}")
        with tempfile.NamedTemporaryFile(suffix=_temporary_suffix(upload.filename), delete=False) as temp:
            path = Path(temp.name)
            try:
                shutil.copyfileobj(upload.file, temp)
                temp.flush()
                schema, details = _sanitization_details(_read_upload_table(path, upload.filename).schema)
                schemas.append(schema)
                sanitization.append(details)
            except Exception as error:
                raise HTTPException(400, f"Cannot read {upload.filename} as Parquet: {error}") from error
            finally:
                path.unlink(missing_ok=True)
                upload.file.seek(0)
    return schemas, sanitization


def _first_upload_contract(upload: UploadFile) -> tuple[list[dict[str, str]], list[str]]:
    """Profile every populated value of the first upload for table creation."""
    with tempfile.NamedTemporaryFile(suffix=_temporary_suffix(upload.filename or "upload"), delete=False) as temp:
        path = Path(temp.name)
        try:
            shutil.copyfileobj(upload.file, temp)
            temp.flush()
            table = _read_upload_table(path, upload.filename or "upload")
            sanitized_schema, plan = sanitised_schema(table.schema)
            forced_strings = set(plan.identifier_columns) | set(plan.postal_columns) | set(plan.age_columns)
            return schema_from_table(table, sanitized_schema, forced_strings)
        finally:
            path.unlink(missing_ok=True)
            upload.file.seek(0)


def _create_type_selections(comparisons: list[dict], target: list[dict[str, str]]) -> list[dict[str, str | list[str] | bool]]:
    """Expose first-file conversion choices which define a new table contract.

    The first file alone defines a new table.  Later files in that same
    request must conform to the final selected contract, but must not create
    duplicate or competing type choices in the browser.
    """
    selections = []
    target_by_name = {field["name"]: field for field in target}
    first_comparison = comparisons[0] if comparisons else {"type_conversions": []}
    for conversion in first_comparison["type_conversions"]:
        name = conversion["column"]
        selections.append({
            "column": name,
            "source_type": conversion["source_type"],
            "suggested_target_type": target_by_name[name]["type"],
            "allowed_target_types": list(SELECTABLE_ICEBERG_TYPES),
            "locked": False,
        })
    return selections


def _create_deduplication_candidates(upload: UploadFile, target: list[dict[str, str]]) -> list[dict]:
    """Return every stored first-upload column with ephemeral safe examples."""
    with tempfile.NamedTemporaryFile(suffix=_temporary_suffix(upload.filename or "upload"), delete=False) as temp:
        path = Path(temp.name)
        try:
            shutil.copyfileobj(upload.file, temp)
            temp.flush()
            source = _read_upload_table(path, upload.filename or "upload")
            _, plan = sanitised_schema(source.schema)
            sensitive_sources = set(plan.drop_columns) | set(plan.identifier_columns) | set(plan.postal_columns) | set(plan.age_columns)
            source_by_canonical = dict(zip(normalise_names(source.schema.names), source.schema.names))
            candidates = []
            for field in target:
                source_name = source_by_canonical.get(field["name"])
                masked = source_name in sensitive_sources
                values = []
                non_null_count = 0
                distinct_non_null_count = 0
                if source_name and not masked:
                    column = source[source_name]
                    # Keep preflight bounded even for large healthcare files:
                    # Arrow calculates counts natively, while sampling reads a
                    # small random set of scalars rather than materialising an
                    # entire column as a Python list.
                    non_null_count = int(pc.count(column).as_py())
                    distinct_non_null_count = int(pc.count_distinct(column).as_py())
                    indexes = random.SystemRandom().sample(range(len(column)), min(1024, len(column)))
                    for index in indexes:
                        value = column[index].as_py()
                        if value is not None and str(value).strip().lower() not in {"", "nan", "none", "nat"}:
                            values.append(str(value)[:160])
                            if len(values) == 5:
                                break
                candidates.append({
                    "column": field["name"], "target_type": field["type"],
                    "source_type": str(source.schema.field(source_name).type) if source_name else "MISSING",
                    "sample_values": values, "samples_masked": bool(masked),
                    # The uploader normalises then encrypts identifiers using
                    # the configured stable legacy-compatible representation.
                    # They are therefore valid case-level keys, but values
                    # remain masked in the browser.
                    "deduplication_eligible": True,
                    "deduplication_ineligible_reason": None,
                    # Quality is metadata only. Sensitive fields deliberately
                    # do not expose their value distribution to the browser.
                    "non_null_count": non_null_count if not masked else None,
                    "distinct_non_null_count": distinct_non_null_count if not masked else None,
                })
            return candidates
        finally:
            path.unlink(missing_ok=True)
            upload.file.seek(0)


def _create_type_selection_samples(upload: UploadFile, selections: list[dict]) -> list[dict]:
    """Attach a few privacy-safe, non-persistent examples to type choices.

    Samples exist only in the HTTP preflight response.  They are deliberately
    excluded from the staging manifest, Glue arguments, QC reports, and upload
    history.  Any column covered by healthcare sanitisation is represented by a
    masked notice rather than a source value.
    """
    if not selections:
        return selections
    samples = {item["column"]: item for item in _create_deduplication_candidates(upload, [
        {"name": item["column"], "type": item.get("suggested_target_type", item.get("source_type", "STRING"))}
        for item in selections
    ])}
    for choice in selections:
        choice.update({key: samples[choice["column"]][key] for key in ("sample_values", "samples_masked")})
    return selections


def _validate_create_deduplication_columns(preview: dict, columns: list[str]) -> list[str]:
    """Require a non-empty, known, canonical key selection for new tables."""
    if not columns:
        raise HTTPException(422, "Choose at least one de-duplication column for a new table")
    if len(columns) != len(set(columns)):
        raise HTTPException(422, "A de-duplication column may be selected only once")
    candidates = {item["column"]: item for item in preview.get("deduplication_candidates", [])}
    available = set(candidates)
    invalid = sorted(set(columns) - available)
    if invalid:
        raise HTTPException(422, f"Unknown de-duplication column: {', '.join(invalid)}")
    ineligible = [column for column in columns if not candidates[column].get("deduplication_eligible", True)]
    if ineligible:
        raise HTTPException(422, f"These columns cannot be used for de-duplication: {', '.join(ineligible)}")
    return columns


def _apply_create_type_overrides(preview: dict, overrides: dict[str, str]) -> list[dict[str, str]]:
    """Validate user choices and return the immutable initial table contract."""
    choices = {item["column"]: item for item in preview.get("type_selections", [])}
    invalid = sorted(set(overrides) - set(choices))
    if invalid:
        raise HTTPException(422, f"Type selection is not available for: {', '.join(invalid)}")
    result = []
    for field in preview["target_schema"]:
        selected = overrides.get(field["name"], field["type"])
        choice = choices.get(field["name"])
        if choice and (choice["locked"] or selected not in SELECTABLE_ICEBERG_TYPES):
            raise HTTPException(422, f"Invalid target type selection for {field['name']}")
        result.append({**field, "type": selected})
    return result


def _unsafe_cast_issues(upload: UploadFile, target: list[dict[str, str]]) -> list[dict[str, int | str]]:
    """Return value-level casts Spark would turn into NULL, without values.

    This keeps a bad append out of temporary S3 and Glue.  It deliberately
    returns only column names and counts: raw healthcare values never enter the
    preflight response or logs.
    """
    with tempfile.NamedTemporaryFile(suffix=_temporary_suffix(upload.filename or "upload"), delete=False) as temp:
        path = Path(temp.name)
        try:
            shutil.copyfileobj(upload.file, temp)
            temp.flush()
            table = _read_upload_table(path, upload.filename or "upload")
            sanitized_schema, plan = sanitised_schema(table.schema)
            normalized = normalise_names([field.name for field in sanitized_schema])
            source_columns = dict(zip(normalized, sanitized_schema.names))
            sensitive = set(plan.identifier_columns) | set(plan.postal_columns) | set(plan.age_columns)
            target_by_name = {field["name"]: field["type"] for field in target}
            issues = []
            for name, source_name in source_columns.items():
                target_type = target_by_name.get(name)
                if not target_type or source_name in sensitive:
                    # Sanitization converts these selected source fields to
                    # STRING before Glue sees them.
                    continue
                source = table[source_name]
                source_type = source.type
                if target_type == "STRING" or (
                    target_type == "BIGINT" and pa.types.is_integer(source_type)
                ) or (
                    target_type == "DOUBLE" and (pa.types.is_integer(source_type) or pa.types.is_floating(source_type))
                ) or (
                    target_type == "TIMESTAMP" and (pa.types.is_timestamp(source_type) or pa.types.is_date(source_type))
                ):
                    continue
                series = source.to_pandas()
                non_null = series.notna()
                if target_type in {"BIGINT", "DOUBLE"}:
                    converted = pd.to_numeric(series, errors="coerce")
                elif target_type == "TIMESTAMP":
                    converted = pd.to_datetime(series, errors="coerce")
                elif target_type == "BOOLEAN":
                    converted = series.astype("string").str.strip().str.lower().isin({"true", "false", "0", "1"})
                    invalid = non_null & ~converted
                    count = int(invalid.sum())
                    if count:
                        issues.append({"column": name, "source_type": str(source_type), "target_type": target_type, "unsafe_value_count": count})
                    continue
                else:
                    continue
                invalid = non_null & converted.isna()
                count = int(invalid.sum())
                if count:
                    issues.append({"column": name, "source_type": str(source_type), "target_type": target_type, "unsafe_value_count": count})
            return issues
        finally:
            path.unlink(missing_ok=True)
            upload.file.seek(0)


def _preflight(mode: str, table_bucket_arn: str, namespace: str, table: str, files: list[UploadFile]) -> dict:
    schemas, sanitization = _read_schemas(files)
    if not schemas:
        raise HTTPException(400, "Choose at least one Parquet file")
    if mode == "create":
        target, creation_warnings = _first_upload_contract(files[0])
        contract = {"schema": target, "deduplication_columns": [], "deduplication_policy": "skip-existing-key-report-conflict-v1"}
    else:
        contract = _load_contract_record(table_bucket_arn, namespace, table)
        target, creation_warnings = contract["schema"], []
    comparisons = [compare_schema(schema, target) for schema in schemas]
    target_by_name = {field["name"]: field["type"] for field in target}
    incompatible_sensitive_columns = []
    for schema, details in zip(schemas, sanitization):
        source_fields, _ = schema_from_arrow(schema)
        source_by_original = {field["source_name"]: field["name"] for field in source_fields}
        for original in details["encrypted_columns"] + details["postal_columns"] + details["age_banded_columns"]:
            field_name = source_by_original.get(original)
            target_type = target_by_name.get(field_name)
            if target_type is not None and target_type != "STRING":
                incompatible_sensitive_columns.append({"column": field_name, "target_type": target_type})
    file_results = []
    rejection_reasons = []
    for upload, comparison, details in zip(files, comparisons, sanitization):
        file_rejection_reasons = []
        sanitized_columns = sorted(set(
            details["dropped_columns"]
            + details["encrypted_columns"]
            + details["postal_columns"]
            + details["age_banded_columns"]
        ))
        match_accepted = mode == "create" or comparison["matching_percentage"] >= MIN_APPEND_SCHEMA_MATCH_PERCENT
        if not match_accepted:
            reason = (
                f"{upload.filename}: only {comparison['matching_column_count']} of "
                f"{comparison['target_column_count']} initial-table columns match "
                f"({comparison['matching_percentage']:.1f}%). At least "
                f"{MIN_APPEND_SCHEMA_MATCH_PERCENT:.0f}% is required for an append."
            )
            rejection_reasons.append(reason)
            file_rejection_reasons.append(reason)
        unsafe_casts = _unsafe_cast_issues(upload, target) if mode == "append" else []
        if unsafe_casts:
            columns = ", ".join(
                f"{item['column']} ({item['unsafe_value_count']} invalid value{'s' if item['unsafe_value_count'] != 1 else ''})"
                for item in unsafe_casts
            )
            reason = (
                f"{upload.filename}: values cannot be safely converted to the existing table schema: {columns}."
            )
            rejection_reasons.append(reason)
            file_rejection_reasons.append(reason)
        file_results.append({
            "filename": upload.filename,
            **comparison,
            "sanitization": details,
            "sanitized_columns": sanitized_columns,
            "sanitized_column_count": len(sanitized_columns),
            "unsafe_casts": unsafe_casts,
            "accepted": match_accepted and not unsafe_casts,
            "rejection_reasons": file_rejection_reasons,
        })
    if incompatible_sensitive_columns:
        rejection_reasons.append(
            "The selected table has non-string sensitive columns and cannot accept encrypted or masked values."
        )
    type_selections = _create_type_selection_samples(files[0], _create_type_selections(comparisons, target)) if mode == "create" else []
    return {
        "mode": mode, "table_bucket_arn": table_bucket_arn, "namespace": namespace, "table": table, "target_schema": target, "creation_warnings": creation_warnings,
        "initial_table_column_count": len(target),
        "minimum_append_schema_match_percent": MIN_APPEND_SCHEMA_MATCH_PERCENT,
        "files": file_results,
        "type_selections": type_selections,
        "deduplication_candidates": _create_deduplication_candidates(files[0], target) if mode == "create" else [],
        "deduplication_columns": contract["deduplication_columns"],
        "deduplication_policy": contract["deduplication_policy"],
        "incompatible_sensitive_columns": incompatible_sensitive_columns,
        "accepted": not rejection_reasons,
        "rejection_reasons": rejection_reasons,
        "sensitive_column_scan": "Sanitization is enforced before temporary S3 staging.",
    }


def _make_glue_compatible_parquet(source: Path, filename: str, key=None) -> tuple[Path, bool, dict]:
    """Stage every supported file as Spark-safe Parquet for the Glue job."""
    table = _read_upload_table(source, filename)
    schema, plan = sanitised_schema(table.schema)
    sanitization_required = bool(plan.drop_columns or plan.identifier_columns or plan.postal_columns or plan.age_columns)
    audit = {"dropped_columns": [], "encrypted_columns": [], "postal_columns": [], "age_banded_columns": [], "newly_encrypted_values": 0, "already_encrypted_values": 0}
    if sanitization_required:
        table, audit = sanitise_table(table, key)
    else:
        schema = table.schema
    names = normalise_names([field.name for field in schema])
    has_nanosecond_timestamps = any(
        pa.types.is_timestamp(field.type) and field.type.unit == "ns" for field in schema
    )
    # Spark/Glue cannot read Parquet TIME(MICROS), which Arrow produces for
    # Excel cells containing only a time-of-day.  The table contract models
    # those fields as strings (for example, ``ATIME`` and ``SOPTIME``), so
    # serialise them as text before staging rather than emitting TIME(MICROS).
    has_time_of_day_values = any(pa.types.is_time(field.type) for field in schema)
    has_unsafe_names = names != list(schema.names)
    is_parquet = filename.lower().endswith((".parquet", ".parquet.gzip"))
    if is_parquet and not sanitization_required and not has_nanosecond_timestamps and not has_time_of_day_values and not has_unsafe_names:
        return source, False, audit

    fields = [
        pa.field(
            name,
            pa.timestamp("us", tz=field.type.tz)
            if pa.types.is_timestamp(field.type) and field.type.unit == "ns"
            else pa.string()
            if pa.types.is_time(field.type)
            else field.type,
            nullable=field.nullable,
            metadata=field.metadata,
        )
        for field, name in zip(schema, names)
    ]
    target_schema = pa.schema(fields, metadata=schema.metadata)
    table = table.rename_columns(names).cast(target_schema, safe=False)
    converted = source.with_name(f"{source.stem}-glue-compatible.parquet")
    pq.write_table(table, converted, compression="snappy")
    return converted, True, audit


def _ensure_web_job() -> None:
    script = ROOT / "generic_glue_job.py"
    s3.put_object(Bucket=SOURCE_BUCKET, Key=WEB_SCRIPT_KEY, Body=script.read_bytes(), ContentType="text/x-python", ServerSideEncryption="AES256")
    role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
    conf = " ".join([
        "spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        "--conf spark.sql.legacy.timeParserPolicy=CORRECTED",
        "--conf spark.sql.catalog.s3_rest_catalog=org.apache.iceberg.spark.SparkCatalog",
        "--conf spark.sql.catalog.s3_rest_catalog.type=rest",
        f"--conf spark.sql.catalog.s3_rest_catalog.uri=https://s3tables.{REGION}.amazonaws.com/iceberg",
        f"--conf spark.sql.catalog.s3_rest_catalog.warehouse={TABLE_BUCKET_ARN}",
        "--conf spark.sql.catalog.s3_rest_catalog.rest.sigv4-enabled=true",
        "--conf spark.sql.catalog.s3_rest_catalog.rest.signing-name=s3tables",
        f"--conf spark.sql.catalog.s3_rest_catalog.rest.signing-region={REGION}",
        "--conf spark.sql.catalog.s3_rest_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO",
    ])
    definition = {
        "Role": role_arn, "Command": {"Name": "glueetl", "ScriptLocation": f"s3://{SOURCE_BUCKET}/{WEB_SCRIPT_KEY}", "PythonVersion": "3"},
        "GlueVersion": "5.0", "WorkerType": "G.1X", "NumberOfWorkers": 2, "Timeout": 30, "MaxRetries": 0,
        "ExecutionProperty": {"MaxConcurrentRuns": 1},
        "DefaultArguments": {"--job-language": "python", "--datalake-formats": "iceberg", "--enable-metrics": "true", "--enable-continuous-cloudwatch-log": "true", "--conf": conf},
        "Description": "Local web UI generic append-only S3 Tables pilot ingestion",
    }
    try:
        glue.get_job(JobName=WEB_JOB_NAME)
        glue.update_job(JobName=WEB_JOB_NAME, JobUpdate=definition)
    except glue.exceptions.EntityNotFoundException:
        glue.create_job(Name=WEB_JOB_NAME, **definition)


def _iceberg_row_count(metadata_uri: str | None) -> int | None:
    """Best-effort count from Iceberg metadata; listing must stay responsive."""
    if not metadata_uri or not metadata_uri.startswith("s3://"):
        return None
    try:
        bucket, key = metadata_uri.removeprefix("s3://").split("/", 1)
        metadata = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        current = metadata.get("current-snapshot-id")
        snapshot = next((item for item in metadata.get("snapshots", []) if item.get("snapshot-id") == current), None)
        count = (snapshot or {}).get("summary", {}).get("total-records")
        return int(count) if count is not None else None
    except Exception:
        return None


def _table_summary(table_bucket_arn: str, namespace: str, item: dict) -> dict:
    details = s3tables.get_table(tableBucketARN=table_bucket_arn, namespace=namespace, name=item["name"])
    return {
        "name": item["name"],
        "created_at": str(item.get("createdAt")),
        "modified_at": str(item.get("modifiedAt")),
        "row_count": _iceberg_row_count(details.get("metadataLocation")),
    }


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/static/{asset}")
def static(asset: str):
    if asset not in {"app.js", "style.css"}:
        raise HTTPException(404)
    return FileResponse(ROOT / "static" / asset)


@app.get("/api/buckets")
def list_buckets(user: PilotUser = Depends(_current_user)):
    return {
        "user_id": user.user_id,
        "is_admin": user.is_admin,
        "buckets": [
            {"table_bucket_arn": item.table_bucket_arn, "namespace": item.namespace, "label": item.label}
            for item in user.buckets
        ],
    }


@app.get("/api/tables")
def list_tables(table_bucket_arn: str, namespace: str, user: PilotUser = Depends(_current_user)):
    _require_scope(user, table_bucket_arn, namespace)
    result = s3tables.list_tables(tableBucketARN=table_bucket_arn, namespace=namespace)
    tables = [
        _table_summary(table_bucket_arn, namespace, item)
        for item in result.get("tables", [])
        if item["name"] != UPLOAD_HISTORY_TABLE
    ]
    return {
        "table_bucket": table_bucket_arn,
        "namespace": namespace,
        "is_admin": user.is_admin,
        "tables": sorted(tables, key=lambda item: item["name"]),
    }


@app.post("/api/preflight")
async def preflight(
    mode: Literal["create", "append"] = Form(),
    table_bucket_arn: str = Form(),
    namespace: str = Form(),
    table: str = Form(),
    files: list[UploadFile] = File(),
    user: PilotUser = Depends(_current_user),
):
    _require_scope(user, table_bucket_arn, namespace)
    return _preflight(mode, table_bucket_arn, namespace, _canonical_table_name(table), files)


@app.post("/api/ingestions")
async def start_ingestion(
    request: str = Form(),
    files: list[UploadFile] = File(),
    user: PilotUser = Depends(_current_user),
):
    try:
        payload = IngestionRequest.model_validate_json(request)
    except Exception as error:
        raise HTTPException(400, f"Invalid ingestion request: {error}") from error
    _require_scope(user, payload.table_bucket_arn, payload.namespace)
    if payload.table == UPLOAD_HISTORY_TABLE:
        raise HTTPException(400, "The reserved uploader audit table cannot be selected as an ingestion destination")
    if not files:
        raise HTTPException(400, "Choose at least one supported file")
    preview = _preflight(payload.mode, payload.table_bucket_arn, payload.namespace, payload.table, files)
    if not preview["accepted"]:
        raise HTTPException(
            422,
            detail={
                "message": "Upload rejected by the hard schema and sanitization validation rules.",
                "rejection_reasons": preview["rejection_reasons"],
                "preflight": preview,
            },
        )
    target_schema = _apply_create_type_overrides(preview, payload.type_overrides) if payload.mode == "create" else preview["target_schema"]
    deduplication_columns = (
        _validate_create_deduplication_columns(preview, payload.deduplication_columns)
        if payload.mode == "create" else preview["deduplication_columns"]
    )
    if payload.mode == "create":
        override_issues = []
        for upload in files:
            override_issues.extend(_unsafe_cast_issues(upload, target_schema))
        if override_issues:
            raise HTTPException(
                422,
                detail={
                    "message": "The chosen first-upload types would discard values; choose compatible types.",
                    "unsafe_casts": override_issues,
                },
            )
    if payload.mode == "append":
        try:
            _configure_snapshot_retention(payload.table_bucket_arn, payload.namespace, payload.table)
        except Exception as error:
            raise HTTPException(500, "Unable to configure the required S3 Tables snapshot retention") from error
    sensitive_columns_present = any(
        item["sanitization"]["encrypted_columns"] for item in preview["files"]
    )
    try:
        active_key = encryption_key() if sensitive_columns_present else None
    except Exception as error:
        raise HTTPException(500, "Unable to retrieve the configured encryption key") from error
    request_prefix = f"{WEB_UPLOAD_PREFIX}/{payload.request_id}"
    objects, sanitization_audits = [], []
    for number, upload in enumerate(files):
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(suffix=_temporary_suffix(upload.filename or "upload.parquet"), delete=False) as temp:
            path = Path(temp.name)
            try:
                while block := await upload.read(8 * 1024 * 1024):
                    digest.update(block)
                    temp.write(block)
                temp.flush()
                original_filename = upload.filename or "upload.parquet"
                key = f"{request_prefix}/input/{number:02d}-{Path(original_filename).stem}.parquet"
                staged, transformed, audit = _make_glue_compatible_parquet(path, original_filename, active_key)
                try:
                    with staged.open("rb") as stream:
                        s3.put_object(Bucket=SOURCE_BUCKET, Key=key, Body=stream, ContentType="application/octet-stream", Metadata={"sha256": digest.hexdigest(), "original_filename": original_filename, "spark_compatible_staging": str(transformed).lower(), "sanitized": str(bool(audit["dropped_columns"] or audit["encrypted_columns"] or audit["postal_columns"] or audit["age_banded_columns"])).lower()}, ServerSideEncryption="AES256")
                finally:
                    if staged != path:
                        staged.unlink(missing_ok=True)
                objects.append(f"s3://{SOURCE_BUCKET}/{key}")
                sanitization_audits.append({"filename": original_filename, **audit})
            finally:
                path.unlink(missing_ok=True)
    if payload.mode == "create":
        s3.put_object(
            Bucket=SOURCE_BUCKET,
            Key=_contract_key(payload.table_bucket_arn, payload.namespace, payload.table),
            Body=json.dumps({
                "schema": target_schema,
                "deduplication_columns": deduplication_columns,
                "deduplication_policy": "skip-existing-key-report-conflict-v1",
            }, indent=2).encode(),
            ContentType="application/json", ServerSideEncryption="AES256",
        )
    manifest_key = f"{request_prefix}/manifest.json"
    s3.put_object(
        Bucket=SOURCE_BUCKET, Key=manifest_key,
        Body=json.dumps({
            "files": objects, "schema": target_schema, "sanitization": sanitization_audits,
            "deduplication_columns": deduplication_columns,
            "deduplication_policy": preview["deduplication_policy"],
        }).encode(),
        ContentType="application/json", ServerSideEncryption="AES256",
    )
    _ensure_web_job()
    run_id = str(uuid.uuid4())
    upload_id = _upload_id()
    uploaded_at = datetime.now(timezone.utc).isoformat()
    history_prefix = _history_prefix(payload.table_bucket_arn, payload.namespace, payload.table)
    response = glue.start_job_run(
        JobName=WEB_JOB_NAME,
        Arguments={
            "--MODE": payload.mode, "--MANIFEST_URI": f"s3://{SOURCE_BUCKET}/{manifest_key}",
            "--TABLE_BUCKET_ARN": payload.table_bucket_arn, "--NAMESPACE": payload.namespace,
            "--TABLE": payload.table, "--QC_PREFIX": QC_PREFIX, "--RUN_ID": run_id,
            "--UPLOAD_ID": upload_id, "--UPLOADED_BY": user.user_id,
            "--ORIGINAL_UPLOADED_BY": user.user_id, "--ORIGINAL_UPLOADED_AT": uploaded_at,
            "--REPORTING_MONTH": payload.reporting_month,
            "--FILENAMES_JSON": json.dumps([upload.filename or "upload" for upload in files]),
            # Glue rejects blank argument values. This is deliberately ignored
            # by create/append modes and only validated by rollback mode.
            "--AUDIT_PREFIX": f"s3://{SOURCE_BUCKET}/{history_prefix}", "--ROLLBACK_SNAPSHOT_ID": "not-applicable",
        },
    )
    return {
        "job_run_id": response["JobRunId"], "qc_uri": f"{QC_PREFIX}/web/{run_id}/report.json",
        "request_id": payload.request_id, "upload_id": upload_id, "operation": "ingestion",
    }


@app.delete("/api/tables")
def delete_table(payload: DeleteTableRequest, user: PilotUser = Depends(_current_user)):
    _require_scope(user, payload.table_bucket_arn, payload.namespace)
    if not user.is_admin:
        raise HTTPException(403, "Only administrators may delete S3 Tables")
    if payload.table == UPLOAD_HISTORY_TABLE:
        raise HTTPException(400, "The reserved uploader audit table cannot be deleted through this UI")
    s3tables.delete_table(
        tableBucketARN=payload.table_bucket_arn,
        namespace=payload.namespace,
        name=payload.table,
    )
    return {"deleted": payload.table, "table_bucket_arn": payload.table_bucket_arn, "namespace": payload.namespace}


@app.get("/api/upload-history")
def upload_history(table_bucket_arn: str, namespace: str, table: str, user: PilotUser = Depends(_current_user)):
    _require_scope(user, table_bucket_arn, namespace)
    if not user.is_admin:
        raise HTTPException(403, "Only administrators may view upload history and snapshot IDs")
    if table == UPLOAD_HISTORY_TABLE:
        raise HTTPException(400, "The reserved uploader audit table is not a master-data destination")
    return {
        "table_bucket_arn": table_bucket_arn,
        "namespace": namespace,
        "table": table,
        "history": _history_entries(table_bucket_arn, namespace, table),
    }


@app.post("/api/rollbacks")
def start_rollback(payload: RollbackRequest, user: PilotUser = Depends(_current_user)):
    _require_scope(user, payload.table_bucket_arn, payload.namespace)
    if not user.is_admin:
        raise HTTPException(403, "Only administrators may roll back S3 Tables uploads")
    if not payload.confirm:
        raise HTTPException(400, "Explicit rollback confirmation is required")
    if payload.table == UPLOAD_HISTORY_TABLE:
        raise HTTPException(400, "The reserved uploader audit table cannot be rolled back through this UI")
    history = _history_entries(payload.table_bucket_arn, payload.namespace, payload.table)
    selected = next((item for item in history if item.get("upload_id") == payload.upload_id), None)
    successful = [item for item in history if item.get("status") == "SUCCESS"]
    latest = max(successful, key=lambda item: item.get("uploaded_at") or "", default=None)
    if not selected or selected.get("status") != "SUCCESS":
        raise HTTPException(409, "Only a successful upload that has not already been rolled back can be restored")
    if selected != latest:
        raise HTTPException(409, "Only the latest successful uploader-managed update may be rolled back")
    snapshot_id = selected.get("previous_snapshot_id")
    if not snapshot_id:
        raise HTTPException(409, "The initial table load has no earlier snapshot to restore")
    _ensure_web_job()
    run_id = str(uuid.uuid4())
    response = glue.start_job_run(
        JobName=WEB_JOB_NAME,
        Arguments={
            # The rollback job does not read a manifest, but Glue requires each
            # supplied command argument to have a non-empty value.
            "--MODE": "rollback", "--MANIFEST_URI": f"s3://{SOURCE_BUCKET}/{WEB_UPLOAD_PREFIX}/not-used-for-rollback.json", "--TABLE_BUCKET_ARN": payload.table_bucket_arn,
            "--NAMESPACE": payload.namespace, "--TABLE": payload.table, "--QC_PREFIX": QC_PREFIX,
            "--RUN_ID": run_id, "--UPLOAD_ID": payload.upload_id, "--UPLOADED_BY": user.user_id,
            "--ORIGINAL_UPLOADED_BY": selected.get("uploaded_by") or user.user_id,
            "--ORIGINAL_UPLOADED_AT": selected.get("uploaded_at") or datetime.now(timezone.utc).isoformat(),
            "--REPORTING_MONTH": selected.get("reporting_month") or "", "--FILENAMES_JSON": selected.get("filenames") or "[]",
            "--AUDIT_PREFIX": f"s3://{SOURCE_BUCKET}/{_history_prefix(payload.table_bucket_arn, payload.namespace, payload.table)}",
            "--ROLLBACK_SNAPSHOT_ID": str(snapshot_id),
        },
    )
    return {
        "job_run_id": response["JobRunId"], "qc_uri": f"{QC_PREFIX}/web/{run_id}/report.json",
        "upload_id": payload.upload_id, "operation": "rollback",
    }


@app.get("/api/ingestions/{job_run_id}")
def ingestion_status(job_run_id: str, operation: Literal["ingestion", "rollback"] = "ingestion"):
    run = glue.get_job_run(JobName=WEB_JOB_NAME, RunId=job_run_id, PredecessorsIncluded=False)["JobRun"]
    state = run["JobRunState"]
    message = {
        "STARTING": "Rollback is starting in AWS Glue…" if operation == "rollback" else "ETL is starting in AWS Glue…",
        "RUNNING": "Rollback is in process: restoring and verifying the prior Iceberg snapshot…" if operation == "rollback" else "ETL is in process: validating, snapshotting, and appending the uploaded data…",
        "SUCCEEDED": "Rollback completed and was verified." if operation == "rollback" else "ETL completed successfully.",
        "FAILED": "ETL failed; no successful outcome was reported.",
        "TIMEOUT": "ETL timed out.",
        "STOPPED": "ETL was stopped.",
    }.get(state, f"ETL status: {state}")
    retention_configured, retention_warning = None, None
    # A newly-created table does not exist until its first Glue run succeeds.
    # Configure its recovery policy immediately afterwards; append jobs are
    # configured before they are allowed to start.
    arguments = run.get("Arguments", {})
    if state == "SUCCEEDED" and operation == "ingestion" and arguments.get("--MODE") == "create":
        try:
            _configure_snapshot_retention(
                arguments["--TABLE_BUCKET_ARN"], arguments["--NAMESPACE"], arguments["--TABLE"]
            )
            retention_configured = True
        except Exception as error:
            retention_configured, retention_warning = False, str(error)
    return {
        "state": state, "message": message, "error": run.get("ErrorMessage"),
        "started": str(run.get("StartedOn")), "completed": str(run.get("CompletedOn")),
        "retention_configured": retention_configured, "retention_warning": retention_warning,
    }


@app.get("/api/qc")
def qc(uri: str):
    allowed_prefix = f"{QC_PREFIX}/web/"
    if not uri.startswith(allowed_prefix):
        raise HTTPException(400, "QC URI is outside the local pilot web-ingestion prefix")
    bucket, key = uri.removeprefix("s3://").split("/", 1)
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey as error:
        raise HTTPException(404, "No QC report was created because the Glue job failed before it began") from error
