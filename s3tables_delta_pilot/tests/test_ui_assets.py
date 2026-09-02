import asyncio
import json
import unittest
from datetime import date, time
from datetime import datetime, timezone
from io import BytesIO
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd

from s3tables_delta_pilot.pilot import NAMESPACE, TABLE_BUCKET_ARN
from s3tables_delta_pilot.webapp import (
    CreateNamespaceRequest,
    CreateTableBucketRequest,
    IngestionRequest,
    RollbackRequest,
    UPLOAD_HISTORY_TABLE,
    _apply_create_type_overrides,
    _create_deduplication_candidates,
    _create_type_selection_samples,
    _composite_key_metrics,
    _current_user,
    create_namespace,
    create_table_bucket,
    _history_prefix,
    key_impact_analysis,
    list_buckets,
    list_namespaces,
    local_identity_profiles,
    _make_glue_compatible_parquet,
    _read_upload_table,
    _read_key_analysis_token,
    _require_scope,
    _sign_key_analysis,
    _unsafe_cast_issues,
    _preflight,
    _raw_key_impact_metrics,
    _validate_create_deduplication_columns,
)
from starlette.datastructures import UploadFile


STATIC = Path(__file__).parents[1] / "static"
GLUE_JOB = Path(__file__).parents[1] / "generic_glue_job.py"


