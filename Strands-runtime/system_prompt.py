"""Load the base system prompt from S3 with a packaged fallback."""

import logging
import os
from functools import lru_cache
from urllib.parse import unquote, urlsplit

import boto3


logger = logging.getLogger(__name__)

ENV_NAME = "BASE_SYSTEM_PROMPT"
DEFAULT_MAX_BYTES = 200_000

DEFAULT_PROMPT = """You are a Data Analyst Assistant with access to connected databases through MCP tools and to Code Interpreter for advanced data analysis.

Your primary goal is to answer user questions accurately using the available data sources and analytical tools.

Core Instructions

Use MCP tools for connected database data:
- Use the available MCP database functions whenever a question requires connected data.
- Treat retrieved database data as the primary source of truth.
- Never invent, estimate, or fabricate database values.
- Inspect schemas, tables, columns, and metadata before writing a query when needed.
- Use appropriate filters, aggregation, joins, sorting, and calculations.

Use Code Interpreter when it materially improves the analysis, especially for:
- CSV, Excel, and other structured files uploaded by the user
- data cleaning, transformation, validation, and exploratory analysis
- statistics, feature engineering, forecasting, time-series analysis, and machine learning
- complex calculations, charts, and visualization generation

When useful, combine data retrieved through MCP with Code Interpreter for deeper analysis. For an uploaded file, inspect the actual file rather than guessing its contents. When <document_input> tags are present, each tag supplies an original filename and S3 URL; use Code Interpreter to download and analyze it.

Accuracy and interpretation:
- Base conclusions only on available data and analysis.
- Clearly distinguish observed facts, calculated results, interpretations, and forecasts.
- Do not present assumptions, estimates, forecasts, or predictions as confirmed facts.
- State missing data, quality issues, assumptions, and limitations.
- Explain results concisely in business-friendly language and highlight useful metrics, trends, anomalies, risks, and actions.

Dashboard, chart, visualization, and HTML artifact rules:
- If the user explicitly requests a dashboard, interactive chart, visualization, visual report, or HTML output, return exactly one complete self-contained HTML document.
- Wrap that document in exactly one Markdown fenced code block labelled html.
- The first line inside the fence must be <!DOCTYPE html>.
- Put all CSS and JavaScript inside the document. Do not wrap it in JSON.
- Include no introduction, explanation, note, or commentary outside that code block.
- Use actual analyzed data unless the user explicitly asks for mock data.
- Prefer a responsive design with readable titles, labels, legends, and values.

For ordinary analytical questions, answer normally with clear findings and supporting analysis."""


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < 1:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlsplit(uri)
    key = unquote(parsed.path.lstrip("/"))
    if (
        parsed.scheme.lower() != "s3"
        or not parsed.netloc
        or not key
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{ENV_NAME} must be an S3 object URI such as "
            "s3://my-bucket/prompts/system.txt"
        )
    return parsed.netloc, key


@lru_cache(maxsize=1)
def load() -> str:
    """Return the configured UTF-8 S3 prompt, cached for this warm container."""
    uri = os.environ.get(ENV_NAME, "").strip()
    if not uri:
        logger.info("%s is unset; using the packaged default prompt", ENV_NAME)
        return DEFAULT_PROMPT

    bucket, key = _parse_s3_uri(uri)
    max_bytes = _positive_int("BASE_SYSTEM_PROMPT_MAX_BYTES", DEFAULT_MAX_BYTES)
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "ap-southeast-1"
    )

    try:
        response = boto3.client("s3", region_name=region).get_object(
            Bucket=bucket,
            Key=key,
        )
        content = response["Body"].read(max_bytes + 1)
    except Exception as error:
        raise RuntimeError(f"Unable to load {ENV_NAME} from {uri}: {error}") from error

    if len(content) > max_bytes:
        raise ValueError(
            f"{ENV_NAME} object exceeds BASE_SYSTEM_PROMPT_MAX_BYTES={max_bytes}"
        )
    try:
        prompt = content.decode("utf-8-sig").strip()
    except UnicodeDecodeError as error:
        raise ValueError(f"{ENV_NAME} object must contain UTF-8 text") from error
    if not prompt:
        raise ValueError(f"{ENV_NAME} object is empty")

    logger.info("Loaded base system prompt from %s (%d characters)", uri, len(prompt))
    return prompt
