"""Does the transfer history explain the balance? One check per token, for every chain.

For each token, transfers in minus transfers out (every transfer up to the
snapshot block) should equal the balance read from the token contract at that
block. The result per token is one of:

  reconciled      the two numbers are equal
  not_reconciled  they differ
  not_checkable   the check cannot be made, with a reason:
                    history_truncated           more than the transfer limit exists, so
                                                in minus out is not the whole history
                    balance_not_read            the balance of this token was not read
                                                (EVM reads stablecoins and the 20
                                                most-used tokens, not every token)
                    internal_range_incomplete   reserved for the native coin, where
                                                internal transfers count; token
                                                transfers do not depend on them

Both numbers are kept either way.

The snapshot's history is marked incomplete when either
  - a freezable stablecoin does not reconcile on a history that is not truncated, or
  - the adapter reported a reason the list may miss transfers
    (TransferBatch.incomplete_reasons).
An incomplete history is never presented as a finished answer.

Why both: balance arithmetic misses gaps that net to zero. A wallet that passes
every token straight through holds 0 whether we saw all its transfers or a
handful; Blockscout's log search found 6 of the test wallet's 41 and the
arithmetic still balanced. The adapter's own cross-checks (token list against
logs) catch that case.

Not detected by this check: a token whose balance changes without a Transfer
event, such as a rebasing token, or a stablecoin issuer burning frozen funds
(Tether's destroyBlackFunds emits its own event, not a Transfer). Such a token
shows as not_reconciled; for a stablecoin that marks the history incomplete,
which is the safe direction.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from taw.adapters.base import AddressSnapshot, TransferBatch

RECONCILED = "reconciled"
NOT_RECONCILED = "not_reconciled"
NOT_CHECKABLE = "not_checkable"


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
class Reconciliation:
    checks: tuple[TokenCheck, ...]
    history_incomplete: bool
    incomplete_reasons: tuple[str, ...]


def reconcile(snap: AddressSnapshot, batch: TransferBatch, stablecoins: Iterable[str]) -> Reconciliation:
    if (snap.chain, snap.address) != (batch.chain, batch.address):
        raise ValueError("snapshot and transfers are for different addresses")
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

    checks = []
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
        checks.append(TokenCheck(contract, symbols.get(contract), contract in stable, status, balance, net, reason))

    reasons = list(batch.incomplete_reasons)
    for c in checks:
        if c.stablecoin and c.status == NOT_RECONCILED:
            reasons.append(f"stablecoin {c.contract} does not reconcile: balance {c.balance_raw}, "
                           f"transfers in minus out {c.net_flow_raw}")
    return Reconciliation(tuple(checks), bool(reasons), tuple(reasons))
