-- Exchange wallet lists published by the exchanges themselves
-- (taw/adapters/labels/exchanges.py, docs/DECISIONS.md 2026-11-20 "Labels: exchange wallets").
--
-- okx-por: OKX's monthly proof-of-reserves file, loaded straight from OKX (the zip
--          is kept in the bucket as a raw response).
-- exchange-por: data/exchange_wallets.csv, built from HTX's current list and the
--          2022 lists of Bybit, KuCoin, Bitfinex and Crypto.com. Each row keeps its
--          source URL and the exchange's date.
-- Every label reads "listed by <exchange> as its wallet on <date>". None of these
-- pages states terms for reusing the list (checked 2026-11-20), so resale stays false.

BEGIN;

SELECT set_config('taw.actor', 'migration:0014_exchange_sources', true);

INSERT INTO source (slug, name, kind, url, license, resale_permitted, attribution, terms_checked_on) VALUES
    ('okx-por', 'OKX proof-of-reserves address file', 'label', 'https://www.okx.com/en-us/proof-of-reserves/download',
     'Published by OKX in its monthly proof-of-reserves files. The page states no terms for reusing the list '
     '(checked 2026-11-20); OKX general terms: https://www.okx.com/en-us/help/terms-of-service-us.',
     false, 'listed by OKX as its wallet', '2026-11-20');

UPDATE source
SET name = 'Exchange wallet lists (data/exchange_wallets.csv)',
    license = 'Compiled by TraceAWallet from wallet lists the exchanges published themselves: HTX (GitHub, '
              'MIT-licensed repository), Bybit, KuCoin, Bitfinex and Crypto.com (2022). None of the pages states '
              'terms for reusing the list (checked 2026-11-20). Each row keeps its source URL and the exchange''s date.',
    attribution = 'listed by <exchange> as its wallet',
    terms_checked_on = '2026-11-20'
WHERE slug = 'exchange-por';

COMMIT;
