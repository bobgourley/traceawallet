"""Try the Tron adapter on one address. No database needed.

    railway run --service traceawallet-worker .venv/bin/python scripts/try_tron.py T...

Raw responses are saved under .raw/ (gitignored). TRONGRID_API_KEY comes from
the environment; it is never printed.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from collections import defaultdict
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from taw.adapters.base import LocalRawStore  # noqa: E402
from taw.adapters.chains.tron_trongrid import TronGridAdapter  # noqa: E402

USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"   # Tether's TRC-20 contract; identified by address, not symbol


def fmt(amount: Decimal | None) -> str:
    if amount is None:
        return "?"
    text = f"{amount:,.6f}".rstrip("0").rstrip(".")
    return text or "0"


def token_name(contract: str | None, symbol: str | None) -> str:
    if contract is None:
        return "TRX (native)"
    if contract == USDT:
        return f"USDT (Tether, {contract})"
    return f"{symbol or '?'} ({contract})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("address")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--raw-dir", default=".raw")
    args = ap.parse_args()

    key = os.environ.get("TRONGRID_API_KEY")
    store = LocalRawStore(args.raw_dir)
    adapter = TronGridAdapter(store, key)

    snap = adapter.fetch_snapshot(args.address)
    batch = adapter.fetch_transfers(snap.address, limit=args.limit)
    addr = snap.address

    print(f"Address      {addr}")
    print(f"API key      {'present' if key else 'MISSING (unauthenticated, strict limits)'}")
    print(f"As of block  {snap.as_of_block:,}  ({snap.as_of_time:%Y-%m-%d %H:%M:%S} UTC, confirmed)")
    print(f"Contract     {'yes' if snap.is_contract else 'no'}")
    print()
    print("Balances")
    print(f"  {'TRX (native)':<52} {fmt(snap.native_balance()):>22}")
    for b in sorted(snap.token_balances, key=lambda b: (b.contract != USDT, b.contract)):
        print(f"  {token_name(b.contract, b.symbol):<52} {fmt(b.amount()):>22}")
    if snap.truncated:
        print("  (more tokens held than were looked up; some lack decimals)")
    print()

    transfers = batch.transfers
    note = f" (TRUNCATED: newest {batch.limit:,} only; older history not fetched)" if batch.truncated else ""
    print(f"Transfers    {len(transfers):,}{note}")
    if not transfers:
        print(f"\n{adapter.request_count} requests; raw responses in {args.raw_dir}/")
        return 0

    stats: dict[tuple, dict] = defaultdict(lambda: {"in_n": 0, "in_sum": Decimal(0), "out_n": 0, "out_sum": Decimal(0)})
    for t in transfers:
        k = (t.token_contract, t.token_symbol)
        if t.to_address == addr:
            stats[k]["in_n"] += 1
            stats[k]["in_sum"] += t.amount()
        if t.from_address == addr:
            stats[k]["out_n"] += 1
            stats[k]["out_sum"] += t.amount()

    print()
    print(f"  {'Token':<52} {'In':>5} {'Total in':>18} {'Out':>5} {'Total out':>18}")
    for (contract, symbol), s in sorted(stats.items(), key=lambda kv: (kv[0][0] is not None, kv[0][0] != USDT, kv[0][0] or "")):
        print(f"  {token_name(contract, symbol):<52} {s['in_n']:>5} {fmt(s['in_sum']):>18} "
              f"{s['out_n']:>5} {fmt(s['out_sum']):>18}")

    first, last = transfers[-1], transfers[0]
    print()
    print(f"First activity  {first.block_time:%Y-%m-%d %H:%M:%S} UTC  block {first.block_number:,}  tx {first.tx_hash}"
          + ("  (earliest fetched, not earliest ever)" if batch.truncated else ""))
    print(f"Last activity   {last.block_time:%Y-%m-%d %H:%M:%S} UTC  block {last.block_number:,}  tx {last.tx_hash}")

    dest: dict[str, list] = defaultdict(lambda: [0, Decimal(0)])
    for t in transfers:
        if t.token_contract == USDT and t.from_address == addr:
            dest[t.to_address][0] += 1
            dest[t.to_address][1] += t.amount()
    print()
    print("Top 10 outbound USDT destinations")
    if not dest:
        print("  none")
    for to, (n, total) in sorted(dest.items(), key=lambda kv: kv[1][1], reverse=True)[:10]:
        print(f"  {to}  {n:>4} transfers  {fmt(total):>18} USDT")

    print(f"\n{adapter.request_count} requests; raw responses in {args.raw_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
