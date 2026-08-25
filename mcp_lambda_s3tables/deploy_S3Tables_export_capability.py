"""Safely deploy the shared S3 Tables export capability for the GPT pilot.

This intentionally updates only Lambda code, the existing Gateway target schema,
and the managed Code Interpreter role's exact query-export read permission. It
does not alter harnesses, Gateway membership, or either runtime configuration.
"""

import io
import json
import time
import zipfile
from pathlib import Path

import boto3

from deploy import REGION, TOOL_SCHEMA


ACCOUNT_ID = "964340114883"
LAMBDA_NAME = "ah-analytics-s3tables-mcp"
GATEWAY_ID = "ah-analytics-s3tables-uhtyjdutj7"
TARGET_ID = "YUI2QRACY2"
TARGET_NAME = "s3tables-tools"
CODE_INTERPRETER_ROLE = "AmazonGenesisDefaultServiceRole-7oou1"
CODE_INTERPRETER_POLICY = "StrandsInsightsS3Access"
QUERY_EXPORT_RESOURCE = (
    f"arn:aws:s3:::agentcore-tmp-{ACCOUNT_ID}/athena-results/*"
)


def build_zip() -> bytes:
    source = Path(__file__).with_name("handler.py")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(source, "handler.py")
    return buffer.getvalue()


def update_lambda_code(lambda_client) -> None:
    lambda_client.update_function_code(
        FunctionName=LAMBDA_NAME,
        ZipFile=build_zip(),
        Publish=False,
    )
    lambda_client.get_waiter("function_updated_v2").wait(FunctionName=LAMBDA_NAME)


def update_gateway_target(agentcore) -> None:
    current = agentcore.get_gateway_target(
        gatewayIdentifier=GATEWAY_ID,
        targetId=TARGET_ID,
    )
    lambda_arn = current["targetConfiguration"]["mcp"]["lambda"]["lambdaArn"]
    agentcore.update_gateway_target(
        gatewayIdentifier=GATEWAY_ID,
        targetId=TARGET_ID,
        name=TARGET_NAME,
        description="Lambda: ah-analytics-s3tables-mcp — 4 read-only S3 Tables tools for AH and NUH",
        credentialProviderConfigurations=current["credentialProviderConfigurations"],
        targetConfiguration={
            "mcp": {
                "lambda": {
                    "lambdaArn": lambda_arn,
                    "toolSchema": {"inlinePayload": TOOL_SCHEMA},
                }
            }
        },
    )
    for _ in range(30):
        target = agentcore.get_gateway_target(
            gatewayIdentifier=GATEWAY_ID,
            targetId=TARGET_ID,
        )
        if target["status"] == "READY":
            return
        if target["status"] == "FAILED":
            raise RuntimeError(f"Gateway target update failed: {target}")
        time.sleep(2)
    raise TimeoutError("Gateway target did not become READY within 60 seconds")


def grant_code_interpreter_export_read(iam) -> None:
    document = iam.get_role_policy(
        RoleName=CODE_INTERPRETER_ROLE,
        PolicyName=CODE_INTERPRETER_POLICY,
    )["PolicyDocument"]
    statements = document.setdefault("Statement", [])
    statement_id = "ReadAthenaQueryExports"
    statements[:] = [statement for statement in statements if statement.get("Sid") != statement_id]
    statements.append(
        {
            "Sid": statement_id,
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": QUERY_EXPORT_RESOURCE,
        }
    )
    iam.put_role_policy(
        RoleName=CODE_INTERPRETER_ROLE,
        PolicyName=CODE_INTERPRETER_POLICY,
        PolicyDocument=json.dumps(document),
    )


def main() -> None:
    lambda_client = boto3.client("lambda", region_name=REGION)
    agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)
    iam = boto3.client("iam")

    update_lambda_code(lambda_client)
    update_gateway_target(agentcore)
    grant_code_interpreter_export_read(iam)
    print("Updated shared S3 Tables export capability for the GPT pilot.")


if __name__ == "__main__":
    main()
