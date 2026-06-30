"""
Deploy nuh-analytics MCP Lambda + AgentCore Gateway.

Creates:
  1. IAM role for Lambda (nuh-analytics-mcp-role)
  2. Lambda function in VPC (nuh-analytics-mcp) — zip packaged
  3. Grant AgentCore Gateway service lambda:InvokeFunction on Lambda
  4. IAM role for Gateway (nuh-analytics-gateway-role)
  5. AgentCore Gateway (nuh-analytics-db, MCP protocol, AWS_IAM auth)
  6. Gateway Target (rds-tools) — Lambda type with inline tool schema

Usage:
    python mcp_lambda/deploy.py
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
LAMBDA_NAME = "nuh-analytics-mcp"
LAMBDA_ROLE_NAME = "nuh-analytics-mcp-role"
GATEWAY_ROLE_NAME = "nuh-analytics-gateway-role"
GATEWAY_NAME = "nuh-analytics-db"
TARGET_NAME = "rds-tools"

SECRET_ARN = "arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J"
DB_NAME = "nuh-analytics"
VPC_SUBNETS = ["subnet-061205c705e0f41d4", "subnet-0466b6e1fbb8a49f3"]
VPC_SG = "sg-07258677b7e691e48"

iam = boto3.client("iam")
lambda_client = boto3.client("lambda", region_name=REGION)
agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)

TOOL_SCHEMA = [
    {
        "name": "execute_sql",
        "description": "Run a read-only SELECT query against the nuh-analytics database and return rows as a JSON array",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "A valid SQL SELECT statement"}},
            "required": ["query"],
        },
    },
    {
        "name": "list_tables",
        "description": "List all tables in the nuh-analytics database with their column names and data types",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "describe_table",
        "description": "Get column details and sample rows for a specific table in nuh-analytics",
        "inputSchema": {
            "type": "object",
            "properties": {"table_name": {"type": "string", "description": "Name of the table to describe"}},
            "required": ["table_name"],
        },
    },
]


def get_account_id() -> str:
    return sts.get_caller_identity()["Account"]


# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

def ensure_lambda_role() -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    }
    inline = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": SECRET_ARN},
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "arn:aws:logs:*:*:*"},
            {"Effect": "Allow", "Action": [
                "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces",
                "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses",
                "ec2:UnassignPrivateIpAddresses",
            ], "Resource": "*"},
        ],
    }
    return _upsert_role(LAMBDA_ROLE_NAME, trust, inline, "nuh-analytics MCP Lambda role")


def ensure_gateway_role(lambda_arn: str) -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    }
    inline = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": lambda_arn},
        ],
    }
    return _upsert_role(GATEWAY_ROLE_NAME, trust, inline, "nuh-analytics AgentCore Gateway role")


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
        iam.attach_role_policy(RoleName=name, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        print("  Waiting for role propagation...")
        time.sleep(15)

    iam.put_role_policy(RoleName=name, PolicyName=f"{name}-inline", PolicyDocument=json.dumps(inline))
    return arn


# ---------------------------------------------------------------------------
# Lambda packaging
# ---------------------------------------------------------------------------

def build_zip() -> bytes:
    """Bundle handler.py + psycopg2-binary into a zip."""
    script_dir = os.path.dirname(os.path.abspath(__file__))

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install",
             "-r", os.path.join(script_dir, "requirements.txt"),
             "--target", tmp, "--quiet",
             "--platform", "manylinux2014_x86_64",
             "--only-binary=:all:", "--implementation", "cp", "--python-version", "3.12"],
        )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(os.path.join(script_dir, "handler.py"), "handler.py")
            for root, _, files in os.walk(tmp):
                for fname in files:
                    full_path = os.path.join(root, fname)
                    arcname = os.path.relpath(full_path, tmp)
                    zf.write(full_path, arcname)

        return buf.getvalue()


# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------

def deploy_lambda(role_arn: str) -> str:
    print("  Building zip (installing psycopg2-binary for linux/x86_64)...")
    zip_bytes = build_zip()
    print(f"  Zip size: {len(zip_bytes) / 1024 / 1024:.1f} MB")

    env = {"Variables": {"SECRET_ARN": SECRET_ARN, "DB_NAME": DB_NAME}}
    vpc = {"SubnetIds": VPC_SUBNETS, "SecurityGroupIds": [VPC_SG]}

    try:
        fn = lambda_client.get_function(FunctionName=LAMBDA_NAME)
        fn_arn = fn["Configuration"]["FunctionArn"]
        print(f"  Updating Lambda {LAMBDA_NAME}...")
        lambda_client.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=zip_bytes)
        lambda_client.get_waiter("function_updated_v2").wait(FunctionName=LAMBDA_NAME)
        lambda_client.update_function_configuration(
            FunctionName=LAMBDA_NAME, Environment=env, VpcConfig=vpc, Timeout=30, MemorySize=256,
        )
    except lambda_client.exceptions.ResourceNotFoundException:
        print(f"  Creating Lambda {LAMBDA_NAME}...")
        response = lambda_client.create_function(
            FunctionName=LAMBDA_NAME,
            Runtime="python3.12",
            Role=role_arn,
            Handler="handler.lambda_handler",
            Code={"ZipFile": zip_bytes},
            Environment=env,
            VpcConfig=vpc,
            Timeout=30,
            MemorySize=256,
            Description="MCP server for AgentCore Gateway — nuh-analytics RDS tools",
        )
        fn_arn = response["FunctionArn"]

    lambda_client.get_waiter("function_active_v2").wait(FunctionName=LAMBDA_NAME)
    print(f"  Lambda ready: {fn_arn}")
    return fn_arn


def grant_gateway_invoke():
    """Allow AgentCore Gateway service to invoke the Lambda."""
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
    """Create or find the gateway. Returns gateway ID."""
    paginator = agentcore.get_paginator("list_gateways")
    for page in paginator.paginate():
        for gw in page.get("items", []):
            if gw["name"] == GATEWAY_NAME:
                gw_id = gw["gatewayId"]
                print(f"  Gateway exists: {gw_id}")
                return gw_id

    print(f"  Creating gateway {GATEWAY_NAME}...")
    response = agentcore.create_gateway(
        name=GATEWAY_NAME,
        description="MCP gateway for nuh-analytics RDS — execute_sql, list_tables, describe_table",
        roleArn=gateway_role_arn,
        protocolType="MCP",
        authorizerType="AWS_IAM",
    )
    gw_id = response["gatewayId"]
    print(f"  Gateway created: {gw_id}")

    print("  Waiting for gateway to be READY...")
    for _ in range(30):
        gw = agentcore.get_gateway(gatewayIdentifier=gw_id)
        status = gw.get("status", "")
        if status == "READY":
            print("  Gateway is READY.")
            break
        if "FAILED" in status:
            raise RuntimeError(f"Gateway failed: {status}")
        time.sleep(10)

    return gw_id


def ensure_gateway_target(gateway_id: str, lambda_arn: str):
    """Create or reuse the Lambda gateway target."""
    paginator = agentcore.get_paginator("list_gateway_targets")
    for page in paginator.paginate(gatewayIdentifier=gateway_id):
        for tgt in page.get("items", []):
            if tgt["name"] == TARGET_NAME:
                print(f"  Gateway target exists: {tgt['targetId']}")
                return

    print(f"  Creating gateway target {TARGET_NAME}...")
    response = agentcore.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=TARGET_NAME,
        description="Lambda: nuh-analytics-mcp — 3 read-only RDS tools",
        credentialProviderConfigurations=[
            {"credentialProviderType": "GATEWAY_IAM_ROLE"}
        ],
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    account_id = get_account_id()
    print(f"Account: {account_id}, Region: {REGION}\n")

    print("1. Ensuring Lambda IAM role...")
    lambda_role_arn = ensure_lambda_role()

    print("2. Deploying Lambda function...")
    lambda_arn = deploy_lambda(lambda_role_arn)

    print("3. Granting AgentCore Gateway invoke permission on Lambda...")
    grant_gateway_invoke()

    print("4. Ensuring Gateway IAM role...")
    gateway_role_arn = ensure_gateway_role(lambda_arn)
    time.sleep(10)  # IAM propagation before gateway creation

    print("5. Ensuring AgentCore Gateway...")
    gateway_id = ensure_gateway(gateway_role_arn)

    print("6. Ensuring Gateway Target...")
    ensure_gateway_target(gateway_id, lambda_arn)

    print("\nDone.")
    print(f"\nLambda ARN  : {lambda_arn}")
    print(f"Gateway ID  : {gateway_id}")
    print(f"\nTest in AWS console:")
    print(f"  Bedrock → AgentCore → Gateways → {GATEWAY_NAME} → Test")
    print(f'  Tool: list_tables')
    print(f'  Tool: execute_sql  →  {{"query": "SELECT COUNT(*) FROM emd"}}')


if __name__ == "__main__":
    main()
