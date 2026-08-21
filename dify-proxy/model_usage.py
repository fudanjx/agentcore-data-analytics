"""Calculate and persist detailed Strands Runtime model usage."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import logging
import os
import threading
import time
from urllib.parse import quote, urlsplit, urlunsplit


logger = logging.getLogger("agentcore-dify-proxy.model-usage")

DATABASE_URL = os.environ.get("MODEL_USAGE_DATABASE_URL", "").strip()
DIFY_DATABASE_NAME = "dify"


def _cache_ttl_env() -> float:
    raw = os.environ.get("MODEL_PRICING_CACHE_TTL_SECONDS", "3600").strip()
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid MODEL_PRICING_CACHE_TTL_SECONDS=%r; using 3600 seconds",
            raw,
        )
        return 3600.0
    if value < 0 or value != value or value == float("inf"):
        logger.warning(
            "Invalid MODEL_PRICING_CACHE_TTL_SECONDS=%r; using 3600 seconds",
            raw,
        )
        return 3600.0
    return value


PRICING_CACHE_TTL_SECONDS = _cache_ttl_env()
_USD_PER_MTOK = Decimal("1000000")
_COST_QUANTUM = Decimal("0.0000000001")


@dataclass(frozen=True)
class ModelPricing:
    """One active pricing configuration loaded from ``model_pricing``."""

    pricing_label: str
    currency: str
    input_rate: Decimal
    output_rate: Decimal
    cache_read_rate: Decimal | None
    cache_write_5m_rate: Decimal | None
    cache_write_30m_rate: Decimal | None
    cache_write_1h_rate: Decimal | None
    long_context_threshold_tokens: int | None
    long_input_rate: Decimal | None
    long_output_rate: Decimal | None
    long_cache_read_rate: Decimal | None
    long_cache_write_30m_rate: Decimal | None


_pricing_cache: dict[str, tuple[float, ModelPricing | None]] = {}
_pricing_cache_lock = threading.Lock()


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
_SELECT_MODEL_PRICING_SQL = """
SELECT
    currency,
    input_usd_per_mtok,
    output_usd_per_mtok,
    cache_read_usd_per_mtok,
    cache_write_5m_usd_per_mtok,
    cache_write_30m_usd_per_mtok,
    cache_write_1h_usd_per_mtok,
    long_context_threshold_tokens,
    long_input_usd_per_mtok,
    long_output_usd_per_mtok,
    long_cache_read_usd_per_mtok,
    long_cache_write_30m_usd_per_mtok
FROM model_pricing
WHERE pricing_label = %s
  AND active IS NOT FALSE
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


def _decimal_rate(value, column: str, *, required: bool = False) -> Decimal | None:
    if value is None:
        if required:
            raise ValueError(f"model_pricing.{column} is required")
        return None
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"model_pricing.{column} is not a valid number") from error
    if not rate.is_finite() or rate < 0:
        raise ValueError(f"model_pricing.{column} must be non-negative")
    return rate


def _model_pricing_from_row(pricing_label: str, row) -> ModelPricing:
    if not row or len(row) < 12:
        raise ValueError("model_pricing query returned an incomplete row")

    threshold = None
    if row[7] is not None:
        threshold = _token_count(row[7])
        if threshold is None or threshold == 0:
            raise ValueError(
                "model_pricing.long_context_threshold_tokens must be positive"
            )

    return ModelPricing(
        pricing_label=pricing_label,
        currency=str(row[0] or "USD").upper(),
        input_rate=_decimal_rate(row[1], "input_usd_per_mtok", required=True),
        output_rate=_decimal_rate(row[2], "output_usd_per_mtok", required=True),
        cache_read_rate=_decimal_rate(row[3], "cache_read_usd_per_mtok"),
        cache_write_5m_rate=_decimal_rate(
            row[4], "cache_write_5m_usd_per_mtok"
        ),
        cache_write_30m_rate=_decimal_rate(
            row[5], "cache_write_30m_usd_per_mtok"
        ),
        cache_write_1h_rate=_decimal_rate(
            row[6], "cache_write_1h_usd_per_mtok"
        ),
        long_context_threshold_tokens=threshold,
        long_input_rate=_decimal_rate(row[8], "long_input_usd_per_mtok"),
        long_output_rate=_decimal_rate(row[9], "long_output_usd_per_mtok"),
        long_cache_read_rate=_decimal_rate(
            row[10], "long_cache_read_usd_per_mtok"
        ),
        long_cache_write_30m_rate=_decimal_rate(
            row[11], "long_cache_write_30m_usd_per_mtok"
        ),
    )


def _query_model_pricing(pricing_label: str) -> ModelPricing | None:
    """Load one pricing row using a dedicated, short-lived connection."""
    connection = None
    try:
        connection = _connect()
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(_SELECT_MODEL_PRICING_SQL, (pricing_label,))
                row = cursor.fetchone()
        if row is None:
            return None
        return _model_pricing_from_row(pricing_label, row)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                logger.debug("Unable to close model pricing database", exc_info=True)


