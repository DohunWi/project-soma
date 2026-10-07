-- Preserve existing state_logs rows while identifying the scoring model used
-- by new snapshots. Existing rows intentionally remain NULL.
ALTER TABLE state_logs
    ADD COLUMN IF NOT EXISTS score_model_version TEXT;
