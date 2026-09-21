"""One-shot migration for legacy audit records.

The API used to read upload history from THREE prefixes:

  1. ah-data-analytics/temp_s3_update/web_ingest/upload_history/   (canonical)
  2. <landing_bucket>/<landing_prefix>/audit/                      (fallback)
  3. <landing_bucket>/s3-uploader-v2/audit/                        (fallback)

The refactor dropped the fallbacks. Any records still living in (2) or (3)
become invisible after deploy. Run this script ONCE against each affected
environment to copy the tail into the canonical prefix. It:

- Scans both legacy prefixes.
- For each record, computes the canonical key from ``(table_bucket_arn,
    namespace, target_table, upload_id)`` — the same layout the API writes to.
- Skips records that already exist under the canonical prefix.
- Uses SSE=AES256 on every write (matches the wider standardisation).

Idempotent: safe to run repeatedly. Reads pass over legacy records
untouched — nothing is deleted or renamed at source.

Usage:
    python -m s3tables_uploader.scripts.migrate_legacy_audit \\
        --landing-bucket <bucket> \\
        --landing-prefix <prefix> \\
        [--history-bucket ah-data-analytics] \\
        [--history-prefix temp_s3_update/web_ingest/upload_history] \\
        [--dry-run]

The history bucket and prefix should match this environment's
``S3_UPLOADER_HISTORY_BUCKET`` / ``S3_UPLOADER_HISTORY_PREFIX`` — otherwise
the migrated records will not be visible to the API.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import boto3
from botocore.exceptions import ClientError

from ..core.constants import HISTORY_BUCKET, HISTORY_PREFIX, S3_SSE
from ..job_store import HISTORICAL_LANDING_PREFIX
from ..utils.hashing import scope_key


def _canonical_key(entry: dict[str, Any], history_prefix: str) -> str | None:
    arn = entry.get("table_bucket_arn")
    namespace = entry.get("namespace")
    table = entry.get("target_table")
    upload_id = entry.get("upload_id")
    if not (arn and namespace and table and upload_id):
        return None
    return f"{history_prefix}/{scope_key(str(arn), str(namespace))}/{table}/{upload_id}.json"


def _iter_legacy_records(s3: Any, bucket: str, prefix: str):
    """Yield (source_key, entry_dict) for JSON audit records under ``prefix``."""
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            try:
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                yield key, json.loads(body)
            except (ClientError, json.JSONDecodeError):
                continue


def _canonical_exists(s3: Any, history_bucket: str, canonical_key: str) -> bool:
    try:
        s3.head_object(Bucket=history_bucket, Key=canonical_key)
        return True
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def migrate(
    *,
    landing_bucket: str,
    landing_prefix: str,
    history_bucket: str = HISTORY_BUCKET,
    history_prefix: str = HISTORY_PREFIX,
    dry_run: bool = False,
) -> dict[str, int]:
    """Perform the migration and return a summary counter."""
    s3 = boto3.client("s3")
    stats = {"scanned": 0, "copied": 0, "skipped_existing": 0, "skipped_invalid": 0}
    history_prefix = history_prefix.strip("/")
    legacy_prefixes = [f"{landing_prefix}/audit/"]
    if landing_prefix != HISTORICAL_LANDING_PREFIX:
        legacy_prefixes.append(f"{HISTORICAL_LANDING_PREFIX}/audit/")

    for prefix in legacy_prefixes:
        for source_key, entry in _iter_legacy_records(s3, landing_bucket, prefix):
            stats["scanned"] += 1
            canonical_key = _canonical_key(entry, history_prefix)
            if canonical_key is None:
                stats["skipped_invalid"] += 1
                continue
            if _canonical_exists(s3, history_bucket, canonical_key):
                stats["skipped_existing"] += 1
                continue
            if dry_run:
                print(f"[dry-run] {source_key}  ->  s3://{history_bucket}/{canonical_key}")
                stats["copied"] += 1
                continue
            s3.put_object(
                Bucket=history_bucket,
                Key=canonical_key,
                Body=json.dumps(entry, sort_keys=True).encode(),
                ContentType="application/json",
                ServerSideEncryption=S3_SSE,
            )
            stats["copied"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--landing-bucket", required=True)
    parser.add_argument("--landing-prefix", required=True)
    parser.add_argument("--history-bucket", default=HISTORY_BUCKET)
    parser.add_argument("--history-prefix", default=HISTORY_PREFIX)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    stats = migrate(
        landing_bucket=args.landing_bucket,
        landing_prefix=args.landing_prefix.strip("/"),
        history_bucket=args.history_bucket,
        history_prefix=args.history_prefix,
        dry_run=args.dry_run,
    )
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