def _get_model_pricing(pricing_label: str) -> ModelPricing | None:
    """Return a cached pricing row, including a short-lived cached miss."""
    now = time.monotonic()
    if PRICING_CACHE_TTL_SECONDS > 0:
        with _pricing_cache_lock:
            cached = _pricing_cache.get(pricing_label)
            if cached is not None and cached[0] > now:
                return cached[1]
            if cached is not None:
                _pricing_cache.pop(pricing_label, None)

    pricing = _query_model_pricing(pricing_label)
    if PRICING_CACHE_TTL_SECONDS > 0:
        with _pricing_cache_lock:
            _pricing_cache[pricing_label] = (
                time.monotonic() + PRICING_CACHE_TTL_SECONDS,
                pricing,
            )
    return pricing


def _clear_pricing_cache() -> None:
    """Clear the local pricing cache, primarily for tests and controlled reloads."""
    with _pricing_cache_lock:
        _pricing_cache.clear()


def _pricing_label(payload: dict) -> str | None:
    value = payload.get("pricing_label")
    if not isinstance(value, str):
        legacy = payload.get("pricing")
        value = legacy.get("label") if isinstance(legacy, dict) else None
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _cache_write_rate(
    pricing: ModelPricing,
    prompt_cache_ttl: str,
    long_context: bool,
) -> Decimal | None:
    if prompt_cache_ttl == "30m" and long_context:
        return pricing.long_cache_write_30m_rate
    return {
        "5m": pricing.cache_write_5m_rate,
        "30m": pricing.cache_write_30m_rate,
        "1h": pricing.cache_write_1h_rate,
    }.get(prompt_cache_ttl)


def _rounded_cost(value: Decimal) -> float:
    return float(value.quantize(_COST_QUANTUM))


def _calculate_costs(payload: dict, pricing: ModelPricing) -> dict[str, float] | None:
    """Calculate per-component and total token cost from one pricing row."""
    if pricing.currency != "USD":
        logger.warning(
            "Unable to calculate model usage with non-USD pricing (label=%s, currency=%s)",
            pricing.pricing_label,
            pricing.currency,
        )
        return None

    tokens = {
        "input": _token_count(payload.get("input_tokens")) or 0,
        "output": _token_count(payload.get("output_tokens")) or 0,
        "cache_read": _token_count(payload.get("cache_read_input_tokens")) or 0,
        "cache_write": _token_count(payload.get("cache_write_input_tokens")) or 0,
    }
    total_input_tokens = _token_count(payload.get("total_input_tokens"))
    if total_input_tokens is None:
        total_input_tokens = (
            tokens["input"] + tokens["cache_read"] + tokens["cache_write"]
        )

    long_context = (
        pricing.long_context_threshold_tokens is not None
        and total_input_tokens > pricing.long_context_threshold_tokens
    )
    rates = {
        "input": pricing.long_input_rate if long_context else pricing.input_rate,
        "output": pricing.long_output_rate if long_context else pricing.output_rate,
        "cache_read": (
            pricing.long_cache_read_rate if long_context else pricing.cache_read_rate
        ),
        "cache_write": _cache_write_rate(
            pricing,
            str(payload.get("prompt_cache_ttl") or "").strip().lower(),
            long_context,
        ),
    }

    missing = [name for name, count in tokens.items() if count and rates[name] is None]
    if missing:
        logger.warning(
            "Unable to calculate complete model cost; missing rates (label=%s, fields=%s)",
            pricing.pricing_label,
            ",".join(missing),
        )
        return None

    unrounded = {
        name: Decimal(count) * (rates[name] or Decimal(0)) / _USD_PER_MTOK
        for name, count in tokens.items()
    }
    return {
        "total": _rounded_cost(sum(unrounded.values(), Decimal(0))),
        **{name: _rounded_cost(cost) for name, cost in unrounded.items()},
    }


def persist(payload: dict, session_id: str, user_id: str) -> None:
    """Calculate cost and insert one detailed usage record."""
    if not DATABASE_URL:
        return

    pricing_label = _pricing_label(payload)
    pricing = None
    costs = None
    pricing_load_failed = False
    if pricing_label:
        try:
            pricing = _get_model_pricing(pricing_label)
        except Exception as error:
            pricing_load_failed = True
            logger.warning(
                "Unable to load model pricing (label=%s): %s",
                pricing_label,
                error,
            )
        if pricing is None and not pricing_load_failed:
            logger.warning("No active model pricing found (label=%s)", pricing_label)
        else:
            costs = _calculate_costs(payload, pricing)
    else:
        logger.warning("Model usage payload does not contain pricing_label")

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
        costs.get("total") if costs else None,
        costs.get("input") if costs else None,
        costs.get("output") if costs else None,
        costs.get("cache_read") if costs else None,
        costs.get("cache_write") if costs else None,
        pricing_label,
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
