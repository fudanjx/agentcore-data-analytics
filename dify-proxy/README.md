# AgentCore Dify Proxy

## AgentCore Memory proxy

The proxy exposes two authenticated endpoints for Dify workflows that cannot
sign AgentCore requests with AWS SigV4 themselves:

- `POST /memory/write` stores one user/assistant exchange.
- `POST /memory/retrieve` performs semantic retrieval.

The caller supplies `memory_id` for both operations and `strategy_id` for
retrieval. This allows one proxy deployment to serve multiple configured
memories without storing their identifiers in the Deployment environment.
AWS authentication uses the IAM role attached to the `agentcore-proxy` service
account.

Set a dedicated bearer credential in a Kubernetes Secret:

```bash
kubectl -n agentcore create secret generic agentcore-dify-proxy \
  --from-literal=memory-api-key='REPLACE_WITH_A_LONG_RANDOM_VALUE'
```

The Deployment reads that value as `DIFY_MEMORY_PROXY_API_KEY`. If it is absent,
both memory routes return HTTP 503. Send it from the Dify HTTP Request node as
`Authorization: Bearer <value>`.

Write request:

```json
{
  "memory_id": "memory_dify-kpdzNRHDzW",
  "user_id": "person@example.com",
  "session_id": "stable-conversation-id",
  "user_text": "What the user said",
  "assistant_text": "What the assistant answered"
}
```

Retrieve request:

```json
{
  "memory_id": "memory_dify-kpdzNRHDzW",
  "strategy_id": "semantic_builtin_peexk-ogfGK55koq",
  "user_id": "person@example.com",
  "query": "Current user question",
  "top_k": 5
}
```

`user_id` may be an email address. Existing clients may instead send
`actor_id`; when that value is an email or otherwise contains characters that
AgentCore actor IDs do not support, the proxy applies the same deterministic
UUID conversion used for `user_id` and chat messages. An already-safe
`actor_id`, such as `actor-1`, remains unchanged. Provide exactly one of
`user_id` or `actor_id`. Identity matching is case-sensitive, so callers should
use one canonical email casing consistently.

Grant the proxy service-account role only the memories it is allowed to serve:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:RetrieveMemoryRecords"
      ],
      "Resource": "arn:aws:bedrock-agentcore:ap-southeast-1:ACCOUNT_ID:memory/MEMORY_ID"
    }
  ]
}
```

New events are accepted immediately, but semantic memory extraction is
asynchronous, so retrieval may not return a just-written exchange immediately.

## Generated artifacts

The proxy injects a request-scoped S3 output prefix and ownership tags into
AgentCore calls. Generated CSV, DOCX, HTML, XLSX, PPTX, and PDF files must be
uploaded to that prefix and reported in an `<agentcore-artifacts>` marker. The
proxy verifies the exact user and conversation tags before delivery.

Generated HTML must embed its analyzed data and custom CSS/JavaScript. A
standard Chart.js CDN script is permitted; remote data, styles, fonts, and other
dependencies are not. Raw HTML printed by Code Interpreter is not a delivery
channel: the bounded result contract intentionally retains only concise result
metadata. After validating the S3 object, the proxy downloads it and emits the
complete document using the frontend's contract:

````text
```html
<!DOCTYPE html>
<html lang="en">
...
</html>
```
````

Direct model-generated HTML is rejected. If no validated Code Interpreter HTML
artifact is available, the response reports that dashboard generation failed.
Non-HTML artifacts retain the existing machine-reference or presigned-link
behavior.

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
| OpenAI GPT-5.6 Luna | `bedrock-openai-gpt-5.6-luna-global-standard-ap-southeast-1` | Global inference, called from `ap-southeast-1` |
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
        'bedrock-openai-gpt-5.6-luna-global-standard-ap-southeast-1',
        'openai',
        'GPT-5.6 Luna',
        'openai.gpt-5.6-luna',
        'global.openai.gpt-5.6-luna',
        'bedrock-runtime',
        'ap-southeast-1',
        'global',
        'standard',
        'USD',
        0.200000, 1.200000, 0.020000, NULL, 0.250000, NULL,
        272000, 0.400000, 1.800000, 0.040000, 0.500000,
        'https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-luna.html',
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

The Claude and GPT-5.6 Luna global rows use Global cross-Region inference with `ap-southeast-1` as the source/calling region. Amazon Bedrock prices an inference-profile request from its source region, so only the source regions actually used by a runtime need distinct pricing labels. Global inference is currently available for these models from Singapore even though direct in-region inference is not.

GPT-5.6 Sol, Terra, and Luna are OpenAI models offered through Amazon Bedrock, not the ChatGPT product. The Luna row uses the `bedrock-runtime` endpoint and the `global.openai.gpt-5.6-luna` inference profile from `ap-southeast-1`. The Sol and Terra seed rows use the `bedrock-mantle` endpoint for in-region inference in `us-east-1`; runtimes invoking those rows must use that region and the matching label. All three models have separate rates above 272,000 input/context tokens, which are included in the long-context columns.

Rates and availability above were verified on 2026-10-02 from the [Amazon Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/), the model cards for [Claude Sonnet 4.6](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-6.html), [Claude Opus 4.7](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-4-7.html), [Claude Haiku 4.5](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html), and [GPT-5.6 Luna](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-luna.html), and the [GPT-5.6 availability announcement](https://aws.amazon.com/about-aws/whats-new/2026/07/openai-gpt-sol-terra/). AWS can change prices or regional availability, so update the rows before deploying if the AWS pricing page changes.

The proxy selects the row whose `pricing_label` matches the Runtime's top-level `pricing_label`. It uses the reported cache TTL to choose the 5-minute, 30-minute, or 1-hour cache-write rate. When `total_input_tokens` is greater than `long_context_threshold_tokens`, it uses all four long-context rates for that invocation. Pricing rows and missing-label results are cached independently in each proxy process for `MODEL_PRICING_CACHE_TTL_SECONDS`; restart the pod or set the TTL to `0` when an immediate database update is required.
