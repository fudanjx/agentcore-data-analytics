"""
One-shot bootstrap for user file uploads + Code Interpreter analysis.

Idempotent — safe to re-run.

Creates / ensures:
  1. S3 bucket           : agentcore-user-uploads-964340114883
                           layout uploads/{actor_id}/{conversation_id}/{filename}
                           lifecycle: delete under uploads/* after 24h
                           block public access
  2. IAM role            : agentcore-code-interpreter-role
                           allows s3:GetObject on uploads/* (all actors — isolation
                           is enforced at the proxy layer by prefix-check when
                           agents receive the S3 URI to fetch)
  3. Code Interpreter    : agentcore-user-uploads-ci (SANDBOX network, S3-only)
  4. Harness tool attach : add agentcore_code_interpreter to harness_e52fs
                           and harness_dify (append; other tools preserved)
  5. Harness role grant  : allow harness execution role to
                           bedrock-agentcore:InvokeCodeInterpreter on the CI ARN
  6. Proxy role grant    : allow agentcore-proxy-irsa to s3:PutObject on
                           the new uploads bucket, plus metadata/tag-only
                           validation of OpenWebUI's S3 objects

Usage:
    python infra/user_uploads_bootstrap.py
"""

import json
import time

import boto3
import botocore.exceptions

REGION = "ap-southeast-1"
ACCOUNT_ID = "964340114883"

UPLOADS_BUCKET = f"agentcore-user-uploads-{ACCOUNT_ID}"
UPLOADS_PREFIX = "uploads/"
OPENWEBUI_UPLOADS_BUCKET = f"agentcore-openwebui-test-{ACCOUNT_ID}"
OPENWEBUI_UPLOADS_PREFIX = "openwebui-test/"
LIFECYCLE_DAYS = 1  # delete after 24h

CI_NAME = "agentcore_user_uploads_ci"  # sandbox names: alnum + underscore only
CI_ROLE_NAME = "agentcore-code-interpreter-role"

HARNESSES = {
    "harness_e52fs": "harness_e52fs-Du2DM0RxvF",
    "harness_dify":  "harness_dify-LViqrsm86E",
}

PROXY_ROLE_NAME = "agentcore-proxy-irsa"
PROXY_UPLOADS_POLICY_NAME = "user-uploads-put"

# Harness execution roles — read from get_harness at runtime, but hardcoded here
# for reference and idempotent inline-policy naming.
HARNESS_CI_POLICY_NAME = "code-interpreter-invoke"


s3 = boto3.client("s3", region_name=REGION)
iam = boto3.client("iam")
agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)


# ---------------------------------------------------------------------------

def ensure_uploads_bucket():
    try:
        s3.head_bucket(Bucket=UPLOADS_BUCKET)
        print(f"  Bucket exists: s3://{UPLOADS_BUCKET}")
    except botocore.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise
        print(f"  Creating bucket s3://{UPLOADS_BUCKET} in {REGION}...")
        s3.create_bucket(
            Bucket=UPLOADS_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )

    # Block all public access
    s3.put_public_access_block(
        Bucket=UPLOADS_BUCKET,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
    )

    # Lifecycle: expire objects under uploads/ after N days
    s3.put_bucket_lifecycle_configuration(
        Bucket=UPLOADS_BUCKET,
        LifecycleConfiguration={
            "Rules": [{
                "ID": "expire-user-uploads",
                "Status": "Enabled",
                "Filter": {"Prefix": UPLOADS_PREFIX},
                "Expiration": {"Days": LIFECYCLE_DAYS},
            }],
        },
    )
    print(f"  Lifecycle set: delete objects under {UPLOADS_PREFIX}* after {LIFECYCLE_DAYS} day")


# ---------------------------------------------------------------------------

