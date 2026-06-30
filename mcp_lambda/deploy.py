"""
Deploy nuh-analytics MCP Lambda.

Creates:
  1. IAM role for Lambda (nuh-analytics-mcp-role)
  2. Lambda function in VPC (nuh-analytics-mcp) — zip packaged

AgentCore Gateway wiring is done manually in the AWS console.

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

SECRET_ARN = "arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J"
DB_NAME = "nuh-analytics"
VPC_SUBNETS = ["subnet-061205c705e0f41d4", "subnet-0466b6e1fbb8a49f3"]
VPC_SG = "sg-07258677b7e691e48"

iam = boto3.client("iam")
lambda_client = boto3.client("lambda", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)


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

    try:
        arn = iam.get_role(RoleName=LAMBDA_ROLE_NAME)["Role"]["Arn"]
        print(f"  Role exists: {arn}")
    except iam.exceptions.NoSuchEntityException:
        print(f"  Creating role {LAMBDA_ROLE_NAME}...")
        arn = iam.create_role(
            RoleName=LAMBDA_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="nuh-analytics MCP Lambda role",
        )["Role"]["Arn"]
        iam.attach_role_policy(RoleName=LAMBDA_ROLE_NAME, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        print("  Waiting for role propagation...")
        time.sleep(15)

    iam.put_role_policy(RoleName=LAMBDA_ROLE_NAME, PolicyName=f"{LAMBDA_ROLE_NAME}-inline", PolicyDocument=json.dumps(inline))
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
             "--platform", "manylinux2014_x86_64",  # Lambda is x86_64
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

    env = {
        "Variables": {
            "SECRET_ARN": SECRET_ARN,
            "DB_NAME": DB_NAME,
        }
    }
    vpc = {
        "SubnetIds": VPC_SUBNETS,
        "SecurityGroupIds": [VPC_SG],
    }

    try:
        fn = lambda_client.get_function(FunctionName=LAMBDA_NAME)
        fn_arn = fn["Configuration"]["FunctionArn"]
        print(f"  Updating Lambda {LAMBDA_NAME}...")
        lambda_client.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=zip_bytes)
        lambda_client.get_waiter("function_updated_v2").wait(FunctionName=LAMBDA_NAME)
        lambda_client.update_function_configuration(
            FunctionName=LAMBDA_NAME, Environment=env, VpcConfig=vpc,
            Timeout=30, MemorySize=256,
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

    print("\nDone.")
    print(f"\nLambda ARN: {lambda_arn}")
    print(f"\nNext step: wire this Lambda to AgentCore Gateway manually in the AWS console.")
    print(f"  Bedrock → AgentCore → Gateways → Create Gateway")
    print(f"  Protocol: MCP  |  Target: Lambda  |  Lambda ARN: {lambda_arn}")
    print(f"\nTool schema for gateway target (inlinePayload):")
    tools = [
        {"name": "execute_sql", "description": "Run a read-only SELECT query against nuh-analytics", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        {"name": "list_tables", "description": "List all tables and columns in nuh-analytics", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "describe_table", "description": "Get column details and sample rows for a table", "inputSchema": {"type": "object", "properties": {"table_name": {"type": "string"}}, "required": ["table_name"]}},
    ]
    import json as _json
    print(_json.dumps(tools, indent=2))


if __name__ == "__main__":
    main()
