"""Deploy AgentCore POC.

Creates/updates:
  1. IAM role for AgentCore Runtime
  2. AgentCore Runtime  — hosts the agent container (ECR image) in VPC mode
  3. AgentCore Endpoint — named endpoint for the runtime

Access is via the EKS proxy (agentcore-proxy pod) — no Lambda or API Gateway involved.

Usage:
    export ECR_IMAGE_URI=<account>.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-poc:latest
    export RDS_SECRET_ARN=arn:aws:secretsmanager:ap-southeast-1:...
    export RDS_DB_NAME=nuh-analytics
    python infra/deploy.py
"""

import json
import os
import sys
import time

import boto3

REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
RUNTIME_NAME = "agentcore_poc"
ENDPOINT_NAME = "agentcore_poc_endpoint"
RUNTIME_ROLE_NAME = "agentcore-poc-runtime-role"

# VPC config — private subnets in bot-nuhs-vpc, same VPC as RDS
VPC_SUBNETS = ["subnet-061205c705e0f41d4", "subnet-0466b6e1fbb8a49f3"]
VPC_SECURITY_GROUPS = ["sg-07258677b7e691e48"]  # agentcore-poc-runtime-sg

iam = boto3.client("iam")
agentcore_control = boto3.client("bedrock-agentcore-control", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)


def get_account_id() -> str:
    return sts.get_caller_identity()["Account"]


# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

def ensure_runtime_role() -> str:
    """Create or update the AgentCore Runtime IAM role. Returns ARN."""
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    inline = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": [
                    f"arn:aws:bedrock:{REGION}::foundation-model/*",
                    # Application inference profile in us-east-1 + cross-region routing targets
                    "arn:aws:bedrock:us-east-1:964340114883:application-inference-profile/*",
                    "arn:aws:bedrock:us-east-1::foundation-model/*",
                    "arn:aws:bedrock:us-east-2::foundation-model/*",
                    "arn:aws:bedrock:us-west-2::foundation-model/*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": "arn:aws:logs:*:*:*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                "Resource": "*",
            },
            {
                # Required for VPC mode — AgentCore creates ENIs in the specified subnets
                "Effect": "Allow",
                "Action": [
                    "ec2:CreateNetworkInterface",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DeleteNetworkInterface",
                    "ec2:AssignPrivateIpAddresses",
                    "ec2:UnassignPrivateIpAddresses",
                    "ec2:DescribeSubnets",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeVpcs",
                ],
                "Resource": "*",
            },
        ],
    }

    try:
        arn = iam.get_role(RoleName=RUNTIME_ROLE_NAME)["Role"]["Arn"]
        print(f"  Role exists: {arn}")
    except iam.exceptions.NoSuchEntityException:
        print(f"  Creating role {RUNTIME_ROLE_NAME}...")
        arn = iam.create_role(
            RoleName=RUNTIME_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="AgentCore Runtime execution role",
        )["Role"]["Arn"]

    iam.put_role_policy(
        RoleName=RUNTIME_ROLE_NAME,
        PolicyName=f"{RUNTIME_ROLE_NAME}-inline",
        PolicyDocument=json.dumps(inline),
    )
    # AgentCore validates the role immediately on create/update — wait for IAM propagation
    print("  Waiting for IAM policy to propagate...")
    time.sleep(15)
    return arn


# ---------------------------------------------------------------------------
# AgentCore Runtime
# ---------------------------------------------------------------------------