def ensure_ci_role() -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                "ArnLike": {
                    "aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:*"
                },
            },
        }],
    }
    inline = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow",
             "Action": ["s3:GetObject", "s3:ListBucket"],
             "Resource": [
                 f"arn:aws:s3:::{UPLOADS_BUCKET}",
                 f"arn:aws:s3:::{UPLOADS_BUCKET}/{UPLOADS_PREFIX}*",
             ]},
            {"Effect": "Allow",
             "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": "arn:aws:logs:*:*:*"},
        ],
    }
    try:
        arn = iam.get_role(RoleName=CI_ROLE_NAME)["Role"]["Arn"]
        print(f"  CI role exists: {arn}")
        # Keep trust policy in sync
        iam.update_assume_role_policy(RoleName=CI_ROLE_NAME, PolicyDocument=json.dumps(trust))
    except iam.exceptions.NoSuchEntityException:
        print(f"  Creating CI role {CI_ROLE_NAME}...")
        arn = iam.create_role(
            RoleName=CI_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Execution role for the user-uploads Code Interpreter sandbox",
        )["Role"]["Arn"]
        print("  Waiting for role propagation...")
        time.sleep(15)

    iam.put_role_policy(
        RoleName=CI_ROLE_NAME,
        PolicyName=f"{CI_ROLE_NAME}-inline",
        PolicyDocument=json.dumps(inline),
    )
    return arn


# ---------------------------------------------------------------------------

def ensure_code_interpreter(role_arn: str) -> str:
    """Create the shared Code Interpreter sandbox definition.

    Note: this is the sandbox *definition*; individual sessions are spun up
    on demand when the harness invokes the tool. One definition serves all
    harnesses and all users.
    """
    # Look for existing sandbox by name (idempotent)
    paginator = agentcore.get_paginator("list_code_interpreters")
    for page in paginator.paginate():
        for ci in page.get("items", []):
            if ci["name"] == CI_NAME:
                ci_arn = ci.get("codeInterpreterArn") or ci.get("arn")
                if not ci_arn:
                    # Fallback: build ARN from ID
                    ci_id = ci.get("codeInterpreterId")
                    ci_arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:code-interpreter-custom/{ci_id}"
                print(f"  Code Interpreter exists: {ci_arn}")
                return ci_arn

    print(f"  Creating Code Interpreter '{CI_NAME}' (SANDBOX network)...")
    resp = agentcore.create_code_interpreter(
        name=CI_NAME,
        description="User-uploads analysis sandbox — pandas/pypdf/python-docx/python-pptx pre-installed",
        executionRoleArn=role_arn,
        networkConfiguration={"networkMode": "SANDBOX"},
    )
    ci_arn = resp.get("codeInterpreterArn") or resp.get("arn")
    if not ci_arn:
        ci_id = resp.get("codeInterpreterId")
        ci_arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:code-interpreter-custom/{ci_id}"
    print(f"  Code Interpreter created: {ci_arn}")
    return ci_arn


# ---------------------------------------------------------------------------

def _existing_tool_arn(tools: list) -> str | None:
    for t in tools:
        if t.get("type") == "agentcore_code_interpreter":
            return t.get("config", {}).get("agentCoreCodeInterpreter", {}).get("codeInterpreterArn")
    return None


def attach_ci_to_harness(harness_id: str, ci_arn: str, name_hint: str):
    h = agentcore.get_harness(harnessId=harness_id)["harness"]
    tools = h.get("tools", []) or []

    if _existing_tool_arn(tools) == ci_arn:
        print(f"  [{name_hint}] Code Interpreter already attached — skip")
        return h.get("executionRoleArn")

    if _existing_tool_arn(tools):
        print(f"  [{name_hint}] Existing CI tool found with a different ARN — replacing")
        tools = [t for t in tools if t.get("type") != "agentcore_code_interpreter"]

    tools.append({
        "type": "agentcore_code_interpreter",
        "name": CI_NAME,
        "config": {
            "agentCoreCodeInterpreter": {"codeInterpreterArn": ci_arn},
        },
    })

    print(f"  [{name_hint}] Updating harness with {len(tools)} tools "
          f"(4 gateways + 1 code interpreter)...")
    agentcore.update_harness(harnessId=harness_id, tools=tools)

    # Poll until READY
    for _ in range(30):
        status = agentcore.get_harness(harnessId=harness_id)["harness"].get("status", "")
        if status == "READY":
            break
        if "FAILED" in status:
            raise RuntimeError(f"[{name_hint}] harness update FAILED: {status}")
        time.sleep(5)
    print(f"  [{name_hint}] Harness READY")

    return h.get("executionRoleArn")


