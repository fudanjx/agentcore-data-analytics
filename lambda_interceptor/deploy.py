import argparse
import io
import json
import logging
import sys
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

REGION = "ap-southeast-1"
ROLE_NAME = "LambdaBaseExecutionRole"
FUNCTION_NAME = "s3tables-gateway-interceptor"
FUNCTION_DESCRIPTION = "Interceptor function to manage S3 tables gateway."
HANDLER_FILE = Path(__file__).parent / "lambda_handler.py"
HANDLER_NAME = "lambda_handler.lambda_handler"
RUNTIME = "python3.14"
TIMEOUT_SECONDS = 5
MEMORY_SIZE_MB = 128
EPHEMERAL_STORAGE_MB = 512

MANAGED_POLICY_ARNS = [
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole",
]

RESOURCE_TAGS = {
    "PROJECT-NAME": "Bot-NUHS",
    "PROJECT-NAME-SHORT": "Bot-NUHS",
}

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


def _tags_as_iam_list() -> list[dict]:
    return [{"Key": k, "Value": v} for k, v in RESOURCE_TAGS.items()]


def _build_zip_bytes(source_file: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(source_file, arcname=source_file.name)
    buffer.seek(0)
    return buffer.read()


def _get_role_arn(iam_client, role_name: str) -> str | None:
    try:
        response = iam_client.get_role(RoleName=role_name)
        return response["Role"]["Arn"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NoSuchEntity":
            return None
        raise


def _create_role(iam_client, role_name: str) -> str:
    logger.info("Creating IAM role %s", role_name)
    response = iam_client.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=json.dumps(TRUST_POLICY),
        Description="Base execution role for Lambda functions in Bot-NUHS project.",
        Tags=_tags_as_iam_list(),
    )
    return response["Role"]["Arn"]


def _attach_managed_policies(iam_client, role_name: str, policy_arns: list[str]) -> None:
    for policy_arn in policy_arns:
        logger.info("Attaching policy %s to %s", policy_arn, role_name)
        iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)


def _ensure_role(iam_client, role_name: str) -> str:
    role_arn = _get_role_arn(iam_client, role_name)
    if role_arn:
        logger.info("IAM role %s already exists: %s", role_name, role_arn)
        return role_arn

    role_arn = _create_role(iam_client, role_name)
    _attach_managed_policies(iam_client, role_name, MANAGED_POLICY_ARNS)
    _wait_for_role_propagation(iam_client, role_name)
    return role_arn


def _wait_for_role_propagation(iam_client, role_name: str) -> None:
    logger.info("Waiting for IAM role %s to become available", role_name)
    waiter = iam_client.get_waiter("role_exists")
    waiter.wait(RoleName=role_name)


def _function_exists(lambda_client, function_name: str) -> bool:
    try:
        lambda_client.get_function(FunctionName=function_name)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise


def _create_lambda_function(
    lambda_client,
    function_name: str,
    role_arn: str,
    zip_bytes: bytes,
) -> dict:
    logger.info("Creating Lambda function %s in %s", function_name, REGION)
    return lambda_client.create_function(
        FunctionName=function_name,
        Runtime=RUNTIME,
        Role=role_arn,
        Handler=HANDLER_NAME,
        Code={"ZipFile": zip_bytes},
        Description=FUNCTION_DESCRIPTION,
        Timeout=TIMEOUT_SECONDS,
        MemorySize=MEMORY_SIZE_MB,
        EphemeralStorage={"Size": EPHEMERAL_STORAGE_MB},
        PackageType="Zip",
        Publish=True,
        Tags=RESOURCE_TAGS,
    )


def _update_lambda_function(
    lambda_client,
    function_name: str,
    role_arn: str,
    zip_bytes: bytes,
) -> dict:
    logger.info("Updating Lambda function %s code and configuration", function_name)
    response = lambda_client.update_function_configuration(
        FunctionName=function_name,
        Role=role_arn,
        Handler=HANDLER_NAME,
        Runtime=RUNTIME,
        Description=FUNCTION_DESCRIPTION,
        Timeout=TIMEOUT_SECONDS,
        MemorySize=MEMORY_SIZE_MB,
        EphemeralStorage={"Size": EPHEMERAL_STORAGE_MB},
    )

    lambda_client.tag_resource(
        Resource=response["FunctionArn"],
        Tags=RESOURCE_TAGS,
    )
    
    lambda_client.update_function_code(
            FunctionName=function_name,
            ZipFile=zip_bytes,
            Publish=True,
        )
    lambda_client.get_waiter("function_updated").wait(FunctionName=function_name)
    return response


def _delete_lambda_function(lambda_client, function_name: str) -> None:
    logger.info("Deleting Lambda function %s", function_name)
    lambda_client.delete_function(FunctionName=function_name)


def _prompt_delete_confirmation(function_name: str) -> bool:
    prompt = (
        f"You are about to permanently delete Lambda function '{function_name}' in {REGION}.\n"
        f"Type the function name exactly to confirm deletion: "
    )
    try:
        user_input = input(prompt).strip()
    except EOFError:
        return False
    return user_input == function_name


def deploy() -> dict:
    session = boto3.Session(region_name=REGION)
    iam_client = session.client("iam")
    lambda_client = session.client("lambda")

    role_arn = _ensure_role(iam_client, ROLE_NAME)
    zip_bytes = _build_zip_bytes(HANDLER_FILE)

    if _function_exists(lambda_client, FUNCTION_NAME):
        result = _update_lambda_function(lambda_client, FUNCTION_NAME, role_arn, zip_bytes)
    else:
        result = _create_lambda_function(lambda_client, FUNCTION_NAME, role_arn, zip_bytes)

    logger.info("Lambda deployed: %s", result.get("FunctionArn"))
    return result


def destroy() -> None:
    session = boto3.Session(region_name=REGION)
    lambda_client = session.client("lambda")

    if not _function_exists(lambda_client, FUNCTION_NAME):
        logger.info("Lambda function %s does not exist in %s. Nothing to destroy.", FUNCTION_NAME, REGION)
        return

    if not _prompt_delete_confirmation(FUNCTION_NAME):
        logger.warning("Confirmation did not match. Aborting deletion of %s.", FUNCTION_NAME)
        return

    _delete_lambda_function(lambda_client, FUNCTION_NAME)
    logger.info("Lambda function %s deleted successfully from %s.", FUNCTION_NAME, REGION)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy or destroy the S3 tables gateway interceptor Lambda.",
    )
    parser.add_argument(
        "--destroy",
        action="store_true",
        help="Delete the Lambda function instead of deploying it.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    if args.destroy:
        destroy()
    else:
        deploy()
