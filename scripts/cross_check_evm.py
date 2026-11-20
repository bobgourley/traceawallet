"""Cross-check Blockscout against Etherscan for one address, at one block. No database.

    railway ssh --service traceawallet-worker -- python scripts/cross_check_evm.py 0x... [--chain eth]

Both adapters are pinned to the same block (Etherscan's head unless --at-block is
given) and must report the same transfers, balances and first activity
(taw/adapters/chains/evm_crosscheck.py says exactly what is compared). Prints every
mismatch; exits 1 if there is any. Raw responses go under .raw/cross-<provider>/.
Needs ETHERSCAN_API_KEY and BLOCKSCOUT_API_KEY; neither is printed.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from taw.adapters.base import LocalRawStore  # noqa: E402
from taw.adapters.chains.evm_blockscout import BlockscoutAdapter  # noqa: E402
from taw.adapters.chains.evm_crosscheck import compare  # noqa: E402
from taw.adapters.chains.evm_etherscan import EtherscanAdapter  # noqa: E402
from taw.adapters.registry import stablecoins_for  # noqa: E402
from taw.budget import BudgetExhausted, script_budget_or_exit  # noqa: E402
from taw.reconcile import run_checks  # noqa: E402


def run(adapter, address, limit):
    snap = adapter.fetch_snapshot(address)
    return snap, adapter.fetch_transfers(snap.address, limit=limit)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("address")
    ap.add_argument("--chain", default="eth")
    ap.add_argument("--at-block", type=int, default=None)
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--raw-dir", default=".raw")
    args = ap.parse_args()

    for name in ("ETHERSCAN_API_KEY", "BLOCKSCOUT_API_KEY"):
        if not os.environ.get(name):
            print(f"{name} is not set", file=sys.stderr)
            return 2
    budget, ranges = script_budget_or_exit()
    root = pathlib.Path(args.raw_dir)
    ether = EtherscanAdapter(args.chain, LocalRawStore(root / "cross-etherscan"), os.environ["ETHERSCAN_API_KEY"],
                             at_block=args.at_block)
    block = ether.head().number
    blocks = BlockscoutAdapter(args.chain, LocalRawStore(root / "cross-blockscout"),
                               os.environ["BLOCKSCOUT_API_KEY"], at_block=block, budget=budget, range_store=ranges)

    snap_e, batch_e = run(ether, args.address, args.limit)
    try:
        snap_b, batch_b = run(blocks, args.address, args.limit)
    except BudgetExhausted as exc:
        sys.exit(f"Stopped: {exc}")
    print(f"{args.chain} {snap_e.address} at block {block:,}")
    for name, adapter, snap, batch in (("Etherscan", ether, snap_e, batch_e), ("Blockscout", blocks, snap_b, batch_b)):
        kinds = {k: sum(1 for t in batch.transfers if t.kind == k) for k in ("native", "internal", "token")}
        print(f"  {name:<10} {adapter.request_count:>3} requests  {len(batch.transfers):>5} transfers {kinds}"
              f"  native {snap.native_balance()}  tokens {len(snap.token_balances)}"
              f"  unmatched token rows {adapter.unmatched_token_rows}")

    for name, snap, batch in (("Etherscan", snap_e, batch_e), ("Blockscout", snap_b, batch_b)):
        result = run_checks(snap, batch, stablecoins_for(args.chain))
        r = batch.internal_range
        print(f"\n{name} checks (internal transfers read for blocks {r[0]:,} to {r[1]:,}; "
              f"history_incomplete={result.history_incomplete})" if r else f"\n{name} checks")
        for c in result.checks:
            print(f"  {c.name:<28} {c.status:<8} {c.reason or ''}")
            if c.details:
                print(f"  {'':<28} {dict(c.details)}")
        for t in result.tokens:
            print(f"  token {t.contract} {t.symbol or ''}: {t.status} balance {t.balance_raw} in-out {t.net_flow_raw}"
                  + (f" ({t.reason})" if t.reason else ""))
    print()
    mismatches = compare(snap_e, batch_e, snap_b, batch_b)
    if not mismatches:
        print("No mismatches: same transfers, balances and first activity.")
        return 0
    print(f"{len(mismatches)} mismatch(es) (Etherscan first, Blockscout second):")
    for m in mismatches:
        print(f"  {m}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
