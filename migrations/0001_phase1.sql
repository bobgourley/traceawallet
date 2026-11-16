-- TraceAWallet record store, Phase 1 (PostgreSQL 15+)
-- Trimmed from the Exhibit v0.2 design. Fresh install, no migration path.
--
-- Rules carried over:
--   1. Observations are facts. Assessments are judgments. They never share a table.
--      Phase 1 stores no assessments at all. A third-party label is an observation
--      of that party's claim, and is always worded that way.
--   2. Evidence is append-only. Corrections are new rows that supersede old ones.
--   3. Every record names its source, and the source row holds the license.
--   4. No email, name or contact detail lives here. A user is an opaque Clerk id.
--   5. The lookup table is private. Nothing public reads it. The public counter
--      (lookup_count) is written by the worker and read on its own.
--
-- Phase 1 tables:  source, address, transfer, label, lookup, report, event_log
-- Supporting:      chain (reference), raw_response (index of bucket objects),
--                  job (the queue), lookup_report (private join), lookup_count
-- Phase 2 adds:    trace_hop, assessment, movement_alert, api_key. None of them
--                  needs a change to the tables below; each hangs off address,
--                  transfer, source or raw_response by foreign key.
--
-- Run as the owning role. The app and worker connect as taw_app (defined at the end).

BEGIN;

-- ---------------------------------------------------------------------------
-- Types
-- ---------------------------------------------------------------------------

CREATE TYPE chain_family  AS ENUM ('evm', 'tron', 'btc');     -- btc reserved, no chain seeded
CREATE TYPE source_kind   AS ENUM ('chain_data', 'price', 'label', 'sanctions', 'onchain', 'manual');
CREATE TYPE transfer_kind AS ENUM ('native', 'token', 'internal');
CREATE TYPE label_status  AS ENUM ('asserted', 'withdrawn');
CREATE TYPE job_status    AS ENUM ('queued', 'running', 'done', 'failed');

-- ---------------------------------------------------------------------------
-- Reference: chains and sources
-- ---------------------------------------------------------------------------

CREATE TABLE chain (
    id           text PRIMARY KEY CHECK (id ~ '^[a-z0-9_]+$'),
    family       chain_family NOT NULL,
    name         text NOT NULL,
    evm_chain_id int,
    explorer_url text NOT NULL,                      -- base URL for links out
    CHECK ((family = 'evm') = (evm_chain_id IS NOT NULL))
);

-- Who told us. Mutable, because terms change, but every change is audited and
-- rows are never deleted.
CREATE TABLE source (
    id               serial PRIMARY KEY,
    slug             text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]+$'),
    name             text NOT NULL,
    kind             source_kind NOT NULL,
    url              text,
    license          text NOT NULL,
    resale_permitted boolean NOT NULL DEFAULT false, -- false until the terms are read and say otherwise
    attribution      text,                           -- display wording, e.g. 'flagged by GoPlus'
    terms_checked_on date,                           -- NULL = nobody has read the terms yet
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- Index of raw provider responses. The body lives in the bucket under bucket_key,
-- which is derived from the hash. One row per fetch, so identical bodies fetched
-- twice give two rows and one object. Never store an API key in request_url or
-- request_params.
CREATE TABLE raw_response (
    id             bigserial PRIMARY KEY,
    source_id      int NOT NULL REFERENCES source(id),
    sha256         char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    bucket_key     text NOT NULL,
    request_url    text NOT NULL,
    request_params jsonb,
    http_status    int NOT NULL,
    byte_size      bigint NOT NULL CHECK (byte_size >= 0),
    fetched_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON raw_response (sha256);
CREATE INDEX ON raw_response (source_id, fetched_at);

-- ---------------------------------------------------------------------------
-- Observations (facts)
-- ---------------------------------------------------------------------------

-- Identity only. Balance, contract-or-wallet and freeze status change over time,
-- so they live in report.data snapshots, not here.
CREATE TABLE address (
    id            bigserial PRIMARY KEY,
    chain         text NOT NULL REFERENCES chain(id),
    address       text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chain, address),
    UNIQUE (id, chain)                               -- target for chain-consistent FKs
);

