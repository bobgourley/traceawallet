"""Find the victim's own transaction among an address's transfers.

The result is stored on the lookup row only (lookup.match_result, migration 0017):
never in a report, never in the event log. Results:

  matched               one transfer, with the method that found it:
                          tx_hash             the transaction was read directly by hash
                          sender_amount_date  sender, amount and date all fit
                          amount_date         amount and date fit, and only one transfer does
  candidates            more than one transfer fits at the strongest method that found
                        any; all are returned, none is picked
  not_found             nothing fits in what was read. can_rule_out says whether that
                        settles it: not when the history read was truncated before the
                        date given (or no date was given), or when the hash is unknown
  pays_another_address  the transaction given by hash exists but pays other addresses,
                        not this one: the victim may have the wrong address
  not_attempted         neither a hash nor an amount was given

Amounts. The amount received may be below the amount sent by a small fee: up to
FEE_FRACTION (1%) less, or rounding of the last digit given more. Dates: a day
either side of the date given, in UTC, because the victim's date is local time.
Asset: when given, only transfers of that asset are compared (by contract; the
name typed is matched to our list of recognised assets).

For a match, the next outgoing transfers of the same asset after it are listed,
with the sentence that this does not show those were the victim's funds.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from taw.adapters.base import TransactionFetch, TransferRecord
from taw.report.inputs import asset_of

FEE_FRACTION = Decimal("0.01")
DATE_SLACK = dt.timedelta(days=1)
NEXT_ROWS = 10
KNOWN_ASSETS = ("USDT", "USDC", "DAI", "ETH", "TRX", "BTC")
NOT_PROOF = ("These are the next transfers of the same asset out of this address after yours. This does not "
             "show that they were your funds: an address can hold money from many people.")


@dataclass(frozen=True)
class MatchInputs:
    asset: str | None = None
    amount: Decimal | None = None
    sent_on: dt.date | None = None
    sending_address: str | None = None
    tx_hash: str | None = None

    @staticmethod
    def parse_amount(text) -> Decimal | None:
        if text in (None, ""):
            return None
        try:
            d = Decimal(str(text).replace(",", ""))
        except InvalidOperation as exc:
            raise ValueError(f"not an amount: {text!r}") from exc
        if d <= 0:
            raise ValueError("the amount must be more than zero")
        return d


def _norm(chain: str, a: str | None) -> str | None:
    if a is None:
        return None
    return a if chain == "tron" else a.lower()


def _amount(t: TransferRecord) -> Decimal:
    return Decimal(t.amount_raw).scaleb(-t.token_decimals)


def amount_fits(received: Decimal, sent: Decimal) -> bool:
    exp = sent.as_tuple().exponent
    rounding = Decimal(1).scaleb(exp if isinstance(exp, int) and exp < 0 else 0) / 2
    return sent * (1 - FEE_FRACTION) - rounding <= received <= sent + rounding


def date_fits(when: dt.datetime, sent_on: dt.date) -> bool:
    d = when.astimezone(dt.timezone.utc).date()
    return sent_on - DATE_SLACK <= d <= sent_on + DATE_SLACK


def _row(t: TransferRecord, chain: str) -> dict:
    return {"chain": chain, "tx_hash": t.tx_hash, "kind": t.kind, "event_index": t.event_index,
            "from": t.from_address, "to": t.to_address, "contract": t.token_contract, "symbol": t.token_symbol,
            "asset": asset_of(chain, t.token_contract), "amount": format(_amount(t).normalize(), "f"),
            "amount_raw": str(t.amount_raw), "block": t.block_number,
            "time": t.block_time.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def inbound(chain: str, address: str, transfers) -> list[TransferRecord]:
    me = _norm(chain, address)
    if chain == "btc":
        return [t for t in transfers if t.kind == "utxo_out" and _norm(chain, t.to_address) == me]
    return [t for t in transfers if _norm(chain, t.to_address) == me and t.kind not in ("utxo_in", "utxo_out")]


def next_outgoing(chain: str, address: str, transfers, after: TransferRecord, limit: int = NEXT_ROWS) -> list[dict]:
    """The address's outgoing transfers of the same asset after `after`, oldest first.
    Zero-value transfers are left out: they move nothing (and are often made by others)."""
    me = _norm(chain, address)
    key = _norm(chain, after.token_contract)
    if chain == "btc":
        rows = [t for t in transfers if t.kind == "utxo_in" and _norm(chain, t.from_address) == me]
    else:
        rows = [t for t in transfers if _norm(chain, t.from_address) == me and _norm(chain, t.token_contract) == key
                and t.kind not in ("utxo_in", "utxo_out")]
    later = [t for t in rows if t.amount_raw > 0 and (t.block_number, t.tx_hash) > (after.block_number, after.tx_hash)]
    later.sort(key=lambda t: (t.block_number, t.tx_hash, t.event_index))
    return [_row(t, chain) for t in later[:limit]]


def _asset_filter(chain: str, asset: str | None):
    if not asset:
        return None, None
    a = asset.strip().upper()
    if a not in KNOWN_ASSETS:
        return None, "asset_not_recognised"
    return (lambda t: asset_of(chain, t.token_contract) == a), None


def match_transfers(chain: str, address: str, transfers, inp: MatchInputs, *, truncated: bool,
                    oldest_read: dt.datetime | None) -> dict:
    """Without a hash: sender plus amount plus date, then amount plus date."""
    if inp.amount is None:
        return {"status": "not_attempted", "reason": "needs_amount_or_tx_hash"}
    keep, asset_note = _asset_filter(chain, inp.asset)
    pool = [t for t in inbound(chain, address, transfers) if keep is None or keep(t)]
    fits = [t for t in pool if amount_fits(_amount(t), inp.amount)
            and (inp.sent_on is None or date_fits(t.block_time, inp.sent_on))]
    date_part = "_date" if inp.sent_on else ""
    steps = []
    if inp.sending_address:
        sender = _norm(chain, inp.sending_address)
        steps.append((f"sender_amount{date_part}", [t for t in fits if _norm(chain, t.from_address) == sender]))
    steps.append((f"amount{date_part}", fits))
    base = {"asset_note": asset_note, "compared": len(pool)}
    for method, found in steps:
        found = sorted(found, key=lambda t: (t.block_number, t.tx_hash, t.event_index))
        if len(found) == 1:
            t = found[0]
            return {**base, "status": "matched", "method": method, "transfer": _row(t, chain),
                    "next_outgoing": next_outgoing(chain, address, transfers, t), "next_outgoing_note": NOT_PROOF}
        if len(found) > 1:
            return {**base, "status": "candidates", "method": method, "candidates": [_row(t, chain) for t in found]}
    ruled_out = not truncated or (inp.sent_on is not None and oldest_read is not None
                                  and oldest_read.astimezone(dt.timezone.utc).date() < inp.sent_on - DATE_SLACK)
    return {**base, "status": "not_found", "method": steps[-1][0], "can_rule_out": ruled_out,
            "reason": "not_in_transfers_read" if ruled_out else "history_truncated"}


def match_fetched(chain: str, address: str, fetch: TransactionFetch, transfers) -> dict:
    """With a hash: the transaction as read directly, at any age."""
    if not fetch.found:
        return {"status": "not_found", "method": "tx_hash", "can_rule_out": False, "reason": "tx_not_found",
                "note": fetch.note}
    me = _norm(chain, address)
    paying = [t for t in fetch.records if _norm(chain, t.to_address) == me]
    if chain == "btc":
        paying = [t for t in paying if t.kind == "utxo_out"]
    if paying:
        t = max(paying, key=lambda r: (r.amount_raw, -r.event_index))
        return {"status": "matched", "method": "tx_hash", "transfer": _row(t, chain),
                "also_in_transaction": [_row(x, chain) for x in paying if x is not t],
                "next_outgoing": next_outgoing(chain, address, transfers, t), "next_outgoing_note": NOT_PROOF,
                "note": fetch.note}
    if chain == "btc":
        spenders = {_norm(chain, r.from_address) for r in fetch.records if r.kind == "utxo_in"}
        paid = sorted({r.to_address for r in fetch.records if r.kind == "utxo_out"
                       and _norm(chain, r.to_address) not in spenders})
    else:
        paid = sorted({r.to_address for r in fetch.records if r.to_address})
    return {"status": "pays_another_address", "method": "tx_hash", "paid_addresses": paid,
            "transfers": [_row(r, chain) for r in fetch.records],
            "block": fetch.block_number, "succeeded": fetch.succeeded, "note": fetch.note}


def combine(per_chain: dict[str, dict]) -> dict:
    """One result for a lookup that covers several chains (a 0x address)."""
    if len(per_chain) == 1:
        chain, r = next(iter(per_chain.items()))
        return {**r, "chains": [chain]}
    matched = {c: r for c, r in per_chain.items() if r["status"] == "matched"}
    cands = {c: r for c, r in per_chain.items() if r["status"] == "candidates"}
    if len(matched) == 1 and not cands:
        chain, r = next(iter(matched.items()))
        return {**r, "chains": sorted(per_chain), "matched_chain": chain}
    if len(matched) + len(cands) > 1 or cands:
        rows = [r["transfer"] for r in matched.values()] + [x for r in cands.values() for x in r["candidates"]]
        return {"status": "candidates", "method": "across_chains", "candidates": rows, "chains": sorted(per_chain)}
    paid = {c: r for c, r in per_chain.items() if r["status"] == "pays_another_address"}
    if paid:
        chain, r = next(iter(sorted(paid.items())))
        return {**r, "chains": sorted(per_chain), "matched_chain": chain}
    if all(r["status"] == "not_attempted" for r in per_chain.values()):
        return {"status": "not_attempted", "reason": next(iter(per_chain.values()))["reason"],
                "chains": sorted(per_chain)}
    return {"status": "not_found", "chains": sorted(per_chain),
            "can_rule_out": all(r.get("can_rule_out") for r in per_chain.values()),
            "per_chain": {c: {"reason": r.get("reason"), "can_rule_out": r.get("can_rule_out")}
                          for c, r in sorted(per_chain.items())}}
