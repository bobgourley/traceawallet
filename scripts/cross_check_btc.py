"""Cross-check mempool.space against Blockstream for one Bitcoin address, at one height.

    .venv/bin/python scripts/cross_check_btc.py bc1q...

Both are pinned to mempool.space's tip height read at the start. Prints each
provider's checks and every difference in rows, balance and first activity
(taw/adapters/chains/evm_crosscheck.py compares rows by kind, tx, position,
addresses and amount). Exits 1 on any difference. Raw responses go under
.raw/btc-cross-<provider>/.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from taw.adapters.base import LocalRawStore  # noqa: E402
from taw.adapters.chains.btc_esplora import BLOCKSTREAM, MEMPOOL, BtcEsploraAdapter  # noqa: E402
from taw.adapters.chains.evm_crosscheck import compare  # noqa: E402
from taw.reconcile import run_checks  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("address")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--raw-dir", default=".raw")
    args = ap.parse_args()
    root = pathlib.Path(args.raw_dir)

    mem = BtcEsploraAdapter(LocalRawStore(root / "btc-cross-mempool"), provider=MEMPOOL)
    height = mem.head().height
    blo = BtcEsploraAdapter(LocalRawStore(root / "btc-cross-blockstream"), provider=BLOCKSTREAM, at_height=height)
    out = {}
    for name, adapter in (("mempool.space", mem), ("Blockstream", blo)):
        snap = adapter.fetch_snapshot(args.address)
        batch = adapter.fetch_transfers(snap.address, limit=args.limit)
        out[name] = (snap, batch)
        result = run_checks(snap, batch, ())
        txs = len({r.tx_hash for r in batch.transfers})
        print(f"{name:<14} height {snap.as_of_block:,}  balance {snap.native_balance_raw} sat  {txs} transactions  "
              f"{len(batch.transfers)} rows  {adapter.request_count} requests  "
              f"history_incomplete={result.history_incomplete}")
        for c in result.checks:
            print(f"   {c.name:<26} {c.status:<8} {c.reason or ''}  {dict(c.details or {})}")
    (sa, ba), (sb, bb) = out["mempool.space"], out["Blockstream"]
    mismatches = compare(sa, ba, sb, bb)
    if not mismatches:
        print("\nNo differences: same rows, balance and first activity.")
        return 0
    print(f"\n{len(mismatches)} difference(s) (mempool.space first, Blockstream second):")
    for m in mismatches:
        print(f"  {m}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
