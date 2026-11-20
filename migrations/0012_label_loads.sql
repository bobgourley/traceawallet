-- Label loading: a stable key per claim, and a record of every load
-- (taw/labels.py, docs/DECISIONS.md 2026-11-20 "Labels: OFAC SDN list").
--
-- claim_key identifies one claim from one source, e.g. 'sdn:12345:XBT:<address>' for
-- an SDN entry's address under one currency tag. A daily reload compares keys: a claim still present
-- adds nothing, a new one adds a row, a changed one adds a row that supersedes
-- the old one, and a vanished one adds a 'withdrawn' row that supersedes it.
-- source_date on a label is the publication that made that row; label_load says
-- which publications were loaded and when, including the ones that changed
-- nothing.

BEGIN;

SELECT set_config('taw.actor', 'migration:0012_label_loads', true);

ALTER TABLE label ADD COLUMN claim_key text;
CREATE INDEX label_claim ON label (source_id, claim_key);

-- The view was created with l.* before claim_key existed; refresh it so it has it.
CREATE OR REPLACE VIEW label_current AS
SELECT l.*
FROM label l
WHERE l.status = 'asserted'
  AND NOT EXISTS (SELECT 1 FROM label n WHERE n.supersedes_id = l.id);

CREATE TABLE label_load (
    id              bigserial PRIMARY KEY,
    source_id       int NOT NULL REFERENCES source(id),
    source_date     date NOT NULL,                       -- the publication date the source states
    raw_response_id bigint REFERENCES raw_response(id),  -- the file as fetched; NULL only for the hand-built file
    claims          int NOT NULL CHECK (claims >= 0),    -- claims in this publication that we map to a chain
    added           int NOT NULL CHECK (added >= 0),
    changed         int NOT NULL CHECK (changed >= 0),
    withdrawn       int NOT NULL CHECK (withdrawn >= 0),
    unmapped        int NOT NULL CHECK (unmapped >= 0),  -- entries whose format fits no chain we cover
    detail          jsonb NOT NULL DEFAULT '{}',
    loaded_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER label_load_immutable BEFORE UPDATE OR DELETE ON label_load
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER label_load_no_truncate BEFORE TRUNCATE ON label_load
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();

GRANT SELECT, INSERT ON label_load TO taw_app;
GRANT USAGE, SELECT ON SEQUENCE label_load_id_seq TO taw_app;

UPDATE source
SET url = 'https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML',
    license = 'US government work (OFAC Sanctions List Service). Every "Digital Currency Address" on the SDN '
              'list is loaded and mapped to chains by its format; the currency tag is kept in the label text.'
WHERE slug = 'ofac-sdn';

COMMIT;
