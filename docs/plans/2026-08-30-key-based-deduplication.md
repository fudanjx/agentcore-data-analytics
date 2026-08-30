# Key-Based De-duplication Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the first upload define immutable single- or multi-column de-duplication keys, then skip existing-key rows and report non-key differences as conflicts.

**Architecture:** Store `deduplication_columns` with each new table's schema contract and pass it in the Glue manifest. The Glue job will preserve legacy full-row de-duplication when an older contract has no key; key-enabled tables will append only rows whose selected key is absent and produce value-free aggregate conflict metrics.

**Tech Stack:** FastAPI/Pydantic, browser JavaScript/CSS, PyArrow/Pandas preflight, AWS Glue Spark/Iceberg, Python unittest.

---

### Task 1: Contract and preflight key candidates

**Files:**

- Modify: `s3tables_delta_pilot/webapp.py`
- Modify: `s3tables_delta_pilot/tests/test_ui_assets.py`

**Step 1:** Add failing tests proving a create preflight returns all stored columns with privacy-safe samples and per-candidate key-quality statistics.

**Step 2:** Add a `deduplication_columns` field to `IngestionRequest`; require one or more candidates for a new table and reject unknown/duplicate selections.

**Step 3:** Persist `schema`, `deduplication_columns`, and a policy version in new-table contracts; retain a full-row fallback for old contracts.

**Step 4:** Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s s3tables_delta_pilot/tests -q`.

### Task 2: Creation UI

**Files:**

- Modify: `s3tables_delta_pilot/static/app.js`
- Modify: `s3tables_delta_pilot/static/style.css`
- Test: `s3tables_delta_pilot/tests/test_ui_assets.py`

**Step 1:** Render type-conversion choices first, then all remaining stored columns as selectable key candidates with masked sensitive samples.

**Step 2:** Display selected-key quality: rows, null-key rows, distinct keys, duplicate-key rows, and a warning when the selection is non-unique.

**Step 3:** Disable create upload until at least one key column is selected; include the selection in the request.

**Step 4:** Run JavaScript syntax validation with `node --check s3tables_delta_pilot/static/app.js`.

### Task 3: Glue key de-duplication and conflicts

**Files:**

- Modify: `s3tables_delta_pilot/generic_glue_job.py`
- Test: `s3tables_delta_pilot/tests/test_ui_assets.py`

**Step 1:** Add a failing source-level regression test for key-enabled manifest handling and legacy full-row fallback.

**Step 2:** For key-enabled tables, validate non-null unique incoming keys, append only keys absent from the target, and classify existing keys as exact duplicates or conflicts.

**Step 3:** Emit aggregate-only QC metrics: key columns, exact duplicates skipped, conflicts skipped, and differing-column counts. Never emit row values or keys.

**Step 4:** Run the full test suite and `git diff --check`.

### Task 4: Documentation and local verification

**Files:**

- Modify: `s3tables_delta_pilot/README.md`

**Step 1:** Document the immutable key contract, conflict behavior, and full-row legacy compatibility.

**Step 2:** Restart the local server and manually verify a create preflight shows samples and the key selector.

**Step 3:** Do not commit or push unless requested.
