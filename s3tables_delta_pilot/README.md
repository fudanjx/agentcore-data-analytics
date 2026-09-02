# Local S3 Tables pilot UI

This is a local-only browser interface for the isolated S3 Tables pilot bucket
`ah-soc-delta-pilot`, namespace `pilot`. It does not expose an AWS endpoint;
run it only on the operator's Mac with the existing AWS credential chain. Its
current identity check is a trusted development placeholder, not production
authentication.

Install the local service dependencies:

```bash
.venv/bin/pip install -r s3tables_delta_pilot/requirements.txt
```

Start it from the repository root:

```bash
.venv/bin/uvicorn s3tables_delta_pilot.webapp:app --host 127.0.0.1 --port 8090
```

Open `http://127.0.0.1:8090`.

The UI lists the S3 Tables buckets and namespaces assigned to the current user,
then lists tables only in the selected scope. It supports creating a new table
from the first selected file schema and appending one selected table per
request. It accepts Parquet, Parquet GZIP, XLSX, XLS, CSV, and TSV; non-Parquet
files are converted locally to Glue-readable Parquet before staging. It performs
a server-side preflight on every submit. For an append, every selected file
must contain at least 50% of the initial table's columns after canonical name
normalisation; a non-compliant request is rejected and cannot be overridden.
Only matching columns are appended, additional source columns are ignored, and
missing target columns become `NULL`. Healthcare sanitization is mandatory
before temporary S3 staging.

When the local identity test panel is set to `local-admin`, the destination
section also allows the operator to create an S3 Tables bucket and to create a
one-level namespace in the selected bucket. These control-plane actions are
enforced as administrator-only by the backend; hiding the controls in the
browser is not the authorization boundary. The AWS credential chain used to
start the service must allow `s3tables:CreateTableBucket` and
`s3tables:CreateNamespace`.

All column names are normalised before the first schema is created or an
append is checked: names are lowercased; blanks, `/`, hyphens, and parentheses
become `_`; and collisions are retained deterministically as `column`,
`column_01`, `column_02`, and so on. The preflight panel shows the target and
upload column counts, matches and percentage, sanitised column names, and any
hard rejection reason before the upload action is enabled.

For a new table, the first file is profiled across every populated value in
each column—not merely its first data row. The documented date/time rules are
applied automatically: valid `YYYYMMDD`, `YYYY-MM-DD`, and `YYYY.MM.DD` values
become `DATE`; valid `YYYY-MM-DD HH:mm:ss` values become `TIMESTAMP`; and
time-only `HH:mm:ss` values remain `STRING`. Decimal numbers always become
`DOUBLE`; only wholly integral numbers become `BIGINT`. Healthcare-oriented names such
as surgeon, clinician, specialty, ward, MCR, code, ID, OU, and mode are always
`STRING`, preventing blank Excel columns from creating an incorrect numeric
contract. Only mixed or otherwise ambiguous columns are presented for an
operator type selection, with ephemeral safe samples. The selected
types become the immutable initial table contract; the service validates every
value against those choices before staging, so an unsafe selection is rejected
instead of silently becoming `NULL`. Later uploads always follow that stored
contract: safe conversions proceed automatically, while unsafe values are
rejected before Glue starts. Sanitized identifier, postal, and age fields are
always `STRING` and are not user-selectable.

For a new table, the preflight also presents every stored column as a possible
de-duplication component. It shows up to five ephemeral sample values for
non-sensitive fields, with non-null and distinct-value counts. Samples for
healthcare-sanitized fields remain masked. These review-only values are never
written to S3 staging, Glue arguments, QC reports, or upload history. After an
operator selects a composite key, the local service analyses the complete
incoming dataset before upload can be enabled. It reports exact duplicates,
same-key/different-row conflicts, expected retained rows, and expected skipped
rows without writing to S3 or starting Glue. The operator may revise the key
and repeat the analysis; changing files, types, or key columns invalidates the
required acknowledgement.

The table picker shows every S3 Tables bucket/namespace assigned to the current
user and, for each table, its creation time, last-modified time, and current
Iceberg row count when available. Only administrators can delete a table or
view its recovery history.

## Healthcare sanitization

Every uploaded file is sanitized before it is written to the temporary S3
prefix, for both new-table and append flows. The policy is the union of the AH
and NUH reference scripts, with the approved unified rules:

- Drop patient names, date of birth, phone/fax/contact details, and home-address fields.
- AES-CBC encrypt all detected patient, encounter, CSN, case, bill, HRN, MRN,
  and subsidy/document identifiers as strings.
- Replace `AGE` with five-year bands, capped at `90+`.
- Retain only the first two postal-code digits (`120000`, for example).

The encryption key is read at upload time from Secrets Manager secret
`data-insight-etl-encryption` in `ap-southeast-1`; it is never returned in API,
browser, Glue, or QC output. The local service identity needs
`secretsmanager:GetSecretValue` for that secret.

New ciphertext is marked `enc:v1:`. Existing legacy CBC ciphertext created by
the AH/NUH scripts is recognized with the same key and has `enc:v1:` added
without decrypting or re-encrypting it. This makes every newly staged encrypted
identifier use one representation while preserving the original ciphertext.
Append preflight blocks
the request if the destination table stores a sanitized identifier, postal, or
age field as a non-string type.

New table names must begin with a lowercase letter and contain only `a-z`,
`0-9`, and underscores. Entered hyphens are converted to underscores.

## Identity and authorization placeholder

