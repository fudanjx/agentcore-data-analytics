"""MCP Lambda handler for AgentCore Gateway -> Athena over S3 Tables.

The Gateway exposes read-only AH and NUH analytics tools. Small SQL results
can be returned to an agent directly. Larger results must use the export tool:
Athena writes the CSV to its result location and this Lambda returns only
compact metadata, never a partial row set.
"""

import datetime
import decimal
import json
import logging
import os
import re
import time
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-southeast-1")

# AH remains the default for backwards compatibility with existing callers.
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "ah-s3tables-wg")
ATHENA_CATALOG = os.environ.get("ATHENA_CATALOG", "s3tablescatalog/ah-analytics")
ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "ah")
NUH_ATHENA_WORKGROUP = os.environ.get("NUH_ATHENA_WORKGROUP", ATHENA_WORKGROUP)
NUH_ATHENA_CATALOG = os.environ.get("NUH_ATHENA_CATALOG", "s3tablescatalog/nuh-analytics")
NUH_ATHENA_DATABASE = os.environ.get("NUH_ATHENA_DATABASE", "nuh")
DEFAULT_SOURCE = os.environ.get("DEFAULT_SOURCE", "ah").strip().lower()

SOURCES = {
    "ah": {"label": "AH", "workgroup": ATHENA_WORKGROUP, "catalog": ATHENA_CATALOG, "database": ATHENA_DATABASE},
    "nuh": {"label": "NUH", "workgroup": NUH_ATHENA_WORKGROUP, "catalog": NUH_ATHENA_CATALOG, "database": NUH_ATHENA_DATABASE},
}

# One extra row is requested below so this is a hard safety limit, not a
# silent truncation point. The export path has no row-return limit.
MAX_DIRECT_ROWS = int(os.environ.get("MAX_ROWS", "1000"))
POLL_INTERVAL_SEC = 0.5
POLL_MAX_SEC = 60.0

athena = boto3.client("athena", region_name=REGION)
glue = boto3.client("glue", region_name=REGION)


