-- Domain age, part 1 of 2: a source kind for records about a domain name rather
-- than an address. New enum values must be committed before anything can use
-- them, so they get their own file (as 0002 did). Part 2 is 0023.

ALTER TYPE source_kind ADD VALUE IF NOT EXISTS 'registry';
