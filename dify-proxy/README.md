# AgentCore Dify Proxy

The proxy consumes the Strands Runtime's final `model_usage` sideband event. `model_usage.py` owns PostgreSQL configuration, pricing lookup and caching, cost calculation, table creation, user lookup, and inserts, while `dify-server.py` keeps only the event handling and Dify-compatible token projection. The internal record is not forwarded to Dify; Dify receives only its existing OpenAI-compatible `usage` object with `prompt_tokens`, `completion_tokens`, and `total_tokens`.

Persistence is disabled when `MODEL_USAGE_DATABASE_URL` is empty. Configure one standard PostgreSQL connection URL:

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MODEL_USAGE_DATABASE_URL` | Empty | Full PostgreSQL URL; the user needs permission to create the table and insert rows |
| `MODEL_PRICING_CACHE_TTL_SECONDS` | `300` | Seconds to cache each pricing row or missing label in each proxy process; use `0` to disable caching |

The URL can contain the database, SSL mode, and connection timeout:

```text
postgresql://username:password@postgres-hostname:5432/nuhs?sslmode=disable&connect_timeout=5
```

Percent-encode reserved characters in the username or password before placing them in a URL.

For every usage event, the proxy uses the configured `nuhs` connection for the `model_usage` insert. It also derives a second URL for the `dify` database on the same PostgreSQL server and resolves `user_email` with:

```sql
SELECT session_id FROM end_users WHERE id = :user_id;
```

The resulting `end_users.session_id` is stored as `model_usage.user_email`. The configured database user therefore needs `SELECT` permission on `end_users` in the `dify` database, plus `SELECT` permission on `model_pricing` and table-creation, migration, and insert permission for `model_usage` in `nuhs`. Pricing, lookup, or insert failures are logged, while the user-facing Dify response continues with the compatible aggregate usage data. If pricing cannot be loaded or a required cache rate is missing, the token record is still inserted with null cost fields.

## Model pricing table

Create this table in the `nuhs` database before enabling proxy-side price calculation. Prices are stored in USD per one million tokens. `MODEL_PRICING_LABEL` is a stable lookup key; changing a price in this table therefore does not require rebuilding or reconfiguring the runtime. Only `pricing_label`, `input_usd_per_mtok`, and `output_usd_per_mtok` are compulsory. Cache and long-context rates are nullable because they are only required when that pricing mode applies; the remaining columns are optional metadata or have a default.

The initial labels are:

| Model | `MODEL_PRICING_LABEL` | Invocation region/profile |
| --- | --- | --- |
| Claude Sonnet 4.6 | `bedrock-claude-sonnet-4.6-global-standard-ap-southeast-1` | Global inference, called from `ap-southeast-1` |
| Claude Opus 4.7 | `bedrock-claude-opus-4.7-global-standard-ap-southeast-1` | Global inference, called from `ap-southeast-1` |
| Claude Haiku 4.5 | `bedrock-claude-haiku-4.5-global-standard-ap-southeast-1` | Global inference, called from `ap-southeast-1` |
| OpenAI GPT-5.6 Sol | `bedrock-openai-gpt-5.6-sol-standard-us-east-1` | In-region inference in `us-east-1` |
| OpenAI GPT-5.6 Terra | `bedrock-openai-gpt-5.6-terra-standard-us-east-1` | In-region inference in `us-east-1` |

Run the following SQL while connected to `nuhs`. It is safe to run again: existing rows with the same label are updated.

```sql
BEGIN;

