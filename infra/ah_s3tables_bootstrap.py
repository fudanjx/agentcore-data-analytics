"""
One-shot bootstrap for the AH S3 Tables backend.

Idempotent — safe to re-run.

Creates / ensures:
  1. S3 Tables bucket        : ah-analytics  (arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-analytics)
  2. Glue Data Catalog federation on that bucket (auto-creates s3tablescatalog/ah-analytics child)
  3. Lake Formation admin on the current caller (needed to grant on federated catalog)
  4. Lake Formation grant: DESCRIBE + SELECT on {bucket}.ah_analytics.* to the loader + MCP roles
  5. Namespace              : ah_analytics
  6. Athena workgroup       : ah-s3tables-wg  (result location s3://agentcore-tmp-964340114883/athena-results/)

Namespace creation happens here so the loader Lambda's role only needs table-level perms.
Tables themselves are created by the loader Lambda on first invocation per file.

Usage:
    python infra/ah_s3tables_bootstrap.py
"""

import json
import os
import time

import boto3
import botocore.exceptions

REGION = "ap-southeast-1"
ACCOUNT_ID = "964340114883"

TABLE_BUCKET_NAME = "ah-analytics"
TABLE_BUCKET_ARN = f"arn:aws:s3tables:{REGION}:{ACCOUNT_ID}:bucket/{TABLE_BUCKET_NAME}"
NAMESPACE = "ah_analytics"

ATHENA_WORKGROUP = "ah-s3tables-wg"
ATHENA_RESULTS_BUCKET = f"agentcore-tmp-{ACCOUNT_ID}"
ATHENA_RESULTS_PREFIX = "athena-results/"

FEDERATED_CATALOG = f"s3tablescatalog/{TABLE_BUCKET_NAME}"

# Roles that need read/write on the federated catalog. Loader gets full,
# MCP gets read-only. Both roles are created by their respective deploy scripts.
LOADER_ROLE_NAME = "ah-analytics-s3tables-loader-role"
MCP_ROLE_NAME = "ah-analytics-s3tables-mcp-role"

s3tables = boto3.client("s3tables", region_name=REGION)
glue = boto3.client("glue", region_name=REGION)
lakeformation = boto3.client("lakeformation", region_name=REGION)
athena = boto3.client("athena", region_name=REGION)
iam = boto3.client("iam")
sts = boto3.client("sts", region_name=REGION)


# ---------------------------------------------------------------------------

def ensure_table_bucket() -> str:
    try:
        resp = s3tables.get_table_bucket(tableBucketARN=TABLE_BUCKET_ARN)
        print(f"  Table bucket exists: {resp['arn']}")
        return resp["arn"]
    except s3tables.exceptions.NotFoundException:
        pass

    print(f"  Creating table bucket '{TABLE_BUCKET_NAME}' in {REGION}...")
    resp = s3tables.create_table_bucket(name=TABLE_BUCKET_NAME)
    arn = resp["arn"]
    print(f"  Created: {arn}")
    return arn


def ensure_glue_integration(bucket_arn: str):
    """Register the S3 Tables bucket with Glue Data Catalog federation.

    Once enabled at the account level, AWS auto-creates the federated
    `s3tablescatalog` parent catalog and child catalogs per bucket.

    This uses the AWS-recommended one-time account-level integration:
    Glue catalog `s3tablescatalog` created via glue.create_catalog with
    FederatedCatalog type = "aws:s3tables". If it already exists, skip.
    """
    parent_catalog_id = "s3tablescatalog"
    try:
        glue.get_catalog(CatalogId=parent_catalog_id)
        print(f"  Federated parent catalog '{parent_catalog_id}' already exists")
        return
    except glue.exceptions.EntityNotFoundException:
        pass
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] not in ("AccessDeniedException", "EntityNotFoundException"):
            raise
        # Fall through — treat access-denied on get_catalog as "not enabled yet"

    print(f"  Creating federated parent catalog '{parent_catalog_id}' → S3 Tables service...")
    try:
        glue.create_catalog(
            Name=parent_catalog_id,
            CatalogInput={
                "FederatedCatalog": {
                    "Identifier": f"arn:aws:s3tables:{REGION}:{ACCOUNT_ID}:bucket/*",
                    "ConnectionName": "aws:s3tables",
                },
                "CreateDatabaseDefaultPermissions": [],
                "CreateTableDefaultPermissions": [],
            },
        )
        print("  Federated catalog created")
    except glue.exceptions.AlreadyExistsException:
        print("  Federated catalog already exists (race)")


def ensure_namespace(bucket_arn: str):
    try:
        s3tables.get_namespace(tableBucketARN=bucket_arn, namespace=NAMESPACE)
        print(f"  Namespace '{NAMESPACE}' exists")
        return
    except s3tables.exceptions.NotFoundException:
        pass

    print(f"  Creating namespace '{NAMESPACE}'...")
    s3tables.create_namespace(tableBucketARN=bucket_arn, namespace=[NAMESPACE])
    print("  Namespace created")


