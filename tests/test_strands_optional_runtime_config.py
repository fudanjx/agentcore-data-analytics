import importlib.util
import json
import sys
from pathlib import Path

import pytest


RUNTIME_DIR = Path(__file__).parents[1] / "Strands-runtime"


def _load_module(monkeypatch, filename):
    module_name = f"strands_runtime_{Path(filename).stem}_optional_config"
    module_path = RUNTIME_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("raw", [None, "", "   ", "{}"])
def test_gateway_configuration_can_be_empty(monkeypatch, raw):
    module = _load_module(monkeypatch, "gateway_config.py")
    if raw is None:
        monkeypatch.delenv(module.ENV_NAME, raising=False)
        configs = module.load_gateway_configs()
    else:
        configs = module.load_gateway_configs(raw)

    assert configs == {}


def test_gateway_configuration_still_loads_configured_tools(monkeypatch):
    module = _load_module(monkeypatch, "gateway_config.py")
    raw = json.dumps(
        {
            "analytics": {
                "label": "Analytics",
                "url": (
                    "https://analytics-123.gateway.bedrock-agentcore."
                    "ap-southeast-1.amazonaws.com"
                ),
                "arn": (
                    "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:"
                    "gateway/analytics-123"
                ),
            }
        }
    )

    configs = module.load_gateway_configs(raw)

    assert list(configs) == ["analytics"]
    assert configs["analytics"].label == "Analytics"


@pytest.mark.parametrize("value", ["[]", '"gateway"', "null"])
def test_gateway_configuration_rejects_non_object_json(monkeypatch, value):
    module = _load_module(monkeypatch, "gateway_config.py")

    with pytest.raises(ValueError, match=module.ENV_NAME):
        module.load_gateway_configs(value)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_base_system_prompt_can_be_empty(monkeypatch, raw):
    module = _load_module(monkeypatch, "system_prompt.py")
    if raw is None:
        monkeypatch.delenv(module.ENV_NAME, raising=False)
    else:
        monkeypatch.setenv(module.ENV_NAME, raw)

    assert module.load() == ""
