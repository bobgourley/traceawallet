-- USD prices from Chainlink feeds read on-chain (taw/adapters/prices/chainlink.py,
-- docs/DECISIONS.md 2026-11-20 "Prices: Chainlink at bucket starts").
--
-- One row per asset and time bucket: hourly for transfers in the last 48 hours,
-- daily before that. The feed is read with latestRoundData() at the last block at
-- or before the bucket's start. A read that gives no usable price (the feed did
-- not exist yet, its answer was older than allowed, or not positive) is kept with
-- usd NULL and the reason, so it is not read again; a report shows "no price
-- available" for it. Append-only, like every other evidence table.

BEGIN;

SELECT set_config('taw.actor', 'migration:0011_price', true);

INSERT INTO source (slug, name, kind, url, license, resale_permitted, attribution, terms_checked_on) VALUES
    ('chainlink', 'Chainlink price feeds (read on-chain)', 'price', 'https://data.chain.link',
     'Chainlink Foundation Terms of Service, Version 6.0, effective 2026-08-18 (https://chain.link/terms). '
     'Feed data is read from the proxy contracts on-chain. No attribution required; naming Chainlink must not '
     'suggest endorsement. The distribution clause needs a written answer before any paid API.',
     false,
     'USD value estimated from a Chainlink price feed; approximate, not an exchange price; not affiliated with '
     'or endorsed by Chainlink',
     '2026-11-20');

CREATE TYPE price_status AS ENUM ('ok', 'stale', 'no_feed', 'invalid');

CREATE TABLE price (
    id                    bigserial PRIMARY KEY,
    asset                 text NOT NULL CHECK (asset ~ '^[A-Z0-9]+$'),
    bucket_start          timestamptz NOT NULL,
    bucket_size           text NOT NULL CHECK (bucket_size IN ('hour', 'day')),
    status                price_status NOT NULL,
    usd                   numeric(38, 18),             -- NULL unless status = 'ok'
    feed_chain            text NOT NULL REFERENCES chain(id),
    feed_address          text NOT NULL CHECK (feed_address ~ '^0x[0-9a-f]{40}$'),
    block_number          bigint NOT NULL CHECK (block_number >= 0),
    round_id              numeric(78, 0),
    feed_updated_at       timestamptz,                 -- the round's updatedAt
    age_seconds           bigint,                      -- bucket_start minus feed_updated_at
    max_age_seconds       bigint NOT NULL,             -- the limit the read was judged against
    source_id             int NOT NULL REFERENCES source(id),             -- chainlink
    raw_response_id       bigint NOT NULL REFERENCES raw_response(id),    -- the latestRoundData read
    block_raw_response_id bigint NOT NULL REFERENCES raw_response(id),    -- the block-at-time lookup
    observed_at           timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'ok') = (usd IS NOT NULL)),
    CHECK (usd IS NULL OR usd > 0),
    CHECK (date_trunc(bucket_size, bucket_start AT TIME ZONE 'UTC') = bucket_start AT TIME ZONE 'UTC'),
    UNIQUE (asset, bucket_start, bucket_size, feed_address)
);

CREATE TRIGGER price_immutable BEFORE UPDATE OR DELETE ON price
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER price_no_truncate BEFORE TRUNCATE ON price
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();

GRANT SELECT, INSERT ON price TO taw_app;
GRANT USAGE, SELECT ON SEQUENCE price_id_seq TO taw_app;

COMMIT;
