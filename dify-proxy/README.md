# AgentCore Dify Proxy

The proxy consumes the Strands Runtime's final `model_usage` sideband event. `model_usage.py` owns PostgreSQL configuration, table creation, user lookup, and inserts, while `dify-server.py` keeps only the event handling and Dify-compatible token projection. The internal record is not forwarded to Dify; Dify receives only its existing OpenAI-compatible `usage` object with `prompt_tokens`, `completion_tokens`, and `total_tokens`.

Persistence is disabled when `MODEL_USAGE_DATABASE_URL` is empty. Configure one standard PostgreSQL connection URL:

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MODEL_USAGE_DATABASE_URL` | Empty | Full PostgreSQL URL; the user needs permission to create the table and insert rows |

The URL can contain the database, SSL mode, and connection timeout:

```text
postgresql://username:password@postgres-hostname:5432/nuhs?sslmode=disable&connect_timeout=5
```

Percent-encode reserved characters in the username or password before placing them in a URL.

For every usage event, the proxy uses the configured `nuhs` connection for the `model_usage` insert. It also derives a second URL for the `dify` database on the same PostgreSQL server and resolves `user_email` with:

```sql
SELECT session_id FROM end_users WHERE id = :user_id;
```

The resulting `end_users.session_id` is stored as `model_usage.user_email`. The configured database user therefore needs `SELECT` permission on `end_users` in the `dify` database, plus table-creation, migration, and insert permission in `nuhs`. Lookup or insert failures are logged, while the user-facing Dify response continues with the compatible aggregate usage data.
