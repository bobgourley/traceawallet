-- Resumable backfill of the Ethereum blacklist index (scripts/backfill_blacklist.py,
-- taw/blacklist_backfill.py; docs/DECISIONS.md, 2026-11-21 "Ethereum blacklist backfill").
--
-- blacklist_backfill_part: one row per provider reply the backfill has finished
-- with, cut to the fields the parser reads. A stopped run (allowance used up, a
-- timeout, a restart) starts again after the last row. Nothing reaches
-- blacklist_event until every part is in and Etherscan agrees, so the daily
-- blacklist_refresh job never sees a half-filled index. Operational, like
-- provider_budget: rows may be deleted to start a backfill over.
--
-- provider_budget gains a third consumer, 'backfill', which may use the allowance
-- down to a reserve kept for the production worker (taw/budget.py).

BEGIN;

CREATE TABLE blacklist_backfill_part (
    chain       text   NOT NULL REFERENCES chain(id),
    contract    text   NOT NULL,     -- the contract the reply was about (the USDT owner multisig for its parts)
    part        text   NOT NULL,     -- what was read: an event name, 'owner_executions', 'owner_submissions', ...
    from_block  bigint NOT NULL CHECK (from_block >= 0),
    to_block    bigint NOT NULL CHECK (to_block >= from_block),
    rows        jsonb  NOT NULL CHECK (jsonb_typeof(rows) = 'array'),
    sha256      char(64) NOT NULL,   -- the raw reply, saved to the bucket as usual
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chain, contract, part, from_block)
);

ALTER TABLE provider_budget DROP CONSTRAINT provider_budget_consumer_check;
ALTER TABLE provider_budget ADD CONSTRAINT provider_budget_consumer_check
    CHECK (consumer IN ('worker', 'scripts', 'backfill'));

GRANT SELECT, INSERT, DELETE ON blacklist_backfill_part TO taw_app;

COMMIT;
