"""
MCP Lambda handler for AgentCore Gateway → RDS nuh-analytics.

Exposes three read-only tools:
  execute_sql    — run a SELECT query, return rows as JSON
  list_tables    — list all tables with column types
  describe_table — column info + 3 sample rows for one table

AgentCore Gateway invokes this Lambda with:
  {"tool": "<tool_name>", "arguments": {<args>}}

Returns:
  {"result": <data>}  on success
  {"error": "<msg>"}  on failure
"""

import json
import logging
import os
import re

import boto3
import psycopg2
import psycopg2.extras

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
SECRET_ARN = os.environ["SECRET_ARN"]
DB_NAME = os.environ.get("DB_NAME", "nuh-analytics")

_creds_cache = None


def _get_creds() -> dict:
    global _creds_cache
    if _creds_cache is None:
        sm = boto3.client("secretsmanager", region_name=REGION)
        _creds_cache = json.loads(
            sm.get_secret_value(SecretId=SECRET_ARN)["SecretString"]
        )
    return _creds_cache


def _get_conn():
    creds = _get_creds()
    return psycopg2.connect(
        host=creds["host"],
        port=int(creds.get("port", 5432)),
        user=creds["username"],
        password=creds["password"],
        dbname=DB_NAME,
        connect_timeout=10,
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def execute_sql(arguments: dict) -> list:
    query = arguments.get("query", "").strip()
    if not query:
        raise ValueError("query is required")
    upper = query.upper().lstrip()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise ValueError("Only SELECT queries are allowed")

    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query)
            rows = cur.fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.close()


def list_tables(arguments: dict) -> dict:
    sql = """
        SELECT c.table_name, c.column_name, c.data_type
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON c.table_name = t.table_name AND c.table_schema = t.table_schema
        WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
        ORDER BY c.table_name, c.ordinal_position
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.close()

    tables: dict[str, list] = {}
    for table_name, column_name, data_type in rows:
        tables.setdefault(table_name, []).append(
            {"column": column_name, "type": data_type}
        )
    return tables


def describe_table(arguments: dict) -> dict:
    table_name = arguments.get("table_name", "").strip()
    if not table_name:
        raise ValueError("table_name is required")

    # Validate table name (alphanumeric + underscore only — prevent injection)
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table_name):
        raise ValueError("Invalid table name")

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # Column info
            cur.execute("""
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
            """, (table_name,))
            columns = [
                {"column": r[0], "type": r[1], "nullable": r[2] == "YES"}
                for r in cur.fetchall()
            ]
            if not columns:
                raise ValueError(f"Table '{table_name}' not found in public schema")

            # Sample rows
            cur.execute(
                f'SELECT * FROM "{table_name}" LIMIT 3'
            )
            col_names = [desc[0] for desc in cur.description]
            samples = [dict(zip(col_names, row)) for row in cur.fetchall()]
    finally:
        conn.close()

    return {"columns": columns, "sample_rows": samples}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _infer_tool(event: dict) -> str:
    """
    AgentCore Gateway sends only the tool arguments as the top-level event.
    Infer which tool was called from the argument shape:
      - has "query"       → execute_sql
      - has "table_name"  → describe_table
      - empty / no keys   → list_tables
    """
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

    # AgentCore Gateway sends tool arguments directly as the event body.
    # Infer the tool from the argument shape.
    tool_name = _infer_tool(event)
    logger.info("Inferred tool: %s", tool_name)

    try:
        result = TOOLS[tool_name](event)
        return {"result": result}
    except ValueError as e:
        logger.warning("Tool error: %s", e)
        return {"error": str(e)}
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        return {"error": f"Internal error: {e}"}
