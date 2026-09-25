"""Ensure the account-wide S3 Tables Glue catalog uses IAM access control.

The parent ``s3tablescatalog`` federates every S3 Tables table bucket in the
account and Region. Once the parent is configured, existing and future table
buckets are mounted automatically as child catalogs.

Run this with a deployment administrator, not from the uploader API task:

    python infra/ensure_s3tables_catalog.py

To replace Lake Formation permissions on existing child catalogs with the IAM
defaults, use the explicit migration flag once:

    python infra/ensure_s3tables_catalog.py \
        --overwrite-existing-child-permissions
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError


CATALOG_ID = "s3tablescatalog"
DEFAULT_REGION = "ap-southeast-1"

IAM_DEFAULT_PERMISSIONS = [
    {
        "Principal": {
            "DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS",
        },
        "Permissions": ["ALL"],
    }
]


def _catalog_input(
    *,
    region: str,
    account_id: str,
    overwrite_existing_child_permissions: bool,
) -> dict[str, Any]:
    catalog_input: dict[str, Any] = {
        "Description": "Federated catalog for Amazon S3 Tables",
        "FederatedCatalog": {
            "Identifier": f"arn:aws:s3tables:{region}:{account_id}:bucket/*",
            "ConnectionName": "aws:s3tables",
        },
        "CreateDatabaseDefaultPermissions": IAM_DEFAULT_PERMISSIONS,
        "CreateTableDefaultPermissions": IAM_DEFAULT_PERMISSIONS,
    }
    if overwrite_existing_child_permissions:
        catalog_input["OverwriteChildResourcePermissionsWithDefault"] = "Accept"
    return catalog_input


def ensure_catalog(
    *,
    region: str,
    profile: str | None = None,
    overwrite_existing_child_permissions: bool = False,
) -> str:
    """Create or update the parent catalog and return ``created``/``updated``."""
    session = boto3.Session(profile_name=profile, region_name=region)
    identity = session.client("sts").get_caller_identity()
    account_id = identity["Account"]
    caller_arn = identity["Arn"]
    glue = session.client("glue")

    catalog_input = _catalog_input(
        region=region,
        account_id=account_id,
        overwrite_existing_child_permissions=overwrite_existing_child_permissions,
    )

    try:
        glue.get_catalog(CatalogId=CATALOG_ID)
    except glue.exceptions.EntityNotFoundException:
        glue.create_catalog(Name=CATALOG_ID, CatalogInput=catalog_input)
        print(
            f"Created {CATALOG_ID} in {region} with IAM access-control "
            "defaults. Existing and future table buckets will be mounted "
            "automatically."
        )
        return "created"
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "Unknown")
        raise RuntimeError(
            f"Unable to inspect Glue catalog '{CATALOG_ID}' ({code}). "
            f"The current identity is {caller_arn}. Run this script as a "
            "Lake Formation data lake administrator or an identity with "
            "Super user permission on this catalog, plus the required Glue "
            "IAM permissions."
        ) from error

    glue.update_catalog(CatalogId=CATALOG_ID, CatalogInput=catalog_input)
    if overwrite_existing_child_permissions:
        print(
            f"Updated {CATALOG_ID} in {region} and replaced existing child "
            "resource permissions with IAM access-control defaults."
        )
    else:
        print(
            f"Updated {CATALOG_ID} in {region}. Future child resources will "
            "inherit IAM access-control defaults; existing child permissions "
            "were not overwritten."
        )
    return "updated"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create or update the account-wide S3 Tables Glue catalog with "
            "IAM access-control defaults."
        )
    )
    parser.add_argument(
        "--region",
        default=(
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or DEFAULT_REGION
        ),
        help=f"AWS Region (default: environment or {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--profile",
        help="Optional AWS shared-config profile to use for the migration",
    )
    parser.add_argument(
        "--overwrite-existing-child-permissions",
        action="store_true",
        help=(
            "Replace Lake Formation permissions on every existing child "
            "catalog resource with IAM_ALLOWED_PRINCIPALS/ALL. Use only for "
            "an intentional one-time migration to IAM access control."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.overwrite_existing_child_permissions:
        print(
            "WARNING: existing child catalog permissions will be overwritten "
            "with IAM access-control defaults."
        )
    ensure_catalog(
        region=args.region,
        profile=args.profile,
        overwrite_existing_child_permissions=(
            args.overwrite_existing_child_permissions
        ),
    )


if __name__ == "__main__":
    main()
