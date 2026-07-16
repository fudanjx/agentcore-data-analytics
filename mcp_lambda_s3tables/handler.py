"""
MCP Lambda handler for AgentCore Gateway → Athena over S3 Tables (ah-analytics).

Exposes three read-only tools matching the RDS-side shape:
  execute_sql    — run a SELECT/WITH query via Athena, return rows as JSON
  list_tables    — list all tables in the ah_analytics namespace + column types
  describe_table — column info + 3 sample rows for one table

AgentCore Gateway invokes this Lambda with the tool arguments as the top-level event.

Returns:
  {"result": <data>}  on success
  {"error": "<msg>"}  on failure
"""

import datetime
import decimal
import json
import logging
import os
import re
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


REGION = os.environ.get("AWS_REGION", "ap-southeast-1")
ATHENA_WORKGROUP = os.environ["ATHENA_WORKGROUP"]
ATHENA_CATALOG = os.environ["ATHENA_CATALOG"]        # e.g. "s3tablescatalog/ah-analytics"
ATHENA_DATABASE = os.environ["ATHENA_DATABASE"]       # "ah_analytics"

MAX_ROWS = int(os.environ.get("MAX_ROWS", "1000"))
POLL_INTERVAL_SEC = 0.5
POLL_MAX_SEC = 60.0

athena = boto3.client("athena", region_name=REGION)
glue = boto3.client("glue", region_name=REGION)


def _json_default(obj):
    if isinstance(obj, (datetime.date, datetime.datetime, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


# ---------------------------------------------------------------------------
# Athena helpers
# ---------------------------------------------------------------------------

def _run_query(sql: str, max_rows: int = MAX_ROWS) -> list[dict]:
    resp = athena.start_query_execution(
        QueryString=sql,
        WorkGroup=ATHENA_WORKGROUP,
        QueryExecutionContext={
            "Catalog": ATHENA_CATALOG,
            "Database": ATHENA_DATABASE,
        },
    )
    qid = resp["QueryExecutionId"]

    deadline = time.time() + POLL_MAX_SEC
    while True:
        info = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = info["Status"]["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = info["Status"].get("StateChangeReason", state)
            raise RuntimeError(f"Athena query {state}: {reason}")
        if time.time() > deadline:
            athena.stop_query_execution(QueryExecutionId=qid)
            raise RuntimeError(f"Athena query timed out after {POLL_MAX_SEC}s")
        time.sleep(POLL_INTERVAL_SEC)

    rows: list[dict] = []
    paginator = athena.get_paginator("get_query_results")
    header: list[str] | None = None
    for page in paginator.paginate(QueryExecutionId=qid, PaginationConfig={"MaxItems": max_rows + 1}):
        page_rows = page["ResultSet"]["Rows"]
        if header is None and page_rows:
            header = [c.get("VarCharValue") for c in page_rows[0]["Data"]]
            page_rows = page_rows[1:]
        for r in page_rows:
            values = [c.get("VarCharValue") for c in r["Data"]]
            rows.append(dict(zip(header, values)))
            if len(rows) >= max_rows:
                return rows
    return rows


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_SELECT_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


def execute_sql(arguments: dict) -> list[dict]:
    query = (arguments.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    if not _SELECT_RE.match(query):
        raise ValueError("Only SELECT / WITH queries are allowed")
    query = query.rstrip(";").rstrip()
    return _run_query(query, max_rows=MAX_ROWS)


def _glue_catalog_id() -> str:
    # Glue's federated child catalog id format: "<account>:s3tablescatalog/<bucket>"
    # ATHENA_CATALOG is already "s3tablescatalog/<bucket>"
    return f"{_account_id()}:{ATHENA_CATALOG}"


def list_tables(arguments: dict) -> dict:
    """Return {table_name: [{column, type}, ...]} for every table in the namespace."""
    tables: dict[str, list] = {}
    paginator = glue.get_paginator("get_tables")
    for page in paginator.paginate(CatalogId=_glue_catalog_id(), DatabaseName=ATHENA_DATABASE):
        for t in page.get("TableList", []):
            cols = [
                {"column": c["Name"], "type": c["Type"]}
                for c in t.get("StorageDescriptor", {}).get("Columns", [])
            ]
            tables[t["Name"]] = cols
    return tables


def describe_table(arguments: dict) -> dict:
    table_name = (arguments.get("table_name") or "").strip()
    if not table_name:
        raise ValueError("table_name is required")
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table_name):
        raise ValueError("Invalid table name")

    try:
        t = glue.get_table(
            CatalogId=_glue_catalog_id(),
            DatabaseName=ATHENA_DATABASE,
            Name=table_name,
        )["Table"]
    except glue.exceptions.EntityNotFoundException:
        raise ValueError(f"Table '{table_name}' not found in {ATHENA_DATABASE}")

    columns = [
        {"column": c["Name"], "type": c["Type"], "nullable": True}
        for c in t.get("StorageDescriptor", {}).get("Columns", [])
    ]
    samples = _run_query(f'SELECT * FROM "{table_name}" LIMIT 3', max_rows=3)
    return {"columns": columns, "sample_rows": samples}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_account_id_cache = None


def _account_id() -> str:
    global _account_id_cache
    if _account_id_cache is None:
        _account_id_cache = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    return _account_id_cache


def _infer_tool(event: dict) -> str:
    if "query" in event:
        return "execute_sql"
    if "table_name" in event:
        return "describe_table"
    return "list_tables"


TOOLS = {
    "execute_sql": execute_sql,
    "list_tables": list_tables,
    "describe_table": describe_table,
}


def lambda_handler(event, context):
    logger.info("Event: %s", json.dumps(event, default=str)[:500])
    tool_name = _infer_tool(event)
    logger.info("Inferred tool: %s", tool_name)
    try:
        result = TOOLS[tool_name](event)
        return json.loads(json.dumps({"result": result}, default=_json_default))
    except ValueError as e:
        logger.warning("Tool error: %s", e)
        return {"error": str(e)}
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        return {"error": f"Internal error: {e}"}