def ensure_lf_admin():
    """Ensure the current caller is a Lake Formation data-lake administrator.

    Federated S3 Tables catalogs are managed by Lake Formation; the caller
    must be an LF admin to grant permissions to loader/MCP roles.
    """
    caller_arn = sts.get_caller_identity()["Arn"]
    settings = lakeformation.get_data_lake_settings()["DataLakeSettings"]
    admins = settings.get("DataLakeAdmins", [])
    admin_arns = {a["DataLakePrincipalIdentifier"] for a in admins}

    if caller_arn in admin_arns:
        print(f"  Caller {caller_arn} is already an LF admin")
        return

    print(f"  Adding {caller_arn} as Lake Formation data-lake admin...")
    settings["DataLakeAdmins"] = admins + [{"DataLakePrincipalIdentifier": caller_arn}]
    lakeformation.put_data_lake_settings(DataLakeSettings=settings)
    print("  LF admin added")


def _role_arn_or_none(role_name: str) -> str | None:
    try:
        return iam.get_role(RoleName=role_name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        return None


def ensure_lf_grants():
    """Grant SELECT on the federated catalog's namespace to the MCP role.

    The loader writes directly via the S3 Tables API (PyIceberg + SigV4),
    which is authorized by IAM only — no Lake Formation grants needed.
    The MCP role reads through Athena over the Glue-federated catalog,
    so it needs DESCRIBE + SELECT via Lake Formation. Grants are idempotent.
    """
    catalog_id = f"{ACCOUNT_ID}:{FEDERATED_CATALOG}"

    for role_name, db_perms, table_perms in [
        (MCP_ROLE_NAME, ["DESCRIBE"], ["DESCRIBE", "SELECT"]),
    ]:
        role_arn = _role_arn_or_none(role_name)
        if not role_arn:
            print(f"  Role '{role_name}' not yet created — skipping LF grant (re-run bootstrap after deploy)")
            continue

        # Grant on database (namespace) — LF database permissions do NOT include SELECT
        try:
            lakeformation.grant_permissions(
                Principal={"DataLakePrincipalIdentifier": role_arn},
                Resource={
                    "Database": {
                        "CatalogId": catalog_id,
                        "Name": NAMESPACE,
                    }
                },
                Permissions=db_perms,
            )
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] != "AlreadyExistsException":
                raise
        # Grant on all tables in the namespace (wildcard) — SELECT + DESCRIBE
        try:
            lakeformation.grant_permissions(
                Principal={"DataLakePrincipalIdentifier": role_arn},
                Resource={
                    "Table": {
                        "CatalogId": catalog_id,
                        "DatabaseName": NAMESPACE,
                        "TableWildcard": {},
                    }
                },
                Permissions=table_perms,
            )
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] != "AlreadyExistsException":
                raise
        print(f"  LF grant db={db_perms} tables={table_perms} on {NAMESPACE}.* → {role_name}")


def ensure_athena_workgroup():
    try:
        athena.get_work_group(WorkGroup=ATHENA_WORKGROUP)
        print(f"  Athena workgroup '{ATHENA_WORKGROUP}' exists")
        return
    except athena.exceptions.InvalidRequestException:
        pass

    print(f"  Creating Athena workgroup '{ATHENA_WORKGROUP}'...")
    athena.create_work_group(
        Name=ATHENA_WORKGROUP,
        Description="Workgroup for ah-analytics S3 Tables (Iceberg) queries via MCP",
        Configuration={
            "ResultConfiguration": {
                "OutputLocation": f"s3://{ATHENA_RESULTS_BUCKET}/{ATHENA_RESULTS_PREFIX}",
            },
            "EnforceWorkGroupConfiguration": True,
            "PublishCloudWatchMetricsEnabled": True,
            "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"},
        },
    )
    print("  Workgroup created")


# ---------------------------------------------------------------------------

def main():
    print(f"Bootstrapping S3 Tables backend for AH analytics")
    print(f"  Account: {ACCOUNT_ID}  Region: {REGION}\n")

    print("1. Ensuring S3 Tables bucket...")
    bucket_arn = ensure_table_bucket()

    print("\n2. Ensuring Glue federated catalog...")
    ensure_glue_integration(bucket_arn)

    print("\n3. Ensuring namespace...")
    ensure_namespace(bucket_arn)

    print("\n4. Ensuring Lake Formation admin (current caller)...")
    ensure_lf_admin()

    print("\n5. Granting LF permissions to loader + MCP roles (if they exist)...")
    ensure_lf_grants()

    print("\n6. Ensuring Athena workgroup...")
    ensure_athena_workgroup()

    print("\nDone.")
    print(f"\n  Table bucket ARN : {bucket_arn}")
    print(f"  Namespace        : {NAMESPACE}")
    print(f"  Athena catalog   : {FEDERATED_CATALOG}")
    print(f"  Athena workgroup : {ATHENA_WORKGROUP}")
    print(f"\nNext:")
    print(f"  1. Deploy loader Lambda:  python infra/deploy_s3tables_loader.py")
    print(f"  2. Deploy MCP Lambda:     python mcp_lambda_s3tables/deploy.py")
    print(f"  3. Re-run this bootstrap to grant LF permissions to the new roles")


if __name__ == "__main__":
    main()
