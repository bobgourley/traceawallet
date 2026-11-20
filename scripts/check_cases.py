"""Check the ground-truth cases in tests/cases/ against live chain data.

    railway ssh --service traceawallet-worker -- python scripts/check_cases.py [case-name ...]

Each case's expected.json lists the facts its complaint states, each with the
paragraph and the complaint's own words. A fact with a "check" is tested here
through our adapters (Blockscout for EVM chains, TronGrid for Tron, mempool.space
for Bitcoin) and reported as CONFIRMED, DIFFERS or NOT FOUND, with what the chain
shows. A fact without one says why it cannot be checked yet. Blockscout calls come
out of the scripts' 30% of the daily allowance (taw/budget.py), which is why this
runs on the worker; Tron and Bitcoin cases need no database.

Amounts: "approximately" allows 1%; any other amount must match to the precision
it is printed with. Dates: complaints give dates "on or about", in US local time,
so a stated date matches block times from the day before to the day after (UTC).
The complaint names a token ("USDT"), not its contract; the checker uses the
issuers' contracts on that chain (TOKENS below), which is our reading, not the
complaint's.

Nothing here judges any address. Tests check on-chain facts only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from taw.adapters.base import LocalRawStore  # noqa: E402
from taw.adapters.chains import evm_etherscan as es  # noqa: E402
from taw.adapters.chains.btc_esplora import BtcEsploraAdapter  # noqa: E402
from taw.adapters.chains.evm_blockscout import BlockscoutAdapter  # noqa: E402
from taw.adapters.chains.tron_trongrid import TronGridAdapter  # noqa: E402
from taw.budget import BudgetExhausted, script_budget_or_exit  # noqa: E402

CASES = pathlib.Path(__file__).resolve().parent.parent / "tests" / "cases"
TOKENS = {"eth": {"USDT": ("0xdac17f958d2ee523a2206206994597c13d831ec7", 6),
                  "USDC": ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6)},
          "tron": {"USDT": ("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", 6)}}


def same(a: str | None, b: str | None) -> bool:
    """Addresses compared without case: EVM rows are lower case, Tron's are base58."""
    return (a or "").lower() == (b or "").lower()


def tolerance(amount: str, approx: bool) -> Decimal:
    value = Decimal(amount)
    if approx:
        return abs(value) / 100
    places = -value.as_tuple().exponent if value.as_tuple().exponent < 0 else 0
    return Decimal(1).scaleb(-places) / 2


def close(actual: Decimal, amount: str, approx: bool) -> bool:
    return abs(actual - Decimal(amount)) <= tolerance(amount, approx)


def day_window(date: str) -> tuple[dt.datetime, dt.datetime]:
    d = dt.date.fromisoformat(date)
    start = dt.datetime.combine(d - dt.timedelta(days=1), dt.time(), dt.timezone.utc)
    return start, start + dt.timedelta(days=3)


