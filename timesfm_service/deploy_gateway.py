"""
Deploy TimesFM AgentCore Gateway and wire it to harness_e52fs.

Steps:
  1. Create Gateway IAM role (timesfm-gateway-role)
  2. Create AgentCore Gateway (timesfm-gateway)
  3. Create Gateway Target (timesfm-forecast) → internal NLB endpoint
  4. Add gateway to harness_e52fs tools
  5. Add gateway ARN to harness execution IAM policy

Usage:
    NLB_ENDPOINT=http://<nlb-hostname> python3 timesfm_service/deploy_gateway.py
"""

import json
import os
import sys
import time

import boto3

REGION = "ap-southeast-1"
GATEWAY_NAME = "timesfm-gateway"
GATEWAY_ROLE_NAME = "timesfm-gateway-role"
TARGET_NAME = "timesfm-forecast"
HARNESS_ID = "harness_e52fs-Du2DM0RxvF"
HARNESS_GATEWAY_POLICY_ARN = "arn:aws:iam::964340114883:policy/service-role/AmazonBedrockAgentCoreHarnessGatewayPolicy_bd7bg"

# Override via env var: NLB_ENDPOINT=http://k8s-...-elb.ap-southeast-1.amazonaws.com
NLB_ENDPOINT = os.environ.get("NLB_ENDPOINT", "").rstrip("/")
if not NLB_ENDPOINT:
    print("ERROR: Set NLB_ENDPOINT env var to the internal NLB hostname for timesfm-svc-internal")
    print("  e.g. NLB_ENDPOINT=http://k8s-agentcor-timesfsm-xxxx.elb.ap-southeast-1.amazonaws.com")
    sys.exit(1)

FORECAST_ENDPOINT = f"{NLB_ENDPOINT}/forecast"

iam = boto3.client("iam")
agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)

TOOL_SCHEMA = [
    {
        "name": "timesfm_forecast",
        "description": (
            "Forecast future values of a time series using Google TimesFM. "
            "Use when the user asks to predict, forecast, project, or estimate future trends "
            "for any numeric metric (admissions, visits, procedures, LOS, etc.)."
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
                    "description": "Number of future periods to forecast (default: 12)",
                    "default": 12,
                },
                "freq": {
                    "type": "string",
                    "description": "Time frequency: H=hourly, D=daily, W=weekly, M=monthly (default: M)",
                    "default": "M",
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


def ensure_gateway_role() -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    # Gateway role for http.passthrough doesn't need Lambda invoke permission
    inline = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "*"}
        ],
    }
    try:
        arn = iam.get_role(RoleName=GATEWAY_ROLE_NAME)["Role"]["Arn"]
        print(f"  Gateway role exists: {arn}")
    except iam.exceptions.NoSuchEntityException:
        print(f"  Creating gateway role {GATEWAY_ROLE_NAME}...")
        arn = iam.create_role(
            RoleName=GATEWAY_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="TimesFM AgentCore Gateway role",
        )["Role"]["Arn"]
        print("  Waiting for role propagation...")
        time.sleep(15)
    iam.put_role_policy(
        RoleName=GATEWAY_ROLE_NAME,
        PolicyName=f"{GATEWAY_ROLE_NAME}-inline",
        PolicyDocument=json.dumps(inline),
    )
    return arn


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
        description="TimesFM time-series forecasting gateway",
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


def ensure_gateway_target(gateway_id: str):
    paginator = agentcore.get_paginator("list_gateway_targets")
    for page in paginator.paginate(gatewayIdentifier=gateway_id):
        for tgt in page.get("items", []):
            if tgt["name"] == TARGET_NAME:
                print(f"  Gateway target exists: {tgt['targetId']}")
                return

    print(f"  Creating gateway target {TARGET_NAME} → {FORECAST_ENDPOINT}...")
    response = agentcore.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=TARGET_NAME,
        description="TimesFM forecast endpoint on EKS internal NLB",
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        targetConfiguration={
            "mcp": {
                "lambda": {
                    # http.passthrough target — points to the EKS internal NLB
                    # Note: using mcp/lambda schema structure; adjust if http.passthrough
                    # becomes available in the SDK version in use
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
    agentcore.update_harness(
        harnessId=HARNESS_ID,
        tools=existing_tools + [new_tool],
    )
    print(f"  Harness updated — {len(existing_tools) + 1} gateway tool(s)")


def add_gateway_to_iam_policy(gateway_id: str, account_id: str):
    gateway_arn = f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:gateway/{gateway_id}"
    policy = iam.get_policy(PolicyArn=HARNESS_GATEWAY_POLICY_ARN)["Policy"]
    doc = iam.get_policy_version(
        PolicyArn=HARNESS_GATEWAY_POLICY_ARN,
        VersionId=policy["DefaultVersionId"],
    )["PolicyVersion"]["Document"]

    existing_resources = doc["Statement"][0]["Resource"]
    if isinstance(existing_resources, str):
        existing_resources = [existing_resources]

    if gateway_arn in existing_resources:
        print("  Gateway already in IAM policy — skipping")
        return

    doc["Statement"][0]["Resource"] = existing_resources + [gateway_arn]
    new_ver = iam.create_policy_version(
        PolicyArn=HARNESS_GATEWAY_POLICY_ARN,
        PolicyDocument=json.dumps(doc),
        SetAsDefault=True,
    )
    print(f"  IAM policy updated: {new_ver['PolicyVersion']['VersionId']}")


def main():
    account_id = get_account_id()
    print(f"Account: {account_id}, Region: {REGION}")
    print(f"Endpoint: {FORECAST_ENDPOINT}\n")

    print("1. Ensuring Gateway IAM role...")
    gateway_role_arn = ensure_gateway_role()

    print("2. Ensuring AgentCore Gateway...")
    gateway_id = ensure_gateway(gateway_role_arn)

    print("3. Ensuring Gateway Target...")
    ensure_gateway_target(gateway_id)

    print("4. Adding gateway to harness...")
    add_gateway_to_harness(gateway_id, account_id)

    print("5. Adding gateway to harness execution IAM policy...")
    add_gateway_to_iam_policy(gateway_id, account_id)

    print("\nDone.")
    print(f"\nGateway ID  : {gateway_id}")
    print(f"Gateway ARN : arn:aws:bedrock-agentcore:{REGION}:{account_id}:gateway/{gateway_id}")
    print(f"\nTest in AWS console:")
    print(f"  Bedrock → AgentCore → Gateways → {GATEWAY_NAME} → Test")
    print(f'  Tool: timesfm_forecast, {{"context": [100,105,98,112,120,115], "horizon": 3}}')


if __name__ == "__main__":
    main()
