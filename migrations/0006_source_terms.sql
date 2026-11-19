-- Record what each source's terms say, read on 2026-11-19, and add Blockscout,
-- the EVM source the worker now uses. Details and quotes:
-- docs/research/evm-sources.md and docs/research/data-sources.md.
--
-- resale_permitted stays false everywhere except OFAC (US government work). The
-- audit trigger on source logs every change with the actor below.

BEGIN;

SELECT set_config('taw.actor', 'migration:0006_source_terms', true);

INSERT INTO source (slug, name, kind, url, license, resale_permitted, attribution, terms_checked_on) VALUES
    ('blockscout', 'Blockscout PRO API', 'chain_data', 'https://blockscout.com',
     'Blockscout Terms & Conditions v1.1, last updated 2024-12-06. Licence to use the Products "for their '
     'intended purposes"; no internal-use limit, no redistribution, caching or deletion clause. Clause 7(h) bars '
     '"duplicating its essential elements" without written consent, which a later paid API must clear. Written '
     'confirmation of our use requested; launch to testers waits for it.',
     false, 'Blockscout', DATE '2026-11-19');

UPDATE source SET license =
    'TronGrid Terms of Service, last updated 2026-09-24 (Singapore law). Licence to use the Services "for their '
    'intended purpose". Bars "resell, sublicense or provide unauthorized third-party access to the Services or API '
    'keys". Silent on storage; docs recommend caching. No attribution. A paid API needs TronGrid''s agreement.',
    terms_checked_on = DATE '2026-11-19'
WHERE slug = 'trongrid';

UPDATE source SET license =
    'mempool.space Terms of Service, updated 2024-07-10. Cover the API but contain only disclaimers: no licence, '
    'use or storage clause. Silent on a public site and on resale. Software is AGPL-3.0; self-hosting avoids the '
    'hosted terms.',
    terms_checked_on = DATE '2026-11-19'
WHERE slug = 'mempool-space';

UPDATE source SET license =
    'Blockstream general terms are silent on API use. The Explorer API has its own terms, shown only at first '
    'login; not read yet. Software (esplora, electrs) is MIT; self-hosting avoids the hosted terms.',
    terms_checked_on = DATE '2026-11-19'
WHERE slug = 'blockstream';

UPDATE source SET license =
    'GoPlus API License Agreement, last updated 2023-08-21 (Tops Labs Ltd, New York law). Display allowed; the GoPlus '
    'logo with "Powered by GoPlus" is mandatory; results must not be modified. No commercial use of "our original '
    'data" to "generate revenue" without written permission. Silent on storage. Does not cover Bitcoin.',
    terms_checked_on = DATE '2026-11-19'
WHERE slug = 'goplus';

UPDATE source SET license =
    'DefiLlama Terms of Use, effective 2025-06-24 (DefiLlama Limited, UAE). Licence "for personal, non-commercial '
    'purposes"; bars "republish the data in any form without permission" and reselling the data. Its FAQ calls the '
    'API "open" and "free to use"; the Terms bind. Written permission needed before prices appear on the public site.',
    terms_checked_on = DATE '2026-11-19'
WHERE slug = 'defillama';

UPDATE source SET license =
    'US government work, no copyright (17 U.S.C. 105). Free; no key. Use the list files, not the search tool '
    '(FAQ 287). Listings "are not likely to be exhaustive" (FAQ 562); do not imply Treasury endorsement.',
    terms_checked_on = DATE '2026-11-19'
WHERE slug = 'ofac-sdn';

UPDATE source SET license =
    'No terms published: the matal.mod.gov.il terms page holds placeholder text; footer reserves all rights to '
    'NBCTF and the Ministry of Defense. robots.txt disallows crawling. Showing a match with a link to the order is '
    'low risk; written permission needed before the public site uses it, and for any resale.',
    url = 'https://matal.mod.gov.il',
    terms_checked_on = DATE '2026-11-19'
WHERE slug = 'nbctf';

DO $$
BEGIN
    IF (SELECT count(*) FROM source
        WHERE slug IN ('blockscout', 'trongrid', 'mempool-space', 'blockstream', 'goplus', 'defillama',
                       'ofac-sdn', 'nbctf')
          AND terms_checked_on = DATE '2026-11-19') <> 8 THEN
        RAISE EXCEPTION 'a source row is missing or was not updated';
    END IF;
END $$;

COMMIT;
