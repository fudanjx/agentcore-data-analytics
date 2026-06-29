import json
import os

import boto3
import psycopg2
import psycopg2.extras


def get_connection():
    secret_arn = os.environ.get("RDS_SECRET_ARN")
    if not secret_arn:
        raise RuntimeError("RDS not configured: RDS_SECRET_ARN env var is not set")

    # Parse region from the ARN so the client always targets the right region
    # ARN format: arn:aws:secretsmanager:<region>:<account>:secret:<name>
    arn_parts = secret_arn.split(":")
    secret_region = arn_parts[3] if len(arn_parts) >= 4 else os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    client = boto3.client("secretsmanager", region_name=secret_region)
    try:
        response = client.get_secret_value(SecretId=secret_arn)
    except client.exceptions.ResourceNotFoundException:
        raise RuntimeError(f"RDS not configured: secret not found in Secrets Manager ({secret_arn})")
    creds = json.loads(response["SecretString"])

    return psycopg2.connect(
        host=creds["host"],
        port=int(creds.get("port", 5432)),
        dbname=os.environ.get("RDS_DB_NAME") or creds.get("dbname"),
        user=creds["username"],
        password=creds["password"],
        connect_timeout=10,
    )


def get_schema() -> str:
    """Return a human-readable schema string for all user tables in the database."""
    sql = """
        SELECT
            c.table_name,
            c.column_name,
            c.data_type,
            c.is_nullable
        FROM information_schema.columns c
        JOIN information_schema.tables t
            ON c.table_name = t.table_name
            AND c.table_schema = t.table_schema
        WHERE c.table_schema = 'public'
          AND t.table_type = 'BASE TABLE'
        ORDER BY c.table_name, c.ordinal_position
    """
    rows = execute_query(sql)

    tables: dict[str, list[str]] = {}
    for row in rows:
        tbl = row["table_name"]
        nullable = "" if row["is_nullable"] == "YES" else " NOT NULL"
        col = f"{row['column_name']} {row['data_type'].upper()}{nullable}"
        tables.setdefault(tbl, []).append(col)

    if not tables:
        return "No tables found in the public schema."

    lines = []
    for table, columns in tables.items():
        lines.append(f"Table: {table} ({', '.join(columns)})")
    return "\n".join(lines)


def execute_query(sql: str) -> list[dict]:
    """Execute a read-only SQL query and return rows as a list of dicts."""
    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT") and not stripped.startswith("WITH"):
        raise ValueError("Only SELECT queries are allowed")

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            return [dict(row) for row in rows]
    finally:
        conn.close()