class UiAssetTests(unittest.TestCase):
    def test_unselected_table_card_has_a_dark_text_colour(self):
        css = (STATIC / "style.css").read_text()
        self.assertIn(".table {", css)
        self.assertIn("background: #fff; color: #14213d;", css)

    def test_hyphenated_new_table_name_is_canonicalised_for_s3_tables(self):
        request = IngestionRequest(
            mode="append", table="nuh-surgery", request_id="test",
            table_bucket_arn=TABLE_BUCKET_ARN, namespace=NAMESPACE, reporting_month="2026-08",
        )
        self.assertEqual("nuh_surgery", request.table)

    def test_free_form_user_tag_is_required_for_a_recoverable_upload(self):
        request = IngestionRequest(
            mode="append", table="soc", request_id="test",
            table_bucket_arn=TABLE_BUCKET_ARN, namespace=NAMESPACE, reporting_month="August 2026 | legacy refresh",
        )
        self.assertEqual("August 2026 | legacy refresh", request.reporting_month)
        with self.assertRaises(Exception):
            IngestionRequest(
                mode="append", table="soc", request_id="test",
                table_bucket_arn=TABLE_BUCKET_ARN, namespace=NAMESPACE, reporting_month="",
            )

    def test_rollback_request_requires_explicit_confirmation_field(self):
        request = RollbackRequest(
            table="soc", table_bucket_arn=TABLE_BUCKET_ARN, namespace=NAMESPACE,
            upload_id="UPLOAD-202608-ABCD1234",
        )
        self.assertFalse(request.confirm)

    def test_ui_explains_the_destination_required_before_review(self):
        html = (STATIC / "index.html").read_text()
        javascript = (STATIC / "app.js").read_text()
        self.assertIn('id="destination-help"', html)
        self.assertIn('id="review-requirements"', html)
        self.assertIn('id="reporting-month" type="text"', html)
        self.assertIn('id="history"', html)
        self.assertIn('id="namespace"', html)
        self.assertIn('Select one existing table', javascript)
        self.assertIn('/api/rollbacks', javascript)
        self.assertIn('Rollback upload', javascript)
        self.assertIn('function userTag()', javascript)
        self.assertIn('Enter a user tag', javascript)
        self.assertIn('To enable Review upload:', javascript)
        self.assertIn("if (data.tables.length === 0)", javascript)

    def test_admin_ui_can_create_table_buckets_and_namespaces(self):
        html = (STATIC / "index.html").read_text()
        javascript = (STATIC / "app.js").read_text()
        self.assertIn('id="admin-provisioning"', html)
        self.assertIn('id="new-bucket"', html)
        self.assertIn('id="new-namespace"', html)
        self.assertIn("async function createTableBucket", javascript)
        self.assertIn("async function createSelectedNamespace", javascript)
        self.assertIn("'/api/buckets'", javascript)
        self.assertIn("'/api/namespaces'", javascript)

    def test_admin_can_create_a_bucket_and_namespace_but_editor_cannot(self):
        with patch.dict("os.environ", {}, clear=True):
            admin = _current_user("local-admin")
            editor = _current_user("local-editor")

        bucket_request = CreateTableBucketRequest(name="hospital-analytics")
        with patch(
            "s3tables_delta_pilot.webapp.s3tables.create_table_bucket",
            return_value={"arn": "arn:aws:s3tables:ap-southeast-1:123456789012:bucket/hospital-analytics"},
        ) as create_bucket:
            result = create_table_bucket(bucket_request, admin)
        self.assertEqual("hospital-analytics", result["label"])
        create_bucket.assert_called_once_with(name="hospital-analytics")

        bucket_arn = result["table_bucket_arn"]
        namespace_request = CreateNamespaceRequest(table_bucket_arn=bucket_arn, namespace="reporting")
        with patch(
            "s3tables_delta_pilot.webapp._discover_table_buckets",
            return_value=[{"table_bucket_arn": bucket_arn, "label": "hospital-analytics"}],
        ), patch(
            "s3tables_delta_pilot.webapp.s3tables.create_namespace",
            return_value={"tableBucketARN": bucket_arn, "namespace": ["reporting"]},
        ) as create_namespace_call:
            namespace_result = create_namespace(namespace_request, admin)
        self.assertEqual("reporting", namespace_result["namespace"])
        create_namespace_call.assert_called_once_with(tableBucketARN=bucket_arn, namespace=["reporting"])

        with self.assertRaises(Exception) as denied:
            create_table_bucket(bucket_request, editor)
        self.assertEqual(403, denied.exception.status_code)

    def test_bucket_and_namespace_creation_names_are_validated(self):
        with self.assertRaises(Exception):
            CreateTableBucketRequest(name="Uppercase Bucket")
        with self.assertRaises(Exception):
            CreateNamespaceRequest(table_bucket_arn="arn:test", namespace="not-valid")

    def test_history_projection_is_scoped_to_table_bucket_namespace_and_table(self):
        prefix = _history_prefix("arn:aws:s3tables:ap-southeast-1:123:bucket/example", "pilot", "soc")
        self.assertTrue(prefix.endswith("/soc/"))
        self.assertNotIn("arn:aws", prefix)

    def test_audit_table_is_reserved(self):
        self.assertEqual("uploader_upload_history", UPLOAD_HISTORY_TABLE)

    def test_glue_job_records_snapshot_history_and_uses_iceberg_rollback(self):
        script = GLUE_JOB.read_text()
        self.assertIn('status="PROCESSING"', script)
        self.assertIn('status="SUCCESS"', script)
        self.assertIn("rollback_to_snapshot", script)
        self.assertIn('status="ROLLED_BACK"', script)

    def test_glue_job_deduplicates_full_rows_before_append(self):
        script = GLUE_JOB.read_text()
        self.assertIn("def _deduplicate_incoming_by_keys", script)
        self.assertIn("def _exclude_existing_rows", script)
        self.assertIn("def _keyed_rows_to_append", script)
        self.assertIn("existing_key_conflicts", script)
        self.assertIn("within_upload_key_conflicts", script)
        self.assertIn("def _with_composite_key", script)
        self.assertIn("__uploader_composite_key", script)
        self.assertIn("eqNullSafe", script)
        self.assertIn("duplicate_rows_within_upload", script)
        self.assertIn("duplicate_rows_already_in_table", script)

    def test_staging_converts_time_of_day_to_spark_compatible_string(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "times.parquet"
            pq.write_table(pa.table({"ATIME": pa.array([time(8, 30), None], type=pa.time64("us"))}), source)
            staged, transformed, _ = _make_glue_compatible_parquet(source, source.name)
            try:
                self.assertTrue(transformed)
                self.assertEqual(pa.string(), pq.read_schema(staged).field("atime").type)
            finally:
                if staged != source:
                    staged.unlink(missing_ok=True)

    def test_staging_normalizes_documented_date_values_to_date(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "dates.parquet"
            pq.write_table(pa.table({"Visit Date": ["20240513", "2024.05.14"]}), source)
            staged, transformed, _ = _make_glue_compatible_parquet(
                source, source.name, target_schema=[{"name": "visit_date", "type": "DATE"}]
            )
            try:
                self.assertTrue(transformed)
                self.assertEqual(pa.date32(), pq.read_schema(staged).field("visit_date").type)
            finally:
                if staged != source:
                    staged.unlink(missing_ok=True)

    def test_staging_preserves_a_valid_year_9999_date(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "sentinel.parquet"
            pq.write_table(pa.table({"End Date": ["9999-12-31", "2024-02-29"]}), source)
            staged, transformed, _ = _make_glue_compatible_parquet(
                source, source.name, target_schema=[{"name": "end_date", "type": "DATE"}]
            )
            try:
                self.assertTrue(transformed)
                self.assertEqual([date(9999, 12, 31), date(2024, 2, 29)], pq.read_table(staged)["end_date"].to_pylist())
            finally:
                if staged != source:
                    staged.unlink(missing_ok=True)

    def test_unsafe_date_validation_reports_invalid_values_without_crashing(self):
        sink = pa.BufferOutputStream()
        pq.write_table(pa.table({"End Date": ["9999-12-31", "9999-02-29"]}), sink)
        upload = UploadFile(filename="dates.parquet", file=BytesIO(sink.getvalue().to_pybytes()))
        issues = _unsafe_cast_issues(upload, [{"name": "end_date", "type": "DATE"}])
        self.assertEqual([{"column": "end_date", "source_type": "string", "target_type": "DATE", "unsafe_value_count": 1}], issues)

    def test_key_impact_analysis_accepts_year_9999_without_s3_or_glue_work(self):
        sink = pa.BufferOutputStream()
        pq.write_table(pa.table({"End Date": ["9999-12-31"], "Case": ["C1"]}), sink)
        upload = UploadFile(filename="sentinel.parquet", file=BytesIO(sink.getvalue().to_pybytes()))
        request = json.dumps({
            "table": "sentinel_dates",
            "table_bucket_arn": TABLE_BUCKET_ARN,
            "namespace": NAMESPACE,
            "type_overrides": {},
            "deduplication_columns": ["case"],
        })
        user = _current_user("local-editor")
        with patch("s3tables_delta_pilot.webapp.s3.put_object") as put_object, patch(
            "s3tables_delta_pilot.webapp.glue.start_job_run"
        ) as start_job, patch("s3tables_delta_pilot.webapp.sanitise_table") as sanitise, patch(
            "s3tables_delta_pilot.webapp.encryption_key"
        ) as key, patch("s3tables_delta_pilot.webapp._preflight") as preflight:
            result = asyncio.run(key_impact_analysis(request=request, files=[upload], user=user))
        self.assertEqual(1, result["metrics"]["incoming_rows"])
        self.assertTrue(result["no_storage_or_glue_side_effects"])
        put_object.assert_not_called()
        start_job.assert_not_called()
        sanitise.assert_not_called()
        key.assert_not_called()
        preflight.assert_not_called()

    def test_retention_configuration_uses_local_service_not_glue_boto3(self):
        job = GLUE_JOB.read_text()
        webapp = (Path(__file__).parents[1] / "webapp.py").read_text()
        self.assertNotIn('boto3.client("s3tables"', job)
        self.assertIn('def _configure_snapshot_retention', webapp)
        self.assertIn('"minSnapshotsToKeep": 12', webapp)
        self.assertIn('"maxSnapshotAgeHours": 365 * 24', webapp)

    def test_glue_job_arguments_never_use_an_empty_optional_value(self):
        source = (Path(__file__).parents[1] / "webapp.py").read_text()
        self.assertNotIn('"--ROLLBACK_SNAPSHOT_ID": ""', source)
        self.assertNotIn('"--MANIFEST_URI": ""', source)

    def test_history_ui_allows_only_the_latest_successful_upload_to_roll_back(self):
        javascript = (STATIC / "app.js").read_text()
        self.assertIn("latestRollbackUploadId", javascript)
        self.assertIn("state.canRollbackUploads", javascript)
        self.assertIn("item.uploaded_by === state.userId", javascript)
        self.assertIn("rollback.disabled = !canRollback", javascript)
        self.assertIn("Original upload:", javascript)
        self.assertIn("Latest action:", javascript)

    def test_preflight_ui_has_no_manual_approval_control(self):
        html = (STATIC / "index.html").read_text()
        javascript = (STATIC / "app.js").read_text()
        self.assertNotIn('id="allow-casts"', html)
        self.assertNotIn("allow_unsafe_casts", javascript)
        self.assertIn("matching_percentage", javascript)
        self.assertIn("sanitized_columns", javascript)
        self.assertIn("file.rejection_reasons", javascript)
        self.assertIn("fileDecision", javascript)

    def test_create_preflight_exposes_type_selection_and_submission_sends_it(self):
        javascript = (STATIC / "app.js").read_text()
        self.assertIn("Choose ambiguous initial column types", javascript)
        self.assertIn("data-type-override", javascript)
        self.assertIn("type_overrides: selectedTypeOverrides()", javascript)
        self.assertIn("Random non-empty examples", javascript)
        self.assertIn("samples_masked", javascript)
        self.assertIn("Choose de-duplication columns", javascript)
        self.assertIn("deduplication_columns: selectedDeduplicationColumns()", javascript)

    def test_type_selection_samples_are_random_and_sensitive_samples_are_masked(self):
        sink = pa.BufferOutputStream()
        pq.write_table(pa.table({"Numeric Text": ["10", "20", "30", "40", "50", "60"], "PAT_ENC_CSN_ID": ["100", "200", "300", "400", "500", "600"]}), sink)
        upload = UploadFile(filename="samples.parquet", file=BytesIO(sink.getvalue().to_pybytes()))
        selections = [
            {"column": "numeric_text", "source_type": "STRING"},
            {"column": "pat_enc_csn_id", "source_type": "STRING"},
        ]
        result = _create_type_selection_samples(upload, selections)
        self.assertEqual(5, len(result[0]["sample_values"]))
        self.assertFalse(result[0]["samples_masked"])
        self.assertEqual([], result[1]["sample_values"])
        self.assertTrue(result[1]["samples_masked"])

    def test_deduplication_candidates_list_all_columns_with_safe_examples(self):
        sink = pa.BufferOutputStream()
        pq.write_table(pa.table({"Case Number": ["C1", "C2"], "PAT_ENC_CSN_ID": ["100", "200"]}), sink)
        upload = UploadFile(filename="keys.parquet", file=BytesIO(sink.getvalue().to_pybytes()))
        result = _create_deduplication_candidates(upload, [
            {"name": "case_number", "type": "STRING"},
            {"name": "pat_enc_csn_id", "type": "STRING"},
        ])
        self.assertEqual(["case_number", "pat_enc_csn_id"], [item["column"] for item in result])
        self.assertTrue(result[0]["sample_values"])
        self.assertEqual(2, result[0]["non_null_count"])
        self.assertEqual(2, result[0]["distinct_non_null_count"])
        self.assertTrue(result[1]["samples_masked"])
        self.assertTrue(result[1]["deduplication_eligible"])

    def test_new_table_requires_known_deduplication_columns(self):
        preview = {"deduplication_candidates": [{"column": "case_number"}, {"column": "visit_date"}]}
        self.assertEqual(["case_number", "visit_date"], _validate_create_deduplication_columns(preview, ["case_number", "visit_date"]))
        with self.assertRaises(Exception):
            _validate_create_deduplication_columns(preview, [])
        with self.assertRaises(Exception):
            _validate_create_deduplication_columns(preview, ["unknown"])

    def test_create_type_selection_changes_only_an_available_column(self):
        preview = {
            "target_schema": [
                {"name": "arrival_mode", "type": "BIGINT", "source_name": "Arrival Mode"},
                {"name": "note", "type": "STRING", "source_name": "Note"},
            ],
            "type_selections": [{
                "column": "arrival_mode",
                "source_type": "STRING",
                "suggested_target_type": "BIGINT",
                "allowed_target_types": ["STRING", "BIGINT", "DOUBLE", "TIMESTAMP", "BOOLEAN"],
                "locked": False,
            }],
        }
        result = _apply_create_type_overrides(preview, {"arrival_mode": "STRING"})
        self.assertEqual("STRING", result[0]["type"])
        self.assertEqual("STRING", result[1]["type"])

    def test_create_type_selection_rejects_unknown_or_invalid_type(self):
        preview = {
            "target_schema": [{"name": "arrival_mode", "type": "BIGINT", "source_name": "Arrival Mode"}],
            "type_selections": [{
                "column": "arrival_mode", "source_type": "STRING", "suggested_target_type": "BIGINT",
                "allowed_target_types": ["STRING", "BIGINT"], "locked": False,
            }],
        }
        with self.assertRaises(Exception):
            _apply_create_type_overrides(preview, {"unknown": "STRING"})
        with self.assertRaises(Exception):
            _apply_create_type_overrides(preview, {"arrival_mode": "DECIMAL"})

    def test_key_impact_analysis_matches_the_glue_conflict_policy(self):
        frame = pd.DataFrame({
            "case": ["A", "A", "B", "B", "C"],
            "value": [1, 1, 1, 2, 1],
        })
        result = _composite_key_metrics(frame, ["case"])
        self.assertEqual(5, result["incoming_rows"])
        self.assertEqual(3, result["unique_composite_keys"])
        self.assertEqual(1, result["exact_duplicate_rows"])
        self.assertEqual(1, result["conflicting_key_groups"])
        self.assertEqual(2, result["rows_in_conflicting_key_groups"])
        self.assertEqual(2, result["expected_retained_rows"])
        self.assertEqual(3, result["expected_skipped_rows"])

    def test_raw_csv_key_analysis_treats_percentage_values_as_text(self):
        """A later percentage must not break raw CSV key review via inference."""
        with TemporaryDirectory() as directory:
            source = Path(directory) / "bmu.csv"
            source.write_text("case,bor\nA,82\nB,83%\nA,82\n", encoding="utf-8")
            result = _raw_key_impact_metrics([(source, source.name)], ["case"])
        self.assertEqual(3, result["incoming_rows"])
        self.assertEqual(1, result["exact_duplicate_rows"])
        self.assertEqual(2, result["expected_retained_rows"])

    def test_ui_requires_a_current_acknowledged_key_analysis_before_create_upload(self):
        javascript = (STATIC / "app.js").read_text()
        self.assertIn("/api/key-impact-analysis", javascript)
        self.assertIn("acknowledge-key-analysis", javascript)
        self.assertIn("key_analysis_token", javascript)
        self.assertIn("Analyse selected key impact", javascript)
        self.assertIn("Current composite key:", javascript)
        self.assertIn("key-analysis-status", javascript)
        self.assertIn("Analysing selected key…", javascript)

    def test_ui_shows_in_progress_and_failure_status_for_upload_review(self):
        html = (STATIC / "index.html").read_text()
        javascript = (STATIC / "app.js").read_text()
        self.assertIn('id="review-status"', html)
        self.assertLess(html.index('id="review"'), html.index('id="upload-actions"'))
        self.assertIn('id="upload-status"', html)
        self.assertIn("Reviewing upload…", javascript)
        self.assertIn("Analysing file structure, column names, types, and sanitization requirements…", javascript)
        self.assertIn("Upload review failed:", javascript)
        self.assertIn("Starting upload…", javascript)
        self.assertIn("Preparing the sanitized upload, recovery point, and ETL job…", javascript)
        self.assertIn("Upload could not start:", javascript)

    def test_staged_upload_archive_has_a_30_day_lifecycle_contract(self):
        source = (Path(__file__).parents[1] / "webapp.py").read_text()
        self.assertIn("WEB_UPLOAD_PREFIX", source)
        self.assertIn("UPLOAD_ARCHIVE_RETENTION_DAYS = 30", source)
        self.assertIn("_ensure_upload_archive_lifecycle", source)
        self.assertIn("sanitized_archive_uri", source)
        self.assertNotIn("WEB_BACKUP_PREFIX", source)
        self.assertNotIn("sanitized_backup_uri", source)

    def test_key_analysis_acknowledgement_is_tamper_evident(self):
        token = _sign_key_analysis({"expires_at": int(datetime.now(timezone.utc).timestamp()) + 60, "user_id": "local-admin"})
        self.assertEqual("local-admin", _read_key_analysis_token(token)["user_id"])
        with self.assertRaises(Exception):
            _read_key_analysis_token(token[:-1] + ("A" if token[-1] != "A" else "B"))

    def test_append_preflight_rejects_less_than_50_percent_schema_overlap(self):
        sink = pa.BufferOutputStream()
        pq.write_table(pa.table({"A Col": [1], "unmatched": [2]}), sink)
        upload = UploadFile(filename="small.parquet", file=BytesIO(sink.getvalue().to_pybytes()))
        target = [
            {"name": "a_col", "type": "BIGINT", "source_name": "A Col"},
            {"name": "b_col", "type": "BIGINT", "source_name": "B Col"},
            {"name": "c_col", "type": "BIGINT", "source_name": "C Col"},
        ]
        with patch("s3tables_delta_pilot.webapp._load_contract_record", return_value={"schema": target, "deduplication_columns": [], "deduplication_policy": "legacy-full-row-v1"}):
            result = _preflight("append", TABLE_BUCKET_ARN, NAMESPACE, "soc", [upload])
        self.assertFalse(result["accepted"])
        self.assertEqual(1, result["files"][0]["matching_column_count"])
        self.assertIn("At least 50%", result["rejection_reasons"][0])

    def test_append_preflight_accepts_50_percent_schema_overlap(self):
        sink = pa.BufferOutputStream()
        matching_source = {f"Column {number}": [number] for number in range(1, 11)}
        matching_source["extra"] = [99]
        pq.write_table(pa.table(matching_source), sink)
        upload = UploadFile(filename="enough.parquet", file=BytesIO(sink.getvalue().to_pybytes()))
        target = [
            {"name": f"column_{number}", "type": "BIGINT", "source_name": f"Column {number}"}
            for number in range(1, 21)
        ]
        with patch("s3tables_delta_pilot.webapp._load_contract_record", return_value={"schema": target, "deduplication_columns": [], "deduplication_policy": "legacy-full-row-v1"}):
            result = _preflight("append", TABLE_BUCKET_ARN, NAMESPACE, "soc", [upload])
        self.assertTrue(result["accepted"])
        self.assertEqual(50.0, result["files"][0]["matching_percentage"])

    def test_append_preflight_rejects_values_that_would_become_null_after_cast(self):
        sink = pa.BufferOutputStream()
        pq.write_table(pa.table({"Count": ["not-a-number"]}), sink)
        upload = UploadFile(filename="bad-value.parquet", file=BytesIO(sink.getvalue().to_pybytes()))
        target = [{"name": "count", "type": "BIGINT", "source_name": "Count"}]
        with patch("s3tables_delta_pilot.webapp._load_contract_record", return_value={"schema": target, "deduplication_columns": [], "deduplication_policy": "legacy-full-row-v1"}):
            result = _preflight("append", TABLE_BUCKET_ARN, NAMESPACE, "soc", [upload])
        self.assertFalse(result["accepted"])
        self.assertEqual("count", result["files"][0]["unsafe_casts"][0]["column"])
        self.assertIn("cannot be safely converted", result["files"][0]["rejection_reasons"][0])
        self.assertIn("cannot be safely converted", result["rejection_reasons"][0])

    def test_staging_parquet_suffixes_case_colliding_columns(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.parquet"
            pq.write_table(
                pa.table({"Accident_type": ["first"], "Accident_Type": ["second"]}),
                source,
            )
            staged, transformed, _ = _make_glue_compatible_parquet(source, "source.parquet")

            self.assertTrue(transformed)
            self.assertEqual(
                ["accident_type", "accident_type_01"],
                pq.ParquetFile(staged).schema_arrow.names,
            )

    def test_staging_applies_healthcare_sanitization_before_s3(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.parquet"
            pq.write_table(pa.table({"Patient_Name": ["Jane"], "Ext_Pat_ID": [12345]}), source)
            staged, transformed, audit = _make_glue_compatible_parquet(
                source, "source.parquet", b"R92oGhcdhyxFbicuopsdataAIO2701211"
            )

            self.assertTrue(transformed)
            self.assertEqual(["ext_pat_id"], pq.ParquetFile(staged).schema_arrow.names)
            self.assertEqual(["Patient_Name"], audit["dropped_columns"])
            self.assertEqual(["Ext_Pat_ID"], audit["encrypted_columns"])

    def test_csv_is_staged_as_parquet(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.csv"
            source.write_text("Visit Date,Count\n2026-08-01,3\n")
            staged, transformed, _ = _make_glue_compatible_parquet(source, "source.csv")

            self.assertTrue(transformed)
            self.assertEqual(["visit_date", "count"], pq.ParquetFile(staged).schema_arrow.names)

    def test_parquet_gzip_filename_is_accepted_as_parquet(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.parquet.gzip"
            pq.write_table(pa.table({"value": [1]}), source)
            staged, transformed, _ = _make_glue_compatible_parquet(source, "source.parquet.gzip")

            self.assertFalse(transformed)
            self.assertEqual(source, staged)
            self.assertEqual(1, pq.ParquetFile(staged).metadata.num_rows)

    def test_xlsx_is_staged_as_parquet(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.xlsx"
            import pandas as pd
            pd.DataFrame({"Visit Date": ["2026-08-01"], "Count": [3]}).to_excel(source, index=False)
            staged, transformed, _ = _make_glue_compatible_parquet(source, "source.xlsx")

            self.assertTrue(transformed)
            self.assertEqual(["visit_date", "count"], pq.ParquetFile(staged).schema_arrow.names)

    def test_xlsx_mixed_numeric_and_text_column_becomes_string(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "mixed.xlsx"
            import pandas as pd
            pd.DataFrame({"Block": [671, "672A"]}).to_excel(source, index=False)

            table = _read_upload_table(source, "mixed.xlsx")

            self.assertTrue(pa.types.is_string(table.schema.field("Block").type) or pa.types.is_large_string(table.schema.field("Block").type))
            self.assertEqual(["671", "672A"], table["Block"].to_pylist())

    def test_placeholder_user_scope_is_limited_to_its_configured_bucket(self):
        access = '{"analyst":{"is_admin":false,"buckets":[{"table_bucket_arn":"arn:test:one","namespace":"pilot"}]}}'
        with patch.dict("os.environ", {"PILOT_USER_ACCESS_JSON": access}, clear=False):
            user = _current_user("analyst")
            self.assertFalse(user.is_admin)
            self.assertEqual("arn:test:one", _require_scope(user, "arn:test:one", "pilot").table_bucket_arn)
            with self.assertRaises(Exception):
                _require_scope(user, "arn:test:two", "pilot")

    def test_admin_discovers_all_buckets_and_namespaces_while_editor_stays_scoped(self):
        with patch.dict("os.environ", {}, clear=True):
            admin = _current_user("local-admin")
            editor = _current_user("local-editor")
        discovered = [
            {"table_bucket_arn": "arn:test:ah", "label": "ah-analytics"},
            {"table_bucket_arn": "arn:test:nuh", "label": "nuh-analytics"},
        ]
        with patch("s3tables_delta_pilot.webapp._discover_table_buckets", return_value=discovered), patch(
            "s3tables_delta_pilot.webapp._discover_namespaces", return_value=["ah", "pilot"]
        ):
            self.assertEqual(discovered, list_buckets(admin)["buckets"])
            self.assertEqual(["ah", "pilot"], list_namespaces("arn:test:ah", admin)["namespaces"])
            self.assertEqual("nuh-analytics", _require_scope(admin, "arn:test:nuh", "pilot").label)
        self.assertEqual([NAMESPACE], list_namespaces(TABLE_BUCKET_ARN, editor)["namespaces"])
        with self.assertRaises(Exception):
            _require_scope(editor, TABLE_BUCKET_ARN, "ah")

    def test_local_identity_profiles_expose_only_header_based_test_context(self):
        with patch.dict("os.environ", {}, clear=True):
            profiles = local_identity_profiles()["profiles"]
        self.assertEqual(["local-admin", "local-editor", "local-unassigned"], [item["user_id"] for item in profiles])
        self.assertTrue(profiles[0]["is_admin"])
        self.assertTrue(profiles[1]["expected_access"])
        self.assertTrue(profiles[1]["can_view_upload_history"])
        self.assertTrue(profiles[1]["can_rollback_uploads"])
        self.assertFalse(profiles[2]["expected_access"])
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(Exception):
                _current_user("local-unassigned")

    def test_identity_test_panel_sends_only_the_user_id_header(self):
        html = (STATIC / "index.html").read_text()
        javascript = (STATIC / "app.js").read_text()
        self.assertIn('id="identity-panel"', html)
        self.assertIn('id="effective-identity"', html)
        self.assertIn('id="outgoing-identity"', html)
        self.assertIn("function apiFetch", javascript)
        self.assertIn("X-Pilot-User-Id", javascript)
        self.assertIn("body_user_fields: {}", javascript)
        self.assertIn("function loadNamespaces", javascript)
        self.assertIn("/api/namespaces", javascript)
        self.assertIn("browse-only", javascript)
        self.assertIn("/api/identity", javascript)


if __name__ == "__main__":
    unittest.main()
