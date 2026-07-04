"""
Bridge Lambda: AgentCore Gateway MCP → TimesFM EKS service.

The Gateway invokes this Lambda with tool arguments as the event body directly
(same convention as nuh-analytics-mcp). We forward that payload as an HTTP POST
to the TimesFM FastAPI service on the internal NLB inside the same VPC.

Returns:
  {"result": <forecast response>}  on success
  {"error": "<msg>"}                on failure
"""

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TIMESFM_URL = os.environ["TIMESFM_URL"]  # http://<nlb>/forecast
TIMEOUT_SECS = int(os.environ.get("TIMEOUT_SECS", "60"))


def lambda_handler(event, context):
    logger.info("Event: %s", json.dumps(event, default=str)[:500])

    payload = json.dumps(event).encode("utf-8")
    req = urllib.request.Request(
        TIMESFM_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return {"result": data}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        logger.warning("TimesFM HTTP %s: %s", e.code, detail)
        return {"error": f"TimesFM {e.code}: {detail}"}
    except urllib.error.URLError as e:
        logger.error("TimesFM connection error: %s", e)
        return {"error": f"Cannot reach TimesFM service: {e.reason}"}
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        return {"error": f"Internal error: {e}"}
