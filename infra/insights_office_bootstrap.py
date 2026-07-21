"""Provision the isolated Office Harness while preserving Insights memory.

This is deliberately additive except for one controlled operation: the
existing Insights Harness's managed Memory is disassociated and immediately
reattached as an explicit BYO AgentCore Memory resource.  This preserves the
same Memory ARN and lets the existing and Office Harnesses share it.

The Office Code Interpreter has a distinct execution role.  It can read the
existing Insights upload prefix but can write only generated artifacts below
``openwebui-insights/outputs/``.  The proxy verifies the user/chat prefix and
S3 tags before an artifact is exposed through OpenWebUI.

The script is idempotent and is safe to rerun after an interrupted provision.
"""

from __future__ import annotations

import copy
import json
import time

import boto3
import botocore.exceptions


REGION = "ap-southeast-1"
ACCOUNT_ID = "964340114883"
BUCKET = f"agentcore-openwebui-insights-{ACCOUNT_ID}"
PREFIX = "openwebui-insights/"
OUTPUT_PREFIX = f"{PREFIX}outputs/"

SOURCE_HARNESS_ID = "harness_e52fs-Du2DM0RxvF"
SOURCE_MEMORY_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:"
    "memory/harness_harness_e52fs_8d3d-vtE3DJC9ia"
)
OFFICE_HARNESS_NAME = "harness_insights_office"
OFFICE_CI_NAME = "agentcore_insights_office_ci"
OFFICE_CI_ROLE_NAME = "AgentCoreCodeInterpreterS3Role"
HARNESS_CI_POLICY_NAME = "code-interpreter-invoke"

agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)
iam = boto3.client("iam")


def wait_for_harness(harness_id: str) -> dict:
    """Wait for a Harness update/create and return its ready configuration."""
    for _ in range(72):
        harness = agentcore.get_harness(harnessId=harness_id)["harness"]
        status = harness.get("status")
        if status == "READY":
            return harness
        if status and "FAILED" in status:
            raise RuntimeError(f"Harness {harness_id} failed: {status}")
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for Harness {harness_id}")


def wait_for_code_interpreter(code_interpreter_id: str) -> dict:
    for _ in range(72):
        code_interpreter = agentcore.get_code_interpreter(
            codeInterpreterId=code_interpreter_id
        )
        status = code_interpreter.get("status")
        if status == "READY":
            return code_interpreter
        if status and "FAILED" in status:
            raise RuntimeError(
                f"Code Interpreter {code_interpreter_id} failed: {status}"
            )
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for Code Interpreter {code_interpreter_id}")


def ensure_office_ci_role() -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:*"
                        )
                    },
                },
            }
        ],
    }
    try:
        role = iam.get_role(RoleName=OFFICE_CI_ROLE_NAME)["Role"]
        iam.update_assume_role_policy(
            RoleName=OFFICE_CI_ROLE_NAME, PolicyDocument=json.dumps(trust)
        )
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            RoleName=OFFICE_CI_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=(
                "AgentCore Office Code Interpreter: read Insights uploads and "
                "write validated generated artifacts only"
            ),
        )["Role"]
        # IAM can take a few seconds to make a freshly-created role assumable.
        time.sleep(15)

    policy = {
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
                "Sid": "WriteTaggedOfficeArtifactsOnly",
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
            {
                "Sid": "CodeInterpreterLogs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": "arn:aws:logs:*:*:*",
            },
        ],
    }
    iam.put_role_policy(
        RoleName=OFFICE_CI_ROLE_NAME,
        PolicyName="InsightsOfficeS3Access",
        PolicyDocument=json.dumps(policy),
    )
    return role["Arn"]


def ensure_office_code_interpreter(role_arn: str) -> str:
    paginator = agentcore.get_paginator("list_code_interpreters")
    for page in paginator.paginate():
        for summary in page.get("codeInterpreterSummaries", []):
            if summary.get("name") == OFFICE_CI_NAME:
                ready = wait_for_code_interpreter(summary["codeInterpreterId"])
                return ready["codeInterpreterArn"]

    created = agentcore.create_code_interpreter(
        name=OFFICE_CI_NAME,
        description=(
            "Office artifact sandbox: read Insights uploads and write tagged "
            "DOCX/XLSX/PPTX/PDF/CSV outputs"
        ),
        executionRoleArn=role_arn,
        networkConfiguration={"networkMode": "SANDBOX"},
    )
    ready = wait_for_code_interpreter(created["codeInterpreterId"])
    return ready["codeInterpreterArn"]


def ensure_shared_memory(source_harness: dict) -> dict:
    """Convert managed memory to BYO and keep it on the existing Harness."""
    memory = agentcore.get_memory(
        memoryId=SOURCE_MEMORY_ARN.rsplit("/", 1)[-1]
    )["memory"]
    if memory.get("arn") != SOURCE_MEMORY_ARN or memory.get("status") != "ACTIVE":
        raise RuntimeError("The expected source AgentCore Memory is not active")

    # Reapplying the same explicit ARN is harmless and keeps reruns idempotent.
    agentcore.update_harness(
        harnessId=source_harness["harnessId"],
        memory={
            "optionalValue": {
                "agentCoreMemoryConfiguration": {"arn": SOURCE_MEMORY_ARN}
            }
        },
    )
    return wait_for_harness(source_harness["harnessId"])