def deploy_agent_runtime(image_uri: str, role_arn: str) -> str:
    """Create or update the AgentCore Runtime. Returns runtime ID."""
    env_vars = {
        "AWS_DEFAULT_REGION": REGION,
        # Tells the claude subprocess to use Bedrock IAM auth (no API key needed)
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "RDS_SECRET_ARN": os.environ.get("RDS_SECRET_ARN", ""),
        "RDS_DB_NAME": os.environ.get("RDS_DB_NAME", ""),
    }

    existing_id = _find_existing_runtime()

    if existing_id:
        print(f"  Updating AgentCore Runtime {existing_id}...")
        agentcore_control.update_agent_runtime(
            agentRuntimeId=existing_id,
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
            roleArn=role_arn,
            networkConfiguration={
                "networkMode": "VPC",
                "networkModeConfig": {
                    "securityGroups": VPC_SECURITY_GROUPS,
                    "subnets": VPC_SUBNETS,
                },
            },
            environmentVariables=env_vars,
            protocolConfiguration={"serverProtocol": "HTTP"},
        )
        return existing_id
    else:
        print(f"  Creating AgentCore Runtime {RUNTIME_NAME}...")
        response = agentcore_control.create_agent_runtime(
            agentRuntimeName=RUNTIME_NAME,
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
            roleArn=role_arn,
            networkConfiguration={
                "networkMode": "VPC",
                "networkModeConfig": {
                    "securityGroups": VPC_SECURITY_GROUPS,
                    "subnets": VPC_SUBNETS,
                },
            },
            environmentVariables=env_vars,
            protocolConfiguration={"serverProtocol": "HTTP"},
            description="AgentCore POC — RDS analytics agent",
        )
        runtime_id = response["agentRuntimeId"]
        print(f"  Runtime created: {runtime_id}")
        return runtime_id


def _find_existing_runtime() -> str | None:
    paginator = agentcore_control.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for rt in page.get("agentRuntimes", []):
            if rt["agentRuntimeName"] == RUNTIME_NAME:
                return rt["agentRuntimeId"]
    return None


def wait_for_runtime(runtime_id: str):
    print("  Waiting for runtime to be READY...")
    for _ in range(60):
        rt = agentcore_control.get_agent_runtime(agentRuntimeId=runtime_id)
        status = rt["status"]
        if status == "READY":
            print("  Runtime is READY.")
            return
        if "FAILED" in status:
            raise RuntimeError(f"AgentCore Runtime failed: {status}")
        time.sleep(10)
    raise TimeoutError("Timed out waiting for AgentCore Runtime to be READY")


def deploy_runtime_endpoint(runtime_id: str) -> str:
    """Create or reuse the named runtime endpoint. Returns endpoint ARN."""
    paginator = agentcore_control.get_paginator("list_agent_runtime_endpoints")
    for page in paginator.paginate(agentRuntimeId=runtime_id):
        for ep in page.get("runtimeEndpoints", []):
            if ep["name"] == ENDPOINT_NAME:
                arn = ep["agentRuntimeEndpointArn"]
                print(f"  Endpoint exists: {arn}")
                return arn

    print(f"  Creating endpoint {ENDPOINT_NAME}...")
    response = agentcore_control.create_agent_runtime_endpoint(
        agentRuntimeId=runtime_id,
        name=ENDPOINT_NAME,
        description="AgentCore POC default endpoint",
    )
    arn = response["agentRuntimeEndpointArn"]
    print(f"  Endpoint ARN: {arn}")
    return arn


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    image_uri = os.environ.get("ECR_IMAGE_URI")
    if not image_uri:
        print("ERROR: ECR_IMAGE_URI is required. Run infra/build_and_push.sh first.")
        sys.exit(1)

    account_id = get_account_id()
    print(f"Deploying to account {account_id}, region {REGION}\n")

    print("1. Ensuring AgentCore Runtime IAM role...")
    runtime_role_arn = ensure_runtime_role()

    print("2. Deploying AgentCore Runtime...")
    runtime_id = deploy_agent_runtime(image_uri, runtime_role_arn)
    wait_for_runtime(runtime_id)

    print("3. Deploying AgentCore endpoint...")
    endpoint_arn = deploy_runtime_endpoint(runtime_id)

    print("\nDone.")
    print(f"\nAgentCore Runtime ID : {runtime_id}")
    print(f"AgentCore Endpoint   : {endpoint_arn}")
    print(f"\nAccess via EKS proxy (VPC-internal, no auth required):")
    print(f"  DIFY (in-cluster) :    http://agentcore-proxy.agentcore.svc.cluster.local")
    print(f"  Open WebUI (via NLB):  http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com")
    print(f"\nDirect Python access:")
    print(f"  python3 py_sdk.py \"your question\"")


if __name__ == "__main__":
    main()
