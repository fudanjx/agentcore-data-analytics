"""
Deploy the S3-event-triggered S3 Tables loader Lambda.

Creates / updates:
  1. ECR repo                                   : ah-analytics-s3tables-loader
  2. Container image build + push               : x86_64 (docker buildx)
  3. IAM role                                   : ah-analytics-s3tables-loader-role
  4. Lambda function (container image)          : ah-analytics-s3tables-loader
       memory 10240 MB, timeout 900 s, /tmp 4096 MB
  5. Lambda permission for S3 bucket to invoke  : ah-data-analytics
  6. S3 bucket notification config              : ObjectCreated → this Lambda,
                                                   filtered to Combined_*_encoded.parquet.gzip

Idempotent. Assumes ah_s3tables_bootstrap.py has already been run
(table bucket + namespace exist).

Usage:
    python lambda_s3tables_loader/deploy.py
"""

import base64
import json
import os
import subprocess
import time

import boto3
import botocore.exceptions

REGION = "ap-southeast-1"
ACCOUNT_ID = "964340114883"

REPO_NAME = "ah-analytics-s3tables-loader"
IMAGE_TAG = "latest"
FUNCTION_NAME = "ah-analytics-s3tables-loader"
ROLE_NAME = "ah-analytics-s3tables-loader-role"

SOURCE_BUCKET = "ah-data-analytics"
TABLE_BUCKET_ARN = f"arn:aws:s3tables:{REGION}:{ACCOUNT_ID}:bucket/ah-analytics"
NAMESPACE = "ah_analytics"

REPO_URI = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{REPO_NAME}"
IMAGE_URI = f"{REPO_URI}:{IMAGE_TAG}"

# Files that should trigger the loader
TRIGGER_KEYS = [
    "Combined_SOC_encoded.parquet.gzip",
    "Combined_UCC_encoded.parquet.gzip",
    "Combined_adm_encoded.parquet.gzip",
    "Combined_disch_encoded.parquet.gzip",
    "Combined_inflight_encoded.parquet.gzip",
    "Combined_procedure_encoded.parquet.gzip",
]

ecr = boto3.client("ecr", region_name=REGION)
iam = boto3.client("iam")
lambda_client = boto3.client("lambda", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# ECR
# ---------------------------------------------------------------------------

def ensure_repo() -> str:
    try:
        ecr.describe_repositories(repositoryNames=[REPO_NAME])
        print(f"  ECR repo exists: {REPO_URI}")
    except ecr.exceptions.RepositoryNotFoundException:
        print(f"  Creating ECR repo {REPO_NAME}...")
        ecr.create_repository(repositoryName=REPO_NAME, imageScanningConfiguration={"scanOnPush": True})
    return REPO_URI


def docker_login():
    tok = ecr.get_authorization_token()["authorizationData"][0]
    user_pw = base64.b64decode(tok["authorizationToken"]).decode()
    _, pw = user_pw.split(":", 1)
    endpoint = tok["proxyEndpoint"]
    subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", endpoint],
        input=pw, text=True, check=True, capture_output=True,
    )


def build_and_push():
    print("  Docker login to ECR...")
    docker_login()
    dockerfile = os.path.join(REPO_ROOT, "lambda_s3tables_loader", "Dockerfile")
    print(f"  Building image (x86_64) → {IMAGE_URI} ...")
    subprocess.check_call([
        "docker", "buildx", "build",
        "--platform", "linux/amd64",
        "-f", dockerfile,
        "-t", IMAGE_URI,
        "--provenance=false",
        "--push",
        REPO_ROOT,
    ])
    print("  Image pushed")


# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

