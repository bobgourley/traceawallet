-- Audit for bulk label loads (taw/labels.py, docs/DECISIONS.md 2026-11-20
-- "Audit: one entry per label load").
--
-- A label written by a file load (OFAC's SDN list, the exchange file, OKX's
-- monthly file) carries the id of its label_load row. Those rows skip the
-- per-row audit copy. Instead the load writes one event_log entry, in the same
-- transaction: source, file sha256, row count, and a sha256 over the loaded
-- rows in a fixed order (labels.load_digest). scripts/verify_label_loads.py
-- recomputes it. Labels written one at a time (freeze status, GoPlus) have no
-- load id and keep the full per-row copy.

BEGIN;

SELECT set_config('taw.actor', 'migration:0015_label_load_audit', true);

ALTER TABLE label ADD COLUMN load_id bigint REFERENCES label_load(id);
CREATE INDEX label_load_rows ON label (load_id, id) WHERE load_id IS NOT NULL;

CREATE OR REPLACE VIEW label_current AS
SELECT l.*
FROM label l
WHERE l.status = 'asserted'
  AND NOT EXISTS (SELECT 1 FROM label n WHERE n.supersedes_id = l.id);

DROP TRIGGER audit ON label;
CREATE TRIGGER audit AFTER INSERT ON label FOR EACH ROW WHEN (NEW.load_id IS NULL)
    EXECUTE FUNCTION audit_row('full');

ALTER TABLE label_load ADD COLUMN file_sha256 char(64) CHECK (file_sha256 ~ '^[0-9a-f]{64}$');

COMMIT;
