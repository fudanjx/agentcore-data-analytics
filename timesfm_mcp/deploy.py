"""
Deploy timesfm-mcp Lambda + wire to existing timesfm-gateway + harness_e52fs.

Creates:
  1. Lambda IAM role (timesfm-mcp-role)
  2. Lambda function in VPC (timesfm-mcp) — thin urllib bridge to EKS NLB
  3. Grant AgentCore Gateway service lambda:InvokeFunction on Lambda
  4. Update timesfm-gateway-role to allow invoking this Lambda
  5. Create Gateway Target (timesfm-forecast) — mcp.lambda type with inline schema
  6. Add gateway to harness_e52fs tools
  7. Add gateway ARN to harness gateway IAM policy

Usage:
    NLB_ENDPOINT=http://<nlb-hostname> python3 timesfm_mcp/deploy.py
"""

import io
import json
import os
import sys
import time
import zipfile

import boto3

REGION = "ap-southeast-1"
LAMBDA_NAME = "timesfm-mcp"
LAMBDA_ROLE_NAME = "timesfm-mcp-role"
GATEWAY_NAME = "timesfm-gateway"
GATEWAY_ROLE_NAME = "timesfm-gateway-role"
TARGET_NAME = "timesfm-forecast"
HARNESS_ID = "harness_e52fs-Du2DM0RxvF"
HARNESS_GATEWAY_POLICY_ARN = (
    "arn:aws:iam::964340114883:policy/service-role/AmazonBedrockAgentCoreHarnessGatewayPolicy_bd7bg"
)

VPC_SUBNETS = ["subnet-061205c705e0f41d4", "subnet-0466b6e1fbb8a49f3"]
VPC_SG = "sg-07258677b7e691e48"

NLB_ENDPOINT = os.environ.get("NLB_ENDPOINT", "").rstrip("/")
if not NLB_ENDPOINT:
    print("ERROR: Set NLB_ENDPOINT env var to the internal NLB hostname for timesfm-svc-internal")
    sys.exit(1)

FORECAST_URL = f"{NLB_ENDPOINT}/forecast"

iam = boto3.client("iam")
lambda_client = boto3.client("lambda", region_name=REGION)
agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)

TOOL_SCHEMA = [
    {
        "name": "timesfm_forecast",
        "description": (
            "Forecast future values of a time series using Google TimesFM. "
            "Use when the user asks to predict, forecast, project, or estimate future trends "
            "for any numeric metric (admissions, visits, procedures, LOS, etc.). "
            "Returns point forecast, 80% prediction interval, and forecast dates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Historical values in chronological order (at least 12 recommended)",
                },
                "horizon": {
                    "type": "integer",
                    "description": "Number of future periods to forecast (default 12, max 128)",
                },
                "freq": {
                    "type": "string",
                    "description": "Time frequency: H=hourly, D=daily, W=weekly, M=monthly (default M)",
                },
                "context_dates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ISO date strings for each context value, e.g. ['2024-01', '2024-02', ...]",
                },
            },
            "required": ["context"],
        },
    }
]


def get_account_id() -> str:
    return sts.get_caller_identity()["Account"]


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
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "arn:aws:logs:*:*:*"},
            {"Effect": "Allow", "Action": [
                "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces",
                "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses",
                "ec2:UnassignPrivateIpAddresses",
            ], "Resource": "*"},
        ],
    }
    return _upsert_role(LAMBDA_ROLE_NAME, trust, inline, "TimesFM MCP bridge Lambda role")


def update_gateway_role(lambda_arn: str):
    """Update existing timesfm-gateway-role to allow invoking this Lambda."""
    inline = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": lambda_arn},
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "*"},
        ],
    }
    iam.put_role_policy(
        RoleName=GATEWAY_ROLE_NAME,
        PolicyName=f"{GATEWAY_ROLE_NAME}-inline",
        PolicyDocument=json.dumps(inline),
    )
    print(f"  Gateway role {GATEWAY_ROLE_NAME} inline policy updated")


