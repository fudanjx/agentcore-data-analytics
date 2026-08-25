"""
Deploy ah-analytics-s3tables MCP Lambda + AgentCore Gateway, then wire it into
the harness_e52fs harness alongside the existing ah-analytics-db gateway.

Creates:
  1. IAM role for Lambda   : ah-analytics-s3tables-mcp-role
  2. Lambda function       : ah-analytics-s3tables-mcp  (zip; boto3-only)
  3. Gateway invoke perm on Lambda
  4. IAM role for Gateway  : ah-analytics-s3tables-gateway-role
  5. AgentCore Gateway     : ah-analytics-s3tables  (MCP, AWS_IAM auth)
  6. Gateway target        : ah-s3tables-tools (Lambda + inline tool schema)
  7. Adds gateway to harness harness_e52fs

Assumes ah_s3tables_bootstrap.py has already been run.
After running this, re-run ah_s3tables_bootstrap.py so the LF grant
picks up the new role ARN.

Usage:
    python mcp_lambda_s3tables/deploy.py
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile

import boto3

REGION = "ap-southeast-1"
ACCOUNT_ID = "964340114883"

LAMBDA_NAME = "ah-analytics-s3tables-mcp"
LAMBDA_ROLE_NAME = "ah-analytics-s3tables-mcp-role"
GATEWAY_ROLE_NAME = "ah-analytics-s3tables-gateway-role"
GATEWAY_NAME = "ah-analytics-s3tables"
TARGET_NAME = "ah-s3tables-tools"
HARNESS_ID = "harness_e52fs-Du2DM0RxvF"
HARNESS_GATEWAY_POLICY_ARN = "arn:aws:iam::964340114883:policy/service-role/AmazonBedrockAgentCoreHarnessGatewayPolicy_bd7bg"

TABLE_BUCKET_ARN = f"arn:aws:s3tables:{REGION}:{ACCOUNT_ID}:bucket/ah-analytics"
ATHENA_WORKGROUP = "ah-s3tables-wg"
ATHENA_CATALOG = "s3tablescatalog/ah-analytics"
ATHENA_DATABASE = "ah"
NUH_ATHENA_CATALOG = "s3tablescatalog/nuh-analytics"
NUH_ATHENA_DATABASE = "nuh"

ATHENA_RESULTS_BUCKET = f"agentcore-tmp-{ACCOUNT_ID}"
ATHENA_RESULTS_PREFIX = "athena-results/"

iam = boto3.client("iam")
lambda_client = boto3.client("lambda", region_name=REGION)
agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)


SOURCE_PROPERTY = {
    "type": "string",
    "description": "Which S3 Tables source to use: 'ah' (default) or 'nuh'.",
}

TOOL_SCHEMA = [
    {
        "name": "execute_sql",
        "description": (
            "Run a read-only SELECT/WITH query against the selected AH or NUH S3 "
            "Tables (Iceberg) backend via Athena and return a small JSON result. "
            "For results that may exceed 1,000 rows, use execute_sql_export instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A valid Athena SELECT/WITH statement"},
                "source": SOURCE_PROPERTY,
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_tables",
        "description": "List all tables in the selected AH or NUH S3 Tables namespace with column names and types.",
        "inputSchema": {"type": "object", "properties": {"source": SOURCE_PROPERTY}},
    },
    {
        "name": "describe_table",
        "description": "Get column details and 3 sample rows for a table in the selected AH or NUH S3 Tables namespace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string", "description": "Name of the table to describe"},
                "source": SOURCE_PROPERTY,
            },
            "required": ["table_name"],
        },
    },
    {
        "name": "execute_sql_export",
        "description": (
            "Run a read-only SELECT/WITH query against AH or NUH S3 Tables and return "
            "only Athena result metadata, including an S3 CSV URI. Use for large or "
            "multi-month queries; download and process the CSV in Code Interpreter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A valid Athena SELECT/WITH statement"},
                "source": SOURCE_PROPERTY,
                "export": {
                    "type": "boolean",
                    "description": "Must be true. Identifies this call as the metadata-only export operation.",
                },
            },
            "required": ["query", "export"],
        },
    },
]


# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

def _upsert_role(name: str, trust: dict, inline: dict, description: str) -> str:
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        print(f"  Role exists: {arn}")
    except iam.exceptions.NoSuchEntityException:
        print(f"  Creating role {name}...")
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=description,
        )["Role"]["Arn"]
        iam.attach_role_policy(
            RoleName=name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
        print("  Waiting for role propagation...")
        time.sleep(20)
    iam.put_role_policy(RoleName=name, PolicyName=f"{name}-inline", PolicyDocument=json.dumps(inline))
    return arn


def ensure_lambda_role() -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    }
    inline = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow",
             "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": "arn:aws:logs:*:*:*"},
            # Athena — start/get queries + read metadata
            {"Effect": "Allow",
             "Action": [
                 "athena:StartQueryExecution",
                 "athena:StopQueryExecution",
                 "athena:GetQueryExecution",
                 "athena:GetQueryResults",
                 "athena:GetWorkGroup",
                 "athena:GetDataCatalog",
             ],
             "Resource": [
                 f"arn:aws:athena:{REGION}:{ACCOUNT_ID}:workgroup/{ATHENA_WORKGROUP}",
                 f"arn:aws:athena:{REGION}:{ACCOUNT_ID}:datacatalog/*",
             ]},
            # Glue metadata for list_tables / describe_table
            {"Effect": "Allow",
             "Action": [
                 "glue:GetDatabase", "glue:GetDatabases",
                 "glue:GetTable", "glue:GetTables",
                 "glue:GetPartitions",
                 "glue:GetCatalog", "glue:GetCatalogs",
             ],
             "Resource": "*"},
            # S3 Tables — read table data via Athena's engine
            {"Effect": "Allow",
             "Action": [
                 "s3tables:GetTableBucket",
                 "s3tables:GetNamespace",
                 "s3tables:ListNamespaces",
                 "s3tables:GetTable",
                 "s3tables:ListTables",
                 "s3tables:GetTableMetadataLocation",
                 "s3tables:GetTableData",
             ],
             "Resource": [TABLE_BUCKET_ARN, f"{TABLE_BUCKET_ARN}/*"]},
            # Athena result location — read/write query outputs
            {"Effect": "Allow",
             "Action": [
                 "s3:GetBucketLocation",
                 "s3:GetObject",
                 "s3:ListBucket",
                 "s3:ListBucketMultipartUploads",
                 "s3:ListMultipartUploadParts",
                 "s3:PutObject",
                 "s3:AbortMultipartUpload",
             ],
             "Resource": [
                 f"arn:aws:s3:::{ATHENA_RESULTS_BUCKET}",
                 f"arn:aws:s3:::{ATHENA_RESULTS_BUCKET}/{ATHENA_RESULTS_PREFIX}*",
             ]},
            # Lake Formation — required for Athena to read federated S3 Tables catalog
            {"Effect": "Allow",
             "Action": ["lakeformation:GetDataAccess"],
             "Resource": "*"},
        ],
    }
    return _upsert_role(LAMBDA_ROLE_NAME, trust, inline, "MCP Lambda role for ah-analytics S3 Tables (Athena)")


def ensure_gateway_role(lambda_arn: str) -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    }
    inline = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": lambda_arn}],
    }
    return _upsert_role(GATEWAY_ROLE_NAME, trust, inline, "AgentCore Gateway role for ah-analytics-s3tables")


# ---------------------------------------------------------------------------
# Lambda packaging — no VPC (Athena is a public AWS API), boto3-only deps
# ---------------------------------------------------------------------------

def build_zip() -> bytes:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "-r", os.path.join(script_dir, "requirements.txt"),
            "--target", tmp, "--quiet",
            "--platform", "manylinux2014_x86_64",
            "--only-binary=:all:", "--implementation", "cp", "--python-version", "3.12",
        ])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(os.path.join(script_dir, "handler.py"), "handler.py")
            for root, _, files in os.walk(tmp):
                for fname in files:
                    full_path = os.path.join(root, fname)
                    zf.write(full_path, os.path.relpath(full_path, tmp))
        return buf.getvalue()


def deploy_lambda(role_arn: str) -> str:
    print("  Building zip...")
    zip_bytes = build_zip()
    print(f"  Zip size: {len(zip_bytes) / 1024 / 1024:.1f} MB")

    env = {"Variables": {
        "ATHENA_WORKGROUP": ATHENA_WORKGROUP,
        "ATHENA_CATALOG": ATHENA_CATALOG,
        "ATHENA_DATABASE": ATHENA_DATABASE,
        "NUH_ATHENA_WORKGROUP": ATHENA_WORKGROUP,
        "NUH_ATHENA_CATALOG": NUH_ATHENA_CATALOG,
        "NUH_ATHENA_DATABASE": NUH_ATHENA_DATABASE,
    }}

    try:
        fn = lambda_client.get_function(FunctionName=LAMBDA_NAME)
        fn_arn = fn["Configuration"]["FunctionArn"]
        print(f"  Updating Lambda {LAMBDA_NAME}...")
        lambda_client.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=zip_bytes)
        lambda_client.get_waiter("function_updated_v2").wait(FunctionName=LAMBDA_NAME)
        lambda_client.update_function_configuration(
            FunctionName=LAMBDA_NAME, Environment=env, Timeout=90, MemorySize=512,
        )
    except lambda_client.exceptions.ResourceNotFoundException:
        print(f"  Creating Lambda {LAMBDA_NAME}...")
        fn_arn = lambda_client.create_function(
            FunctionName=LAMBDA_NAME,
            Runtime="python3.12",
            Role=role_arn,
            Handler="handler.lambda_handler",
            Code={"ZipFile": zip_bytes},
            Environment=env,
            Timeout=90,
            MemorySize=512,
            Description="MCP server for AgentCore Gateway — ah-analytics S3 Tables (Athena)",
        )["FunctionArn"]

    lambda_client.get_waiter("function_active_v2").wait(FunctionName=LAMBDA_NAME)
    print(f"  Lambda ready: {fn_arn}")
    return fn_arn


def grant_gateway_invoke():
    try:
        lambda_client.add_permission(
            FunctionName=LAMBDA_NAME,
            StatementId="agentcore-gateway-invoke",
            Action="lambda:InvokeFunction",
            Principal="bedrock-agentcore.amazonaws.com",
        )
        print("  Invoke permission granted to bedrock-agentcore")
    except lambda_client.exceptions.ResourceConflictException:
        print("  Invoke permission already exists")


# ---------------------------------------------------------------------------
# AgentCore Gateway
# ---------------------------------------------------------------------------

def ensure_gateway(gateway_role_arn: str) -> str:
    paginator = agentcore.get_paginator("list_gateways")
    for page in paginator.paginate():
        for gw in page.get("items", []):
            if gw["name"] == GATEWAY_NAME:
                gw_id = gw["gatewayId"]
                print(f"  Gateway exists: {gw_id}")
                return gw_id

    print(f"  Creating gateway {GATEWAY_NAME}...")
    gw_id = agentcore.create_gateway(
        name=GATEWAY_NAME,
        description="MCP gateway for ah-analytics S3 Tables (Iceberg) — Athena-backed",
        roleArn=gateway_role_arn,
        protocolType="MCP",
        authorizerType="AWS_IAM",
    )["gatewayId"]
    print(f"  Gateway created: {gw_id}")

    print("  Waiting for gateway to be READY...")
    for _ in range(30):
        status = agentcore.get_gateway(gatewayIdentifier=gw_id).get("status", "")
        if status == "READY":
            print("  Gateway is READY.")
            break
        if "FAILED" in status:
            raise RuntimeError(f"Gateway failed: {status}")
        time.sleep(10)
    return gw_id


def ensure_gateway_target(gateway_id: str, lambda_arn: str):
    paginator = agentcore.get_paginator("list_gateway_targets")
    for page in paginator.paginate(gatewayIdentifier=gateway_id):
        for tgt in page.get("items", []):
            if tgt["name"] == TARGET_NAME:
                existing = agentcore.get_gateway_target(
                    gatewayIdentifier=gateway_id,
                    targetId=tgt["targetId"],
                )
                agentcore.update_gateway_target(
                    gatewayIdentifier=gateway_id,
                    targetId=tgt["targetId"],
                    name=TARGET_NAME,
                    description="Lambda: ah-analytics-s3tables-mcp — 4 read-only S3 Tables tools for AH and NUH",
                    credentialProviderConfigurations=existing.get("credentialProviderConfigurations", []),
                    targetConfiguration={
                        "mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": {"inlinePayload": TOOL_SCHEMA}}}
                    },
                )
                print(f"  Gateway target updated: {tgt['targetId']}")
                return

    print(f"  Creating gateway target {TARGET_NAME}...")
    response = agentcore.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=TARGET_NAME,
        description="Lambda: ah-analytics-s3tables-mcp — 4 read-only S3 Tables tools for AH and NUH",
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        targetConfiguration={
            "mcp": {
                "lambda": {
                    "lambdaArn": lambda_arn,
                    "toolSchema": {"inlinePayload": TOOL_SCHEMA},
                }
            }
        },
    )
    print(f"  Gateway target created: {response['targetId']}")


def add_gateway_to_harness_policy(gateway_arn: str):
    """Add the gateway ARN to the harness's IAM policy for `bedrock-agentcore:InvokeGateway`.

    Without this, the harness gets 403 Forbidden when calling the new gateway.
    Bedrock does NOT auto-update this policy when a gateway is added to a harness's tools.
    """
    doc = iam.get_policy_version(
        PolicyArn=HARNESS_GATEWAY_POLICY_ARN,
        VersionId=iam.get_policy(PolicyArn=HARNESS_GATEWAY_POLICY_ARN)["Policy"]["DefaultVersionId"],
    )["PolicyVersion"]["Document"]

    for stmt in doc["Statement"]:
        if "bedrock-agentcore:InvokeGateway" in stmt.get("Action", []):
            resources = stmt.get("Resource", [])
            if isinstance(resources, str):
                resources = [resources]
            if gateway_arn in resources:
                print("  Gateway ARN already in harness policy")
                return
            resources.append(gateway_arn)
            stmt["Resource"] = resources
            break

    # IAM policies allow at most 5 versions; drop the oldest non-default if needed
    versions = iam.list_policy_versions(PolicyArn=HARNESS_GATEWAY_POLICY_ARN)["Versions"]
    if len(versions) >= 5:
        oldest = sorted(
            (v for v in versions if not v["IsDefaultVersion"]),
            key=lambda v: v["CreateDate"],
        )[0]
        iam.delete_policy_version(PolicyArn=HARNESS_GATEWAY_POLICY_ARN, VersionId=oldest["VersionId"])

    iam.create_policy_version(
        PolicyArn=HARNESS_GATEWAY_POLICY_ARN,
        PolicyDocument=json.dumps(doc),
        SetAsDefault=True,
    )
    print(f"  Harness policy updated with new default version")


def add_gateway_to_harness(gateway_id: str):
    harness = agentcore.get_harness(harnessId=HARNESS_ID)["harness"]
    existing_tools = harness.get("tools", [])
    gateway_arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:gateway/{gateway_id}"

    for tool in existing_tools:
        cfg = tool.get("config", {}).get("agentCoreGateway", {})
        if cfg.get("gatewayArn") == gateway_arn:
            print("  Gateway already in harness tools — skipping")
            return

    new_tool = {
        "type": "agentcore_gateway",
        "name": gateway_id,
        "config": {
            "agentCoreGateway": {
                "gatewayArn": gateway_arn,
                "outboundAuth": {"awsIam": {}},
            }
        },
    }
    agentcore.update_harness(
        harnessId=HARNESS_ID,
        tools=existing_tools + [new_tool],
    )
    print(f"  Harness updated — now has {len(existing_tools) + 1} gateway tool(s)")


# ---------------------------------------------------------------------------

def main():
    print(f"Account: {ACCOUNT_ID}, Region: {REGION}\n")

    print("1. Ensuring Lambda IAM role...")
    lambda_role_arn = ensure_lambda_role()

    print("\n2. Deploying Lambda function...")
    lambda_arn = deploy_lambda(lambda_role_arn)

    print("\n3. Granting AgentCore Gateway invoke permission...")
    grant_gateway_invoke()

    print("\n4. Ensuring Gateway IAM role...")
    gateway_role_arn = ensure_gateway_role(lambda_arn)
    time.sleep(10)

    print("\n5. Ensuring AgentCore Gateway...")
    gateway_id = ensure_gateway(gateway_role_arn)

    print("\n6. Ensuring Gateway Target...")
    ensure_gateway_target(gateway_id, lambda_arn)

    print("\n7. Adding gateway to harness...")
    add_gateway_to_harness(gateway_id)

    print("\n8. Adding gateway ARN to harness IAM policy (InvokeGateway)...")
    add_gateway_to_harness_policy(
        f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:gateway/{gateway_id}"
    )

    print("\nDone.")
    print(f"\n  Lambda ARN : {lambda_arn}")
    print(f"  Gateway ID : {gateway_id}")
    print(f"\nRemember: re-run 'python infra/ah_s3tables_bootstrap.py' so the")
    print(f"Lake Formation grant picks up the new role '{LAMBDA_ROLE_NAME}'.")


if __name__ == "__main__":
    main()