-- Normalize per chain family. Shape check only: verify EIP-55, base58check and
-- bech32 checksums in the application before insert.
CREATE FUNCTION address_normalize() RETURNS trigger AS $$
DECLARE
    fam chain_family;
BEGIN
    SELECT family INTO fam FROM chain WHERE id = NEW.chain;
    IF fam IS NULL THEN
        RAISE EXCEPTION 'unknown chain %', NEW.chain;
    END IF;
    NEW.address := btrim(NEW.address);

    IF fam = 'evm' THEN
        NEW.address := lower(NEW.address);
        IF NEW.address !~ '^0x[0-9a-f]{40}$' THEN
            RAISE EXCEPTION 'malformed EVM address %', NEW.address;
        END IF;
    ELSIF fam = 'tron' THEN                          -- base58, case-sensitive
        IF NEW.address !~ '^T[1-9A-HJ-NP-Za-km-z]{33}$' THEN
            RAISE EXCEPTION 'malformed Tron address %', NEW.address;
        END IF;
    ELSIF fam = 'btc' THEN
        IF lower(NEW.address) LIKE 'bc1%' THEN       -- bech32 is case-insensitive
            NEW.address := lower(NEW.address);
            IF NEW.address !~ '^bc1[02-9ac-hj-np-z]{11,71}$' THEN
                RAISE EXCEPTION 'malformed bech32 address %', NEW.address;
            END IF;
        ELSIF NEW.address !~ '^[13][1-9A-HJ-NP-Za-km-z]{25,34}$' THEN
            RAISE EXCEPTION 'malformed Bitcoin address %', NEW.address;
        END IF;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER address_normalize BEFORE INSERT ON address
    FOR EACH ROW EXECUTE FUNCTION address_normalize();

-- FACT: one value movement inside one transaction. Immutable.
-- The token is identified by contract, never by symbol: anyone can deploy a "USDT".
-- event_index is the log index (token), the trace position (internal) or 0 (native).
-- Where a provider gives no index, the adapter uses the position within the
-- transaction as returned, and says so in its docstring.
CREATE TABLE transfer (
    id              bigserial PRIMARY KEY,
    chain           text NOT NULL REFERENCES chain(id),
    tx_hash         text NOT NULL CHECK (tx_hash ~ '^(0x)?[0-9a-f]{64}$'),
    kind            transfer_kind NOT NULL,
    event_index     int NOT NULL DEFAULT 0 CHECK (event_index >= 0),
    from_address_id bigint NOT NULL,
    to_address_id   bigint NOT NULL,
    token_contract  text,                            -- NULL for native and internal
    token_symbol    text,                            -- display only, never a key
    token_decimals  int NOT NULL CHECK (token_decimals BETWEEN 0 AND 77),
    amount_raw      numeric(78, 0) NOT NULL CHECK (amount_raw >= 0),
    block_number    bigint NOT NULL CHECK (block_number >= 0),
    block_time      timestamptz NOT NULL,
    usd_value       numeric(38, 6),                  -- at block_time; NULL when no price was found
    price_source_id int REFERENCES source(id),
    source_id       int NOT NULL REFERENCES source(id),
    raw_response_id bigint NOT NULL REFERENCES raw_response(id),
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (from_address_id, chain) REFERENCES address (id, chain),
    FOREIGN KEY (to_address_id,   chain) REFERENCES address (id, chain),
    CHECK ((kind = 'token') = (token_contract IS NOT NULL)),
    CHECK ((usd_value IS NULL) = (price_source_id IS NULL)),
    UNIQUE (chain, tx_hash, kind, event_index, source_id)
);
CREATE INDEX ON transfer (to_address_id, block_number);
CREATE INDEX ON transfer (from_address_id, block_number);
CREATE INDEX ON transfer (chain, tx_hash);

