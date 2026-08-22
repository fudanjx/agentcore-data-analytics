"""Validated AgentCore Gateway configuration shared by the runtime."""

import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse


ENV_NAME = "AGENTCORE_GATEWAYS_JSON"
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws(?:-us-gov|-cn)?):bedrock-agentcore:"
    r"(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"gateway/(?P<gateway_id>[A-Za-z0-9_-]+)$"
)

@dataclass(frozen=True)
class GatewayConfig:
    slug: str
    label: str
    url: str
    arn: str
    region: str


def _validate_gateway(slug: object, value: object) -> GatewayConfig:
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"{ENV_NAME} gateway slugs must match {_SLUG_RE.pattern!r}: {slug!r}"
        )
    if not isinstance(value, dict):
        raise ValueError(f"{ENV_NAME}[{slug!r}] must be a JSON object")

    label = " ".join(str(value.get("label") or "").split())
    url = str(value.get("url") or "").strip().rstrip("/")
    arn = str(value.get("arn") or "").strip()
    if not label or len(label) > 60:
        raise ValueError(f"{ENV_NAME}[{slug!r}].label must contain 1-60 characters")

    arn_match = _ARN_RE.fullmatch(arn)
    if not arn_match:
        raise ValueError(f"{ENV_NAME}[{slug!r}].arn is not an AgentCore Gateway ARN")

    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{ENV_NAME}[{slug!r}].url must be an HTTPS gateway base URL")

    partition = arn_match.group("partition")
    gateway_id = arn_match.group("gateway_id").lower()
    region = arn_match.group("region")
    dns_suffix = "amazonaws.com.cn" if partition == "aws-cn" else "amazonaws.com"
    expected_hostname = (
        f"{gateway_id}.gateway.bedrock-agentcore.{region}.{dns_suffix}"
    )
    if parsed.hostname.lower() != expected_hostname:
        raise ValueError(
            f"{ENV_NAME}[{slug!r}] URL does not match its Gateway ARN and region"
        )

    return GatewayConfig(slug=slug, label=label, url=url, arn=arn, region=region)


def load_gateway_configs(raw: str | None = None) -> dict[str, GatewayConfig]:
    """Load optional Gateway mappings from JSON."""
    if raw is None:
        raw = os.environ.get(ENV_NAME)
    if not raw or not raw.strip():
        return {}

    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{ENV_NAME} must contain valid JSON: {error}") from error

    if not isinstance(values, dict):
        raise ValueError(f"{ENV_NAME} must be a JSON object")
    return {slug: _validate_gateway(slug, value) for slug, value in values.items()}


def serialize_gateway_configs(configs: dict[str, GatewayConfig]) -> str:
    return json.dumps(
        {
            slug: {"label": item.label, "url": item.url, "arn": item.arn}
            for slug, item in configs.items()
        },
        separators=(",", ":"),
    )
