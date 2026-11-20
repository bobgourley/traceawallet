-- What a running job is doing, and why a failed attempt failed, for the waiting page.
--
-- step, step_at: the worker's current step on an address_lookup job
--   (reading_balances, reading_transfers, checking, checking_lists, building_report)
--   and when it began. Jobs are shared and hold nothing about who asked, so this
--   is not private.
-- failure_kind: a code for the last failed attempt (provider_slow, provider_busy,
--   provider_error, provider_unreachable, provider_indexing, allowance,
--   cannot_look_up, service_problem). The page words it; last_error keeps the
--   technical detail for us.

BEGIN;

SELECT set_config('taw.actor', 'migration:0018_job_progress', true);

ALTER TABLE job
    ADD COLUMN step text,
    ADD COLUMN step_at timestamptz,
    ADD COLUMN failure_kind text;

COMMIT;
