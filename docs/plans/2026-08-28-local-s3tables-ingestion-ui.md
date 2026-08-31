# Local S3 Tables ingestion UI design

The first UI is deliberately local-only and targets the isolated S3 Tables
pilot bucket `ah-soc-delta-pilot`, namespace `pilot`. It is served on the
operator's Mac and uses the existing AWS credentials; it must not be exposed
on a network without authentication and authorization.

The browser first lists the namespace tables. An operator either chooses one
existing table for one append request or chooses to create one new table. A
new table's first selected Parquet file establishes its normalised Iceberg
schema. All subsequent source files are projected into that schema: extra
columns are ignored and missing columns become `NULL`.

Before upload, the local backend parses every selected Parquet schema. It
reports name collisions, extra/missing columns, conversions, and unsupported
types. Conversions require an explicit browser confirmation. Sensitive-column
detection and anonymisation are represented by a non-mutating placeholder;
they are not security controls in this pilot.

On approval, each file is uploaded to a unique request-scoped prefix under
`temp_s3_update/web_ingest/uploads/`, along with an aggregate manifest. A
separate generic Glue job creates or appends one selected S3 Table with
validation and atomic Iceberg append/reconciliation. Its QC JSON contains only
aggregate counts/errors and is shown in the UI when the job completes.

The existing SOC-specific job remains untouched. It is still the path for a
full-snapshot SOC feed because it has the reviewed SOC schema and hybrid
business-key delta policy. The generic UI workflow intentionally does not
invent a key or cross-request deduplication for arbitrary new tables.
