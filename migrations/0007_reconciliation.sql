-- Does the transfer history explain the balance? (taw/reconcile.py)
--
-- token_reconciliation: one object per token, [{"contract", "symbol",
--   "stablecoin", "status" (reconciled | not_reconciled | not_checkable),
--   "balance_raw", "net_flow_raw" (strings, no precision lost), "reason"}].
-- history_incomplete: true when a freezable stablecoin does not reconcile on a
--   history that is not truncated, or the adapter reported that its list may
--   miss transfers. Such a snapshot is never presented as a finished answer;
--   the report must say the history may be incomplete.
-- incomplete_reasons: why, in plain words.
--
-- Rows written before this migration have NULL in all three: the check was not
-- run, which also must never be read as complete. The table stays append-only;
-- adding columns does not change any existing row's values.

BEGIN;

ALTER TABLE address_snapshot
    ADD COLUMN token_reconciliation jsonb CHECK (token_reconciliation IS NULL
                                               OR jsonb_typeof(token_reconciliation) = 'array'),
    ADD COLUMN history_incomplete boolean,
    ADD COLUMN incomplete_reasons text[],
    ADD CONSTRAINT snapshot_reconciliation_together CHECK (
        (token_reconciliation IS NULL) = (history_incomplete IS NULL)
        AND (history_incomplete IS NULL) = (incomplete_reasons IS NULL));

COMMIT;