class Chains:
    """One adapter per (chain, address), so each history is read once."""

    def __init__(self, raw_root: pathlib.Path):
        self.raw_root, self.batches, self.adapters = raw_root, {}, {}
        self._metered = None

    def metered(self) -> dict:
        """Blockscout's budget and range store, opened on first use, so a run that only
        checks Bitcoin or Tron needs no database."""
        if self._metered is None:
            budget, ranges = script_budget_or_exit()
            self._metered = {"budget": budget, "range_store": ranges}
        return self._metered

    def evm(self, chain: str, address: str):
        key = (chain, address.lower())
        if key not in self.batches:
            adapter = BlockscoutAdapter(chain, LocalRawStore(self.raw_root / f"{chain}-{address.lower()}"),
                                        os.environ["BLOCKSCOUT_API_KEY"], **self.metered())
            self.adapters[key] = adapter
            self.batches[key] = adapter.fetch_transfers(address)
        return self.adapters[key], self.batches[key]

    def tron(self, address: str):
        key = ("tron", address)
        if key not in self.batches:
            adapter = TronGridAdapter(LocalRawStore(self.raw_root / f"tron-{address}"),
                                      os.environ.get("TRONGRID_API_KEY"))
            self.adapters[key] = adapter
            self.batches[key] = adapter.fetch_transfers(address)
        return self.adapters[key], self.batches[key]

    def history(self, chain: str, address: str):
        return self.tron(address) if chain == "tron" else self.evm(chain, address)

    def btc(self, address: str):
        key = ("btc", address)
        if key not in self.batches:
            adapter = BtcEsploraAdapter(LocalRawStore(self.raw_root / f"btc-{address}"))
            snap = adapter.fetch_snapshot(address)
            self.adapters[key] = (adapter, snap)
            self.batches[key] = adapter.fetch_transfers(address)
        return self.adapters[key], self.batches[key]

    def block_at_end_of(self, chain: str, date: str, adapter) -> int:
        """The last block of that UTC day, by binary search on block times."""
        end = dt.datetime.combine(dt.date.fromisoformat(date) + dt.timedelta(days=1), dt.time(), dt.timezone.utc)
        target = int(end.timestamp())
        lo, hi = 0, adapter.head().number
        while hi - lo > 1:
            mid = (lo + hi) // 2
            block, _ = adapter._block(mid)
            if es._int(block["timestamp"]) < target:
                lo = mid
            else:
                hi = mid
        return lo


def token_rows(batch, contract):
    return [t for t in batch.transfers if t.kind == "token" and same(t.token_contract, contract)]


def balance_through(batch, contract, address, until=None):
    """The token balance from the address's transfer history, up to (not including) `until`."""
    total = Decimal(0)
    for t in token_rows(batch, contract):
        if until is not None and t.block_time >= until:
            continue
        total += t.amount() if same(t.to_address, address) else 0
        total -= t.amount() if same(t.from_address, address) else 0
    return total


def activation(batch, address):
    """The first incoming TRX transfer. On Tron that is what creates (activates) an
    account; a TRC-20 transfer can arrive earlier without creating it."""
    rows = [t for t in batch.transfers if t.kind == "native" and same(t.to_address, address)]
    return min(rows, key=lambda t: (t.block_number, t.event_index)) if rows else None


