import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from app.db import execute_query


@tool(
    "execute_sql",
    "Run a read-only SELECT query against the connected business analytics database and return the results as JSON",
    {"query": str},
)
async def execute_sql(args: dict) -> dict:
    try:
        rows = execute_query(args["query"])
        return {"content": [{"type": "text", "text": json.dumps(rows, default=str)}]}
    except ValueError as e:
        return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
    except RuntimeError as e:
        return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Query failed: {e}"}], "is_error": True}


sql_server = create_sdk_mcp_server(
    name="db_tools",
    version="1.0.0",
    tools=[execute_sql],
)
