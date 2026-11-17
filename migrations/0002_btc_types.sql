-- Bitcoin support, part 1 of 2.
-- New enum values must be committed before anything can use them, so they get
-- their own migration file. Part 2 is 0003_btc.sql.

ALTER TYPE transfer_kind ADD VALUE IF NOT EXISTS 'utxo_in';
ALTER TYPE transfer_kind ADD VALUE IF NOT EXISTS 'utxo_out';
