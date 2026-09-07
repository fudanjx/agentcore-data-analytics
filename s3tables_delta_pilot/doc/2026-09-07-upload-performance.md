# Upload Performance Implementation Plan

**Goal:** Remove redundant local upload processing and redundant Glue work while preserving the reviewed data contract.

**Architecture:** Reuse private session artifacts and reviewed metadata, use vectorized full-column validation, and stage bounded batches of contract-typed Parquet. A versioned preparation manifest carries raw deduplication results and row counts so Glue can validate prepared schema and perform only target-key comparison and atomic append. Keep existing transport and single-instance ownership assumptions.

**Tech Stack:** FastAPI, Arrow, Polars, pandas Excel readers, Spark/Glue, node:test, unittest.

1. Record a reproducible synthetic baseline. Add behavioral fixtures for date edge cases, repeated parse prevention, sampled NRIC equivalence, cross-file raw deduplication, and prepared Glue dispatch.
2. Add session-owned memory-mapped Arrow artifacts, cached digests and per-phase safe timings. Reuse them for profiling, validation and preparation; retain existing expiry/cleanup ownership.
3. Combine schema/type-choice inference and replace temporal Python callbacks with vectorized strict calendar validation. Keep explicit lossy first-upload selections and strict append semantics.
4. Replace full-list NRIC sampling with native filtering and bounded scalar extraction, preserving deterministic sampling.
5. Reuse preflight only after checking destination contract freshness; validate selected conversions and signed acknowledgements. Deduplicate raw rows across files and stage retained rows in bounded, physically typed batches.
6. Validate the prepared manifest in Glue, bypass incoming deduplication/count actions for prepared data, and materialize one narrow target-key anti-join for keyed appends. Keep the legacy path for old manifests and audit reconciliation.
7. Run the pilot unittest suite, node UI tests, focused differential/dispatch tests, syntax checks and git diff checks. Repeat the synthetic benchmark on the same interpreter. Record results and limitations; do not infer a live Glue speedup without a deployment benchmark.

No deployment or commit is included. Preserve current UI fixes and unrelated work.
