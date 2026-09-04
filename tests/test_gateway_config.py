import json

import pytest

from app.gateway_config import (
    ENV_NAME,
    load_gateway_configs,
    serialize_gateway_configs,
)


@pytest.mark.parametrize("raw", [None, "", "   ", "{}"])
def test_gateway_configuration_can_be_empty(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv(ENV_NAME, raising=False)
        configs = load_gateway_configs()
    else:
        configs = load_gateway_configs(raw)

    assert configs == {}


def test_loads_custom_gateway_names_urls_arns_and_labels():
    raw = json.dumps(
        {
            "analytics": {
                "label": "New Analytics",
                "url": (
                    "https://analytics-new-AbC123.gateway.bedrock-agentcore."
                    "us-west-2.amazonaws.com"
                ),
                "arn": (
                    "arn:aws:bedrock-agentcore:us-west-2:123456789012:"
                    "gateway/analytics-new-AbC123"
                ),
            }
        }
    )

    configs = load_gateway_configs(raw)

    assert list(configs) == ["analytics"]
    assert configs["analytics"].label == "New Analytics"
    assert configs["analytics"].region == "us-west-2"
    assert json.loads(serialize_gateway_configs(configs)) == json.loads(raw)


def test_loads_gateway_mapping_from_environment(monkeypatch):
    raw = json.dumps(
        {
            "reports": {
                "label": "Reports DB",
                "url": (
                    "https://reports-gateway-123.gateway.bedrock-agentcore."
                    "eu-west-1.amazonaws.com"
                ),
                "arn": (
                    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
                    "gateway/reports-gateway-123"
                ),
            }
        }
    )
    monkeypatch.setenv(ENV_NAME, raw)

    configs = load_gateway_configs()

    assert configs["reports"].label == "Reports DB"
    assert configs["reports"].region == "eu-west-1"


@pytest.mark.parametrize(
    "value",
    [
        "not JSON",
        "[]",
        '"gateway"',
        "null",
        '{"Bad Slug":{"label":"Bad","url":"https://example.com","arn":"bad"}}',
        json.dumps(
            {
                "analytics": {
                    "label": "Analytics",
                    "url": (
                        "https://different-id.gateway.bedrock-agentcore."
                        "us-west-2.amazonaws.com"
                    ),
                    "arn": (
                        "arn:aws:bedrock-agentcore:us-west-2:123456789012:"
                        "gateway/analytics-id"
                    ),
                }
            }
        ),
    ],
)
def test_rejects_invalid_gateway_configuration(value):
    with pytest.raises(ValueError, match=ENV_NAME):
        load_gateway_configs(value)