def build_zip() -> bytes:
    """Bundle handler.py only — no third-party deps needed (urllib is stdlib)."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(os.path.join(script_dir, "handler.py"), "handler.py")
    return buf.getvalue()


def deploy_lambda(role_arn: str) -> str:
    print("  Building zip...")
    zip_bytes = build_zip()
    print(f"  Zip size: {len(zip_bytes) / 1024:.1f} KB")

    env = {"Variables": {"TIMESFM_URL": FORECAST_URL, "TIMEOUT_SECS": "60"}}
    vpc = {"SubnetIds": VPC_SUBNETS, "SecurityGroupIds": [VPC_SG]}

    try:
        fn = lambda_client.get_function(FunctionName=LAMBDA_NAME)
        fn_arn = fn["Configuration"]["FunctionArn"]
        print(f"  Updating Lambda {LAMBDA_NAME}...")
        lambda_client.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=zip_bytes)
        lambda_client.get_waiter("function_updated_v2").wait(FunctionName=LAMBDA_NAME)
        lambda_client.update_function_configuration(
            FunctionName=LAMBDA_NAME, Environment=env, VpcConfig=vpc, Timeout=90, MemorySize=256,
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
            VpcConfig=vpc,
            Timeout=90,
            MemorySize=256,
            Description="Bridge Lambda: AgentCore Gateway MCP → TimesFM EKS service",
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


def ensure_gateway_exists() -> str:
    """Find the pre-existing timesfm-gateway created by the earlier script."""
    paginator = agentcore.get_paginator("list_gateways")
    for page in paginator.paginate():
        for gw in page.get("items", []):
            if gw["name"] == GATEWAY_NAME:
                gw_id = gw["gatewayId"]
                print(f"  Gateway exists: {gw_id}")
                return gw_id
    raise RuntimeError(f"Gateway '{GATEWAY_NAME}' not found — create it first via the earlier deploy script")


def ensure_gateway_target(gateway_id: str, lambda_arn: str):
    paginator = agentcore.get_paginator("list_gateway_targets")
    for page in paginator.paginate(gatewayIdentifier=gateway_id):
        for tgt in page.get("items", []):
            if tgt["name"] == TARGET_NAME:
                print(f"  Gateway target exists: {tgt['targetId']}")
                return

    print(f"  Creating gateway target {TARGET_NAME} → {lambda_arn}...")
    response = agentcore.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=TARGET_NAME,
        description="Lambda bridge to TimesFM EKS forecasting service",
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


def add_gateway_to_harness(gateway_id: str, account_id: str):
    harness = agentcore.get_harness(harnessId=HARNESS_ID)["harness"]
    existing_tools = harness.get("tools", [])
    gateway_arn = f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:gateway/{gateway_id}"

    for tool in existing_tools:
        cfg = tool.get("config", {}).get("agentCoreGateway", {})
        if cfg.get("gatewayArn") == gateway_arn:
            print("  Gateway already in harness — skipping")
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
    agentcore.update_harness(harnessId=HARNESS_ID, tools=existing_tools + [new_tool])
    print(f"  Harness updated — {len(existing_tools) + 1} gateway tool(s)")


def add_gateway_to_iam_policy(gateway_id: str, account_id: str):
    gateway_arn = f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:gateway/{gateway_id}"
    policy = iam.get_policy(PolicyArn=HARNESS_GATEWAY_POLICY_ARN)["Policy"]
    doc = iam.get_policy_version(
        PolicyArn=HARNESS_GATEWAY_POLICY_ARN,
        VersionId=policy["DefaultVersionId"],
    )["PolicyVersion"]["Document"]

    existing = doc["Statement"][0]["Resource"]
    if isinstance(existing, str):
        existing = [existing]
    if gateway_arn in existing:
        print("  Gateway already in IAM policy — skipping")
        return

    doc["Statement"][0]["Resource"] = existing + [gateway_arn]

    # Trim old non-default versions to stay under the 5-version limit
    versions = iam.list_policy_versions(PolicyArn=HARNESS_GATEWAY_POLICY_ARN)["Versions"]
    non_default_old = sorted(
        [v for v in versions if not v["IsDefaultVersion"]],
        key=lambda v: v["CreateDate"],
    )
    while len(non_default_old) >= 4:
        old = non_default_old.pop(0)
        iam.delete_policy_version(PolicyArn=HARNESS_GATEWAY_POLICY_ARN, VersionId=old["VersionId"])

    new_ver = iam.create_policy_version(
        PolicyArn=HARNESS_GATEWAY_POLICY_ARN,
        PolicyDocument=json.dumps(doc),
        SetAsDefault=True,
    )
    print(f"  IAM policy updated: {new_ver['PolicyVersion']['VersionId']}")


def main():
    account_id = get_account_id()
    print(f"Account: {account_id}, Region: {REGION}")
    print(f"TimesFM URL: {FORECAST_URL}\n")

    print("1. Ensuring Lambda IAM role...")
    lambda_role_arn = ensure_lambda_role()

    print("2. Deploying Lambda function...")
    lambda_arn = deploy_lambda(lambda_role_arn)

    print("3. Granting AgentCore Gateway invoke permission on Lambda...")
    grant_gateway_invoke()

    print("4. Updating Gateway IAM role to invoke this Lambda...")
    update_gateway_role(lambda_arn)
    time.sleep(5)

    print("5. Finding existing AgentCore Gateway...")
    gateway_id = ensure_gateway_exists()

    print("6. Ensuring Gateway Target (mcp.lambda)...")
    ensure_gateway_target(gateway_id, lambda_arn)

    print("7. Adding gateway to harness...")
    add_gateway_to_harness(gateway_id, account_id)

    print("8. Adding gateway to harness execution IAM policy...")
    add_gateway_to_iam_policy(gateway_id, account_id)

    print("\nDone.")
    print(f"\nLambda ARN  : {lambda_arn}")
    print(f"Gateway ID  : {gateway_id}")


if __name__ == "__main__":
    main()