The local server accepts `X-Pilot-User-Id`. This is a **trusted placeholder**,
not authentication: replace `_current_user()` with verified frontend identity
claims before exposing this outside an operator's machine. Until then,
`local-admin` is the default local identity and dynamically discovers every S3
Tables bucket and one-level namespace visible to the local service's IAM role.
`local-editor` remains limited to `ah-soc-delta-pilot/pilot`.

The expander at the top of the local UI is an identity-integration aid. It
shows the exact user context sent by the browser—currently only the
`X-Pilot-User-Id` header—and separately shows the backend-resolved role and
bucket/namespace grants. The browser never sends `is_admin` or grants. With no
custom access configuration, it offers three local test identities:
`local-admin` (administrator), `local-editor` (scoped editor with recovery), and
`local-unassigned` (expected authorization denial). This panel and the
`/api/dev/identity-profiles` endpoint are strictly local development helpers
and must be removed or replaced by verified claims before external deployment.

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

Each non-admin request is restricted to a configured bucket/namespace scope.
An `is_admin: true` user may select any account-visible table bucket, then any
namespace within it; the backend independently validates both selections.
Only admins see the delete control and can call the delete API. Deletion is
permanent and the browser requires confirmation. Tables without an uploader
schema/de-duplication contract are listed as browse-only; they cannot be
appended, deleted, recovered, or have uploader history viewed through this UI.

Uploads are written under a unique
`s3://ah-data-analytics/temp_s3_update/web_ingest/uploads/<request-id>/`
prefix. The local service creates or updates a separate Glue job named
`ah-soc-delta-pilot-web-ingest`, which uses the existing pilot role and the
same append-only Iceberg/S3 Tables configuration. The original SOC delta job
remains unchanged and is still the correct path for its business-key-aware
full-snapshot delta policy.

For troubleshooting, the exact **sanitized, canonicalized, Glue-compatible
Parquet** submitted to Glue is retained only under its request-scoped
`temp_s3_update/web_ingest/uploads/<request-id>/input/` path. This is both the
Glue staging input and the sole uploader archive; no duplicate object is
written under a separate backup prefix. Raw pre-sanitization upload bytes are
never retained. A prefix-only lifecycle rule retains this archive for 30 days
before expiry.

For tables created after the key-policy enhancement, the first upload defines
both the immutable schema and one or more de-duplication columns. Later uploads
are reduced by that key before any write. A new key is appended; an existing
key with an identical full row is skipped as a duplicate; and an existing key
with different non-key values is skipped as a conflict. QC reports counts and
differing column names only—never row values. Existing tables without a saved
key contract retain the original null-safe full-row de-duplication behavior.
Encrypted case and patient identifiers (for example CSN, case number, HRN, and
MRN) remain masked in the browser but may be selected as de-duplication keys:
the uploader's normalisation and legacy-compatible encryption representation is
stable for equivalent source values.

For sparse legacy records, a composite key is always the full selected tuple:
missing values are retained as explicit blank components. For example,
`(A, B, C, blank)` and `(blank, B, C, D)` are different composite keys and
therefore different records. No full-row fallback is used for key-enabled
tables; all de-duplication and conflict decisions depend only on the selected
composite key.

Before the user acknowledges a new table's key contract, key-impact analysis
reads the raw file locally with Polars. It does not sanitize, encrypt, mask,
stage to S3, or call Glue. The result is therefore a fast source-data estimate
of exact duplicates and same-key conflicts; sanitization remains mandatory only
when the acknowledged upload actually begins.

## Snapshot recovery and upload history

Every uploader-managed create or append is processed by the Glue job as a
recoverable Iceberg transaction:

1. Validate and stage the uploaded files.
2. Capture the master table's current Iceberg snapshot ID and row count.
3. Append an immutable `PROCESSING` event to the namespace-local
   `uploader_upload_history` S3 Table, then write a value-free JSON projection
   for the local UI.
4. Configure S3 Tables snapshot management for the master table with at least
   12 snapshots and a maximum age of 365 days.
5. Append the prepared data atomically, capture the new snapshot and row
   count, then append a `SUCCESS` or `FAILED` audit event.

The audit table is reserved and deliberately hidden from destination selection
and deletion controls. It records upload ID, the free-form user tag, filenames,
original uploader/time, rollback executor/time, previous/new snapshot IDs, row
counts, status, and an error summary. It never records source row values,
identifiers, encryption keys, or temporary object locations.

Administrators see full upload history after selecting a table. The local editor
sees only its own uploader-managed history. A rollback requires a browser
confirmation and is allowed only for the latest successful uploader-managed
update with a recorded prior snapshot. The Glue job calls the Iceberg
`rollback_to_snapshot` procedure, reloads the table, verifies the restored row
count against the selected historical snapshot, and records either
`ROLLED_BACK` or `ROLLBACK_FAILED`. It never tries to delete the bad upload's
rows manually.

The local editor may roll back only its own upload and only when that upload is
also the table's latest successful uploader-managed update. It cannot delete a
table or inspect another user's upload history. The backend enforces these
rules; the UI merely reflects its resolved capabilities.

The local uploader service identity needs the existing table-control rights plus
`s3tables:PutTableMaintenanceConfiguration` for each assigned table bucket.
The operation deliberately runs locally rather than in Glue: Glue 5's bundled
`boto3` can lack the S3 Tables service model. The Glue role still needs its
existing data-plane table read/write permissions.
The UI's history projection is under
`temp_s3_update/web_ingest/upload_history/` and is also scoped by a hash of the
assigned bucket/namespace and the selected table.

The local uploader also needs `s3:GetLifecycleConfiguration` and
`s3:PutLifecycleConfiguration` on `ah-data-analytics` to maintain the
prefix-only 30-day sanitized-upload archive rule.
