# AgentCore Dify Proxy

The proxy consumes the Strands Runtime's final `model_usage` sideband event. `model_usage.py` owns PostgreSQL configuration, table creation, and inserts, while `dify-server.py` keeps only the event handling and Dify-compatible token projection. The internal record is not forwarded to Dify; Dify receives only its existing OpenAI-compatible `usage` object with `prompt_tokens`, `completion_tokens`, and `total_tokens`.

Persistence is disabled when `MODEL_USAGE_DATABASE_URL` is empty. Configure one standard PostgreSQL connection URL:

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MODEL_USAGE_DATABASE_URL` | Empty | Full PostgreSQL URL; the user needs permission to create the table and insert rows |

The URL can contain the database, SSL mode, and connection timeout:

```text
postgresql://username:password@postgres-hostname:5432/postgres?sslmode=require&connect_timeout=5
```

Percent-encode reserved characters in the username or password before placing them in a URL.

The deployment reads the URL from the optional `agentcore-dify-proxy-model-usage-db` Kubernetes Secret. Create it before deploying to enable persistence:

```bash
kubectl create secret generic agentcore-dify-proxy-model-usage-db \
  --namespace agentcore \
  --from-literal=database-url='postgresql://username:password@postgres-hostname:5432/postgres?sslmode=require&connect_timeout=5'
```

Do not place the password directly in `deployment.yaml`. Database connection or insert failures are logged, while the user-facing Dify response continues with the compatible aggregate usage data.
