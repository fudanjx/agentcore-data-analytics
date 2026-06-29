"""Deploy AgentCore POC.

Two resources are created:
  1. AgentCore Runtime  — hosts the agent container (ECR image).
  2. AgentCore Endpoint — exposes the runtime for invocation.
  3. OpenAI wrapper Lambda + API Gateway resource — translates
     POST /v1/chat/completions → invoke_agent_runtime → OpenAI response.

Usage:
    export ECR_IMAGE_URI=<account>.dkr.ecr.us-east-1.amazonaws.com/agentcore-poc:latest
    python infra/deploy.py

Optional env vars:
    RDS_SECRET_ARN      Secrets Manager ARN for DB credentials.
    RDS_DB_NAME         Database name.
    AWS_DEFAULT_REGION  Region (default: us-east-1).
"""

import json
import os
import sys
import textwrap
import time
import zipfile
from io import BytesIO

import boto3

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
RUNTIME_NAME = "agentcore_poc"
ENDPOINT_NAME = "agentcore_poc_endpoint"
WRAPPER_FUNCTION_NAME = "agentcore-poc-openai-wrapper"
WRAPPER_ROLE_NAME = "agentcore-poc-wrapper-role"
RUNTIME_ROLE_NAME = "agentcore-poc-runtime-role"
API_GW_ID = "eoqmjqt5p1"
API_GW_STAGE = "prod"
API_GW_REGION = "us-east-1"  # existing API Gateway lives in us-east-1
BEDROCK_URL = f"https://bedrock-runtime.{REGION}.amazonaws.com"

# VPC config — private subnets in the same VPC as RDS
# NAT Gateway in this VPC handles outbound to Bedrock/Secrets Manager (no VPC endpoints needed)
VPC_SUBNETS = ["subnet-061205c705e0f41d4", "subnet-0466b6e1fbb8a49f3"]  # private subnets 1a + 1b
VPC_SECURITY_GROUPS = ["sg-07258677b7e691e48"]  # agentcore-poc-runtime-sg

iam = boto3.client("iam", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=API_GW_REGION)  # wrapper Lambda must be in same region as API GW
agentcore_control = boto3.client("bedrock-agentcore-control", region_name=REGION)
apigw = boto3.client("apigateway", region_name=API_GW_REGION)
sts = boto3.client("sts", region_name=REGION)


def get_account_id() -> str:
    return sts.get_caller_identity()["Account"]


# ---------------------------------------------------------------------------
# IAM helpers
# ---------------------------------------------------------------------------

def ensure_runtime_role() -> str:
    """IAM role for the AgentCore Runtime container. Returns ARN."""
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
                    # AgentCore runtime region
                    f"arn:aws:bedrock:{REGION}::foundation-model/*",
                    # Application inference profile (us-east-1) + all 3 regions it routes to
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
    return _upsert_role(RUNTIME_ROLE_NAME, trust, inline, "AgentCore Runtime execution role")


def ensure_wrapper_role() -> str:
    """IAM role for the OpenAI wrapper Lambda. Returns ARN."""
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    inline = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": "arn:aws:logs:*:*:*",
            },
        ],
    }
    return _upsert_role(WRAPPER_ROLE_NAME, trust, inline, "AgentCore OpenAI wrapper Lambda role", propagate_wait=True)


def _upsert_role(name, trust, inline, description, propagate_wait=False) -> str:
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
        if propagate_wait:
            print("  Waiting for role to propagate...")
            time.sleep(10)
    iam.put_role_policy(RoleName=name, PolicyName=f"{name}-inline", PolicyDocument=json.dumps(inline))
    # Always wait after any policy change — AgentCore validates the role immediately
    # and fails if IAM hasn't propagated yet.
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
        # CLAUDE_CODE_USE_BEDROCK tells the claude subprocess to use Bedrock IAM auth.
        # The subprocess picks up AWS credentials automatically from the runtime IAM role.
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "RDS_SECRET_ARN": os.environ.get("RDS_SECRET_ARN", ""),
        "RDS_DB_NAME": os.environ.get("RDS_DB_NAME", ""),
    }

    # Check if runtime already exists
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
            print(f"  Runtime is READY.")
            return
        if "FAILED" in status:
            raise RuntimeError(f"AgentCore Runtime failed: {status}")
        time.sleep(10)
    raise TimeoutError("Timed out waiting for AgentCore Runtime to be READY")


def deploy_runtime_endpoint(runtime_id: str) -> str:
    """Create or reuse a runtime endpoint. Returns endpoint ARN."""
    # Check if endpoint already exists
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
# OpenAI wrapper Lambda
# ---------------------------------------------------------------------------

