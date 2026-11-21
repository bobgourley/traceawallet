-- California DFPI's Crypto Scam Tracker, and the private result of checking the
-- platform a user entered (taw/adapters/labels/dfpi.py, taw/platform.py;
-- docs/DECISIONS.md, 2026-11-21 "DFPI Crypto Scam Tracker").
--
-- Addresses named in the tracker are labels like any list's. Its websites and
-- subjects are kept per load in site_list_load / site_list_entry, append-only, and
-- are only ever compared with what a user typed; that result lives on the user's
-- own lookup row (platform_result) and is erased with it.

BEGIN;

SELECT set_config('taw.actor', 'migration:0021_dfpi_tracker', true);

INSERT INTO source (slug, name, kind, url, license, resale_permitted, attribution, terms_checked_on) VALUES
    ('dfpi-tracker', 'California DFPI Crypto Scam Tracker', 'label',
     'https://dfpi.ca.gov/consumers/crypto/crypto-scam-tracker/',
     'Public domain under DFPI''s Conditions of Use (https://dfpi.ca.gov/conditions-of-use/), read 2026-11-20; '
     'photographs excluded, so the screenshots are not copied. The entries are consumer complaints; DFPI states '
     'that it has not verified the losses reported.', true,
     'named in a complaint reported to California''s DFPI', '2026-11-21');

CREATE TABLE site_list_load (
    id              bigserial PRIMARY KEY,
    source_id       int NOT NULL REFERENCES source(id),
    read_on         date NOT NULL,
    observed_at     timestamptz NOT NULL DEFAULT now(),
    raw_response_id bigint REFERENCES raw_response(id),
    page_sha256     char(64) NOT NULL,
    entries         int NOT NULL CHECK (entries >= 0)
);
CREATE INDEX site_list_load_latest ON site_list_load (source_id, id DESC);

CREATE TABLE site_list_entry (
    load_id     bigint NOT NULL REFERENCES site_list_load(id),
    position    int NOT NULL CHECK (position > 0),
    entry_key   text NOT NULL,
    subject     text NOT NULL,
    websites    text[] NOT NULL,        -- host names, lower case, without "www."
    scam_types  text[] NOT NULL,        -- the source's own categories, kept as data only
    PRIMARY KEY (load_id, position)
);
CREATE INDEX site_list_entry_websites ON site_list_entry USING gin (websites);

CREATE TRIGGER site_list_load_immutable BEFORE UPDATE OR DELETE ON site_list_load
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER site_list_load_no_truncate BEFORE TRUNCATE ON site_list_load
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();
CREATE TRIGGER site_list_entry_immutable BEFORE UPDATE OR DELETE ON site_list_entry
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER site_list_entry_no_truncate BEFORE TRUNCATE ON site_list_entry
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();

GRANT SELECT, INSERT ON site_list_load, site_list_entry TO taw_app;
GRANT USAGE, SELECT ON SEQUENCE site_list_load_id_seq TO taw_app;

-- The private result for the platform the user entered: matches in the site lists,
-- and (later) the domain's registration and certificate dates.
ALTER TABLE lookup ADD COLUMN platform_result jsonb,
                   ADD COLUMN platform_checked_at timestamptz;

CREATE OR REPLACE FUNCTION lookup_redact(p_id uuid) RETURNS void AS $$
    UPDATE lookup
    SET clerk_user_id = NULL, session_hash = NULL, ip_hash = NULL,
        chain_hint = NULL, asset = NULL, amount = NULL, sent_on = NULL,
        sending_address = NULL, tx_hash = NULL, platform_name = NULL, platform_url = NULL,
        matched_transfer_id = NULL, match_method = NULL, match_result = NULL, match_checked_at = NULL,
        platform_result = NULL, platform_checked_at = NULL,
        redacted_at = now()
    WHERE id = p_id;
$$ LANGUAGE sql;

COMMIT;
