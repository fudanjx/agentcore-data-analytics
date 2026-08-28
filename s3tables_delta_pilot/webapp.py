"""Local-only web UI for the isolated S3 Tables pilot namespace."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .contract import TARGET_COLUMNS, TIMESTAMP_TARGET_COLUMNS
from .ingest_contract import compare_schema, normalise_names, schema_from_arrow
from .pilot import NAMESPACE, QC_PREFIX, REGION, ROLE_NAME, SOURCE_BUCKET, SOURCE_PREFIX, TABLE_BUCKET_ARN

WEB_JOB_NAME = "ah-soc-delta-pilot-web-ingest"
WEB_SCRIPT_KEY = f"{SOURCE_PREFIX}/_pilot_assets/generic_glue_job.py"
WEB_CONTRACT_PREFIX = f"{SOURCE_PREFIX}/web_ingest/table_contracts"
WEB_UPLOAD_PREFIX = f"{SOURCE_PREFIX}/web_ingest/uploads"
ROOT = Path(__file__).parent
s3 = boto3.client("s3", region_name=REGION)
s3tables = boto3.client("s3tables", region_name=REGION)
glue = boto3.client("glue", region_name=REGION)
iam = boto3.client("iam")
app = FastAPI(title="AH S3 Tables Pilot", docs_url=None, redoc_url=None)
SUPPORTED_UPLOAD_SUFFIXES = (".parquet", ".parquet.gzip", ".xlsx", ".xls", ".csv", ".tsv")


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
    allow_unsafe_casts: bool = False

    @field_validator("table", mode="before")
    @classmethod
    def canonicalise_table(cls, value: str) -> str:
        return _canonical_table_name(value)


class DeleteTableRequest(BaseModel):
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")


def _soc_schema() -> list[dict[str, str]]:
    return [{"name": column, "type": "TIMESTAMP" if column in TIMESTAMP_TARGET_COLUMNS else "BIGINT" if column == "cnt" else "STRING"} for column in TARGET_COLUMNS]


def _contract_key(table_bucket_arn: str, namespace: str, table: str) -> str:
    scope = hashlib.sha256(f"{table_bucket_arn}|{namespace}".encode()).hexdigest()[:16]
    return f"{WEB_CONTRACT_PREFIX}/{scope}/{table}.json"


def _load_contract(table_bucket_arn: str, namespace: str, table: str) -> list[dict[str, str]]:
    if table == "soc":
        return _soc_schema()
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
    return json.loads(response["Body"].read())["schema"]


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
    return pa.Table.from_pandas(frame, preserve_index=False)


def _read_schemas(files: list[UploadFile]) -> list:
    schemas = []
    for upload in files:
        if not upload.filename or not upload.filename.lower().endswith(SUPPORTED_UPLOAD_SUFFIXES):
            raise HTTPException(400, f"Supported files are Parquet, XLSX, XLS, CSV, and TSV: {upload.filename or '<unnamed>'}")
        with tempfile.NamedTemporaryFile(suffix=_temporary_suffix(upload.filename), delete=False) as temp:
            path = Path(temp.name)
            try:
                shutil.copyfileobj(upload.file, temp)
                temp.flush()
                schemas.append(_read_upload_table(path, upload.filename).schema)
            except Exception as error:
                raise HTTPException(400, f"Cannot read {upload.filename} as Parquet: {error}") from error
            finally:
                path.unlink(missing_ok=True)
                upload.file.seek(0)
    return schemas


def _preflight(mode: str, table_bucket_arn: str, namespace: str, table: str, files: list[UploadFile]) -> dict:
    schemas = _read_schemas(files)
    if not schemas:
        raise HTTPException(400, "Choose at least one Parquet file")
    if mode == "create":
        target, creation_warnings = schema_from_arrow(schemas[0])
    else:
        target, creation_warnings = _load_contract(table_bucket_arn, namespace, table), []
    comparisons = [compare_schema(schema, target) for schema in schemas]
    return {
        "mode": mode, "table_bucket_arn": table_bucket_arn, "namespace": namespace, "table": table, "target_schema": target, "creation_warnings": creation_warnings,
        "files": [{"filename": upload.filename, **comparison} for upload, comparison in zip(files, comparisons)],
        "requires_confirmation": bool(creation_warnings or any(item["type_conversions"] or item["warnings"] for item in comparisons)),
        "sensitive_column_scan": "Placeholder only: anonymisation is not enabled in this pilot.",
    }


def _make_glue_compatible_parquet(source: Path, filename: str) -> tuple[Path, bool]:
    """Stage every supported file as Spark-safe Parquet for the Glue job."""
    table = _read_upload_table(source, filename)
    schema = table.schema
    names = normalise_names([field.name for field in schema])
    has_nanosecond_timestamps = any(
        pa.types.is_timestamp(field.type) and field.type.unit == "ns" for field in schema
    )
    has_unsafe_names = names != list(schema.names)
    is_parquet = filename.lower().endswith((".parquet", ".parquet.gzip"))
    if is_parquet and not has_nanosecond_timestamps and not has_unsafe_names:
        return source, False

    fields = [
        pa.field(
            name,
            pa.timestamp("us", tz=field.type.tz)
            if pa.types.is_timestamp(field.type) and field.type.unit == "ns"
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
    return converted, True


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
    tables = [_table_summary(table_bucket_arn, namespace, item) for item in result.get("tables", [])]
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
    if not files:
        raise HTTPException(400, "Choose at least one supported file")
    preview = _preflight(payload.mode, payload.table_bucket_arn, payload.namespace, payload.table, files)
    if preview["requires_confirmation"] and not payload.allow_unsafe_casts:
        raise HTTPException(409, detail={"message": "Confirmation required for schema conversions", "preflight": preview})
    request_prefix = f"{WEB_UPLOAD_PREFIX}/{payload.request_id}"
    objects = []
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
                staged, transformed = _make_glue_compatible_parquet(path, original_filename)
                try:
                    with staged.open("rb") as stream:
                        s3.put_object(Bucket=SOURCE_BUCKET, Key=key, Body=stream, ContentType="application/octet-stream", Metadata={"sha256": digest.hexdigest(), "original_filename": original_filename, "spark_compatible_staging": str(transformed).lower()}, ServerSideEncryption="AES256")
                finally:
                    if staged != path:
                        staged.unlink(missing_ok=True)
                objects.append(f"s3://{SOURCE_BUCKET}/{key}")
            finally:
                path.unlink(missing_ok=True)
    if payload.mode == "create":
        s3.put_object(Bucket=SOURCE_BUCKET, Key=_contract_key(payload.table_bucket_arn, payload.namespace, payload.table), Body=json.dumps({"schema": preview["target_schema"]}, indent=2).encode(), ContentType="application/json", ServerSideEncryption="AES256")
    manifest_key = f"{request_prefix}/manifest.json"
    s3.put_object(Bucket=SOURCE_BUCKET, Key=manifest_key, Body=json.dumps({"files": objects, "schema": preview["target_schema"], "allow_unsafe_casts": payload.allow_unsafe_casts}).encode(), ContentType="application/json", ServerSideEncryption="AES256")
    _ensure_web_job()
    run_id = str(uuid.uuid4())
    response = glue.start_job_run(JobName=WEB_JOB_NAME, Arguments={"--MODE": payload.mode, "--MANIFEST_URI": f"s3://{SOURCE_BUCKET}/{manifest_key}", "--TABLE_BUCKET_ARN": payload.table_bucket_arn, "--NAMESPACE": payload.namespace, "--TABLE": payload.table, "--QC_PREFIX": QC_PREFIX, "--RUN_ID": run_id})
    return {"job_run_id": response["JobRunId"], "qc_uri": f"{QC_PREFIX}/web/{run_id}/report.json", "request_id": payload.request_id}


@app.delete("/api/tables")
def delete_table(payload: DeleteTableRequest, user: PilotUser = Depends(_current_user)):
    _require_scope(user, payload.table_bucket_arn, payload.namespace)
    if not user.is_admin:
        raise HTTPException(403, "Only administrators may delete S3 Tables")
    s3tables.delete_table(
        tableBucketARN=payload.table_bucket_arn,
        namespace=payload.namespace,
        name=payload.table,
    )
    return {"deleted": payload.table, "table_bucket_arn": payload.table_bucket_arn, "namespace": payload.namespace}


@app.get("/api/ingestions/{job_run_id}")
def ingestion_status(job_run_id: str):
    run = glue.get_job_run(JobName=WEB_JOB_NAME, RunId=job_run_id, PredecessorsIncluded=False)["JobRun"]
    state = run["JobRunState"]
    message = {
        "STARTING": "ETL is starting in AWS Glue…",
        "RUNNING": "ETL is in process: validating and appending the uploaded data…",
        "SUCCEEDED": "ETL completed successfully.",
        "FAILED": "ETL failed; no successful outcome was reported.",
        "TIMEOUT": "ETL timed out.",
        "STOPPED": "ETL was stopped.",
    }.get(state, f"ETL status: {state}")
    return {"state": state, "message": message, "error": run.get("ErrorMessage"), "started": str(run.get("StartedOn")), "completed": str(run.get("CompletedOn"))}


@app.get("/api/qc")
def qc(uri: str):
    allowed_prefix = f"{QC_PREFIX}/web/"
    if not uri.startswith(allowed_prefix):
        raise HTTPException(400, "QC URI is outside the local pilot web-ingestion prefix")
    bucket, key = uri.removeprefix("s3://").split("/", 1)
    return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