def _office_system_prompt(source_prompt: list[dict]) -> list[dict]:
    prompt = copy.deepcopy(source_prompt or [])
    prompt.append(
        {
            "text": """

Office artifact delivery
When the user asks for a file to be created, preserve all original uploaded
files and create a new output in a modern business format: DOCX, XLSX, PPTX,
PDF, or CSV. Use Code Interpreter for the creation. The proxy supplies a
trusted per-request output location and exact tagging instructions as a system
message. Follow those instructions exactly. Do not overwrite uploaded files.

After successfully uploading every generated file, end the response with one
and only one marker, on its own lines, containing a JSON array. Include every
generated output exactly once:
<agentcore-artifacts>
[{"s3_uri":"s3://...","filename":"report.xlsx"}]
</agentcore-artifacts>

Do not put raw S3 locations anywhere else in the response. The application
will replace the marker with authenticated download links. If no file was
created, do not emit the marker.
""".strip()
        }
    )
    return prompt


def _office_environment(source: dict) -> dict | None:
    runtime = (
        source.get("environment", {})
        .get("agentCoreRuntimeEnvironment", {})
    )
    if not runtime:
        return None
    copied: dict = {}
    for name in (
        "lifecycleConfiguration",
        "networkConfiguration",
        "filesystemConfigurations",
    ):
        if name in runtime:
            copied[name] = copy.deepcopy(runtime[name])
    return {"agentCoreRuntimeEnvironment": copied} if copied else None


def _office_tools(source_tools: list[dict], office_ci_arn: str) -> list[dict]:
    tools = [
        copy.deepcopy(tool)
        for tool in (source_tools or [])
        if tool.get("type") != "agentcore_code_interpreter"
    ]
    tools.append(
        {
            "type": "agentcore_code_interpreter",
            "name": OFFICE_CI_NAME,
            "config": {
                "agentCoreCodeInterpreter": {"codeInterpreterArn": office_ci_arn}
            },
        }
    )
    return tools


def grant_harness_invoke_ci(harness_role_arn: str, office_ci_arn: str) -> None:
    """Extend, rather than replace, the current CI invocation policy."""
    role_name = harness_role_arn.rsplit("/", 1)[-1]
    try:
        document = iam.get_role_policy(
            RoleName=role_name, PolicyName=HARNESS_CI_POLICY_NAME
        )["PolicyDocument"]
    except iam.exceptions.NoSuchEntityException:
        document = {"Version": "2012-10-17", "Statement": []}

    actions = [
        "bedrock-agentcore:StartCodeInterpreterSession",
        "bedrock-agentcore:InvokeCodeInterpreter",
        "bedrock-agentcore:StopCodeInterpreterSession",
        "bedrock-agentcore:GetCodeInterpreterSession",
        "bedrock-agentcore:ListCodeInterpreterSessions",
    ]
    target = next(
        (
            statement
            for statement in document.get("Statement", [])
            if statement.get("Effect") == "Allow"
            and set(actions).issubset(set(statement.get("Action", [])))
        ),
        None,
    )
    if target is None:
        target = {"Effect": "Allow", "Action": actions, "Resource": []}
        document.setdefault("Statement", []).append(target)
    resources = target.get("Resource", [])
    if isinstance(resources, str):
        resources = [resources]
    for resource in (office_ci_arn, f"{office_ci_arn}/*"):
        if resource not in resources:
            resources.append(resource)
    target["Resource"] = resources
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=HARNESS_CI_POLICY_NAME,
        PolicyDocument=json.dumps(document),
    )


def find_office_harness() -> dict | None:
    paginator = agentcore.get_paginator("list_harnesses")
    for page in paginator.paginate():
        for summary in page.get("harnesses", []):
            if summary.get("harnessName") == OFFICE_HARNESS_NAME:
                return wait_for_harness(summary["harnessId"])
    return None


def ensure_office_harness(source: dict, office_ci_arn: str) -> dict:
    existing = find_office_harness()
    if existing:
        return existing

    request = {
        "harnessName": OFFICE_HARNESS_NAME,
        "executionRoleArn": source["executionRoleArn"],
        "model": copy.deepcopy(source.get("model")),
        "systemPrompt": _office_system_prompt(source.get("systemPrompt", [])),
        "tools": _office_tools(source.get("tools", []), office_ci_arn),
        "skills": copy.deepcopy(source.get("skills", [])),
        "allowedTools": copy.deepcopy(source.get("allowedTools", [])),
        "memory": {
            "agentCoreMemoryConfiguration": {"arn": SOURCE_MEMORY_ARN}
        },
        "truncation": copy.deepcopy(source.get("truncation")),
        "maxIterations": source.get("maxIterations"),
        "timeoutSeconds": source.get("timeoutSeconds"),
        "tags": {"Purpose": "OpenWebUIInsightsOffice"},
    }
    environment = _office_environment(source)
    if environment:
        request["environment"] = environment
    request = {key: value for key, value in request.items() if value is not None}
    created = agentcore.create_harness(**request)["harness"]
    return wait_for_harness(created["harnessId"])


def main() -> None:
    source = agentcore.get_harness(harnessId=SOURCE_HARNESS_ID)["harness"]
    if source.get("status") != "READY":
        raise RuntimeError(f"Source Harness is not ready: {source.get('status')}")

    print("1. Converting existing managed Memory to explicit shared BYO Memory...")
    source = ensure_shared_memory(source)
    print(f"   shared_memory={SOURCE_MEMORY_ARN}")

    print("2. Ensuring Office Code Interpreter role and sandbox...")
    office_role_arn = ensure_office_ci_role()
    office_ci_arn = ensure_office_code_interpreter(office_role_arn)
    print(f"   office_ci={office_ci_arn}")

    print("3. Granting the shared Harness execution role access to the Office sandbox...")
    grant_harness_invoke_ci(source["executionRoleArn"], office_ci_arn)

    print("4. Creating the Office Harness with copied tools, skills, limits, and shared memory...")
    office = ensure_office_harness(source, office_ci_arn)
    print(f"   office_harness={office['arn']}")
    print("Done.")


if __name__ == "__main__":
    main()
