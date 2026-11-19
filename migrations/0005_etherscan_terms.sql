-- Record what Etherscan's API Terms say, read on 2026-11-19 (terms last updated
-- 2026-10-01). See docs/DECISIONS.md, 2026-11-19.
--
-- resale_permitted stays false. The audit trigger on source logs this change
-- in event_log with the actor below.

BEGIN;

SELECT set_config('taw.actor', 'migration:0005_etherscan_terms', true);

UPDATE source
SET license = 'Etherscan API Terms of Service, last updated 2026-10-01. '
              'Display only, with the attribution "Powered by Etherscan.io APIs" and a link to '
              'https://etherscan.io wherever API data is shown. No redistribution, resale, or '
              'passing data to third parties as an API, feed or proxy; no bulk download or '
              'archive of their datasets; API data must be destroyed when the agreement ends. '
              'A website offered to third parties, free or paid, is Commercial Application use, '
              'which the terms reserve for the Advanced plan and above. '
              'Phase 1 display source only; nothing from it may feed a public API.',
    attribution = 'Powered by Etherscan.io APIs',
    terms_checked_on = DATE '2026-11-19'
WHERE slug = 'etherscan';

DO $$
BEGIN
    IF (SELECT terms_checked_on FROM source WHERE slug = 'etherscan') IS DISTINCT FROM DATE '2026-11-19' THEN
        RAISE EXCEPTION 'etherscan source row missing or not updated';
    END IF;
END $$;

COMMIT;
