"""Create the AgentCore Code Interpreter used for geospatial visualisation.

AgentCore Code Interpreter definitions currently do not accept a custom image
or Python dependency layer.  This script creates a separate interpreter with a
dedicated execution role; the AgentCore runtime then installs GeoPandas and
Folium once at the start of each session through the optional
``CODE_INTERPRETER_BOOTSTRAP_PACKAGES`` setting.

The default PUBLIC network mode is intentional: pip must reach the package
index during session bootstrap.  Use a private package mirror and VPC mode if
the deployment cannot allow public egress.

Usage::

    python infra/geospatial_code_interpreter_bootstrap.py

The script is idempotent and prints the interpreter ID and environment values
needed by ``infra/deploy.py``. Set ``GEO_CODE_INTERPRETER_EXECUTION_ROLE_ARN``
to reuse an existing execution role (for example, the role used by the
standard coder interpreter) instead of creating the dedicated role.
"""

from __future__ import annotations

import json
import os
import time

import boto3
import botocore.exceptions


REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
CI_NAME = os.environ.get("GEO_CODE_INTERPRETER_NAME", "agentcore_geo_maps_ci")
ROLE_NAME = os.environ.get(
    "GEO_CODE_INTERPRETER_ROLE_NAME", "agentcore-geo-code-interpreter-role"
)
EXECUTION_ROLE_ARN = os.environ.get("GEO_CODE_INTERPRETER_EXECUTION_ROLE_ARN", "").strip()
NETWORK_MODE = os.environ.get("GEO_CODE_INTERPRETER_NETWORK_MODE", "PUBLIC").upper()
if NETWORK_MODE not in {"PUBLIC", "SANDBOX", "VPC"}:
    raise ValueError("GEO_CODE_INTERPRETER_NETWORK_MODE must be PUBLIC, SANDBOX, or VPC")

agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)
iam = boto3.client("iam")
sts = boto3.client("sts", region_name=REGION)


def _account_id() -> str:
    return sts.get_caller_identity()["Account"]


def _trust_policy(account_id: str, region: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account_id}:*"
                    },
                },
            }
        ],
    }


def ensure_execution_role() -> str:
    """Create or update the least-privilege role used by the interpreter."""
    account_id = _account_id()
    trust = _trust_policy(account_id, REGION)
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
        iam.update_assume_role_policy(
            RoleName=ROLE_NAME, PolicyDocument=json.dumps(trust)
        )
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Execution role for AgentCore geospatial Code Interpreter",
        )["Role"]
        time.sleep(10)

    # Inline policy is deliberately limited to logging.  Inline file uploads
    # do not require S3 permissions.  Add optional read-only S3 access when a
    # bucket/prefix is explicitly supplied by the operator.
    statements = [
        {
            "Sid": "CodeInterpreterLogs",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
            ],
            "Resource": "arn:aws:logs:*:*:*",
        }
    ]
    bucket = os.environ.get("GEO_CODE_INTERPRETER_S3_BUCKET", "").strip()
    prefix = os.environ.get("GEO_CODE_INTERPRETER_S3_PREFIX", "").strip().strip("/")
    if bucket:
        object_arn = f"arn:aws:s3:::{bucket}/{prefix}/*" if prefix else f"arn:aws:s3:::{bucket}/*"
        statements.extend(
            [
                {
                    "Sid": "ListConfiguredDataPrefix",
                    "Effect": "Allow",
                    "Action": "s3:ListBucket",
                    "Resource": f"arn:aws:s3:::{bucket}",
                    "Condition": {"StringLike": {"s3:prefix": [prefix, f"{prefix}/*"] if prefix else ["*"]}},
                },
                {
                    "Sid": "ReadConfiguredDataPrefix",
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                    "Resource": object_arn,
                },
            ]
        )
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName=f"{ROLE_NAME}-inline",
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}),
    )
    return role["Arn"]


def _summary_id(summary: dict) -> str | None:
    return summary.get("codeInterpreterId") or summary.get("id")


def _summary_arn(summary: dict, interpreter_id: str) -> str:
    return summary.get("codeInterpreterArn") or summary.get("arn") or (
        f"arn:aws:bedrock-agentcore:{REGION}:{_account_id()}:"
        f"code-interpreter-custom/{interpreter_id}"
    )


def _wait_until_ready(interpreter_id: str) -> dict:
    for _ in range(72):
        result = agentcore.get_code_interpreter(codeInterpreterId=interpreter_id)
        status = result.get("status")
        if status == "READY":
            return result
        if status and "FAILED" in status:
            raise RuntimeError(
                f"Code Interpreter {interpreter_id} failed: "
                f"{result.get('failureReason') or status}"
            )
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for Code Interpreter {interpreter_id}")


def ensure_code_interpreter(role_arn: str) -> tuple[str, str]:
    paginator = agentcore.get_paginator("list_code_interpreters")
    for page in paginator.paginate():
        summaries = page.get("codeInterpreterSummaries", []) or page.get("items", [])
        for summary in summaries:
            if summary.get("name") != CI_NAME:
                continue
            interpreter_id = _summary_id(summary)
            if not interpreter_id:
                raise RuntimeError(f"Found {CI_NAME} without an interpreter ID")
            ready = _wait_until_ready(interpreter_id)
            existing_role = ready.get("executionRoleArn")
            if existing_role and existing_role != role_arn:
                raise RuntimeError(
                    f"Code Interpreter {CI_NAME} already uses {existing_role}; "
                    "AgentCore does not support updating executionRoleArn. "
                    "Delete this interpreter and rerun with the desired role."
                )
            return interpreter_id, _summary_arn(ready, interpreter_id)

    response = agentcore.create_code_interpreter(
        name=CI_NAME,
        description=(
            "Geospatial analysis and Singapore map visualisation with "
            "GeoPandas and Folium session bootstrap"
        ),
        executionRoleArn=role_arn,
        networkConfiguration={"networkMode": NETWORK_MODE},
        # Client-token syntax permits alphanumeric characters and hyphens, but
        # not the underscores allowed in Code Interpreter names.
        clientToken=f"geospatial-{CI_NAME.replace('_', '-')}-{int(time.time())}",
        tags={"Purpose": "geospatial-visualisation", "ManagedBy": "AgentCore"},
    )
    interpreter_id = response["codeInterpreterId"]
    ready = _wait_until_ready(interpreter_id)
    return interpreter_id, _summary_arn(ready, interpreter_id)


def main() -> None:
    role_arn = EXECUTION_ROLE_ARN or ensure_execution_role()
    if EXECUTION_ROLE_ARN:
        print(f"Using existing execution role: {EXECUTION_ROLE_ARN}")
    interpreter_id, interpreter_arn = ensure_code_interpreter(role_arn)
    print(f"Geospatial Code Interpreter ready: {interpreter_id}")
    print(f"Code Interpreter ARN: {interpreter_arn}")
    print("Set these runtime values before deploying the AgentCore Runtime:")
    print(f"CODE_INTERPRETER_ID={interpreter_id}")
    print("CODE_INTERPRETER_BOOTSTRAP_PACKAGES=geopandas==1.1.4 folium==0.20.0")


if __name__ == "__main__":
    main()
