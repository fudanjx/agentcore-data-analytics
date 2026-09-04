"""Local deployment and controlled runner for the AH SOC S3 Tables delta pilot."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import time
import uuid
from pathlib import Path

import boto3


REGION = "ap-southeast-1"
ACCOUNT_ID = "964340114883"
SOURCE_BUCKET = "ah-data-analytics"
SOURCE_PREFIX = "temp_s3_update"
TABLE_BUCKET_NAME = "ah-soc-delta-pilot"
TABLE_BUCKET_ARN = f"arn:aws:s3tables:{REGION}:{ACCOUNT_ID}:bucket/{TABLE_BUCKET_NAME}"
NAMESPACE = "pilot"
TABLE = "soc"
JOB_NAME = "ah-soc-delta-pilot"
ROLE_NAME = "ah-soc-delta-pilot-glue-role"
SCRIPT_KEY = f"{SOURCE_PREFIX}/_pilot_assets/glue_job.py"
CONTRACT_KEY = f"{SOURCE_PREFIX}/_pilot_assets/contract.py"
QC_PREFIX = f"s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}/qc"
SOURCES = {
    "may": {
        "path": Path("/Users/jinxin/Documents/Claude/encryption/tmp/basedeck_tillmay/Combined_SOC.parquet.gzip"),
        "key": f"{SOURCE_PREFIX}/soc/SOC_202605_d7ebafa00ad5.parquet.gzip",
        "sha256": "d7ebafa00ad5fe568f5e5a50ae596ae264eff9c4eefff50ac51a7f6fc1df2234",
        "rows": 1_117_856,
    },
    "jun": {
        "path": Path("/Users/jinxin/Documents/Claude/encryption/tmp/basedeck_tilljun/Combined_SOC.parquet.gzip"),
        "key": f"{SOURCE_PREFIX}/soc/SOC_202606_345558fbc3ab.parquet.gzip",
        "sha256": "345558fbc3aba8522f79ff7549048daa70a0b236b4803a5a239ba8e52753f0f6",
        "rows": 1_138_633,
    },
}

s3 = boto3.client("s3", region_name=REGION)
s3tables = boto3.client("s3tables", region_name=REGION)
iam = boto3.client("iam")
glue = boto3.client("glue", region_name=REGION)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_uri(which: str) -> str:
    return f"s3://{SOURCE_BUCKET}/{SOURCES[which]['key']}"


def _ensure_bucket() -> None:
    try:
        existing = s3tables.get_table_bucket(tableBucketARN=TABLE_BUCKET_ARN)
        if existing["arn"] != TABLE_BUCKET_ARN:
            raise RuntimeError(f"Unexpected pilot table-bucket ARN: {existing['arn']}")
    except s3tables.exceptions.NotFoundException:
        s3tables.create_table_bucket(name=TABLE_BUCKET_NAME)
    try:
        s3tables.get_namespace(tableBucketARN=TABLE_BUCKET_ARN, namespace=NAMESPACE)
    except s3tables.exceptions.NotFoundException:
        s3tables.create_namespace(tableBucketARN=TABLE_BUCKET_ARN, namespace=[NAMESPACE])


def _ensure_role() -> str:
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "glue.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        role_arn = iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust), Description="AH SOC S3 Tables delta pilot Glue role")["Role"]["Arn"]
        time.sleep(12)
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "arn:aws:logs:ap-southeast-1:964340114883:*"},
            {"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*", "Condition": {"StringEquals": {"cloudwatch:namespace": "Glue"}}},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": f"arn:aws:s3:::{SOURCE_BUCKET}/{SOURCE_PREFIX}/*"},
            {"Effect": "Allow", "Action": ["s3tables:GetTableBucket", "s3tables:GetNamespace", "s3tables:ListNamespaces", "s3tables:CreateNamespace", "s3tables:GetTable", "s3tables:ListTables", "s3tables:CreateTable", "s3tables:GetTableMetadataLocation", "s3tables:UpdateTableMetadataLocation", "s3tables:GetTableData", "s3tables:PutTableData"], "Resource": [TABLE_BUCKET_ARN, f"{TABLE_BUCKET_ARN}/*"]},
        ],
    }
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="pilot-least-privilege", PolicyDocument=json.dumps(policy))
    return role_arn


def _upload_assets() -> tuple[str, str]:
    script = Path(__file__).with_name("glue_job.py")
    contract = Path(__file__).with_name("contract.py")
    s3.put_object(Bucket=SOURCE_BUCKET, Key=SCRIPT_KEY, Body=script.read_bytes(), ContentType="text/x-python", ServerSideEncryption="AES256")
    s3.put_object(Bucket=SOURCE_BUCKET, Key=CONTRACT_KEY, Body=contract.read_bytes(), ContentType="text/x-python", ServerSideEncryption="AES256")
    return f"s3://{SOURCE_BUCKET}/{SCRIPT_KEY}", f"s3://{SOURCE_BUCKET}/{CONTRACT_KEY}"


def deploy() -> None:
    _ensure_bucket()
    role_arn = _ensure_role()
    script_uri, contract_uri = _upload_assets()
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
        "Role": role_arn, "Command": {"Name": "glueetl", "ScriptLocation": script_uri, "PythonVersion": "3"},
        "GlueVersion": "5.0", "WorkerType": "G.1X", "NumberOfWorkers": 2, "Timeout": 30, "MaxRetries": 0,
        "ExecutionProperty": {"MaxConcurrentRuns": 1},
        "DefaultArguments": {"--job-language": "python", "--datalake-formats": "iceberg", "--enable-metrics": "true", "--enable-continuous-cloudwatch-log": "true", "--extra-py-files": contract_uri, "--conf": conf},
        "Description": "Isolated AH SOC append-only S3 Tables delta pilot",
    }
    try:
        glue.get_job(JobName=JOB_NAME)
        glue.update_job(JobName=JOB_NAME, JobUpdate=definition)
    except glue.exceptions.EntityNotFoundException:
        glue.create_job(Name=JOB_NAME, **definition)
    print(json.dumps({"table_bucket_arn": TABLE_BUCKET_ARN, "job": JOB_NAME, "script": script_uri}, indent=2))


def upload() -> None:
    for label, item in SOURCES.items():
        path = item["path"]
        if not path.exists():
            raise FileNotFoundError(path)
        actual = _sha256(path)
        if actual != item["sha256"]:
            raise RuntimeError(f"{label} SHA-256 mismatch: expected {item['sha256']}, got {actual}")
        try:
            existing = s3.head_object(Bucket=SOURCE_BUCKET, Key=item["key"])
        except s3.exceptions.ClientError as error:
            if error.response["Error"]["Code"] not in {"404", "NoSuchKey", "NotFound"}:
                raise
            existing = None
        if existing:
            if existing.get("Metadata", {}).get("sha256") != actual or existing["ContentLength"] != path.stat().st_size:
                raise RuntimeError(f"Refusing to overwrite immutable source {item['key']}")
            continue
        with path.open("rb") as stream:
            s3.put_object(Bucket=SOURCE_BUCKET, Key=item["key"], Body=stream, ContentType="application/octet-stream", Metadata={"sha256": actual, "delivery_month": "2026-05" if label == "may" else "2026-06"}, ServerSideEncryption="AES256")
        verified = s3.head_object(Bucket=SOURCE_BUCKET, Key=item["key"])
        if verified.get("Metadata", {}).get("sha256") != actual or verified["ContentLength"] != path.stat().st_size:
            raise RuntimeError(f"Post-upload verification failed for {item['key']}")
    print(json.dumps({label: _source_uri(label) for label in SOURCES}, indent=2))


def _run(mode: str, source: str) -> str:
    run_id = str(uuid.uuid4())
    job_run_id = glue.start_job_run(JobName=JOB_NAME, Arguments={"--MODE": mode, "--SOURCE_URI": _source_uri(source), "--TABLE_BUCKET_ARN": TABLE_BUCKET_ARN, "--NAMESPACE": NAMESPACE, "--TABLE": TABLE, "--QC_PREFIX": QC_PREFIX, "--RUN_ID": run_id})["JobRunId"]
    while True:
        run = glue.get_job_run(JobName=JOB_NAME, RunId=job_run_id, PredecessorsIncluded=False)["JobRun"]
        state = run["JobRunState"]
        if state in {"SUCCEEDED", "FAILED", "STOPPED", "TIMEOUT", "ERROR", "EXPIRED"}:
            if state != "SUCCEEDED":
                raise RuntimeError(f"Glue {mode} failed: state={state}, error={run.get('ErrorMessage', '')}")
            print(json.dumps({"job_run_id": job_run_id, "qc_uri": f"{QC_PREFIX}/{run_id}/report.json"}, indent=2))
            return run_id
        time.sleep(10)


def run_bootstrap() -> None:
    _run("bootstrap", "may")


def run_delta() -> None:
    _run("delta", "jun")


def verify() -> None:
    _run("verify", "jun")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("deploy", "upload", "bootstrap", "delta", "verify"))
    command = parser.parse_args().command
    {"deploy": deploy, "upload": upload, "bootstrap": run_bootstrap, "delta": run_delta, "verify": verify}[command]()


if __name__ == "__main__":
    main()