CREATE TABLE IF NOT EXISTS model_pricing (
    pricing_label TEXT PRIMARY KEY,
    provider TEXT,
    model_name TEXT,
    model_id TEXT,
    inference_profile_id TEXT,
    endpoint_type TEXT,
    billing_region TEXT,
    pricing_scope TEXT,
    service_tier TEXT,
    currency CHAR(3) DEFAULT 'USD',

    input_usd_per_mtok NUMERIC(18, 6) NOT NULL,
    output_usd_per_mtok NUMERIC(18, 6) NOT NULL,
    cache_read_usd_per_mtok NUMERIC(18, 6),
    cache_write_5m_usd_per_mtok NUMERIC(18, 6),
    cache_write_30m_usd_per_mtok NUMERIC(18, 6),
    cache_write_1h_usd_per_mtok NUMERIC(18, 6),

    -- When set, requests above this input/context size use the long-context rates.
    long_context_threshold_tokens BIGINT,
    long_input_usd_per_mtok NUMERIC(18, 6),
    long_output_usd_per_mtok NUMERIC(18, 6),
    long_cache_read_usd_per_mtok NUMERIC(18, 6),
    long_cache_write_30m_usd_per_mtok NUMERIC(18, 6),
    source_url TEXT,
    active BOOLEAN DEFAULT TRUE,
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT model_pricing_currency_check
        CHECK (currency = 'USD'),
    CONSTRAINT model_pricing_base_rates_check
        CHECK (
            input_usd_per_mtok >= 0
            AND output_usd_per_mtok >= 0
            AND (cache_read_usd_per_mtok IS NULL OR cache_read_usd_per_mtok >= 0)
            AND (cache_write_5m_usd_per_mtok IS NULL OR cache_write_5m_usd_per_mtok >= 0)
            AND (cache_write_30m_usd_per_mtok IS NULL OR cache_write_30m_usd_per_mtok >= 0)
            AND (cache_write_1h_usd_per_mtok IS NULL OR cache_write_1h_usd_per_mtok >= 0)
        ),
    CONSTRAINT model_pricing_long_context_check
        CHECK (
            (
                long_context_threshold_tokens IS NULL
                AND long_input_usd_per_mtok IS NULL
                AND long_output_usd_per_mtok IS NULL
                AND long_cache_read_usd_per_mtok IS NULL
                AND long_cache_write_30m_usd_per_mtok IS NULL
            )
            OR
            (
                long_context_threshold_tokens > 0
                AND long_input_usd_per_mtok IS NOT NULL
                AND long_input_usd_per_mtok >= 0
                AND long_output_usd_per_mtok IS NOT NULL
                AND long_output_usd_per_mtok >= 0
                AND long_cache_read_usd_per_mtok IS NOT NULL
                AND long_cache_read_usd_per_mtok >= 0
                AND long_cache_write_30m_usd_per_mtok IS NOT NULL
                AND long_cache_write_30m_usd_per_mtok >= 0
            )
        )
);

INSERT INTO model_pricing (
    pricing_label,
    provider,
    model_name,
    model_id,
    inference_profile_id,
    endpoint_type,
    billing_region,
    pricing_scope,
    service_tier,
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
    long_cache_write_30m_usd_per_mtok,
    source_url,
    active
)
VALUES
    (
        'bedrock-claude-sonnet-4.6-global-standard-ap-southeast-1',
        'anthropic',
        'Claude Sonnet 4.6',
        'anthropic.claude-sonnet-4-6',
        'global.anthropic.claude-sonnet-4-6',
        'bedrock-runtime',
        'ap-southeast-1',
        'global',
        'standard',
        'USD',
        3.000000, 15.000000, 0.300000, 3.750000, NULL, 6.000000,
        NULL, NULL, NULL, NULL, NULL,
        'https://aws.amazon.com/bedrock/pricing/',
        TRUE
    ),
    (
        'bedrock-claude-opus-4.7-global-standard-ap-southeast-1',
        'anthropic',
        'Claude Opus 4.7',
        'anthropic.claude-opus-4-7',
        'global.anthropic.claude-opus-4-7',
        'bedrock-runtime',
        'ap-southeast-1',
        'global',
        'standard',
        'USD',
        5.000000, 25.000000, 0.500000, 6.250000, NULL, 10.000000,
        NULL, NULL, NULL, NULL, NULL,
        'https://aws.amazon.com/bedrock/pricing/',
        TRUE
    ),
    (
        'bedrock-claude-haiku-4.5-global-standard-ap-southeast-1',
        'anthropic',
        'Claude Haiku 4.5',
        'anthropic.claude-haiku-4-5-20251001-v1:0',
        'global.anthropic.claude-haiku-4-5-20251001-v1:0',
        'bedrock-runtime',
        'ap-southeast-1',
        'global',
        'standard',
        'USD',
        1.000000, 5.000000, 0.100000, 1.250000, NULL, 2.000000,
        NULL, NULL, NULL, NULL, NULL,
        'https://aws.amazon.com/bedrock/pricing/',
        TRUE
    ),
    (
        'bedrock-openai-gpt-5.6-sol-standard-us-east-1',
        'openai',
        'GPT-5.6 Sol',
        'openai.gpt-5.6-sol',
        NULL,
        'bedrock-mantle',
        'us-east-1',
        'in-region',
        'standard',
        'USD',
        5.500000, 33.000000, 0.550000, NULL, 6.875000, NULL,
        272000, 11.000000, 49.500000, 1.100000, 13.750000,
        'https://aws.amazon.com/bedrock/pricing/',
        TRUE
    ),
    (
        'bedrock-openai-gpt-5.6-terra-standard-us-east-1',
        'openai',
        'GPT-5.6 Terra',
        'openai.gpt-5.6-terra',
        NULL,
        'bedrock-mantle',
        'us-east-1',
        'in-region',
        'standard',
        'USD',
        2.200000, 13.200000, 0.220000, NULL, 2.750000, NULL,
        272000, 4.400000, 19.800000, 0.440000, 5.500000,
        'https://aws.amazon.com/bedrock/pricing/',
        TRUE
    )