-- FACT: on this date, this source said this about this address. Append-only.
-- A delisting or a corrected label is a new row that supersedes the old one;
-- status = 'withdrawn' records that the source no longer makes the claim.
-- Sanctions hits, GoPlus flags, exchange wallets from the proof-of-reserves file
-- and stablecoin freezes (source kind 'onchain') all land here.
CREATE TABLE label (
    id              bigserial PRIMARY KEY,
    address_id      bigint NOT NULL REFERENCES address(id),
    source_id       int NOT NULL REFERENCES source(id),
    category        text NOT NULL CHECK (category IN
                      ('exchange', 'entity', 'sanctioned', 'seized', 'frozen',
                       'malicious_flag', 'scam_report', 'other')),
    label           text NOT NULL,                   -- the source's own wording
    claim_url       text,                            -- where a reader can check it
    source_date     date,                            -- when the source published or last updated the claim
    observed_at     timestamptz NOT NULL,            -- when we fetched it
    status          label_status NOT NULL DEFAULT 'asserted',
    supersedes_id   bigint REFERENCES label(id),
    raw_response_id bigint REFERENCES raw_response(id),  -- NULL only for the hand-built file
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (status = 'asserted' OR supersedes_id IS NOT NULL)
);
CREATE INDEX ON label (address_id);
-- A row can be superseded once. No forks in the correction history.
CREATE UNIQUE INDEX label_supersedes_once ON label (supersedes_id) WHERE supersedes_id IS NOT NULL;

-- Read this, not the table.
CREATE VIEW label_current AS
SELECT l.*
FROM label l
WHERE l.status = 'asserted'
  AND NOT EXISTS (SELECT 1 FROM label n WHERE n.supersedes_id = l.id);

-- ---------------------------------------------------------------------------
-- Reports
-- ---------------------------------------------------------------------------

