#!/usr/bin/env python3
"""Grant the Strands Code Interpreter least-privilege Insights S3 access.

The Strands runtime uses ``code_interpreter_runtime_dev-PEpoCecsBL``. Its
execution role must read OpenWebUI uploads and write tagged generated artifacts
under the same user/chat-scoped Insights prefix used by the proxy.

This script is idempotent: it only updates the dedicated inline policy on the
Code Interpreter execution role and does not alter the Office interpreter role.
"""

from __future__ import annotations

import json

import boto3


REGION = "ap-southeast-1"
ACCOUNT_ID = "964340114883"
CODE_INTERPRETER_ID = "code_interpreter_runtime_dev-PEpoCecsBL"
BUCKET = f"agentcore-openwebui-insights-{ACCOUNT_ID}"
PREFIX = "openwebui-insights/"
OUTPUT_PREFIX = f"{PREFIX}outputs/"
POLICY_NAME = "StrandsInsightsS3Access"


def policy_document() -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ListInsightsObjects",
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": f"arn:aws:s3:::{BUCKET}",
                "Condition": {
                    "StringLike": {"s3:prefix": [PREFIX, f"{PREFIX}*"]}
                },
            },
            {
                "Sid": "ReadInsightsInputsAndOutputs",
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:GetObjectTagging",
                ],
                "Resource": f"arn:aws:s3:::{BUCKET}/{PREFIX}*",
            },
            {
                "Sid": "WriteTaggedInsightsArtifactsOnly",
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:PutObjectTagging",
                    "s3:AbortMultipartUpload",
                    "s3:ListMultipartUploadParts",
                ],
                "Resource": f"arn:aws:s3:::{BUCKET}/{OUTPUT_PREFIX}*",
                "Condition": {
                    "Null": {
                        "s3:RequestObjectTag/OpenWebUI-User-Id": "false",
                        "s3:RequestObjectTag/OpenWebUI-Chat-Id": "false",
                        "s3:RequestObjectTag/AgentCore-Artifact": "false",
                    }
                },
            },
        ],
    }


def main() -> None:
    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    iam = boto3.client("iam", region_name=REGION)
    interpreter = control.get_code_interpreter(
        codeInterpreterId=CODE_INTERPRETER_ID
    )
    if interpreter.get("status") != "READY":
        raise RuntimeError(
            f"Code Interpreter {CODE_INTERPRETER_ID} is not READY: "
            f"{interpreter.get('status')}"
        )
    role_arn = str(interpreter.get("executionRoleArn") or "")
    if not role_arn:
        raise RuntimeError("Code Interpreter has no execution role")
    role_name = role_arn.rsplit("/", 1)[-1]
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=POLICY_NAME,
        PolicyDocument=json.dumps(policy_document()),
    )
    print(f"role={role_arn}")
    print(f"policy={POLICY_NAME}")
    print(f"read_prefix=s3://{BUCKET}/{PREFIX}")
    print(f"write_prefix=s3://{BUCKET}/{OUTPUT_PREFIX}")


if __name__ == "__main__":
    main()