def _json_default(obj: Any):
    if isinstance(obj, (datetime.date, datetime.datetime, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _resolve_source(arguments: dict) -> str:
    source = str(arguments.get("source") or DEFAULT_SOURCE).strip().lower()
    if source not in SOURCES:
        raise ValueError("source must be 'ah' or 'nuh'")
    return source


def _validate_select_query(arguments: dict) -> str:
    query = (arguments.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    if not re.match(r"^\s*(SELECT|WITH)\b", query, re.IGNORECASE):
        raise ValueError("Only SELECT / WITH queries are allowed")
    return query.rstrip(";").rstrip()


def _start_and_wait(sql: str, source: str) -> tuple[str, dict]:
    """Run a query and return its Athena execution identifier and metadata."""
    cfg = SOURCES[source]
    response = athena.start_query_execution(
        QueryString=sql,
        WorkGroup=cfg["workgroup"],
        QueryExecutionContext={"Catalog": cfg["catalog"], "Database": cfg["database"]},
    )
    query_execution_id = response["QueryExecutionId"]
    deadline = time.time() + POLL_MAX_SEC
    while True:
        execution = athena.get_query_execution(QueryExecutionId=query_execution_id)["QueryExecution"]
        state = execution["Status"]["State"]
        if state == "SUCCEEDED":
            return query_execution_id, execution
        if state in ("FAILED", "CANCELLED"):
            reason = execution["Status"].get("StateChangeReason", state)
            raise RuntimeError(f"Athena query {state}: {reason}")
        if time.time() > deadline:
            athena.stop_query_execution(QueryExecutionId=query_execution_id)
            raise RuntimeError(f"Athena query timed out after {POLL_MAX_SEC}s")
        time.sleep(POLL_INTERVAL_SEC)


def _read_direct_rows(query_execution_id: str, max_rows: int) -> list[dict]:
    """Read at most one row beyond the cap and fail closed if it is exceeded."""
    rows: list[dict] = []
    header: list[str] | None = None
    paginator = athena.get_paginator("get_query_results")
    # Include one header row and one sentinel data row above the hard cap.
    for page in paginator.paginate(
        QueryExecutionId=query_execution_id,
        PaginationConfig={"MaxItems": max_rows + 2},
    ):
        page_rows = page["ResultSet"].get("Rows", [])
        if header is None and page_rows:
            header = [cell.get("VarCharValue") for cell in page_rows[0].get("Data", [])]
            page_rows = page_rows[1:]
        for row in page_rows:
            values = [cell.get("VarCharValue") for cell in row.get("Data", [])]
            rows.append(dict(zip(header or [], values)))
            if len(rows) > max_rows:
                raise ValueError(
                    f"Result exceeds the direct-response limit of {max_rows} rows. "
                    "Use execute_sql_export to return an S3 CSV URI instead."
                )
    return rows


def _run_direct_query(sql: str, source: str, max_rows: int = MAX_DIRECT_ROWS) -> list[dict]:
    query_execution_id, _ = _start_and_wait(sql, source)
    return _read_direct_rows(query_execution_id, max_rows)


def _export_metadata(query_execution_id: str, source: str, execution: dict) -> dict:
    output_location = execution.get("ResultConfiguration", {}).get("OutputLocation", "").strip()
    if not output_location.startswith("s3://"):
        raise RuntimeError("Athena did not provide an S3 result location")
    statistics = execution.get("Statistics", {})
    return {
        "query_execution_id": query_execution_id,
        "source": source,
        "result_s3_uri": output_location,
        "status": "SUCCEEDED",
        "data_scanned_bytes": statistics.get("DataScannedInBytes", 0),
        "engine_execution_time_ms": statistics.get("EngineExecutionTimeInMillis", 0),
        "instruction": "Download result_s3_uri with Code Interpreter and process it locally. Do not request the CSV rows in the model context.",
    }


def execute_sql(arguments: dict) -> list[dict]:
    """Return a small SQL result, rejecting oversized results rather than truncating."""
    source = _resolve_source(arguments)
    return _run_direct_query(_validate_select_query(arguments), source)


def execute_sql_export(arguments: dict) -> dict:
    """Run read-only SQL and return only Athena's S3 CSV result metadata."""
    source = _resolve_source(arguments)
    query_execution_id, execution = _start_and_wait(_validate_select_query(arguments), source)
    return _export_metadata(query_execution_id, source, execution)


def _glue_catalog_id(catalog: str) -> str:
    return f"{_account_id()}:{catalog}"


def list_tables(arguments: dict) -> dict:
    source = _resolve_source(arguments)
    cfg = SOURCES[source]
    tables: dict[str, list] = {}
    paginator = glue.get_paginator("get_tables")
    for page in paginator.paginate(CatalogId=_glue_catalog_id(cfg["catalog"]), DatabaseName=cfg["database"]):
        for table in page.get("TableList", []):
            tables[table["Name"]] = [
                {"column": column["Name"], "type": column["Type"]}
                for column in table.get("StorageDescriptor", {}).get("Columns", [])
            ]
    return tables


def describe_table(arguments: dict) -> dict:
    source = _resolve_source(arguments)
    cfg = SOURCES[source]
    table_name = (arguments.get("table_name") or "").strip()
    if not table_name:
        raise ValueError("table_name is required")
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table_name):
        raise ValueError("Invalid table name")
    try:
        table = glue.get_table(CatalogId=_glue_catalog_id(cfg["catalog"]), DatabaseName=cfg["database"], Name=table_name)["Table"]
    except glue.exceptions.EntityNotFoundException:
        raise ValueError(f"Table '{table_name}' not found in {cfg['database']}")
    columns = [
        {"column": column["Name"], "type": column["Type"], "nullable": True}
        for column in table.get("StorageDescriptor", {}).get("Columns", [])
    ]
    samples = _run_direct_query(f'SELECT * FROM "{table_name}" LIMIT 3', source=source, max_rows=3)
    return {"columns": columns, "sample_rows": samples}


_account_id_cache: str | None = None


def _account_id() -> str:
    global _account_id_cache
    if _account_id_cache is None:
        _account_id_cache = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    return _account_id_cache


def _infer_tool(event: dict) -> str:
    """Gateway sends arguments directly, so export carries a required marker."""
    if event.get("export") is True:
        return "execute_sql_export"
    if "query" in event:
        return "execute_sql"
    if "table_name" in event:
        return "describe_table"
    return "list_tables"


TOOLS = {
    "execute_sql": execute_sql,
    "execute_sql_export": execute_sql_export,
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
    except ValueError as error:
        logger.warning("Tool error: %s", error)
        return {"error": str(error)}
    except Exception as error:
        logger.error("Unexpected error: %s", error, exc_info=True)
        return {"error": f"Internal error: {error}"}