def _build_wrapper_zip(runtime_arn: str) -> bytes:
    """Build an in-memory zip containing the wrapper Lambda handler."""
    source = textwrap.dedent(f"""
        import json
        import uuid
        import boto3

        RUNTIME_ARN = "{runtime_arn}"
        REGION = "{REGION}"

        client = boto3.client("bedrock-agentcore", region_name=REGION)


        def handler(event, context):
            body = json.loads(event.get("body") or "{{}}")
            messages = body.get("messages", [])
            model = body.get("model", "agentcore")

            if not messages:
                return {{
                    "statusCode": 400,
                    "body": json.dumps({{"error": "messages must not be empty"}}),
                }}

            # Send messages JSON directly — AgentCore passes body straight to container
            payload = json.dumps({{"messages": messages}}).encode()

            response = client.invoke_agent_runtime(
                agentRuntimeArn=RUNTIME_ARN,
                contentType="application/json",
                accept="application/json",
                payload=payload,
            )

            # invoke_agent_runtime returns the response under the "response" key
            raw = response["response"].read()
            result_body = json.loads(raw)
            result_text = result_body.get("result", str(result_body))

            openai_response = {{
                "id": f"chatcmpl-{{uuid.uuid4().hex[:12]}}",
                "object": "chat.completion",
                "model": model,
                "choices": [{{
                    "index": 0,
                    "message": {{"role": "assistant", "content": result_text}},
                    "finish_reason": "stop",
                }}],
                "usage": {{"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
            }}

            return {{
                "statusCode": 200,
                "headers": {{"Content-Type": "application/json"}},
                "body": json.dumps(openai_response),
            }}
    """).lstrip()

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("handler.py", source)
    return buf.getvalue()


def deploy_wrapper_lambda(runtime_arn: str, role_arn: str) -> str:
    """Deploy the OpenAI wrapper Lambda. Returns function ARN."""
    zip_bytes = _build_wrapper_zip(runtime_arn)

    try:
        fn = lambda_client.get_function(FunctionName=WRAPPER_FUNCTION_NAME)
        fn_arn = fn["Configuration"]["FunctionArn"]
        print(f"  Updating wrapper Lambda {WRAPPER_FUNCTION_NAME}...")
        lambda_client.update_function_code(FunctionName=WRAPPER_FUNCTION_NAME, ZipFile=zip_bytes)
        waiter = lambda_client.get_waiter("function_updated_v2")
        waiter.wait(FunctionName=WRAPPER_FUNCTION_NAME)
        lambda_client.update_function_configuration(
            FunctionName=WRAPPER_FUNCTION_NAME,
            Timeout=300,
            MemorySize=256,
        )
    except lambda_client.exceptions.ResourceNotFoundException:
        print(f"  Creating wrapper Lambda {WRAPPER_FUNCTION_NAME}...")
        response = lambda_client.create_function(
            FunctionName=WRAPPER_FUNCTION_NAME,
            Runtime="python3.12",
            Role=role_arn,
            Handler="handler.handler",
            Code={"ZipFile": zip_bytes},
            Timeout=300,
            MemorySize=256,
        )
        fn_arn = response["FunctionArn"]

    waiter = lambda_client.get_waiter("function_active_v2")
    waiter.wait(FunctionName=WRAPPER_FUNCTION_NAME)
    print(f"  Wrapper Lambda ready: {fn_arn}")
    return fn_arn


# ---------------------------------------------------------------------------
# API Gateway wiring
# ---------------------------------------------------------------------------