def ensure_role() -> str:
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
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": "arn:aws:logs:*:*:*",
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": f"arn:aws:s3:::{SOURCE_BUCKET}/*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3tables:GetTableBucket",
                    "s3tables:GetNamespace",
                    "s3tables:CreateNamespace",
                    "s3tables:ListNamespaces",
                    "s3tables:CreateTable",
                    "s3tables:GetTable",
                    "s3tables:ListTables",
                    "s3tables:GetTableMetadataLocation",
                    "s3tables:UpdateTableMetadataLocation",
                    "s3tables:GetTableData",
                    "s3tables:PutTableData",
                ],
                "Resource": [
                    TABLE_BUCKET_ARN,
                    f"{TABLE_BUCKET_ARN}/*",
                ],
            },
        ],
    }
    try:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        print(f"  Role exists: {arn}")
    except iam.exceptions.NoSuchEntityException:
        print(f"  Creating role {ROLE_NAME}...")
        arn = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Execution role for ah-analytics-s3tables-loader Lambda",
        )["Role"]["Arn"]
        print("  Waiting for role propagation...")
        time.sleep(15)

    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName=f"{ROLE_NAME}-inline",
        PolicyDocument=json.dumps(inline),
    )
    return arn


# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------

def ensure_lambda(role_arn: str) -> str:
    env = {"Variables": {
        "TABLE_BUCKET_ARN": TABLE_BUCKET_ARN,
        "NAMESPACE": NAMESPACE,
    }}
    ephemeral = {"Size": 4096}

    try:
        fn = lambda_client.get_function(FunctionName=FUNCTION_NAME)
        fn_arn = fn["Configuration"]["FunctionArn"]
        print(f"  Updating Lambda {FUNCTION_NAME}...")
        lambda_client.update_function_code(FunctionName=FUNCTION_NAME, ImageUri=IMAGE_URI)
        lambda_client.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)
        lambda_client.update_function_configuration(
            FunctionName=FUNCTION_NAME,
            Role=role_arn,
            Environment=env,
            Timeout=900,
            MemorySize=10240,
            EphemeralStorage=ephemeral,
        )
    except lambda_client.exceptions.ResourceNotFoundException:
        print(f"  Creating Lambda {FUNCTION_NAME}...")
        fn_arn = lambda_client.create_function(
            FunctionName=FUNCTION_NAME,
            PackageType="Image",
            Code={"ImageUri": IMAGE_URI},
            Role=role_arn,
            Environment=env,
            Timeout=900,
            MemorySize=10240,
            EphemeralStorage=ephemeral,
            Architectures=["x86_64"],
            Description="S3-event-triggered loader: AH parquet → S3 Tables (Iceberg)",
        )["FunctionArn"]

    lambda_client.get_waiter("function_active_v2").wait(FunctionName=FUNCTION_NAME)
    print(f"  Lambda ready: {fn_arn}")
    return fn_arn


def grant_s3_invoke(fn_arn: str):
    """Allow the source S3 bucket to invoke this Lambda."""
    try:
        lambda_client.add_permission(
            FunctionName=FUNCTION_NAME,
            StatementId=f"s3-{SOURCE_BUCKET}-invoke",
            Action="lambda:InvokeFunction",
            Principal="s3.amazonaws.com",
            SourceArn=f"arn:aws:s3:::{SOURCE_BUCKET}",
            SourceAccount=ACCOUNT_ID,
        )
        print("  S3 invoke permission granted")
    except lambda_client.exceptions.ResourceConflictException:
        print("  S3 invoke permission already exists")


def configure_s3_notifications(fn_arn: str):
    """Register one LambdaFunctionConfiguration per source file (suffix filter).

    Merges with any existing notifications on the bucket rather than replacing.
    """
    existing = s3.get_bucket_notification_configuration(Bucket=SOURCE_BUCKET)
    lambda_configs = existing.get("LambdaFunctionConfigurations", [])
    kept = [c for c in lambda_configs if c.get("LambdaFunctionArn") != fn_arn]

    for key in TRIGGER_KEYS:
        kept.append({
            "Id": f"ah-s3tables-{key}",
            "LambdaFunctionArn": fn_arn,
            "Events": ["s3:ObjectCreated:*"],
            "Filter": {
                "Key": {
                    "FilterRules": [{"Name": "prefix", "Value": key}],
                }
            },
        })

    config = {"LambdaFunctionConfigurations": kept}
    for k in ("TopicConfigurations", "QueueConfigurations", "EventBridgeConfiguration"):
        if k in existing:
            config[k] = existing[k]

    s3.put_bucket_notification_configuration(
        Bucket=SOURCE_BUCKET,
        NotificationConfiguration=config,
    )
    print(f"  S3 notifications configured on '{SOURCE_BUCKET}' → {FUNCTION_NAME}")


# ---------------------------------------------------------------------------

def main():
    print(f"Deploying {FUNCTION_NAME} in {REGION}\n")

    print("1. Ensuring ECR repo...")
    ensure_repo()

    print("\n2. Building and pushing container image...")
    build_and_push()

    print("\n3. Ensuring IAM role...")
    role_arn = ensure_role()
    time.sleep(5)

    print("\n4. Creating/updating Lambda...")
    fn_arn = ensure_lambda(role_arn)

    print("\n5. Granting S3 invoke permission...")
    grant_s3_invoke(fn_arn)

    print("\n6. Configuring S3 notifications...")
    configure_s3_notifications(fn_arn)

    print("\nDone.")
    print(f"\n  Lambda ARN : {fn_arn}")
    print(f"\nTest:")
    print(f"  aws s3 cp s3://{SOURCE_BUCKET}/Combined_SOC_encoded.parquet.gzip \\")
    print(f"           s3://{SOURCE_BUCKET}/Combined_SOC_encoded.parquet.gzip --metadata-directive REPLACE")
    print(f"  aws logs tail /aws/lambda/{FUNCTION_NAME} --follow")


if __name__ == "__main__":
    main()
