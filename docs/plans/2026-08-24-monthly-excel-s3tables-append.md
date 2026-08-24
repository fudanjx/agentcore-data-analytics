# Monthly Excel to S3 Tables Append Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Safely append each approved monthly Excel extract to an existing AH S3 Table without changing its schema, duplicating a file on retry, or rewriting historical data.

**Architecture:** A manually started, short-lived ECS Fargate task reads one immutable Excel object from a normal S3 landing bucket, validates and coerces it against the live Iceberg table schema loaded through the S3 Tables REST catalog, and commits one `append` snapshot. The job writes an auditable run record and QC report; it is idempotent by S3 object version and checksum. Keep approval/manual invocation for the first 2–3 monthly cycles, then optionally place the same task behind SQS/Step Functions for event-driven execution.

**Tech Stack:** Python 3.12, pandas/openpyxl, PyArrow, PyIceberg REST catalog with SigV4, Amazon S3, Amazon S3 Tables (Iceberg), ECS Fargate, DynamoDB, CloudWatch, Athena.

---

## Design decisions and non-negotiable rules

* Do **not** upload an XLSX file into the S3 Table bucket or reuse `lambda_s3tables_loader/handler.py`: it only accepts Parquet and calls `table.overwrite(...)`, which would replace historical rows. It also defaults to `ah_analytics`, but the deployed AH namespace is `ah`.
* For every run, load the target table first and treat `table.schema()` and its partition specification as the source of truth. Do not create a schema from Excel dtype inference.
* The task must create an Arrow table using `target.schema().as_arrow()`, in the existing field order, then call `target.append(...)` exactly once for a successful source object. This creates an additive Iceberg snapshot and does not rewrite prior snapshots.
* Store monthly input files in a versioned normal S3 landing bucket/prefix, for example `s3://ah-data-analytics/monthly-excel/<table>/year=YYYY/month=MM/<source>.xlsx`. S3 Tables data and metadata remain service-managed.
* Reject unexpected headers, duplicate headers after normalisation, invalid values, missing mandatory business columns, and duplicate business keys inside a file. A missing target field is filled with null only when it is explicitly allowed in the table contract and the Iceberg field is optional.
* Plain append is valid only for a new, non-overlapping delivery. If a supplier redelivers a prior month with corrections, route it to a separate correction/upsert workflow; never silently append it.

## Live baseline to preserve

The live table bucket is `arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-analytics`; its current AH namespace is `ah`. The append configuration must target the existing table names: `admission`, `discharge`, `inflight`, `outpatient`, `procedure`, and `urgentcarecenter`. Record each target's live schema, partition transform, current snapshot ID, and row count during preflight; do not rely on the older code constant `NAMESPACE="ah_analytics"`.

### Task 1: Capture the deployed data contract and monthly-source policy

**Files:**
- Create: `monthly_s3tables_append/contracts/ah.yaml`
- Create: `monthly_s3tables_append/inspect_target.py`
- Create: `tests/monthly_s3tables_append/test_contract.py`
- Modify: `requirements.txt`

**Step 1: Write the failing contract tests**

```python
def test_contract_has_an_explicit_mapping_and_key_for_every_target():
    contract = load_contract("monthly_s3tables_append/contracts/ah.yaml")
    for target in contract.tables.values():
        assert target.excel_sheet
        assert target.header_mapping
        assert target.business_key
        assert target.month_column


def test_contract_refuses_a_mapping_to_an_unknown_target_field():
    contract = contract_with_mapping({"Source ID": "not_in_iceberg"})
    with pytest.raises(ContractError, match="unknown target field"):
        validate_contract_against_schema(contract, target_arrow_schema())
```

**Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/monthly_s3tables_append/test_contract.py -v`

Expected: FAIL because the monthly append package and contract loader do not yet exist.

**Step 3: Add the explicit, reviewed table contract**

Create a YAML entry per table with: the target table name; exact Excel worksheet name; raw header-to-lowercase-Iceberg-field mapping; approved nullable target fields; the business deduplication key; month/date column; allowed date formats; and whether the monthly source is incremental-only. Do not derive this mapping automatically from headers. Include an `inspect_target.py` command that connects to the S3 Tables REST endpoint, prints the live Arrow/Iceberg schema, partition spec, and snapshot ID as JSON, and makes contract review possible before each supplier/template change.

Pin `openpyxl` in `requirements.txt` and add the same dependency to the container requirements. Preserve strings such as MRN, encounter ID, postcode, and any identifiers with leading zeros by declaring them strings in the contract.

**Step 4: Run the contract tests**

Run: `python3 -m pytest tests/monthly_s3tables_append/test_contract.py -v`

Expected: PASS. A mapping to a non-existent deployed field fails before any S3 Tables write.

**Step 5: Perform the read-only live baseline**

Run: `python3 -m monthly_s3tables_append.inspect_target --bucket-arn arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-analytics --namespace ah --table outpatient --output reports/outpatient-live-contract.json`

Expected: JSON contains table schema, partition spec, current snapshot ID, and table identifier `ah.outpatient`; no data is modified.

**Step 6: Commit**

```bash
git add requirements.txt monthly_s3tables_append/contracts/ah.yaml monthly_s3tables_append/inspect_target.py tests/monthly_s3tables_append/test_contract.py
git commit -m "feat: define monthly S3 Tables append contracts"
```

### Task 2: Build strict Excel-to-target-schema normalisation

**Files:**
- Create: `monthly_s3tables_append/excel_normalise.py`
- Create: `tests/monthly_s3tables_append/test_excel_normalise.py`

**Step 1: Write the failing normalisation tests**

```python
def test_normalise_reorders_and_casts_using_the_existing_arrow_schema():
    target = pa.schema([pa.field("visit_id", pa.string()), pa.field("visit_date", pa.timestamp("us")), pa.field("age", pa.int64())])
    result = normalise_excel_frame(pd.DataFrame({"Age": ["41"], "Visit date": ["2026-08-01"], "Visit ID": ["00017"]}), contract_for(target), target)
    assert result.schema == target
    assert result.to_pylist() == [{"visit_id": "00017", "visit_date": datetime(2026, 8, 1), "age": 41}]


def test_normalise_rejects_unknown_or_colliding_headers():
    with pytest.raises(SchemaValidationError, match="unexpected|duplicate"):
        normalise_excel_frame(pd.DataFrame([[1]], columns=["Visit ID", "visit-id"]), contract_for_schema())
```

**Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/monthly_s3tables_append/test_excel_normalise.py -v`

Expected: FAIL because `normalise_excel_frame` does not yet exist.

**Step 3: Implement normalisation and QC output**

Implement these ordered operations:

1. Read the configured worksheet with `dtype=str`, preserving source text and leading zeros; remove only rows that are entirely blank.
2. Canonicalise source header whitespace/case only for matching, then apply the approved mapping. Reject a header collision instead of appending a suffix.
3. Reject mapped-but-unknown input columns and missing mandatory input fields. Add only contract-approved missing optional target fields as null.
4. Coerce each field with a type-specific converter driven by the existing Arrow schema: strict integers/decimals, booleans, exact date/timestamp formats using `Asia/Singapore` for naive local dates, and strings without destructive number conversion. Report row numbers and raw values for every failed conversion.
5. Check nullability, monthly date-range policy, and duplicate business keys. Generate a JSON/CSV QC report with source key/version/checksum, input/accepted/rejected row counts, detected headers, target schema fingerprint, date range, and null/parse-error counts.
6. Select the target field order and build `pa.Table.from_pandas(..., schema=target_arrow_schema, preserve_index=False, safe=True)`.

Do not truncate strings, replace nulls with empty strings, infer `dayfirst`, or allow `safe=False` casts.

**Step 4: Run the tests**

Run: `python3 -m pytest tests/monthly_s3tables_append/test_excel_normalise.py -v`

Expected: PASS, including preservation of `"00017"` and rejection of bad numeric/date cells.

**Step 5: Commit**

```bash
git add monthly_s3tables_append/excel_normalise.py tests/monthly_s3tables_append/test_excel_normalise.py
git commit -m "feat: validate Excel data against existing Iceberg schema"
```

### Task 3: Add an idempotent, append-only Iceberg writer

**Files:**
- Create: `monthly_s3tables_append/catalog.py`
- Create: `monthly_s3tables_append/ledger.py`
- Create: `monthly_s3tables_append/append_job.py`
- Create: `tests/monthly_s3tables_append/test_append_job.py`
- Create: `tests/monthly_s3tables_append/test_ledger.py`

**Step 1: Write the failing append/idempotency tests**

```python
def test_new_approved_object_appends_once_with_source_provenance(fake_table, source_object):
    report = append_source(source_object, fake_table, approved_arrow_table())
    fake_table.append.assert_called_once()
    assert report.status == "committed"
    assert report.snapshot_properties["source_version_id"] == source_object.version_id


def test_retry_after_a_successful_commit_is_skipped(fake_table, committed_source_object):
    assert append_source(committed_source_object, fake_table, approved_arrow_table()).status == "already_committed"
    fake_table.append.assert_not_called()
```

**Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/monthly_s3tables_append/test_append_job.py tests/monthly_s3tables_append/test_ledger.py -v`

Expected: FAIL because no append job or source ledger exists.

**Step 3: Implement the transactional append path**

Create a REST catalog with the existing S3 Tables endpoint, bucket ARN, and SigV4 settings already used by the repository's S3 Tables code. `load_table((namespace, table))` must be used; `create_table`, `overwrite`, and schema evolution are prohibited in this job.

Create a DynamoDB ledger whose primary key is `<table-bucket-arn>#<namespace>#<table>#<source-key>#<source-version-id>`, with SHA-256/ETag, status, input rows, appended rows, schema fingerprint, started/completed timestamps, snapshot ID, and QC-report URI. Use a conditional write to acquire a `RUNNING` record. Before retrying a `RUNNING`/unknown run, inspect Iceberg snapshot summary properties for the same source version/checksum; if found, finalise the ledger as committed without re-appending.

After QC passes, call:

```python
target.append(
    arrow_table,
    snapshot_properties={
        "source_key": source.key,
        "source_version_id": source.version_id,
        "source_sha256": source.sha256,
        "ingestion_run_id": run_id,
        "schema_fingerprint": fingerprint,
    },
)
```

Reload the target and record the new snapshot ID. Retry only optimistic-concurrency/maintenance conflicts with exponential backoff and jitter; do not retry QC failures. An input object must remain immutable/versioned so the same key with a changed version is a different candidate run.

**Step 4: Run the tests**

Run: `python3 -m pytest tests/monthly_s3tables_append/test_append_job.py tests/monthly_s3tables_append/test_ledger.py -v`

Expected: PASS. An already committed object never produces a second `append` call.

**Step 5: Commit**

```bash
git add monthly_s3tables_append/catalog.py monthly_s3tables_append/ledger.py monthly_s3tables_append/append_job.py tests/monthly_s3tables_append/test_append_job.py tests/monthly_s3tables_append/test_ledger.py
git commit -m "feat: append monthly Excel batches idempotently"
```

### Task 4: Package a manual monthly Fargate task and least-privilege infrastructure

**Files:**
- Create: `monthly_s3tables_append/Dockerfile`
- Create: `monthly_s3tables_append/requirements.txt`
- Create: `monthly_s3tables_append/cli.py`
- Create: `infra/deploy_monthly_s3tables_append.py`
- Create: `infra/monthly_s3tables_append_task.json`
- Create: `tests/monthly_s3tables_append/test_cli.py`

**Step 1: Write the failing CLI tests**

```python
def test_dry_run_validates_and_writes_qc_but_never_appends(monkeypatch):
    result = cli_main(["--source", "s3://ah-data-analytics/monthly-excel/outpatient/year=2026/month=08/file.xlsx", "--table", "outpatient", "--dry-run"])
    assert result.exit_code == 0
    assert append_mock.call_count == 0
```

**Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/monthly_s3tables_append/test_cli.py -v`

Expected: FAIL because the job CLI does not exist.

**Step 3: Implement the task and deploy definition**

Use the existing `embedded-web-app` Fargate cluster and private networking pattern. The command accepts `--source`, `--table`, `--dry-run`, and `--commit`; `--commit` requires an already successful QC report for the exact object version. It writes reports to a dedicated audit prefix, never to S3 Tables managed paths.

Grant the task role only: `s3:GetObject`/`GetObjectVersion` for `monthly-excel/*`; `s3:PutObject` for the audit prefix; DynamoDB ledger read/conditional write; CloudWatch logs; and the minimum `s3tables` read/write data/metadata actions scoped to the `ah-analytics` table bucket and its tables. Do not grant `CreateTable`, `DeleteTable`, broad source-bucket write, or generic `s3:*`.

Set the input bucket to versioned, block public access, and use an IAM-controlled uploader role. Make object ownership/retention and source-file naming part of the operating procedure. Build a Linux/amd64 image, deploy a task definition with enough CPU/memory/ephemeral disk for the largest expected XLSX, and run no service continuously.

**Step 4: Run the tests**

Run: `python3 -m pytest tests/monthly_s3tables_append/test_cli.py -v`

Expected: PASS. `--dry-run` emits the full QC report and cannot call the writer.

**Step 5: Commit**

```bash
git add monthly_s3tables_append/Dockerfile monthly_s3tables_append/requirements.txt monthly_s3tables_append/cli.py infra/deploy_monthly_s3tables_append.py infra/monthly_s3tables_append_task.json tests/monthly_s3tables_append/test_cli.py
git commit -m "feat: package monthly S3 Tables append task"
```

### Task 5: Verify the whole monthly run against a disposable test table

**Files:**
- Create: `monthly_s3tables_append/smoke_test.py`
- Create: `docs/runbooks/monthly-s3tables-append.md`
- Create: `tests/monthly_s3tables_append/test_smoke_test.py`

**Step 1: Write the failing end-to-end verification test**

```python
def test_smoke_test_requires_row_delta_schema_match_and_one_source_snapshot():
    result = verify_append(before_rows=100, after_rows=103, accepted_rows=3, schema_match=True, source_snapshot_count=1)
    assert result.ok
```

**Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/monthly_s3tables_append/test_smoke_test.py -v`

Expected: FAIL because the verification helper does not exist.

**Step 3: Implement the verification and runbook**

The smoke test must create/use a dedicated non-production test table with the same schema/partition spec as one production target. Upload a two- or three-row XLSX fixture, run dry-run then commit, and verify through Iceberg/Athena:

1. Target schema and partition spec are unchanged.
2. `after_row_count == before_row_count + accepted_rows`.
3. The committed snapshot contains the exact input source version/checksum and run ID.
4. Re-running the exact same source version makes no row-count or snapshot change.
5. An unknown column, invalid date, and duplicate business key each fail before a commit.
6. The production source data and production table receive no test write.

Document the monthly checklist: upload immutable input; run `--dry-run`; review QC totals, date range, and duplicate count; run `--commit`; run Athena verification; retain the source/report/snapshot ID; and use the correction workflow for any restatement. Include a preflight question: is this truly new data, or does it overlap/restate a past period?

**Step 4: Run the tests**

Run: `python3 -m pytest tests/monthly_s3tables_append/test_smoke_test.py -v`

Expected: PASS.

**Step 5: Run the deployment smoke test**

Run: `python3 -m monthly_s3tables_append.smoke_test --environment test --table outpatient`

Expected: one append snapshot for the fixture, a matching row delta, then an idempotent no-op on retry.

**Step 6: Commit**

```bash
git add monthly_s3tables_append/smoke_test.py docs/runbooks/monthly-s3tables-append.md tests/monthly_s3tables_append/test_smoke_test.py
git commit -m "test: verify monthly S3 Tables append workflow"
```

### Task 6: Add automation only after the manual process is proven

**Files:**
- Create: `infra/deploy_monthly_s3tables_append_automation.py`
- Modify: `docs/runbooks/monthly-s3tables-append.md`

**Step 1: Write the failing event-filter test**

```python
def test_only_approved_versioned_xlsx_landing_objects_are_dispatched():
    assert accepts_event(s3_event("monthly-excel/outpatient/year=2026/month=08/a.xlsx", version="v1", tags={"approved": "true"}))
    assert not accepts_event(s3_event("monthly-excel/outpatient/a.csv", version="v1", tags={"approved": "true"}))
```

**Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/monthly_s3tables_append/test_automation.py -v`

Expected: FAIL because the dispatcher does not exist.

**Step 3: Implement the optional automation**

After successful manual cycles, add a narrowly filtered S3 event → SQS → dispatcher/Step Functions path for `.xlsx` objects under `monthly-excel/` that carry an `approved=true` tag. The same Fargate task and DynamoDB idempotency key remain the only writer. Merge, rather than replace, existing S3 notification configuration, because the repository already has AH notifications for Parquet ingestion. Configure a DLQ and alarm on QC/append failures; do not make a failed validation auto-retry indefinitely.

**Step 4: Run the tests**

Run: `python3 -m pytest tests/monthly_s3tables_append/test_automation.py -v`

Expected: PASS. Unapproved objects and non-XLSX files never start a job.

**Step 5: Commit**

```bash
git add infra/deploy_monthly_s3tables_append_automation.py docs/runbooks/monthly-s3tables-append.md tests/monthly_s3tables_append/test_automation.py
git commit -m "feat: automate approved monthly append jobs"
```

## Acceptance criteria

* A valid Excel delivery appends only its validated rows and adds exactly one attributable Iceberg snapshot.
* Existing table schema, field IDs, partition specification, and prior data remain unchanged.
* The ingest process derives the output Arrow schema from the live target table, not pandas or Excel.
* Replaying the same S3 object version is a no-op, including after a task timeout or a compaction conflict.
* A source-schema drift, conversion error, duplicate key, or unexpected overlapping/restated period fails with a usable QC report and zero target-table writes.
* Query-time maintenance remains enabled. S3 Tables automatically compacts small files and manages snapshots; the ingestion task retries optimistic-concurrency conflicts rather than disabling maintenance.