def wire_api_gateway(fn_arn: str, account_id: str) -> str:
    """Add POST /v1/chat/completions → wrapper Lambda to existing API GW."""
    resources = apigw.get_resources(restApiId=API_GW_ID)["items"]
    resource_map = {r["path"]: r["id"] for r in resources}
    root_id = resource_map["/"]

    def get_or_create(parent_id: str, part: str, path: str) -> str:
        if path in resource_map:
            return resource_map[path]
        r = apigw.create_resource(restApiId=API_GW_ID, parentId=parent_id, pathPart=part)
        resource_map[path] = r["id"]
        return r["id"]

    v1_id = get_or_create(root_id, "v1", "/v1")
    chat_id = get_or_create(v1_id, "chat", "/v1/chat")
    completions_id = get_or_create(chat_id, "completions", "/v1/chat/completions")

    try:
        apigw.get_method(restApiId=API_GW_ID, resourceId=completions_id, httpMethod="POST")
        print("  POST /v1/chat/completions method already exists")
    except apigw.exceptions.NotFoundException:
        print("  Creating POST /v1/chat/completions...")
        apigw.put_method(restApiId=API_GW_ID, resourceId=completions_id, httpMethod="POST", authorizationType="NONE")
        uri = (
            f"arn:aws:apigateway:{API_GW_REGION}:lambda:path/2015-03-31/functions/{fn_arn}/invocations"
        )
        apigw.put_integration(
            restApiId=API_GW_ID, resourceId=completions_id,
            httpMethod="POST", type="AWS_PROXY", integrationHttpMethod="POST", uri=uri,
        )

    try:
        lambda_client.add_permission(
            FunctionName=WRAPPER_FUNCTION_NAME,
            StatementId="apigw-invoke-agentcore-wrapper",
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=f"arn:aws:execute-api:{API_GW_REGION}:{account_id}:{API_GW_ID}/*/*",
        )
    except lambda_client.exceptions.ResourceConflictException:
        pass

    apigw.create_deployment(restApiId=API_GW_ID, stageName=API_GW_STAGE, description="AgentCore wrapper deployment")
    endpoint = f"https://{API_GW_ID}.execute-api.{API_GW_REGION}.amazonaws.com/{API_GW_STAGE}"
    print(f"  API endpoint: {endpoint}/v1/chat/completions")
    return endpoint


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def deploy_function_url(fn_arn: str) -> str:
    """Create or retrieve a Lambda Function URL for the wrapper. Returns the URL."""
    try:
        resp = lambda_client.get_function_url_config(FunctionName=WRAPPER_FUNCTION_NAME)
        url = resp["FunctionUrl"]
        print(f"  Function URL exists: {url}")
        return url
    except lambda_client.exceptions.ResourceNotFoundException:
        pass

    resp = lambda_client.create_function_url_config(
        FunctionName=WRAPPER_FUNCTION_NAME,
        AuthType="AWS_IAM",
    )
    url = resp["FunctionUrl"]

    # Allow public invocation via the Function URL
    try:
        lambda_client.add_permission(
            FunctionName=WRAPPER_FUNCTION_NAME,
            StatementId="allow-function-url-public",
            Action="lambda:InvokeFunctionUrl",
            Principal="*",
            FunctionUrlAuthType="NONE",
        )
    except lambda_client.exceptions.ResourceConflictException:
        pass

    print(f"  Function URL: {url}")
    return url


def main():
    image_uri = os.environ.get("ECR_IMAGE_URI")
    if not image_uri:
        print("ERROR: ECR_IMAGE_URI is required. Run infra/build_and_push.sh first.")
        sys.exit(1)

    account_id = get_account_id()
    print(f"Deploying to account {account_id}, region {REGION}\n")

    print("1. Ensuring AgentCore Runtime IAM role...")
    runtime_role_arn = ensure_runtime_role()

    print("2. Ensuring OpenAI wrapper IAM role...")
    wrapper_role_arn = ensure_wrapper_role()

    print("3. Deploying AgentCore Runtime...")
    runtime_id = deploy_agent_runtime(image_uri, runtime_role_arn)
    wait_for_runtime(runtime_id)

    print("4. Deploying AgentCore endpoint...")
    endpoint_arn = deploy_runtime_endpoint(runtime_id)

    # invoke_agent_runtime uses the runtime ARN, not the endpoint ARN
    runtime_arn = f"arn:aws:bedrock-agentcore:{REGION}:{account_id}:runtime/{runtime_id}"

    print("5. Deploying OpenAI wrapper Lambda...")
    fn_arn = deploy_wrapper_lambda(runtime_arn, wrapper_role_arn)

    print("6. Wiring API Gateway...")
    api_url = wire_api_gateway(fn_arn, account_id)

    print("7. Creating Lambda Function URL (no 29s API GW timeout limit)...")
    fn_url = deploy_function_url(fn_arn)

    print("\nDone.")
    print(f"\nAgentCore Runtime ID : {runtime_id}")
    print(f"AgentCore Endpoint   : {endpoint_arn}")
    print(f"OpenAI API endpoint  : {api_url}/v1/chat/completions  (29s max — may timeout)")
    print(f"Lambda URL endpoint  : {fn_url}  (no timeout limit — use this for long queries)")
    print(f"\nTest (use Lambda URL for long-running queries):")
    print(f"""  curl -X POST {fn_url} \\
    -H 'Content-Type: application/json' \\
    -d '{{"model":"agentcore","messages":[{{"role":"user","content":"list all tables"}}]}}'""")


if __name__ == "__main__":
    main()
