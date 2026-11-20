-- GoPlus display rules and NBCTF's Phase 1 status (docs/DECISIONS.md, 2026-11-20).
--
-- GoPlus: its API License Agreement (s.3, s.5) requires the GoPlus logo with
-- "Powered by GoPlus" wherever results are shown, and results unmodified. A flag
-- is still worded as GoPlus's claim (design rule 9): "flagged by GoPlus".
-- Never queried or shown for Bitcoin: it answers every Bitcoin address with all
-- flags "0", which would read as clean.
--
-- NBCTF: out of Phase 1. The row stays (rows are never deleted).

BEGIN;

SELECT set_config('taw.actor', 'migration:0009_goplus_nbctf', true);

UPDATE source
SET attribution = 'Powered by GoPlus (shown with the GoPlus logo; results unmodified); a flag reads "flagged by GoPlus"',
    license = license || ' Display rules (2026-11-20): the GoPlus logo with "Powered by GoPlus" wherever results '
                         'are shown; results shown unmodified; never queried or shown for Bitcoin.'
WHERE slug = 'goplus';

UPDATE source
SET license = license || ' Out of Phase 1 (2026-11-20): no published terms or licence, robots.txt disallows '
                         'crawling, and many listed wallets are exchange account numbers, not addresses. '
                         'Revisit with written permission from NBCTF.'
WHERE slug = 'nbctf';

DO $$
BEGIN
    IF (SELECT attribution FROM source WHERE slug = 'goplus') NOT LIKE 'Powered by GoPlus%'
       OR (SELECT license FROM source WHERE slug = 'nbctf') NOT LIKE '%Out of Phase 1%' THEN
        RAISE EXCEPTION 'goplus or nbctf source row missing or not updated';
    END IF;
END $$;

COMMIT;
