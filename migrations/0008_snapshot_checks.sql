-- Every completeness check on a lookup, stored separately (taw/reconcile.py), and
-- the block range whose internal transfers were read in full.
--
-- checks: [{"name", "status" (passed | failed | not_run), "reason", "details",
--   "marks_incomplete"}, ...], one object per check, so a report can say which
--   checks passed, which failed and which could not be run.
-- internal_from_block, internal_to_block: internal transfers were read for these
--   blocks only. Nothing outside the range is checked. NULL where internal
--   transfers are not read at all (Tron, Bitcoin) and on rows from before 0008.
--
-- history_incomplete (0007) is true when any check with marks_incomplete failed.
-- Like 0007, rows from before this migration keep NULL: not checked.

BEGIN;

ALTER TABLE address_snapshot
    ADD COLUMN checks jsonb CHECK (checks IS NULL OR jsonb_typeof(checks) = 'array'),
    ADD COLUMN internal_from_block bigint,
    ADD COLUMN internal_to_block bigint,
    ADD CONSTRAINT snapshot_internal_range CHECK (
        (internal_from_block IS NULL) = (internal_to_block IS NULL)
        AND (internal_from_block IS NULL OR (0 <= internal_from_block AND internal_from_block <= internal_to_block
                                             AND internal_to_block <= as_of_block)));

COMMIT;
