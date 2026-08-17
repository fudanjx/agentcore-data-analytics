"""Deploy the manually invoked NUH Parquet -> S3 Tables migration Lambda.

No event source is configured. The function is invoked once per table only
after its read-only inspection result has been reviewed.
"""

import base64
import json
import os
import subprocess
import time

import boto3

REGION = "ap-southeast-1"
ACCOUNT_ID = "964340114883"
REPO_NAME = "nuh-s3tables-migration"
FUNCTION_NAME = "nuh-s3tables-migration"
ROLE_NAME = "nuh-s3tables-migration-role"
SOURCE_BUCKET = "nuh-analytics"
TABLE_BUCKET_ARN = f"arn:aws:s3tables:{REGION}:{ACCOUNT_ID}:bucket/nuh-analytics"
NAMESPACE = "nuh"
REPO_URI = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{REPO_NAME}"
IMAGE_URI = f"{REPO_URI}:v1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

ecr = boto3.client("ecr", region_name=REGION)
iam = boto3.client("iam")
lambda_client = boto3.client("lambda", region_name=REGION)


def ensure_repo():
    try:
        ecr.describe_repositories(repositoryNames=[REPO_NAME])
    except ecr.exceptions.RepositoryNotFoundException:
        ecr.create_repository(repositoryName=REPO_NAME, imageScanningConfiguration={"scanOnPush": True})


def build_and_push():
    token = ecr.get_authorization_token()["authorizationData"][0]
    _, password = base64.b64decode(token["authorizationToken"]).decode().split(":", 1)
    subprocess.run(["docker", "login", "--username", "AWS", "--password-stdin", token["proxyEndpoint"]], input=password, text=True, check=True)
    subprocess.check_call([
        "docker", "buildx", "build", "--platform", "linux/amd64",
        "--provenance=false", "--push", "-f", os.path.join(ROOT, "nuh_s3tables_migration", "Dockerfile"),
        "-t", IMAGE_URI, ROOT,
    ])


def ensure_role():
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "arn:aws:logs:*:*:*"},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{SOURCE_BUCKET}/em_encoded.parquet.gzip"},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{SOURCE_BUCKET}/in_encoded.parquet.gzip"},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{SOURCE_BUCKET}/sc_encoded.parquet.gzip"},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{SOURCE_BUCKET}/su_encoded.parquet.gzip"},
        {"Effect": "Allow", "Action": ["s3tables:GetTableBucket", "s3tables:GetNamespace", "s3tables:CreateNamespace", "s3tables:ListNamespaces", "s3tables:CreateTable", "s3tables:GetTable", "s3tables:ListTables", "s3tables:GetTableMetadataLocation", "s3tables:UpdateTableMetadataLocation", "s3tables:GetTableData", "s3tables:PutTableData"], "Resource": [TABLE_BUCKET_ARN, f"{TABLE_BUCKET_ARN}/*"]},
    ]}
    try:
        role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        role_arn = iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust), Description="One-time NUH S3 Tables migration") ["Role"]["Arn"]
        time.sleep(15)
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName=f"{ROLE_NAME}-inline", PolicyDocument=json.dumps(policy))
    return role_arn


def ensure_function(role_arn):
    environment = {"Variables": {"SOURCE_BUCKET": SOURCE_BUCKET, "TABLE_BUCKET_ARN": TABLE_BUCKET_ARN, "NAMESPACE": NAMESPACE}}
    try:
        lambda_client.get_function(FunctionName=FUNCTION_NAME)
        lambda_client.update_function_code(FunctionName=FUNCTION_NAME, ImageUri=IMAGE_URI)
        lambda_client.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)
        lambda_client.update_function_configuration(FunctionName=FUNCTION_NAME, Role=role_arn, Environment=environment, Timeout=900, MemorySize=10240, EphemeralStorage={"Size": 4096})
    except lambda_client.exceptions.ResourceNotFoundException:
        lambda_client.create_function(FunctionName=FUNCTION_NAME, PackageType="Image", Code={"ImageUri": IMAGE_URI}, Role=role_arn, Environment=environment, Timeout=900, MemorySize=10240, EphemeralStorage={"Size": 4096}, Architectures=["x86_64"], Description="Manual one-time NUH Parquet to S3 Tables migration")
    lambda_client.get_waiter("function_active_v2").wait(FunctionName=FUNCTION_NAME)


if __name__ == "__main__":
    ensure_repo()
    build_and_push()
    ensure_function(ensure_role())
    print(f"Deployed {FUNCTION_NAME}; run action=inspect before action=migrate.")
