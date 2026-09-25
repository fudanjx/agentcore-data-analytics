"""Cross-cutting constants for the uploader API.

Kept in one place so operational values (SSE choice, cache TTLs, tag set,
header names) never drift across modules.
"""

from __future__ import annotations

from typing import Final


# ----------------------------------------------------------------------------
# S3 encryption

S3_SSE: Final[str] = "AES256"


# ----------------------------------------------------------------------------
# Identity emulation headers

# Frontend modes (LOCAL with serve_local_frontend=True, DEV): profile switcher.
FRONTEND_IDENTITY_HEADER: Final[str] = "X-Pilot-User-Id"

# Hardened modes (LOCAL API-only, STG, PRD): email identity forwarded by the
# calling application. The bearer secret authenticates the caller; this header
# identifies the acting end-user.
HARDENED_IDENTITY_HEADER: Final[str] = "User-ID"


# ----------------------------------------------------------------------------
# Bearer auth secret cache
#
# The BearerAuthService caches the Secrets Manager value in-process. Refresh
# happens on TTL expiry and, opportunistically, on token mismatch (bounded by
# the refresh-min-interval to keep the DoS surface small).

BEARER_CACHE_TTL_SECONDS: Final[int] = 3600
BEARER_REFRESH_MIN_INTERVAL_SECONDS: Final[int] = 300


# ----------------------------------------------------------------------------
# S3 Tables bucket tagging
#
# Every bucket created through this API is tagged so it can be filtered from
# other applications sharing the same AWS account. Listing filters buckets
# whose APP tag does not match APP_TAG_FILTER_VALUE.

APP_TAGS: Final[dict[str, str]] = {
    "PROJECT-NAME": "Bot-NUHS",
    "PROJECT-NAME-SHORT": "Bot-NUHS",
    "APP": "Data-Insights",
}
APP_TAG_FILTER_KEY: Final[str] = "APP"
APP_TAG_FILTER_VALUE: Final[str] = "Data-Insights"

# Tag lookups are per-bucket and not returned by list_table_buckets, so the
# service caches results in-process. Tags are only ever set by this API at
# creation time; drift is rare.
BUCKET_TAG_CACHE_TTL_SECONDS: Final[int] = 6 * 60 * 60  # 6 hours


# ----------------------------------------------------------------------------
# Historical audit projection

HISTORY_BUCKET: Final[str] = "ah-data-analytics"
HISTORY_PREFIX: Final[str] = "temp_s3_update/web_ingest/upload_history"
UPLOAD_HISTORY_TABLE: Final[str] = "uploader_upload_history"


# ----------------------------------------------------------------------------
# Compatibility upload session limits

SUPPORTED_COMPAT_SUFFIXES: Final[tuple[str, ...]] = (
    ".parquet",
    ".parquet.gzip",
    ".xlsx",
    ".xls",
    ".csv",
    ".tsv",
)
UPLOAD_PART_BYTES: Final[int] = 8 * 1024 * 1024
PREUPLOAD_LEASE_MINUTES: Final[int] = 10
ACTIVE_LEASE_MINUTES: Final[int] = 30


# ----------------------------------------------------------------------------
# Ownership sentinel
#
# Retained for legacy record backfill scenarios only. All new records carry a
# real user id (profile key in frontend modes, email in hardened).

BEARER_SERVICE_USER_ID: Final[str] = "bearer-service"


# ----------------------------------------------------------------------------
# NRIC detection policy
#
# The worker's preflight samples each column and flags it as an NRIC column
# when at least `NRIC_MATCH_THRESHOLD` of `NRIC_SAMPLE_SIZE` sampled values
# match the NRIC pattern. `NRIC_POLICY_KIND` is echoed to the API response so
# clients can distinguish policy revisions.

NRIC_SAMPLE_SIZE: Final[int] = 5
NRIC_MATCH_THRESHOLD: Final[int] = 3
NRIC_POLICY_KIND: Final[str] = "sampled-heuristic-v1"
