"""Try an EVM adapter on one address. No database needed.

    railway ssh --service traceawallet-worker -- python scripts/try_evm.py 0x... [--chain eth]
        [--provider blockscout|etherscan]

Blockscout is what the worker uses; Etherscan is for comparison only. Raw
responses are saved under .raw/ (gitignored). BLOCKSCOUT_API_KEY or
ETHERSCAN_API_KEY comes from the environment; it is never printed.
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
from taw.adapters.chains.evm_blockscout import BlockscoutAdapter  # noqa: E402
from taw.adapters.chains.evm_etherscan import CHAINS, EtherscanAdapter  # noqa: E402
from taw.budget import BudgetExhausted, script_budget_or_exit  # noqa: E402


def fmt(amount: Decimal | None) -> str:
    if amount is None:
        return "?"
    text = f"{amount:,.6f}".rstrip("0").rstrip(".")
    return text or "0"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("address")
    ap.add_argument("--chain", default="eth", choices=sorted(CHAINS))
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--raw-dir", default=".raw")
    ap.add_argument("--provider", default="blockscout", choices=("blockscout", "etherscan"))
    ap.add_argument("--at-block", type=int, default=None, help="pin every read to this block")
    args = ap.parse_args()

    cls = BlockscoutAdapter if args.provider == "blockscout" else EtherscanAdapter
    key = os.environ.get(cls.key_env)
    info = CHAINS[args.chain]
    stable = dict(info.stablecoins)
    metered = {}
    if cls is BlockscoutAdapter:
        budget, ranges = script_budget_or_exit()
        metered = {"budget": budget, "range_store": ranges}
    adapter = cls(args.chain, LocalRawStore(args.raw_dir), key, at_block=args.at_block, **metered)

    def token_name(contract: str | None, symbol: str | None) -> str:
        if contract is None:
            return f"{info.native_symbol} (native)"
        if contract in stable:
            return f"{stable[contract]} {contract}"
        return f"{symbol or '?'} {contract}"

    try:
        snap = adapter.fetch_snapshot(args.address)
        batch = adapter.fetch_transfers(snap.address, limit=args.limit)
    except BudgetExhausted as exc:
        sys.exit(f"Stopped: {exc}")
    addr = snap.address

    print(f"Provider     {adapter.provider_name}")
    print(f"Chain        {info.name} (chainid {info.chain_id})")
    print(f"Address      {addr}")
    print(f"API key      {'present' if key else 'MISSING'}")
    print(f"As of block  {snap.as_of_block:,}  ({snap.as_of_time:%Y-%m-%d %H:%M:%S} UTC, "
          f"{info.confirmations} blocks behind the tip)")
    print(f"Contract     {'yes' if snap.is_contract else 'no'}")
    print()
    print("Balances")
    print(f"  {token_name(None, None):<62} {fmt(snap.native_balance()):>22}"
          f"  (exactly {snap.native_balance()} {info.native_symbol})")
    for b in sorted(snap.token_balances, key=lambda b: (b.contract not in stable, b.contract)):
        print(f"  {token_name(b.contract, b.symbol):<62} {fmt(b.amount()):>22}")
    if snap.truncated:
        print("  (only the listed stablecoins and the 20 most-used tokens were checked)")
    print()

    transfers = batch.transfers
    note = f" (TRUNCATED: newest {batch.limit:,} plus the first transfers)" if batch.truncated else ""
    by_kind = defaultdict(int)
    for t in transfers:
        by_kind[t.kind] += 1
    print(f"Transfers    {len(transfers):,}{note}: "
          + ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items())))
    if adapter.unmatched_token_rows or adapter.skipped_no_decimals:
        print(f"  token list rows with no Transfer log: {adapter.unmatched_token_rows}; "
              f"skipped for unreadable decimals: {adapter.skipped_no_decimals}")
    if not transfers:
        print(f"\n{adapter.request_count} requests; raw responses in {args.raw_dir}/")
        return 0

    stats: dict[tuple, dict] = defaultdict(lambda: {"in_n": 0, "in_sum": Decimal(0), "out_n": 0, "out_sum": Decimal(0)})
    for t in transfers:
        k = (t.kind, t.token_contract, t.token_symbol)
        if t.to_address == addr:
            stats[k]["in_n"] += 1
            stats[k]["in_sum"] += t.amount()
        if t.from_address == addr:
            stats[k]["out_n"] += 1
            stats[k]["out_sum"] += t.amount()

    print()
    print(f"  {'Kind':<9}{'Token':<62} {'In':>4} {'Total in':>16} {'Out':>4} {'Total out':>16}")
    for (kind, contract, symbol), s in sorted(stats.items(), key=lambda kv: (kv[0][1] is not None,
                                                                            kv[0][1] not in stable, kv[0][1] or "", kv[0][0])):
        print(f"  {kind:<9}{token_name(contract, symbol):<62} {s['in_n']:>4} {fmt(s['in_sum']):>16} "
              f"{s['out_n']:>4} {fmt(s['out_sum']):>16}")

    oldest = sorted(transfers, key=lambda t: (t.block_number, t.tx_hash))
    first, last = oldest[0], transfers[0]
    funder = next((t for t in oldest if t.to_address == addr), None)
    print()
    print(f"First activity  {first.block_time:%Y-%m-%d %H:%M:%S} UTC  block {first.block_number:,}  tx {first.tx_hash}")
    print(f"Last activity   {last.block_time:%Y-%m-%d %H:%M:%S} UTC  block {last.block_number:,}  tx {last.tx_hash}")
    native_funder = next((t for t in oldest if t.to_address == addr and t.token_contract is None), None)
    for label, t in (("First funder   ", funder), ("First ETH in   ".replace("ETH", info.native_symbol)[:15], native_funder)):
        if t:
            print(f"{label} {t.from_address}  sent {fmt(t.amount())} "
                  f"{token_name(t.token_contract, t.token_symbol)}  ({t.kind}, block {t.block_number:,}, tx {t.tx_hash})")

    dest: dict[tuple, list] = defaultdict(lambda: [0, Decimal(0)])
    for t in transfers:
        if t.token_contract in stable and t.from_address == addr:
            dest[(t.to_address, t.token_contract)][0] += 1
            dest[(t.to_address, t.token_contract)][1] += t.amount()
    print()
    print("Top 10 outbound stablecoin destinations")
    if not dest:
        print("  none")
    for (to, contract), (n, total) in sorted(dest.items(), key=lambda kv: kv[1][1], reverse=True)[:10]:
        print(f"  {to}  {n:>4} transfers  {fmt(total):>16} {stable[contract].split()[0]}")

    print(f"\n{adapter.request_count} requests; raw responses in {args.raw_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