-- The shared snapshot for one address on one chain at one block height. It holds
-- nothing about who asked: the victim's matched transaction is a private overlay
-- on lookup. sha256 covers the canonical JSON of
-- {schema_version, template_version, chain, address, as_of_block, data}
-- (sorted keys, no whitespace, UTF-8), computed by the application.
-- A corrected report is a new row with supersedes_id set.
CREATE TABLE report (
    id               bigserial PRIMARY KEY,
    public_id        uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,  -- for URLs; ids are guessable
    address_id       bigint NOT NULL REFERENCES address(id),
    as_of_block      bigint NOT NULL CHECK (as_of_block >= 0),
    as_of_time       timestamptz NOT NULL,
    schema_version   int NOT NULL,
    template_version text NOT NULL,
    data             jsonb NOT NULL,
    sha256           char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    supersedes_id    bigint REFERENCES report(id),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON report (address_id, as_of_block DESC);
CREATE UNIQUE INDEX report_supersedes_once ON report (supersedes_id) WHERE supersedes_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Queue
-- ---------------------------------------------------------------------------

-- Postgres is the queue. The payload never carries a user id, session or IP:
-- a job is "fetch this address", and two people asking about the same address
-- share one job through dedupe_key.
--
-- Worker claim:
--   UPDATE job SET status = 'running', locked_by = $1, locked_at = now(), attempts = attempts + 1
--   WHERE id = (SELECT id FROM job
--               WHERE status = 'queued' AND run_after <= now()
--               ORDER BY run_after, id
--               FOR UPDATE SKIP LOCKED LIMIT 1)
--   RETURNING *;
CREATE TABLE job (
    id           bigserial PRIMARY KEY,
    kind         text NOT NULL,                      -- 'address_lookup', later 'list_refresh', ...
    dedupe_key   text NOT NULL,                      -- e.g. 'evm:0xabc...' or 'tron:T...'
    payload      jsonb NOT NULL DEFAULT '{}',
    status       job_status NOT NULL DEFAULT 'queued',
    attempts     int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 3,
    run_after    timestamptz NOT NULL DEFAULT now(),
    locked_by    text,
    locked_at    timestamptz,
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);
CREATE INDEX job_ready ON job (run_after, id) WHERE status = 'queued';
CREATE UNIQUE INDEX job_one_live ON job (kind, dedupe_key) WHERE status IN ('queued', 'running');

-- ---------------------------------------------------------------------------
-- Private: who searched for what
-- ---------------------------------------------------------------------------

-- Created on submit, before sign-in, against a hashed session cookie. Sign-in
-- sets clerk_user_id once. Rows are never deleted; an erasure request runs
-- lookup_redact(), which strips the person and their inputs and keeps the fact
-- that a lookup happened. Hashes are salted in the application.
CREATE TABLE lookup (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id              bigint REFERENCES job(id),
    family              chain_family NOT NULL,
    submitted_address   text NOT NULL,               -- normalized form of what was typed
    clerk_user_id       text,
    session_hash        char(64),
    ip_hash             char(64),
    -- optional inputs from the form
    chain_hint          text REFERENCES chain(id),
    asset               text,
    amount              numeric,
    sent_on             date,
    sending_address     text,
    tx_hash             text,
    platform_name       text,
    platform_url        text,
    -- private overlay: the victim's own transaction, when the inputs matched one
    matched_transfer_id bigint REFERENCES transfer(id),
    match_method        text,                        -- 'tx_hash', 'sender_amount_date', ...
    created_at          timestamptz NOT NULL DEFAULT now(),
    claimed_at          timestamptz,
    redacted_at         timestamptz,
    CHECK ((matched_transfer_id IS NULL) = (match_method IS NULL))
);
CREATE INDEX ON lookup (clerk_user_id, created_at) WHERE clerk_user_id IS NOT NULL;
CREATE INDEX ON lookup (ip_hash, created_at)       WHERE ip_hash IS NOT NULL;
CREATE INDEX ON lookup (family, submitted_address);

CREATE FUNCTION lookup_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'lookup rows are redacted, not deleted: use lookup_redact()';
    END IF;
    IF OLD.redacted_at IS NOT NULL THEN
        RAISE EXCEPTION 'lookup % is redacted and closed', OLD.id;
    END IF;
    IF NEW.id <> OLD.id OR NEW.family <> OLD.family
       OR NEW.submitted_address <> OLD.submitted_address OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'lookup identity columns are immutable';
    END IF;
    IF OLD.clerk_user_id IS NOT NULL AND NEW.clerk_user_id IS DISTINCT FROM OLD.clerk_user_id
       AND NEW.redacted_at IS NULL THEN
        RAISE EXCEPTION 'a claimed lookup cannot change owner';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER lookup_guard BEFORE UPDATE OR DELETE ON lookup
    FOR EACH ROW EXECUTE FUNCTION lookup_guard();

-- Erasure. The caller is responsible for recomputing lookup_count afterwards.
CREATE FUNCTION lookup_redact(p_id uuid) RETURNS void AS $$
    UPDATE lookup
    SET clerk_user_id = NULL, session_hash = NULL, ip_hash = NULL,
        chain_hint = NULL, asset = NULL, amount = NULL, sent_on = NULL,
        sending_address = NULL, tx_hash = NULL, platform_name = NULL, platform_url = NULL,
        matched_transfer_id = NULL, match_method = NULL,
        redacted_at = now()
    WHERE id = p_id;
$$ LANGUAGE sql;

-- Which report snapshots a lookup was shown. Private, like lookup.
CREATE TABLE lookup_report (
    lookup_id  uuid NOT NULL REFERENCES lookup(id),
    report_id  bigint NOT NULL REFERENCES report(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (lookup_id, report_id)
);

-- The only lookup-derived number a report may show: how many distinct signed-in
-- accounts have looked this address up. Written by the worker. The report path
-- reads this table and never touches lookup.
CREATE TABLE lookup_count (
    address_id    bigint PRIMARY KEY REFERENCES address(id),
    account_count int NOT NULL DEFAULT 0 CHECK (account_count >= 0),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Event log (hash-chained audit trail)
-- ---------------------------------------------------------------------------

CREATE TABLE event_log (
    id        bigint PRIMARY KEY,                    -- assigned by trigger, under the lock
    ts        timestamptz NOT NULL,
    actor     text NOT NULL,
    action    text NOT NULL,
    entity    text NOT NULL,
    entity_id text,
    payload   jsonb,
    prev_hash char(64),
    hash      char(64) NOT NULL
);
CREATE SEQUENCE event_log_id_seq OWNED BY event_log.id;

-- jsonb_build_array gives an unambiguous encoding: no delimiter can be forged
-- from inside a field.
CREATE FUNCTION event_log_hash(p_prev text, p_id bigint, p_ts timestamptz, p_actor text,
                               p_action text, p_entity text, p_entity_id text, p_payload jsonb)
RETURNS text AS $$
    SELECT encode(sha256(convert_to(
        jsonb_build_array(p_prev, p_id, extract(epoch FROM p_ts)::numeric(20, 6)::text,
                          p_actor, p_action, p_entity, p_entity_id, p_payload)::text,
        'UTF8')), 'hex');
$$ LANGUAGE sql IMMUTABLE;

-- The id is taken inside the lock so id order and chain order are the same.
-- This serializes audited writes, which is fine at Phase 1 volume.
CREATE FUNCTION event_log_chain() RETURNS trigger AS $$
DECLARE
    prev text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('event_log_chain'));
    SELECT hash INTO prev FROM event_log ORDER BY id DESC LIMIT 1;
    NEW.id        := nextval('event_log_id_seq');
    NEW.ts        := clock_timestamp();
    NEW.prev_hash := prev;
    NEW.hash      := event_log_hash(prev, NEW.id, NEW.ts, NEW.actor, NEW.action,
                                    NEW.entity, NEW.entity_id, NEW.payload);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER event_log_chain BEFORE INSERT ON event_log
    FOR EACH ROW EXECUTE FUNCTION event_log_chain();

-- Returns the id of the first broken row, or NULL when the chain is intact.
CREATE FUNCTION event_log_verify() RETURNS bigint AS $$
DECLARE
    r        event_log%ROWTYPE;
    expected text := NULL;
BEGIN
    FOR r IN SELECT * FROM event_log ORDER BY id LOOP
        IF r.prev_hash IS DISTINCT FROM expected
           OR r.hash <> event_log_hash(r.prev_hash, r.id, r.ts, r.actor, r.action,
                                       r.entity, r.entity_id, r.payload) THEN
            RETURN r.id;
        END IF;
        expected := r.hash;
    END LOOP;
    RETURN NULL;
END $$ LANGUAGE plpgsql STABLE;

-- Audit trigger. Mode 'full' copies the row into the log. Mode 'id_only' records
-- that something happened and to which row, and nothing else: the log is
-- immutable, so personal data must never be copied into it.
-- The application sets the actor per transaction:  SET LOCAL taw.actor = 'worker-1';
CREATE FUNCTION audit_row() RETURNS trigger AS $$
DECLARE
    rec jsonb := to_jsonb(CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END);
BEGIN
    INSERT INTO event_log (actor, action, entity, entity_id, payload)
    VALUES (coalesce(nullif(current_setting('taw.actor', true), ''), session_user),
            lower(TG_OP), TG_TABLE_NAME, rec ->> 'id',
            CASE WHEN TG_ARGV[0] = 'full' THEN rec ELSE NULL END);
    RETURN NULL;
END $$ LANGUAGE plpgsql;

-- address, transfer and raw_response are not audited row by row: they are
-- immutable, high-volume, and each transfer already points at the hashed
-- provider response it came from.
CREATE TRIGGER audit AFTER INSERT OR UPDATE ON source FOR EACH ROW EXECUTE FUNCTION audit_row('full');
CREATE TRIGGER audit AFTER INSERT           ON label  FOR EACH ROW EXECUTE FUNCTION audit_row('full');
CREATE TRIGGER audit AFTER INSERT OR UPDATE ON lookup FOR EACH ROW EXECUTE FUNCTION audit_row('id_only');

-- A report row is large; log its hash, not its body.
CREATE FUNCTION audit_report() RETURNS trigger AS $$
BEGIN
    INSERT INTO event_log (actor, action, entity, entity_id, payload)
    VALUES (coalesce(nullif(current_setting('taw.actor', true), ''), session_user),
            'insert', 'report', NEW.id::text,
            jsonb_build_object('sha256', NEW.sha256, 'address_id', NEW.address_id,
                               'as_of_block', NEW.as_of_block, 'supersedes_id', NEW.supersedes_id));
    RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER audit AFTER INSERT ON report FOR EACH ROW EXECUTE FUNCTION audit_report();

-- ---------------------------------------------------------------------------
-- Immutability
-- ---------------------------------------------------------------------------

CREATE FUNCTION forbid_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (% blocked)', TG_TABLE_NAME, TG_OP;
END $$ LANGUAGE plpgsql;

DO $$
DECLARE t text;
BEGIN
    -- No UPDATE or DELETE on evidence.
    FOREACH t IN ARRAY ARRAY['raw_response', 'address', 'transfer', 'label', 'report',
                             'lookup_report', 'event_log'] LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I
                        FOR EACH ROW EXECUTE FUNCTION forbid_change()', t || '_immutable', t);
    END LOOP;
    -- source can be edited but not removed.
    CREATE TRIGGER source_no_delete BEFORE DELETE ON source
        FOR EACH ROW EXECUTE FUNCTION forbid_change();
    -- TRUNCATE skips row triggers, so block it at statement level.
    FOREACH t IN ARRAY ARRAY['source', 'raw_response', 'address', 'transfer', 'label', 'report',
                             'lookup', 'lookup_report', 'event_log'] LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON %I
                        FOR EACH STATEMENT EXECUTE FUNCTION forbid_change()', t || '_no_truncate', t);
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- Seed data
-- ---------------------------------------------------------------------------

INSERT INTO chain (id, family, name, evm_chain_id, explorer_url) VALUES
    ('eth',      'evm',  'Ethereum',        1,     'https://etherscan.io'),
    ('bsc',      'evm',  'BNB Smart Chain', 56,    'https://bscscan.com'),
    ('polygon',  'evm',  'Polygon',         137,   'https://polygonscan.com'),
    ('arbitrum', 'evm',  'Arbitrum One',    42161, 'https://arbiscan.io'),
    ('optimism', 'evm',  'OP Mainnet',      10,    'https://optimistic.etherscan.io'),
    ('base',     'evm',  'Base',            8453,  'https://basescan.org'),
    ('tron',     'tron', 'Tron',            NULL,  'https://tronscan.org');

-- Licenses are placeholders until someone reads each provider's terms and fills
-- in terms_checked_on. resale_permitted stays false until then.
INSERT INTO source (slug, name, kind, url, license, resale_permitted, attribution) VALUES
    ('moralis',      'Moralis',                          'chain_data', 'https://moralis.com',          'Provider terms; not yet reviewed', false, 'Moralis'),
    ('trongrid',     'TronGrid',                         'chain_data', 'https://www.trongrid.io',      'Provider terms; not yet reviewed', false, 'TronGrid'),
    ('defillama',    'DefiLlama',                        'price',      'https://defillama.com',        'Provider terms; not yet reviewed', false, 'DefiLlama'),
    ('goplus',       'GoPlus Security',                  'label',      'https://gopluslabs.io',        'Provider terms; not yet reviewed', false, 'flagged by GoPlus'),
    ('ofac-sdn',     'OFAC SDN list',                    'sanctions',  'https://ofac.treasury.gov',    'US government work',               true,  'listed by OFAC'),
    ('nbctf',        'Israel NBCTF seizure orders',      'sanctions',  'https://nbctf.mod.gov.il',     'Government publication; not yet reviewed', false, 'named in an NBCTF seizure order'),
    ('exchange-por', 'Exchange proof-of-reserves file',  'manual',     NULL,                           'Compiled by TraceAWallet from public exchange disclosures', false, 'published by the exchange'),
    ('usdt-contract','Tether USDT contract state',       'onchain',    NULL,                           'Public blockchain data',           true,  'frozen by Tether'),
    ('usdc-contract','Circle USDC contract state',       'onchain',    NULL,                           'Public blockchain data',           true,  'frozen by Circle');

-- ---------------------------------------------------------------------------
-- Application role
-- ---------------------------------------------------------------------------
-- Created NOLOGIN here. Give it a password out of band:
--   ALTER ROLE taw_app LOGIN PASSWORD '...';
-- and point DATABASE_URL at it. Keep the owner credentials for migrations only.
-- Phase 2: the public API gets its own role with no rights on lookup,
-- lookup_report or job.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'taw_app') THEN
        CREATE ROLE taw_app NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO taw_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO taw_app;
GRANT INSERT ON raw_response, address, transfer, label, report, job, lookup, lookup_report,
                lookup_count, event_log TO taw_app;
GRANT UPDATE ON job, lookup, lookup_count TO taw_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO taw_app;
-- No INSERT or UPDATE on source or chain: those change by migration only.

COMMIT;
