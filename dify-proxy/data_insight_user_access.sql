-- Run this script while connected to the `nuhs` PostgreSQL database.
--
-- Proposed role names for review:
--   viewer      - may use the Data Insight chatbot only
--   querier     - may query the assigned dataset
--   maintainer  - may query and update the assigned dataset, but is not its owner
--   owner       - owns the dataset and may query and update it
--
-- `maintainer` is the proposed name for the non-owner creator/co-creator role.
-- Example dataset values: `ah`, `nuh`.

BEGIN;

DROP TABLE IF EXISTS data_insight_user_access;

CREATE TABLE data_insight_user_access (
    access_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_email VARCHAR(320) NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('custodian', 'maintainer', 'querier')),
    dataset TEXT,
    namespace TEXT,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by VARCHAR(320),
    updated_by VARCHAR(320)
);

CREATE INDEX idx_dia_user_email ON data_insight_user_access (LOWER(user_email)) WHERE is_deleted = FALSE;
CREATE INDEX idx_dia_dataset    ON data_insight_user_access (dataset)           WHERE is_deleted = FALSE;

-- One active entry per (email, dataset) pair.
-- Partial index: soft-deleted rows are invisible so the same user can be re-added after removal.
CREATE UNIQUE INDEX uq_active_email_dataset
    ON data_insight_user_access (LOWER(user_email), dataset)
    WHERE is_deleted = FALSE AND dataset IS NOT NULL;

COMMIT;

-- Example records (intentionally commented out):
-- INSERT INTO public.data_insight_user_access
--     (user_email, role, dataset, created_by)
-- VALUES
--     ('bin_he@nuhs.edu.sg', 'viewer', NULL, 'admin@example.com'),
--     ('ge_ji@nuhs.edu.sg', 'querier', 'ah', 'admin@example.com'),
--     ('xin_ji@nuhs.edu.sg', 'maintainer', 'nuh', 'admin@example.com'),
--     ('dedric@nuhs.edu.sg', 'owner', 'nuh', 'admin@example.com');
