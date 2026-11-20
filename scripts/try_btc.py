"""Try the Bitcoin adapter on one address. No database, no key.

    .venv/bin/python scripts/try_btc.py bc1q... [--provider mempool|blockstream] [--at-height N]

Raw responses are saved under .raw/ (gitignored). Wording follows migration 0003:
outputs of transactions that spent from this address, never "sent to" (one
output is usually change).
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from collections import Counter
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from taw.adapters.base import LocalRawStore  # noqa: E402
from taw.adapters.chains.btc_esplora import BLOCKSTREAM, MEMPOOL, BtcEsploraAdapter  # noqa: E402
from taw.reconcile import run_checks  # noqa: E402


def btc(sat: int) -> str:
    return f"{Decimal(sat).scaleb(-8):,.8f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("address")
    ap.add_argument("--provider", default="mempool", choices=("mempool", "blockstream"))
    ap.add_argument("--at-height", type=int, default=None)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--raw-dir", default=".raw")
    args = ap.parse_args()

    adapter = BtcEsploraAdapter(LocalRawStore(args.raw_dir), at_height=args.at_height,
                                provider=MEMPOOL if args.provider == "mempool" else BLOCKSTREAM)
    snap = adapter.fetch_snapshot(args.address)
    batch = adapter.fetch_transfers(snap.address, limit=args.limit)
    addr, rows = snap.address, batch.transfers
    print(f"Provider     {adapter.provider_name}")
    print(f"Address      {addr}")
    print(f"As of height {snap.as_of_block:,}  ({snap.as_of_time:%Y-%m-%d %H:%M:%S} UTC)")
    print(f"Balance      {btc(snap.native_balance_raw)} BTC  (provider totals at that height)")

    own_in = [r for r in rows if r.kind == "utxo_out" and r.to_address == addr]
    own_out = [r for r in rows if r.kind == "utxo_in" and r.from_address == addr]
    txs = list(dict.fromkeys(r.tx_hash for r in rows))
    print(f"Transactions {len(txs)}{' (TRUNCATED)' if batch.truncated else ''}; rows {len(rows)} "
          f"({dict(Counter(r.kind for r in rows))})")
    print(f"Received     {len(own_in)} outputs paying this address, {btc(sum(r.amount_raw for r in own_in))} BTC")
    print(f"Spent        {len(own_out)} inputs spent from this address, {btc(sum(r.amount_raw for r in own_out))} BTC")

    oldest = sorted(rows, key=lambda r: (r.block_number, r.tx_hash))
    if oldest:
        first, last = oldest[0], max(rows, key=lambda r: r.block_number)
        print(f"First activity  {first.block_time:%Y-%m-%d %H:%M:%S} UTC  height {first.block_number:,}  tx {first.tx_hash}")
        print(f"Last activity   {last.block_time:%Y-%m-%d %H:%M:%S} UTC  height {last.block_number:,}  tx {last.tx_hash}")
        first_in = next((r for r in oldest if r.kind == "utxo_out" and r.to_address == addr), None)
        if first_in:
            funders = sorted({r.from_address for r in rows if r.tx_hash == first_in.tx_hash and r.kind == "utxo_in"})
            print(f"First funding   tx {first_in.tx_hash}: {btc(first_in.amount_raw)} BTC; inputs of that "
                  f"transaction came from {', '.join(funders) or 'a coinbase'}")

    spending = sorted({r.tx_hash for r in own_out})
    print(f"\nOutputs of transactions that spent from this address ({len(spending)} transactions):")
    for txh in spending[:10]:
        outs = [r for r in rows if r.tx_hash == txh and r.kind == "utxo_out"]
        print(f"  tx {txh[:16]}: " + "; ".join(f"{r.to_address} {btc(r.amount_raw)}"
                                                 + (" (this address)" if r.to_address == addr else "") for r in outs))

    result = run_checks(snap, batch, ())
    print(f"\nChecks (history_incomplete={result.history_incomplete})")
    for c in result.checks:
        print(f"  {c.name:<28} {c.status:<8} {c.reason or ''}")
        if c.details:
            print(f"  {'':<28} {dict(c.details)}")
    print(f"\n{adapter.request_count} requests; raw responses in {args.raw_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
