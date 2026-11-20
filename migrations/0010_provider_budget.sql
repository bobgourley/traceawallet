-- Daily call budget for metered providers, and Blockscout's finished
-- internal-transaction range (taw/budget.py, docs/DECISIONS.md, 2026-11-20 "Blockscout daily budget").
--
-- These are operational tables, not evidence: rows are updated in place. Nothing
-- in them is shown to anyone; nothing about users is stored.

BEGIN;

-- Calls and estimated credits per provider, UTC day and consumer (worker, scripts).
CREATE TABLE provider_budget (
    provider text NOT NULL,
    day      date NOT NULL,
    consumer text NOT NULL CHECK (consumer IN ('worker', 'scripts')),
    calls    int  NOT NULL DEFAULT 0 CHECK (calls >= 0),
    credits  int  NOT NULL DEFAULT 0 CHECK (credits >= 0),
    PRIMARY KEY (provider, day, consumer)
);

-- The provider's own count of credits left (Blockscout's x-credits-remaining header).
CREATE TABLE provider_status (
    provider          text PRIMARY KEY,
    credits_remaining int NOT NULL,
    observed_at       timestamptz NOT NULL
);

-- The block range in which Blockscout has finished indexing internal transactions,
-- per chain. Reused for 24 hours; the next search starts from it.
CREATE TABLE blockscout_internal_range (
    chain      text PRIMARY KEY REFERENCES chain(id),
    low_block  bigint NOT NULL CHECK (low_block >= 0),
    high_block bigint NOT NULL CHECK (high_block >= low_block),
    found_at   timestamptz NOT NULL
);

GRANT SELECT, INSERT, UPDATE ON provider_budget, provider_status, blockscout_internal_range TO taw_app;
GRANT DELETE ON blockscout_internal_range TO taw_app;

COMMIT;
