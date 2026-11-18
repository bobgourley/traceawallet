-- What a chain adapter saw for one address at one block height.
--
-- FACT, immutable. Balances and contract status change over time, so they
-- live here (one row per fetch) rather than on address. The worker reads the
-- newest row to decide whether a lookup can reuse a recent fetch (design
-- rule 8). The report builder will read these rows with the transfers.
--
-- token_balances: [{"contract": "T...", "amount_raw": "123", "decimals": 6,
-- "symbol": "USDT"}, ...]. amount_raw is a string so no precision is lost.
-- raw_response_ids lists every provider response the snapshot was built from.

BEGIN;

CREATE TABLE address_snapshot (
    id                  bigserial PRIMARY KEY,
    chain               text NOT NULL REFERENCES chain(id),
    address_id          bigint NOT NULL,
    as_of_block         bigint NOT NULL CHECK (as_of_block >= 0),
    as_of_time          timestamptz NOT NULL,
    native_balance_raw  numeric(78, 0) NOT NULL CHECK (native_balance_raw >= 0),
    native_decimals     int NOT NULL CHECK (native_decimals BETWEEN 0 AND 77),
    token_balances      jsonb NOT NULL DEFAULT '[]',
    is_contract         boolean NOT NULL,
    tokens_truncated    boolean NOT NULL,
    transfer_count      int NOT NULL CHECK (transfer_count >= 0),
    transfers_truncated boolean NOT NULL,
    source_id           int NOT NULL REFERENCES source(id),
    raw_response_ids    bigint[] NOT NULL,
    fetched_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (address_id, chain) REFERENCES address (id, chain),
    CHECK (jsonb_typeof(token_balances) = 'array')
);
CREATE INDEX ON address_snapshot (address_id, fetched_at DESC);

CREATE TRIGGER address_snapshot_immutable BEFORE UPDATE OR DELETE ON address_snapshot
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER address_snapshot_no_truncate BEFORE TRUNCATE ON address_snapshot
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();

-- "Have we already saved this exact request?" Used before re-fetching
-- responses that cannot change, such as the events of a confirmed transaction.
CREATE INDEX raw_response_request ON raw_response (source_id, request_url);

GRANT SELECT, INSERT ON address_snapshot TO taw_app;
GRANT USAGE, SELECT ON SEQUENCE address_snapshot_id_seq TO taw_app;

COMMIT;
