-- Domain age for the website a user entered (taw/domain_age.py; docs/DECISIONS.md,
-- 2026-11-21 "Domain age"). Shown only in the user's private section.
-- Part 2 of 2; part 1 (0022) adds the 'registry' source kind.

BEGIN;

SELECT set_config('taw.actor', 'migration:0023_domain_age_sources', true);

INSERT INTO source (slug, name, kind, url, license, resale_permitted, attribution, terms_checked_on) VALUES
    ('rdap', 'Domain registries'' RDAP services, found through IANA''s bootstrap file', 'registry',
     'https://data.iana.org/rdap/dns.json',
     'Registration data published by each registry through RDAP (RFC 9082/9083). Terms vary by registry; '
     'Verisign''s (.com, .net) allow low-volume lookups (read 2026-11-20). Only the registration date is shown.',
     false, 'according to the domain registry''s RDAP record', '2026-11-21'),
    ('crtsh', 'crt.sh certificate search', 'registry', 'https://crt.sh/',
     'Search over the public certificate transparency logs, run by Sectigo. Terms not stated (checked 2026-11-20).',
     false, 'according to the certificate transparency logs searched through crt.sh', '2026-11-21');

COMMIT;
