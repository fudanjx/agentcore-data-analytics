# Local S3 Tables pilot UI

This is a local-only browser interface for the isolated S3 Tables pilot bucket
`ah-soc-delta-pilot`, namespace `pilot`. It does not expose an AWS endpoint;
run it only on the operator's Mac with the existing AWS credential chain. Its
current identity check is a trusted development placeholder, not production
authentication.

Start it from the repository root:

```bash
uvicorn s3tables_delta_pilot.webapp:app --host 127.0.0.1 --port 8090
```

Open `http://127.0.0.1:8090`.

The UI lists the S3 Tables buckets and namespaces assigned to the current user,
then lists tables only in the selected scope. It supports creating a new table
from the first selected file schema and appending one selected table per
request. It accepts Parquet, Parquet GZIP, XLSX, XLS, CSV, and TSV; non-Parquet
files are converted locally to Glue-readable Parquet before staging. It performs
a server-side preflight on every submit: additional source columns are ignored,
missing target columns become `NULL`, and schema conversions require explicit
operator confirmation. The sensitive-column/anonymisation scan is currently a
non-mutating placeholder.

New table names must begin with a lowercase letter and contain only `a-z`,
`0-9`, and underscores. Entered hyphens are converted to underscores.

## Identity and authorization placeholder

The local server accepts `X-Pilot-User-Id`. This is a **trusted placeholder**,
not authentication: replace `_current_user()` with verified frontend identity
claims before exposing this outside an operator's machine. Until then,
`local-admin` is the default local identity and has access only to this pilot
bucket/namespace.

The future frontend relationship can be supplied for testing through
`PILOT_USER_ACCESS_JSON`:

```json
{
  "alice": {
    "is_admin": false,
    "buckets": [
      {
        "table_bucket_arn": "arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot",
        "namespace": "pilot",
        "label": "AH pilot"
      }
    ]
  },
  "admin": {
    "is_admin": true,
    "buckets": [
      {
        "table_bucket_arn": "arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot",
        "namespace": "pilot",
        "label": "AH pilot"
      }
    ]
  }
}
```

Each request is restricted to a configured bucket/namespace scope. Only an
`is_admin: true` user sees the delete control and can call the delete API.
Deletion is permanent and the browser requires confirmation.

Uploads are written under a unique
`s3://ah-data-analytics/temp_s3_update/web_ingest/uploads/<request-id>/`
prefix. The local service creates or updates a separate Glue job named
`ah-soc-delta-pilot-web-ingest`, which uses the existing pilot role and the
same append-only Iceberg/S3 Tables configuration. The original SOC delta job
remains unchanged and is still the correct path for its business-key-aware
full-snapshot delta policy.

Generic UI ingestion appends submitted rows after schema/cast validation. It
does not infer a business key or deduplicate across separate requests; that is
intentional because new user-created tables do not have an agreed key contract
yet.