def check_fact(fact: dict, chains: Chains, found: dict) -> tuple[str, str]:
    c = fact["check"]
    kind = c["kind"]
    if kind == "token_balance_at" and c["chain"] == "tron":
        # TronGrid has no balance at a past block: rebuild it from the complete transfer history.
        contract, _ = TOKENS["tron"][c["token"]]
        adapter, batch = chains.tron(c["address"])
        if batch.truncated:
            return "NOT CHECKED", "history truncated"
        end = dt.datetime.combine(dt.date.fromisoformat(c["date"]) + dt.timedelta(days=1), dt.time(), dt.timezone.utc)
        actual = balance_through(batch, contract, c["address"], end)
        ok = close(actual, c["amount"], c.get("approx", False))
        return ("CONFIRMED" if ok else "DIFFERS"), (
            f"balance at the end of {c['date']} UTC from {len(token_rows(batch, contract))} {c['token']} transfers: "
            f"{actual}; from all transfers to date: {balance_through(batch, contract, c['address'])}")
    if kind == "token_balance_at":
        contract, decimals = TOKENS[c["chain"]][c["token"]]
        adapter, _ = chains.evm(c["chain"], c["address"])
        block = chains.block_at_end_of(c["chain"], c["date"], adapter)
        out, _ = adapter._eth_call(contract, es.SEL_BALANCE_OF + es._pad(c["address"].lower())[2:], hex(block))
        actual = Decimal(int(out or "0", 16)).scaleb(-decimals)
        ok = close(actual, c["amount"], c.get("approx", False))
        return ("CONFIRMED" if ok else "DIFFERS"), f"balance at block {block:,} (end of {c['date']} UTC): {actual} {c['token']}"
    if kind in ("token_transfer", "token_transfers_total"):
        contract, decimals = TOKENS[c["chain"]][c["token"]]
        # Read the side named first; if its history is truncated and the transfer is not in
        # what was read, read the other side before giving up.
        sides = [c.get(c.get("read", "from"))] + [c.get(r) for r in ("from", "to") if r != c.get("read", "from")]
        truncated, last_rows = [], []
        for side in [s for s in sides if s]:
            _, batch = chains.history(c["chain"], side)
            rows = [t for t in token_rows(batch, contract)
                    if (not c.get("from") or same(t.from_address, c["from"]))
                    and (not c.get("to") or same(t.to_address, c["to"]))
                    # Only the characters the complaint prints; the address is never completed.
                    and (not c.get("from_prefix") or t.from_address.startswith(c["from_prefix"]))]
            if c.get("date"):
                lo, hi = day_window(c["date"])
                rows = [t for t in rows if lo <= t.block_time < hi]
            if c.get("after"):
                prev = found.get(c["after"])
                if prev is None:
                    return "NOT CHECKED", f"depends on {c['after']}, which was not found"
                rows = [t for t in rows if prev <= t.block_time <= prev + dt.timedelta(seconds=c["within_seconds"])]
            last_rows = rows
            if kind == "token_transfers_total":
                rows.sort(key=lambda t: t.block_time)
                for i in range(len(rows) - c["count"] + 1):
                    group = rows[i:i + c["count"]]
                    total = sum((t.amount() for t in group), Decimal(0))
                    if close(total, c["amount"], c.get("approx", False)):
                        found[fact["id"]] = group[-1].block_time
                        return "CONFIRMED", "; ".join(f"{t.amount()} at {t.block_time:%Y-%m-%d %H:%M:%S} UTC tx "
                                                      f"{t.tx_hash}" for t in group)
            else:
                hits = [t for t in rows if close(t.amount(), c["amount"], c.get("approx", False))]
                if hits:
                    t = hits[0]
                    found[fact["id"]] = t.block_time
                    sender = f", from an address beginning {c['from_prefix']}" if c.get("from_prefix") else ""
                    return "CONFIRMED", (f"{t.amount()} {c['token']} at {t.block_time:%Y-%m-%d %H:%M:%S} UTC, block "
                                         f"{t.block_number:,}, tx {t.tx_hash}, log {t.event_index}{sender}")
            if batch.truncated:
                truncated.append(side)
            else:
                break                    # a complete history without it: no need to read the other side
        if truncated and len(truncated) == len([s for s in sides if s]):
            return "NOT CHECKED", (f"history truncated at {len(batch.transfers):,} transfers for "
                                   f"{' and '.join(truncated)}; the transfer may be older than what was read")
        near = ", ".join(f"{t.amount()} at {t.block_time:%Y-%m-%d %H:%M}" for t in last_rows[:5]) or "none"
        verdict = "DIFFERS" if last_rows and c.get("date") and not c.get("after") else "NOT FOUND"
        return verdict, f"no matching transfer; transfers between those addresses in the window: {near}"
    if kind == "token_balance_before":
        contract, _ = TOKENS[c["chain"]][c["token"]]
        when = found.get(c["before"])
        if when is None:
            return "NOT CHECKED", f"depends on {c['before']}, which was not found"
        _, batch = chains.history(c["chain"], c["address"])
        if batch.truncated:
            return "NOT CHECKED", "history truncated"
        running = balance_through(batch, contract, c["address"], when)
        ok = close(Decimal(running), c["amount"], c.get("approx", False))
        return ("CONFIRMED" if ok else "DIFFERS"), f"balance just before {when:%Y-%m-%d %H:%M:%S} UTC: {running}"
    if kind == "token_balance_reached":
        contract, _ = TOKENS[c["chain"]][c["token"]]
        _, batch = chains.history(c["chain"], c["address"])
        if batch.truncated:
            return "NOT CHECKED", "history truncated"
        running, reached = Decimal(0), []
        for t in sorted(token_rows(batch, contract), key=lambda t: (t.block_number, t.event_index)):
            running += t.amount() if same(t.to_address, c["address"]) else 0
            running -= t.amount() if same(t.from_address, c["address"]) else 0
            if close(running, c["amount"], c.get("approx", False)):
                reached.append((t.block_time, running))
        if reached:
            return "CONFIRMED", f"transfers bring the balance to {reached[-1][1]} at {reached[-1][0]:%Y-%m-%d %H:%M} UTC"
        return "DIFFERS", f"the transfer history never reaches {c['amount']}; it ends at {running}"
    if kind == "balance_now":
        if c["chain"] == "btc":
            (adapter, snap), _ = chains.btc(c["address"])
            actual = snap.native_balance()
            return ("CONFIRMED" if close(actual, c["amount"], False) else "DIFFERS"), \
                f"balance at height {snap.as_of_block:,}: {actual} BTC"
        contract, decimals = TOKENS[c["chain"]][c["token"]]
        adapter, _ = chains.evm(c["chain"], c["address"])
        out, _ = adapter._eth_call(contract, es.SEL_BALANCE_OF + es._pad(c["address"].lower())[2:],
                                   hex(adapter.head().number))
        actual = Decimal(int(out or "0", 16)).scaleb(-decimals)
        return ("CONFIRMED" if close(actual, c["amount"], False) else "DIFFERS"), \
            f"balance at block {adapter.head().number:,}: {actual} {c['token']}"
    if kind == "btc_balance_in_month":
        (adapter, _), batch = chains.btc(c["address"])
        if batch.truncated:
            return "NOT CHECKED", "history truncated"
        start = dt.datetime.fromisoformat(c["month"] + "-01T00:00:00+00:00")
        end = (start + dt.timedelta(days=32)).replace(day=1)
        running, during, at_start = 0, [], None
        for r in sorted(batch.transfers, key=lambda r: (r.block_number, r.tx_hash, r.kind == "utxo_out")):
            if r.block_time >= start and at_start is None:
                at_start = running
            if r.block_time >= end:
                break
            if r.kind == "utxo_out" and r.to_address == c["address"]:
                running += r.amount_raw
            if r.kind == "utxo_in" and r.from_address == c["address"]:
                running -= r.amount_raw
            if r.block_time >= start:
                during.append(running)
        at_end = running
        held = {Decimal(v).scaleb(-8) for v in during + [at_start if at_start is not None else at_end, at_end]}
        target = Decimal(c["amount_btc"])
        ok = any(abs(v - target) <= tolerance(c["amount_btc"], c.get("approx", False)) for v in held)
        return ("CONFIRMED" if ok else "DIFFERS"), (
            f"balance at the start of {c['month']}: {Decimal(at_start if at_start is not None else at_end).scaleb(-8)} "
            f"BTC, at the end: {Decimal(at_end).scaleb(-8)} BTC; balances held during the month: "
            f"{sorted(str(v) for v in held)}")
    if kind == "activated_on":
        lo, hi = day_window(c["date"])
        seen, off = [], []
        for a in c["addresses"]:
            _, batch = chains.history(c["chain"], a)
            if batch.truncated:
                return "NOT CHECKED", f"history truncated for {a}"
            first = activation(batch, a)
            if first is None:
                off.append(f"{a}: no incoming TRX transfer")
                continue
            seen.append(first.block_time)
            if not lo <= first.block_time < hi:
                off.append(f"{a}: first incoming TRX transfer {first.block_time:%Y-%m-%d %H:%M} UTC")
        if off:
            return "DIFFERS", "; ".join(off)
        return "CONFIRMED", (f"first incoming TRX transfer (activation) of {len(seen)} address(es) between "
                             f"{min(seen):%Y-%m-%d %H:%M} and {max(seen):%Y-%m-%d %H:%M} UTC")
    if kind == "activated_by_one_address":
        senders = {}
        for a in c["addresses"]:
            _, batch = chains.history(c["chain"], a)
            if batch.truncated:
                return "NOT CHECKED", f"history truncated for {a}"
            first = activation(batch, a)
            senders[a] = first.from_address if first else None
        distinct = set(senders.values())
        if len(distinct) == 1 and None not in distinct:
            return "CONFIRMED", f"all {len(senders)} activating TRX transfers came from {distinct.pop()}"
        return "DIFFERS", "activating TRX transfers came from " + ", ".join(f"{a}: {s}" for a, s in senders.items())
    if kind in ("token_received_run", "received_share"):
        contract, _ = TOKENS[c["chain"]][c["token"]]
        _, batch = chains.history(c["chain"], c["address"])
        if batch.truncated:
            return "NOT CHECKED", "history truncated"
        incoming = sorted((t for t in token_rows(batch, contract) if same(t.to_address, c["address"])),
                          key=lambda t: (t.block_number, t.event_index))
        if kind == "received_share":
            received = sum((t.amount() for t in incoming), Decimal(0))
            share = Decimal(c["amount"]) / received * 100
            ok = close(share, c["percent"], c.get("approx", False))
            return ("CONFIRMED" if ok else "DIFFERS"), (
                f"{c['amount']} is {share:.4f}% of the {received} {c['token']} received in {len(incoming)} transfers")
        if c.get("date"):
            lo, hi = day_window(c["date"])
            incoming = [t for t in incoming if lo <= t.block_time < hi]
        # The complaint gives a total, not the transfers: look for consecutive receipts that add up to it.
        for i in range(len(incoming)):
            total = Decimal(0)
            for k in range(i, len(incoming)):
                total += incoming[k].amount()
                if close(total, c["amount"], c.get("approx", False)):
                    run = incoming[i:k + 1]
                    return "CONFIRMED", (f"{len(run)} consecutive receipts total {total} {c['token']}, "
                                         f"{run[0].block_time:%Y-%m-%d %H:%M} to {run[-1].block_time:%Y-%m-%d %H:%M} UTC, "
                                         f"first tx {run[0].tx_hash}")
        return "NOT FOUND", (f"no run of consecutive receipts among {len(incoming)} adds up to {c['amount']}; the "
                             "complaint does not identify the transfers, so a set that is not consecutive is not "
                             "ruled out")
    raise ValueError(f"unknown check kind {kind}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cases", nargs="*")
    ap.add_argument("--raw-dir", default=".raw/cases")
    args = ap.parse_args()
    names = args.cases or sorted(p.name for p in CASES.iterdir() if (p / "expected.json").exists())
    chains = Chains(pathlib.Path(args.raw_dir))
    for name in names:
        case = json.loads((CASES / name / "expected.json").read_text())
        print(f"\n=== {name}: {case['court']}, {case['docket']}, filed {case['filed']}")
        found: dict = {}
        for fact in case["facts"]:
            if not fact.get("check"):
                print(f"  {fact['id']:<4} ¶{fact['paragraph']:<6} CANNOT CHECK YET  {fact['not_checkable_reason']}")
                continue
            try:
                verdict, detail = check_fact(fact, chains, found)
            except BudgetExhausted as exc:
                sys.exit(f"Stopped at {name} {fact['id']}: {exc}")
            except Exception as exc:                       # report and carry on with the other facts
                verdict, detail = "ERROR", f"{type(exc).__name__}: {exc}"
            recorded = [d["id"] for d in case.get("discrepancies", []) if d["fact"] == fact["id"]]
            if verdict == "DIFFERS" and recorded:
                detail += f" (recorded in expected.json as {', '.join(recorded)})"
            print(f"  {fact['id']:<4} ¶{fact['paragraph']:<6} {verdict:<17} {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
