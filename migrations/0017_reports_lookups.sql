-- Reports, the lookup API and transaction matching (docs/DECISIONS.md 2026-11-20
-- "Reports, matching and the lookup API").
--
-- report.snapshot_id: the address_snapshot a report was built from. A report is
--   built from one snapshot and draws on one source per chain, the one that
--   produced the snapshot. Building again from the same snapshot with the same
--   label cut-off gives the same sha256, so the same build is stored once.
--
-- lookup_job: the jobs a lookup started, one per chain (a 0x address is looked up
--   on every EVM chain Phase 1 checks). Private, like lookup: nothing public reads it.
--
-- lookup.match_result: what transaction matching found for the victim's inputs
--   (taw/matching.py). Stored on the lookup row only: never in a report, never in
--   the event log (lookup is audited id-only). lookup_redact() clears it.

BEGIN;

SELECT set_config('taw.actor', 'migration:0017_reports_lookups', true);

ALTER TABLE report ADD COLUMN snapshot_id bigint REFERENCES address_snapshot(id);
CREATE UNIQUE INDEX report_one_build ON report (snapshot_id, schema_version, template_version, sha256)
    WHERE snapshot_id IS NOT NULL;
CREATE INDEX ON report (snapshot_id);

CREATE TABLE lookup_job (
    lookup_id  uuid NOT NULL REFERENCES lookup(id),
    chain      text NOT NULL REFERENCES chain(id),
    job_id     bigint NOT NULL REFERENCES job(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (lookup_id, chain)
);
CREATE INDEX ON lookup_job (job_id);

CREATE TRIGGER lookup_job_immutable BEFORE UPDATE OR DELETE ON lookup_job
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER lookup_job_no_truncate BEFORE TRUNCATE ON lookup_job
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();

ALTER TABLE lookup
    ADD COLUMN match_result jsonb CHECK (match_result IS NULL OR jsonb_typeof(match_result) = 'object'),
    ADD COLUMN match_checked_at timestamptz;

CREATE OR REPLACE FUNCTION lookup_redact(p_id uuid) RETURNS void AS $$
    UPDATE lookup
    SET clerk_user_id = NULL, session_hash = NULL, ip_hash = NULL,
        chain_hint = NULL, asset = NULL, amount = NULL, sent_on = NULL,
        sending_address = NULL, tx_hash = NULL, platform_name = NULL, platform_url = NULL,
        matched_transfer_id = NULL, match_method = NULL, match_result = NULL, match_checked_at = NULL,
        redacted_at = now()
    WHERE id = p_id;
$$ LANGUAGE sql;

GRANT SELECT, INSERT ON lookup_job TO taw_app;
GRANT SELECT ON report TO taw_app;

COMMIT;
