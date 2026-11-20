-- A global index of stablecoin freeze, unfreeze and destruction events
-- (taw/blacklist.py, taw/adapters/labels/blacklist.py, docs/DECISIONS.md
-- 2026-11-20 "Burned funds: a global index of blacklist events").
--
--   eth   USDT  AddedBlackList / RemovedBlackList / DestroyedBlackFunds   via Blockscout eth_getLogs
--   eth   USDC  Blacklisted / UnBlacklisted                               via Blockscout eth_getLogs
--   tron  USDT  AddedBlackList / RemovedBlackList / DestroyedBlackFunds   via TronGrid's events list
--   tron  USDC  Blacklisted / UnBlacklisted                               via TronGrid's events list
--
-- Ethereum USDT's events do not index the address and TronGrid cannot filter
-- events by address, so a freeze date or a destruction for one address can only
-- be found in a full list of the contract's events. This is that list, backfilled
-- once and updated daily.
--
-- blacklist_scan: one row per scan of one contract over a block range. The scan
-- writes its events and one event_log entry (action 'scan') with the row count
-- and a sha256 over the rows it added, in the same transaction, like a label
-- load (0015). The events skip a per-row audit copy. covered_to_block says how
-- far the index is complete for that contract: nothing found beyond it means
-- nothing was looked for there. The number of events a scan added is in its
-- event_log entry (and countable by scan_id).

BEGIN;

SELECT set_config('taw.actor', 'migration:0016_blacklist_events', true);

CREATE TABLE blacklist_scan (
    id               bigserial PRIMARY KEY,
    chain            text NOT NULL REFERENCES chain(id),
    contract         text NOT NULL,
    token            text NOT NULL,
    source_id        int NOT NULL REFERENCES source(id),      -- the token contract (usdt-contract, usdc-contract)
    raw_source_id    int NOT NULL REFERENCES source(id),      -- who served the responses (blockscout, trongrid)
    from_block       bigint NOT NULL CHECK (from_block >= 0),
    to_block         bigint NOT NULL CHECK (to_block >= from_block),
    to_time          timestamptz,                             -- Tron scans are bounded by time; the block is the head at that time
    calls            int NOT NULL CHECK (calls >= 0),
    detail           jsonb NOT NULL DEFAULT '{}',
    scanned_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX blacklist_scan_contract ON blacklist_scan (chain, contract, to_block);

CREATE TYPE blacklist_action AS ENUM ('freeze', 'unfreeze', 'destroy');

CREATE TABLE blacklist_event (
    id               bigserial PRIMARY KEY,
    scan_id          bigint NOT NULL REFERENCES blacklist_scan(id),
    chain            text NOT NULL REFERENCES chain(id),
    contract         text NOT NULL,
    token            text NOT NULL,
    action           blacklist_action NOT NULL,
    event_name       text NOT NULL,                           -- as the contract names it
    address          text NOT NULL,                           -- the frozen, unfrozen or burned address, stored form
    amount_raw       numeric(78, 0) CHECK ((action = 'destroy') = (amount_raw IS NOT NULL)),
    block_number     bigint NOT NULL,
    block_time       timestamptz,                             -- NULL when the provider's log carried no time
    tx_hash          text NOT NULL,
    log_index        int NOT NULL,
    source_id        int NOT NULL REFERENCES source(id),
    raw_response_id  bigint REFERENCES raw_response(id),
    observed_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chain, contract, tx_hash, log_index)
);
CREATE INDEX blacklist_event_address ON blacklist_event (chain, address, contract, block_number);
CREATE INDEX blacklist_event_scan ON blacklist_event (scan_id, id);

CREATE TRIGGER blacklist_scan_immutable BEFORE UPDATE OR DELETE ON blacklist_scan
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER blacklist_scan_no_truncate BEFORE TRUNCATE ON blacklist_scan
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();
CREATE TRIGGER blacklist_event_immutable BEFORE UPDATE OR DELETE ON blacklist_event
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER blacklist_event_no_truncate BEFORE TRUNCATE ON blacklist_event
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();

GRANT SELECT, INSERT ON blacklist_scan, blacklist_event TO taw_app;
GRANT USAGE, SELECT ON SEQUENCE blacklist_scan_id_seq, blacklist_event_id_seq TO taw_app;

COMMIT;