# ---------------------------------------------------------------------------

def grant_harness_invoke_ci(role_arn: str, ci_arn: str, name_hint: str):
    """Attach an inline policy on the harness execution role that lets it
    call bedrock-agentcore:InvokeCodeInterpreter on our CI ARN."""
    if not role_arn:
        print(f"  [{name_hint}] no execution role ARN — skip")
        return
    role_name = role_arn.split("/")[-1]

    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:StartCodeInterpreterSession",
                "bedrock-agentcore:InvokeCodeInterpreter",
                "bedrock-agentcore:StopCodeInterpreterSession",
                "bedrock-agentcore:GetCodeInterpreterSession",
                "bedrock-agentcore:ListCodeInterpreterSessions",
            ],
            "Resource": [ci_arn, f"{ci_arn}/*"],
        }],
    }
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=HARNESS_CI_POLICY_NAME,
        PolicyDocument=json.dumps(policy),
    )
    print(f"  [{name_hint}] granted InvokeCodeInterpreter on role {role_name}")


# ---------------------------------------------------------------------------

def grant_proxy_upload():
    """Grant upload writes and metadata-only validation of OpenWebUI objects."""
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ManageProxyUploads",
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:AbortMultipartUpload",
                    "s3:ListBucketMultipartUploads",
                    "s3:ListMultipartUploadParts",
                    # for the actor-prefix verification hook
                    "s3:GetObject",
                ],
                "Resource": [
                    f"arn:aws:s3:::{UPLOADS_BUCKET}",
                    f"arn:aws:s3:::{UPLOADS_BUCKET}/{UPLOADS_PREFIX}*",
                ],
            },
            {
                "Sid": "ListOpenWebUIUploadMetadata",
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": f"arn:aws:s3:::{OPENWEBUI_UPLOADS_BUCKET}",
                "Condition": {
                    "StringLike": {
                        "s3:prefix": f"{OPENWEBUI_UPLOADS_PREFIX}*"
                    }
                },
            },
            {
                "Sid": "ReadOpenWebUIUploadTags",
                "Effect": "Allow",
                "Action": "s3:GetObjectTagging",
                "Resource": (
                    f"arn:aws:s3:::{OPENWEBUI_UPLOADS_BUCKET}/"
                    f"{OPENWEBUI_UPLOADS_PREFIX}*"
                ),
            },
        ],
    }
    iam.put_role_policy(
        RoleName=PROXY_ROLE_NAME,
        PolicyName=PROXY_UPLOADS_POLICY_NAME,
        PolicyDocument=json.dumps(policy),
    )
    print(f"  Proxy role granted s3:PutObject on {UPLOADS_BUCKET}/{UPLOADS_PREFIX}*")
    print(
        "  Proxy role granted metadata/tag validation on "
        f"{OPENWEBUI_UPLOADS_BUCKET}/{OPENWEBUI_UPLOADS_PREFIX}*"
    )


# ---------------------------------------------------------------------------

def main():
    print(f"Bootstrapping user uploads + Code Interpreter")
    print(f"  Account: {ACCOUNT_ID}  Region: {REGION}\n")

    print("1. Ensuring S3 uploads bucket + lifecycle...")
    ensure_uploads_bucket()

    print("\n2. Ensuring Code Interpreter execution role...")
    ci_role_arn = ensure_ci_role()

    print("\n3. Ensuring Code Interpreter sandbox...")
    ci_arn = ensure_code_interpreter(ci_role_arn)

    print("\n4. Attaching Code Interpreter to each harness...")
    for name_hint, hid in HARNESSES.items():
        harness_role_arn = attach_ci_to_harness(hid, ci_arn, name_hint)
        grant_harness_invoke_ci(harness_role_arn, ci_arn, name_hint)

    print("\n5. Extending proxy IRSA role...")
    grant_proxy_upload()

    print("\nDone.")
    print(f"\n  Uploads bucket : s3://{UPLOADS_BUCKET}/{UPLOADS_PREFIX}")
    print(f"  CI ARN         : {ci_arn}")
    print(f"  CI role        : {ci_role_arn}")


if __name__ == "__main__":
    main()
