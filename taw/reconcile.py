"""Completeness checks on a lookup: does what we fetched add up?

Each check gives one CheckResult (taw/adapters/base.py): passed, failed, or
not_run with the reason. All of them are stored on the snapshot, so a report can
say which passed, which failed and which could not be run. Any failed check
whose `marks_incomplete` is set marks the history incomplete: the report must
say the history may be incomplete, and it is never presented as a finished answer.

  stablecoin_reconciliation   For each freezable stablecoin, transfers in minus out
                              (up to the snapshot block) equals the balance read at
                              that block. Per-token numbers in `tokens`.
  other_token_reconciliation  The same for any other token whose balance was read.
                              Informational: rebasing and fee-on-transfer tokens
                              legitimately differ, so it never marks incomplete.
  native_reconciliation       Native balance = in - out (native and internal)
                              - fees on transactions sent + validator withdrawals.
                              Ethereum and Arbitrum only, and only when the whole
                              history and the whole internal-transfer range were read.
  tx_count                    The address's nonce at the snapshot block equals the
                              number of transactions it sent in the provider's list,
                              failed ones included. EVM only; skipped for contracts
                              and EIP-7702 accounts (see the reasons below).
  (adapter checks)            Whatever the adapter checked on its own lists, e.g.
                              token_lists_agree (Blockscout) or token_list_matches_logs
                              (Etherscan).

Why the adapter's own checks matter: arithmetic cannot see a gap that nets to
zero. Blockscout's log search returned 6 of the test wallet's 41 token transfers;
the wallet passes everything through, so in minus out still equalled 0.

Not seen by any check: a balance that changes without a Transfer event (a
rebasing token, an issuer burning frozen funds). It shows as a failed
reconciliation, which for a stablecoin marks the history incomplete: the safe
direction.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from taw.adapters.base import FAILED, NOT_RUN, PASSED, AddressSnapshot, CheckResult, TransferBatch

RECONCILED = "reconciled"
NOT_RECONCILED = "not_reconciled"
NOT_CHECKABLE = "not_checkable"

NATIVE_CHECK_CHAINS = ("eth", "arbitrum")
NATIVE_NOT_RUN = {
    "optimism": "Optimism's L1 data fee is not read yet, so the fees paid cannot be totalled.",
    "tron": "TRX staking, unstaking and resource delegation are not parsed yet; they move the balance.",
}
NO_NONCE = {
    "tron": "Tron accounts have no transaction nonce.",
    "btc": "Bitcoin addresses have no transaction nonce.",
}


@dataclass(frozen=True)
class TokenCheck:
    contract: str
    symbol: str | None
    stablecoin: bool
    status: str
    balance_raw: int | None          # read from the contract at the snapshot block
    net_flow_raw: int                # transfers in minus transfers out, up to that block
    reason: str | None = None        # for not_checkable

    def as_json(self) -> dict:
        d = asdict(self)
        d["balance_raw"] = None if self.balance_raw is None else str(self.balance_raw)
        d["net_flow_raw"] = str(self.net_flow_raw)        # strings: no precision lost
        return d


@dataclass(frozen=True)
class Completeness:
    tokens: tuple[TokenCheck, ...]      # per token
    checks: tuple[CheckResult, ...]     # every check, the adapter's included
    history_incomplete: bool
    incomplete_reasons: tuple[str, ...]


# -- tokens -------------------------------------------------------------------------

def token_checks(snap: AddressSnapshot, batch: TransferBatch, stablecoins: Iterable[str]) -> tuple[TokenCheck, ...]:
    stable = set(stablecoins)
    held = {b.contract: b for b in snap.token_balances}
    read = None if snap.balances_read is None else set(snap.balances_read)

    flow: dict[str, int] = {}
    symbols: dict[str, str | None] = {b.contract: b.symbol for b in snap.token_balances}
    for t in batch.transfers:
        if t.token_contract is None or t.block_number > snap.as_of_block:
            continue
        net = flow.get(t.token_contract, 0)
        if t.to_address == batch.address:
            net += t.amount_raw
        if t.from_address == batch.address:
            net -= t.amount_raw
        flow[t.token_contract] = net
        symbols.setdefault(t.token_contract, t.token_symbol)

    out = []
    for contract in sorted(set(flow) | set(held) | (stable & (set(read) if read is not None else set()))):
        net = flow.get(contract, 0)
        if contract in held:
            balance = held[contract].amount_raw
        elif read is None or contract in read:
            balance = 0
        else:
            balance = None
        if balance == 0 and net == 0 and contract not in flow:
            continue                              # checked, never touched: nothing to say
        if batch.truncated:
            status, reason = NOT_CHECKABLE, "history_truncated"
        elif balance is None:
            status, reason = NOT_CHECKABLE, "balance_not_read"
        elif balance == net:
            status, reason = RECONCILED, None
        else:
            status, reason = NOT_RECONCILED, None
        out.append(TokenCheck(contract, symbols.get(contract), contract in stable, status, balance, net, reason))
    return tuple(out)


def _token_summary(name: str, tokens: list[TokenCheck], marks_incomplete: bool, what: str) -> CheckResult:
    failed = [t for t in tokens if t.status == NOT_RECONCILED]
    done = [t for t in tokens if t.status == RECONCILED]
    details = {"reconciled": len(done), "not_reconciled": len(failed),
               "not_checkable": len(tokens) - len(done) - len(failed)}
    if failed:
        reason = "; ".join(f"{what} {t.contract} does not reconcile: balance {t.balance_raw}, "
                           f"transfers in minus out {t.net_flow_raw}" for t in failed)
        return CheckResult(name, FAILED, reason, details, marks_incomplete)
    if done:
        return CheckResult(name, PASSED, None, details, marks_incomplete)
    if not tokens:
        return CheckResult(name, NOT_RUN, f"no {what} was held or moved", details, marks_incomplete)
    reasons = sorted({t.reason for t in tokens if t.reason})
    return CheckResult(name, NOT_RUN, f"not checkable: {', '.join(reasons)}", details, marks_incomplete)


# -- native coin ----------------------------------------------------------------------

def native_check(snap: AddressSnapshot, batch: TransferBatch) -> CheckResult:
    name = "native_reconciliation"
    if snap.chain not in NATIVE_CHECK_CHAINS:
        return CheckResult(name, NOT_RUN, NATIVE_NOT_RUN.get(snap.chain, "Not built for this chain yet."))
    if batch.truncated:
        return CheckResult(name, NOT_RUN, f"The history is truncated at {batch.limit:,} transfers.")
    lo_hi = batch.internal_range
    if lo_hi is None or lo_hi != (0, snap.as_of_block):
        span = f"blocks {lo_hi[0]:,} to {lo_hi[1]:,}" if lo_hi else "no blocks"
        return CheckResult(name, NOT_RUN, f"Internal transfers were read only for {span}; "
                                          "outside that range nothing is checked.")
    if batch.sent_tx_count is None or batch.fees_paid_raw is None:
        return CheckResult(name, NOT_RUN, "The list of transactions sent was not read in full.")
    if batch.withdrawals_raw is None:
        return CheckResult(name, NOT_RUN, "Validator withdrawals were not read.")
    inflow = outflow = 0
    for t in batch.transfers:
        if t.token_contract is not None or t.block_number > snap.as_of_block:
            continue
        if t.to_address == batch.address:
            inflow += t.amount_raw
        if t.from_address == batch.address:
            outflow += t.amount_raw
    expected = inflow - outflow - batch.fees_paid_raw + batch.withdrawals_raw
    details = {"balance_raw": str(snap.native_balance_raw), "in_raw": str(inflow), "out_raw": str(outflow),
               "fees_raw": str(batch.fees_paid_raw), "withdrawals_raw": str(batch.withdrawals_raw),
               "expected_raw": str(expected), "transactions_sent": batch.sent_tx_count}
    if expected == snap.native_balance_raw:
        return CheckResult(name, PASSED, None, details)
    return CheckResult(name, FAILED, f"{snap.native_symbol} balance {snap.native_balance_raw} but in - out - fees "
                                     f"+ withdrawals = {expected}", details)


# -- transaction count --------------------------------------------------------------------

def tx_count_check(snap: AddressSnapshot, batch: TransferBatch) -> CheckResult:
    name = "tx_count"
    if snap.chain in NO_NONCE:
        return CheckResult(name, NOT_RUN, NO_NONCE[snap.chain])
    if snap.nonce is None:
        return CheckResult(name, NOT_RUN, "The provider could not read the nonce at the snapshot block.")
    if snap.is_contract:
        return CheckResult(name, NOT_RUN, "The address is a contract: its nonce counts the contracts it created, "
                                          "not transactions it sent.")
    if snap.eip7702_delegate:
        return CheckResult(name, NOT_RUN, "The address delegates its code under EIP-7702: authorizations it signs "
                                          "raise its nonce without a transaction from it.",
                           {"delegate": snap.eip7702_delegate})
    if batch.truncated or batch.sent_tx_count is None:
        return CheckResult(name, NOT_RUN, "The history is truncated, so not every transaction sent was read.")
    details: dict = {"nonce": snap.nonce, "transactions_sent": batch.sent_tx_count}
    expected = snap.nonce
    if snap.history_start_block is not None:
        if snap.nonce_before_history is None:
            return CheckResult(name, NOT_RUN, f"The nonce at block {snap.history_start_block:,}, where this chain's "
                                              "listed history starts, could not be read.")
        expected = snap.nonce - snap.nonce_before_history
        details.update({"history_start_block": snap.history_start_block,
                        "transactions_before_history": snap.nonce_before_history,
                        "transactions_before_history_note": "unavailable from any explorer",
                        "nonce_since_history_start": expected})
    if expected == batch.sent_tx_count:
        return CheckResult(name, PASSED, None, details)
    if snap.history_start_block is not None:
        return CheckResult(name, FAILED, f"The address sent {expected} transactions since block "
                                         f"{snap.history_start_block:,} (nonce) but the provider listed "
                                         f"{batch.sent_tx_count}", details)
    return CheckResult(name, FAILED, f"The address has sent {snap.nonce} transactions (nonce) but the provider "
                                     f"listed {batch.sent_tx_count}", details)


# -- all of them -------------------------------------------------------------------------

def run_checks(snap: AddressSnapshot, batch: TransferBatch, stablecoins: Iterable[str]) -> Completeness:
    if (snap.chain, snap.address) != (batch.chain, batch.address):
        raise ValueError("snapshot and transfers are for different addresses")
    tokens = token_checks(snap, batch, stablecoins)
    checks = [
        _token_summary("stablecoin_reconciliation", [t for t in tokens if t.stablecoin], True, "stablecoin"),
        _token_summary("other_token_reconciliation", [t for t in tokens if not t.stablecoin], False, "token"),
        native_check(snap, batch),
        tx_count_check(snap, batch),
        *batch.checks,
    ]
    reasons = tuple(c.reason or c.name for c in checks if c.status == FAILED and c.marks_incomplete)
    return Completeness(tokens, tuple(checks), bool(reasons), reasons)


def reconcile(snap: AddressSnapshot, batch: TransferBatch, stablecoins: Iterable[str]) -> Completeness:
    """Kept name: run every check."""
    return run_checks(snap, batch, stablecoins)
