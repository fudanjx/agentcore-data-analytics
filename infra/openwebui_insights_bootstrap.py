"""Create isolated S3 and IAM resources for insights.bot-alex.com."""

import json
import time

import boto3
import botocore.exceptions


REGION = "ap-southeast-1"
ACCOUNT_ID = "964340114883"
INSTANCE_ID = "i-06f7b81355b8c5346"
BUCKET = f"agentcore-openwebui-insights-{ACCOUNT_ID}"
PREFIX = "openwebui-insights/"
ROLE_NAME = "openwebui-insights-ec2"
PROFILE_NAME = ROLE_NAME
PROXY_ROLE_NAME = "agentcore-proxy-irsa"
CI_ROLE_NAME = "agentcore-code-interpreter-role"


s3 = boto3.client("s3", region_name=REGION)
iam = boto3.client("iam")
ec2 = boto3.client("ec2", region_name=REGION)


def ensure_bucket() -> None:
    try:
        s3.head_bucket(Bucket=BUCKET)
    except botocore.exceptions.ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        s3.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )

    s3.put_public_access_block(
        Bucket=BUCKET,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=BUCKET,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "AES256"
                    },
                    "BucketKeyEnabled": True,
                }
            ]
        },
    )
    s3.put_bucket_versioning(
        Bucket=BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )
    s3.put_bucket_lifecycle_configuration(
        Bucket=BUCKET,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-openwebui-insights",
                    "Status": "Enabled",
                    "Filter": {"Prefix": PREFIX},
                    "Expiration": {"Days": 7},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                }
            ]
        },
    )
    s3.put_bucket_policy(
        Bucket=BUCKET,
        Policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "DenyInsecureTransport",
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "s3:*",
                        "Resource": [
                            f"arn:aws:s3:::{BUCKET}",
                            f"arn:aws:s3:::{BUCKET}/*",
                        ],
                        "Condition": {
                            "Bool": {"aws:SecureTransport": "false"}
                        },
                    }
                ],
            }
        ),
    )


def ensure_instance_role() -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
        iam.update_assume_role_policy(
            RoleName=ROLE_NAME,
            PolicyDocument=json.dumps(trust),
        )
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="EC2 role for insights.bot-alex.com OpenWebUI",
        )["Role"]

    iam.attach_role_policy(
        RoleName=ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    )
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="OpenWebUIInsightsS3",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "BucketMetadata",
                        "Effect": "Allow",
                        "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
                        "Resource": f"arn:aws:s3:::{BUCKET}",
                        "Condition": {
                            "StringLike": {"s3:prefix": [PREFIX, f"{PREFIX}*"]}
                        },
                    },
                    {
                        "Sid": "ManageOwnedObjects",
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetObject",
                            "s3:PutObject",
                            "s3:DeleteObject",
                            "s3:GetObjectTagging",
                            "s3:PutObjectTagging",
                            "s3:AbortMultipartUpload",
                            "s3:ListMultipartUploadParts",
                        ],
                        "Resource": f"arn:aws:s3:::{BUCKET}/{PREFIX}*",
                    },
                ],
            }
        ),
    )
    return role["Arn"]


def ensure_instance_profile() -> None:
    try:
        iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)
    except iam.exceptions.NoSuchEntityException:
        iam.create_instance_profile(InstanceProfileName=PROFILE_NAME)

    profile = iam.get_instance_profile(
        InstanceProfileName=PROFILE_NAME
    )["InstanceProfile"]
    if not any(role["RoleName"] == ROLE_NAME for role in profile["Roles"]):
        iam.add_role_to_instance_profile(
            InstanceProfileName=PROFILE_NAME,
            RoleName=ROLE_NAME,
        )

    associations = ec2.describe_iam_instance_profile_associations(
        Filters=[{"Name": "instance-id", "Values": [INSTANCE_ID]}]
    )["IamInstanceProfileAssociations"]
    if not associations:
        for attempt in range(6):
            try:
                ec2.associate_iam_instance_profile(
                    IamInstanceProfile={"Name": PROFILE_NAME},
                    InstanceId=INSTANCE_ID,
                )
                break
            except botocore.exceptions.ClientError as error:
                if (
                    attempt == 5
                    or error.response.get("Error", {}).get("Code")
                    != "InvalidParameterValue"
                ):
                    raise
                time.sleep(10)
    elif associations[0]["IamInstanceProfile"]["Arn"].split("/")[-1] != PROFILE_NAME:
        raise RuntimeError(
            "Instance already has a different IAM profile; refusing replacement"
        )


def ensure_reader_policies() -> None:
    proxy_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ListInsightsMetadata",
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": f"arn:aws:s3:::{BUCKET}",
                "Condition": {
                    "StringLike": {"s3:prefix": [PREFIX, f"{PREFIX}*"]}
                },
            },
            {
                "Sid": "ReadInsightsTags",
                "Effect": "Allow",
                "Action": "s3:GetObjectTagging",
                "Resource": f"arn:aws:s3:::{BUCKET}/{PREFIX}*",
            },
        ],
    }
    iam.put_role_policy(
        RoleName=PROXY_ROLE_NAME,
        PolicyName="OpenWebUIInsightsValidation",
        PolicyDocument=json.dumps(proxy_policy),
    )

    ci_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ListInsightsPrefix",
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": f"arn:aws:s3:::{BUCKET}",
                "Condition": {
                    "StringLike": {"s3:prefix": [PREFIX, f"{PREFIX}*"]}
                },
            },
            {
                "Sid": "ReadInsightsObjects",
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:GetObjectTagging",
                ],
                "Resource": f"arn:aws:s3:::{BUCKET}/{PREFIX}*",
            },
        ],
    }
    iam.put_role_policy(
        RoleName=CI_ROLE_NAME,
        PolicyName="OpenWebUIInsightsS3Read",
        PolicyDocument=json.dumps(ci_policy),
    )


def main() -> None:
    ensure_bucket()
    ensure_instance_role()
    ensure_instance_profile()
    ensure_reader_policies()
    print(f"bucket=s3://{BUCKET}/{PREFIX}")
    print(f"instance_profile={PROFILE_NAME}")


if __name__ == "__main__":
    main()
