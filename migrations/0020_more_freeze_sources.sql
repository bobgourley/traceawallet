-- More stablecoin freeze checks (taw/adapters/labels/freeze.py; docs/DECISIONS.md,
-- 2026-11-21 "More stablecoin freeze checks"). Each is public contract state, read
-- from the token contract's own status function.

BEGIN;

SELECT set_config('taw.actor', 'migration:0020_more_freeze_sources', true);

INSERT INTO source (slug, name, kind, url, license, resale_permitted, attribution, terms_checked_on) VALUES
    ('paxos-contracts', 'Paxos PYUSD, USDP and USDG contract state', 'onchain', 'https://www.paxos.com',
     'Public blockchain data. Read from the contracts'' own isFrozen function (PYUSD on Ethereum and Arbitrum, '
     'USDP and USDG on Ethereum).', true, 'frozen by Paxos', '2026-11-21'),
    ('fdusd-contract', 'FDUSD contract state', 'onchain', 'https://firstdigitallabs.com',
     'Public blockchain data. Read from the contract''s own frozen function (Ethereum).', true,
     'frozen by First Digital', '2026-11-21'),
    ('usd1-contract', 'USD1 contract state', 'onchain', NULL,
     'Public blockchain data. Read from the contract''s own frozen function (Ethereum).', true,
     'frozen by the USD1 issuer', '2026-11-21'),
    ('rlusd-contract', 'RLUSD contract state', 'onchain', NULL,
     'Public blockchain data. Read from the contract''s own accountPaused function (Ethereum).', true,
     'paused by the RLUSD issuer', '2026-11-21');

UPDATE source SET name = 'Circle USDC and EURC contract state',
    license = license || ' EURC (Ethereum) from its own isBlacklisted function, from 2026-11-21.'
WHERE slug = 'usdc-contract';
UPDATE source SET license = license || ' USDT0 on OP Mainnet from its own isBlocked function, from 2026-11-21.'
WHERE slug = 'usdt-contract';

COMMIT;
