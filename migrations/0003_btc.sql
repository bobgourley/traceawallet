-- Bitcoin support, part 2 of 2, plus the data sources that replace Moralis.
--
-- A Bitcoin transaction has many inputs and many outputs, so one transfer row
-- is one side of one transaction:
--   utxo_in   this address spent amount_raw as input  number event_index (from set, to NULL)
--   utxo_out  this address received amount_raw as output number event_index (to set, from NULL)
-- There is no single "sender" or "recipient". The report must say "outputs of
-- transactions that spent from this address", never "sent to", because one
-- output is usually change returning to the spender. Outputs with no address
-- (OP_RETURN) are skipped by the adapter. token_decimals is 8.

BEGIN;

ALTER TABLE transfer ALTER COLUMN from_address_id DROP NOT NULL;
ALTER TABLE transfer ALTER COLUMN to_address_id   DROP NOT NULL;

ALTER TABLE transfer ADD CONSTRAINT transfer_sides CHECK (
    CASE kind
        WHEN 'utxo_in'  THEN from_address_id IS NOT NULL AND to_address_id IS NULL
        WHEN 'utxo_out' THEN to_address_id   IS NOT NULL AND from_address_id IS NULL
        ELSE from_address_id IS NOT NULL AND to_address_id IS NOT NULL
    END
);

INSERT INTO chain (id, family, name, evm_chain_id, explorer_url) VALUES
    ('btc', 'btc', 'Bitcoin', NULL, 'https://mempool.space');

INSERT INTO source (slug, name, kind, url, license, resale_permitted, attribution) VALUES
    ('etherscan',     'Etherscan API V2',        'chain_data', 'https://etherscan.io',    'Provider terms; free plan requires attribution; not yet reviewed', false, 'Powered by Etherscan.io APIs'),
    ('mempool-space', 'mempool.space',           'chain_data', 'https://mempool.space',   'Provider terms; not yet reviewed', false, 'mempool.space'),
    ('blockstream',   'Blockstream Esplora API', 'chain_data', 'https://blockstream.info','Provider terms; not yet reviewed', false, 'Blockstream');

COMMIT;
