-- Every question asked of an address label source (stablecoin contracts now,
-- GoPlus next), whatever the answer (taw/labels.py, docs/DECISIONS.md 2026-11-20
-- "Labels: stablecoin freeze status").
--
-- A label row exists only when a source makes a claim. This table records that
-- the question was asked, at which block, and what came back, so a report can
-- say "not frozen at block N" with the raw response behind it. Append-only.

BEGIN;

SELECT set_config('taw.actor', 'migration:0013_label_check', true);

CREATE TABLE label_check (
    id              bigserial PRIMARY KEY,
    source_id       int NOT NULL REFERENCES source(id),
    address_id      bigint NOT NULL REFERENCES address(id),
    subject         text NOT NULL,          -- what was asked, e.g. the contract address
    block_number    bigint,                 -- the block the answer is for; NULL when the source has no block
    result          text NOT NULL,          -- e.g. 'frozen', 'not_frozen', 'flags', 'no_flags'
    detail          jsonb NOT NULL DEFAULT '{}',
    raw_response_id bigint NOT NULL REFERENCES raw_response(id),
    observed_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON label_check (address_id, source_id, observed_at DESC);

CREATE TRIGGER label_check_immutable BEFORE UPDATE OR DELETE ON label_check
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER label_check_no_truncate BEFORE TRUNCATE ON label_check
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();

GRANT SELECT, INSERT ON label_check TO taw_app;
GRANT USAGE, SELECT ON SEQUENCE label_check_id_seq TO taw_app;

UPDATE source SET url = 'https://tether.to', license = license || '. Read from the USDT contracts'' own '
    'blacklist functions: isBlackListed (Ethereum, Tron), isBlocked (USDT0 on Arbitrum).'
WHERE slug = 'usdt-contract';
UPDATE source SET url = 'https://www.circle.com', license = license || '. Read from the USDC contracts'' own '
    'isBlacklisted function (Ethereum, Arbitrum, OP Mainnet, Tron).'
WHERE slug = 'usdc-contract';

COMMIT;
