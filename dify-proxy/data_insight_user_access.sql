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

CREATE TABLE IF NOT EXISTS data_insight_user_access (
    access_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_email VARCHAR(320) NOT NULL,
    role TEXT NOT NULL,
    dataset TEXT,

    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by VARCHAR(320),
    updated_by VARCHAR(320)
);

CREATE UNIQUE INDEX idx_data_insight_user_access_user_dataset
    ON data_insight_user_access (user_email, dataset)
    NULLS NOT DISTINCT;

-- Supports permission lookup by email while ignoring soft-deleted records.
CREATE INDEX IF NOT EXISTS ix_data_insight_user_access_email_lookup
    ON data_insight_user_access (user_email)
    WHERE NOT is_deleted;



COMMIT;

-- Example records (intentionally commented out):
-- INSERT INTO public.data_insight_user_access
--     (user_email, role, dataset, created_by)
-- VALUES
--     ('bin_he@nuhs.edu.sg', 'viewer', NULL, 'admin@example.com'),
--     ('ge_ji@nuhs.edu.sg', 'querier', 'ah', 'admin@example.com'),
--     ('xin_ji@nuhs.edu.sg', 'maintainer', 'nuh', 'admin@example.com'),
--     ('dedric@nuhs.edu.sg', 'owner', 'nuh', 'admin@example.com');
