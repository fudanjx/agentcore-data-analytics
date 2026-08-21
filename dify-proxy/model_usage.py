"""Persist detailed Strands Runtime model usage in PostgreSQL."""

import logging
import os
from urllib.parse import quote, urlsplit, urlunsplit


logger = logging.getLogger("agentcore-dify-proxy.model-usage")

DATABASE_URL = os.environ.get("MODEL_USAGE_DATABASE_URL", "").strip()
DIFY_DATABASE_NAME = "dify"


class Usage:
    """Full Runtime usage with a Dify-compatible aggregate projection."""

    __slots__ = ("completion_tokens", "payload", "prompt_tokens", "total_tokens")

    def __init__(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        payload: dict | None = None,
    ):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens
        self.payload = dict(payload or {})

    def as_openai(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def _token_count(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return count if count >= 0 else None


def from_payload(payload) -> Usage | None:
    """Validate Runtime usage and calculate the Dify aggregate token fields."""
    if not isinstance(payload, dict):
        return None

    prompt_tokens = _token_count(payload.get("total_input_tokens"))
    if prompt_tokens is None:
        prompt_tokens = _token_count(payload.get("prompt_tokens"))
    if prompt_tokens is None:
        input_tokens = _token_count(payload.get("input_tokens"))
        cache_read_tokens = _token_count(payload.get("cache_read_input_tokens"))
        cache_write_tokens = _token_count(payload.get("cache_write_input_tokens"))
        if any(
            count is not None
            for count in (input_tokens, cache_read_tokens, cache_write_tokens)
        ):
            prompt_tokens = sum(
                count or 0
                for count in (input_tokens, cache_read_tokens, cache_write_tokens)
            )

    completion_tokens = _token_count(payload.get("output_tokens"))
    if completion_tokens is None:
        completion_tokens = _token_count(payload.get("completion_tokens"))
    if prompt_tokens is None or completion_tokens is None:
        return None
    return Usage(prompt_tokens, completion_tokens, payload)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS model_usage (
    invocation_id BIGSERIAL PRIMARY KEY,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    session_id TEXT NOT NULL,
    user_id TEXT,
    user_email TEXT,
    model_id TEXT,
    model_slug TEXT,
    stream BOOLEAN,
    succeeded BOOLEAN,
    duration_ms BIGINT,
    prompt_cache_ttl TEXT,
    input_tokens BIGINT,
    cache_read_input_tokens BIGINT,
    cache_write_input_tokens BIGINT,
    total_input_tokens BIGINT,
    output_tokens BIGINT,
    total_tokens_reported BIGINT,
    cache_read_ratio DOUBLE PRECISION,
    estimated_cost_usd DOUBLE PRECISION,
    estimated_cost_input_usd DOUBLE PRECISION,
    estimated_cost_output_usd DOUBLE PRECISION,
    estimated_cost_cache_read_usd DOUBLE PRECISION,
    estimated_cost_cache_write_usd DOUBLE PRECISION,
    pricing_label TEXT
);
ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS user_email TEXT;
ALTER TABLE model_usage DROP COLUMN IF EXISTS raw_usage
"""
_INSERT_SQL = """
INSERT INTO model_usage (
    session_id,
    user_id,
    user_email,
    model_id,
    model_slug,
    stream,
    succeeded,
    duration_ms,
    prompt_cache_ttl,
    input_tokens,
    cache_read_input_tokens,
    cache_write_input_tokens,
    total_input_tokens,
    output_tokens,
    total_tokens_reported,
    cache_read_ratio,
    estimated_cost_usd,
    estimated_cost_input_usd,
    estimated_cost_output_usd,
    estimated_cost_cache_read_usd,
    estimated_cost_cache_write_usd,
    pricing_label
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
"""
_SELECT_USER_EMAIL_SQL = """
SELECT session_id
FROM end_users
WHERE id = %s::uuid
LIMIT 1
"""


def _connect(database_url: str | None = None):
    """Open a short-lived PostgreSQL connection."""
    import psycopg2

    return psycopg2.connect(database_url or DATABASE_URL)


def _database_url(database_name: str) -> str:
    """Return the configured server URL with a different database path."""
    parsed = urlsplit(DATABASE_URL)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.netloc:
        raise ValueError("MODEL_USAGE_DATABASE_URL must be a PostgreSQL URL")
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            "/" + quote(database_name, safe=""),
            parsed.query,
            parsed.fragment,
        )
    )


def _lookup_user_email(user_id: str) -> str | None:
    """Resolve Dify end_users.session_id using the request user UUID."""
    if not user_id:
        return None

    connection = None
    try:
        connection = _connect(_database_url(DIFY_DATABASE_NAME))
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(_SELECT_USER_EMAIL_SQL, (user_id,))
                row = cursor.fetchone()
        if row and row[0] is not None:
            return str(row[0])
    except Exception as error:
        logger.warning(
            "Unable to resolve model usage user email (user=%s): %s",
            user_id,
            error,
        )
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                logger.debug("Unable to close Dify database", exc_info=True)
    return None


def persist(payload: dict, session_id: str, user_id: str) -> None:
    """Create the usage table if needed and insert one detailed record."""
    if not DATABASE_URL:
        return

    costs = payload.get("estimated_cost_breakdown_usd")
    if not isinstance(costs, dict):
        costs = {}
    pricing = payload.get("pricing")
    if not isinstance(pricing, dict):
        pricing = {}
    user_email = _lookup_user_email(user_id)
    values = (
        session_id,
        user_id,
        user_email,
        payload.get("model_id"),
        payload.get("model_slug"),
        payload.get("stream"),
        payload.get("succeeded"),
        payload.get("duration_ms"),
        payload.get("prompt_cache_ttl"),
        payload.get("input_tokens"),
        payload.get("cache_read_input_tokens"),
        payload.get("cache_write_input_tokens"),
        payload.get("total_input_tokens"),
        payload.get("output_tokens"),
        payload.get("total_tokens_reported"),
        payload.get("cache_read_ratio"),
        payload.get("estimated_cost_usd"),
        costs.get("input"),
        costs.get("output"),
        costs.get("cache_read"),
        costs.get("cache_write"),
        pricing.get("label"),
    )

    connection = None
    try:
        connection = _connect()
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(_CREATE_TABLE_SQL)
                cursor.execute(_INSERT_SQL, values)
    except Exception as error:
        logger.warning(
            "Unable to persist model usage (session=%s): %s",
            session_id,
            error,
        )
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                logger.debug("Unable to close model usage database", exc_info=True)