ON CONFLICT (pricing_label) DO UPDATE
SET (
    provider,
    model_name,
    model_id,
    inference_profile_id,
    endpoint_type,
    billing_region,
    pricing_scope,
    service_tier,
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
    long_cache_write_30m_usd_per_mtok,
    source_url,
    active,
    updated_at
) = (
    EXCLUDED.provider,
    EXCLUDED.model_name,
    EXCLUDED.model_id,
    EXCLUDED.inference_profile_id,
    EXCLUDED.endpoint_type,
    EXCLUDED.billing_region,
    EXCLUDED.pricing_scope,
    EXCLUDED.service_tier,
    EXCLUDED.currency,
    EXCLUDED.input_usd_per_mtok,
    EXCLUDED.output_usd_per_mtok,
    EXCLUDED.cache_read_usd_per_mtok,
    EXCLUDED.cache_write_5m_usd_per_mtok,
    EXCLUDED.cache_write_30m_usd_per_mtok,
    EXCLUDED.cache_write_1h_usd_per_mtok,
    EXCLUDED.long_context_threshold_tokens,
    EXCLUDED.long_input_usd_per_mtok,
    EXCLUDED.long_output_usd_per_mtok,
    EXCLUDED.long_cache_read_usd_per_mtok,
    EXCLUDED.long_cache_write_30m_usd_per_mtok,
    EXCLUDED.source_url,
    EXCLUDED.active,
    NOW()
);

COMMIT;

SELECT
    pricing_label,
    billing_region,
    input_usd_per_mtok,
    output_usd_per_mtok,
    active
FROM model_pricing
ORDER BY pricing_label;
```

The Claude rows use Global cross-Region inference with `ap-southeast-1` as the source/calling region. Amazon Bedrock prices an inference-profile request from its source region, so only the source regions actually used by a runtime need distinct pricing labels. Global inference is currently available for these Claude models from Singapore even though direct in-region inference is not.

GPT-5.6 Sol and Terra are OpenAI models offered through Amazon Bedrock, not the ChatGPT product. They use the `bedrock-mantle` endpoint. GPT-5.6 is not currently available from `ap-southeast-1`, so the seed data assumes `us-east-1`; the runtime that invokes GPT must use that region and the matching label. Sol and Terra have separate rates above 272,000 input/context tokens, which are included in the long-context columns.

Rates and availability above were verified on 2026-08-21 from the [Amazon Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/), the model cards for [Claude Sonnet 4.6](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-6.html), [Claude Opus 4.7](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-4-7.html), and [Claude Haiku 4.5](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html), and the [GPT-5.6 availability announcement](https://aws.amazon.com/about-aws/whats-new/2026/07/openai-gpt-sol-terra/). AWS can change prices or regional availability, so update the rows before deploying if the AWS pricing page changes.

The proxy selects the row whose `pricing_label` matches the Runtime's top-level `pricing_label`. It uses the reported cache TTL to choose the 5-minute, 30-minute, or 1-hour cache-write rate. When `total_input_tokens` is greater than `long_context_threshold_tokens`, it uses all four long-context rates for that invocation. Pricing rows and missing-label results are cached independently in each proxy process for `MODEL_PRICING_CACHE_TTL_SECONDS`; restart the pod or set the TTL to `0` when an immediate database update is required.
