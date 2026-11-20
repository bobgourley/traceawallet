"""Backfill the global index of stablecoin blacklist events (taw/blacklist.py), once
per chain. After that the daily blacklist_refresh job keeps it up to date.

    railway ssh --service traceawallet-worker -- python scripts/backfill_blacklist.py tron
    railway ssh --service traceawallet-worker -- python scripts/backfill_blacklist.py eth [--record DIR]

eth: Blockscout eth_getLogs (metered, from the scripts' 30% share), then the same
range from Etherscan's getLogs, compared log by log (transaction hash and log
index) for every event. Any difference is printed and the script exits 1; the
contract's events are not written: the index is never filled in from Etherscan
and never written short. --record DIR also writes both providers' replies,
cut to the fields the parser reads, for tests/test_blacklist.py.

--dry-run scans and prints counts without writing the index. Responses go to a
local folder instead of the bucket, so a dry run can be done from a laptop with
`railway run` before the migration is deployed.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import pathlib
import sys
import time
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from taw import blacklist, config, store  # noqa: E402
from taw.adapters.base import BucketRawStore, LocalRawStore  # noqa: E402
from taw.adapters.labels import blacklist as bl  # noqa: E402

ACTOR = "script:backfill_blacklist"
KEEP = ("address", "topics", "data", "blockNumber", "logIndex", "transactionHash", "timeStamp", "blockTimestamp",
        "removed")


def recording(get, log: dict):
    """Wrap a get_logs so every reply is kept, cut to the fields the parser reads."""
    def wrapped(contract, topic, lo, hi):
        logs, sha = get(contract, topic, lo, hi)
        log[f"{contract}|{topic}|{lo}|{hi}"] = [{k: g[k] for k in KEEP if k in g} for g in logs]
        return logs, sha
    return wrapped


def tally(events) -> Counter:
    return Counter(f"{e.token} {e.event_name}" for e in events)


def run_tron(args, conn, raw_store) -> int:
    from taw.adapters.chains.tron_trongrid import TronGridAdapter
    adapter = TronGridAdapter(raw_store, os.environ["TRONGRID_API_KEY"])
    if args.dry_run:
        head, head_time, _ = adapter.latest_block()
        for c in bl.for_chain("tron"):
            started = time.monotonic()
            result = bl.tron_scan(adapter, c, c.start_time, head_time)
            first = min((e.block_time for e in result.events), default=None)
            print(f"tron {c.token}: {dict(tally(result.events))}; {result.calls} call(s); earliest event "
                  f"{first:%Y-%m-%d} ; to block {head:,} ({head_time:%Y-%m-%d %H:%M} UTC); "
                  f"{time.monotonic() - started:.0f} s", flush=True)
        print(f"TronGrid requests: {adapter.request_count}")
        return 0
    for note in blacklist.update(conn, ACTOR, lambda chain: adapter, ("tron",), backfill=True):
        print(note, flush=True)
    print(f"TronGrid requests: {adapter.request_count}")
    return 0


def run_eth(args, conn, raw_store) -> int:
    from taw.adapters.chains.evm_blockscout import BlockscoutAdapter
    from taw.adapters.chains.evm_etherscan import EtherscanAdapter
    from taw.budget import script_budget_or_exit
    budget, ranges = script_budget_or_exit()
    scout = BlockscoutAdapter("eth", raw_store, os.environ["BLOCKSCOUT_API_KEY"], budget=budget, range_store=ranges)
    ether = EtherscanAdapter("eth", raw_store, os.environ["ETHERSCAN_API_KEY"])
    head, _ = scout._block_number()
    to_block = head - blacklist.EVM_CONFIRMATIONS
    replies = {"blockscout": {}, "etherscan": {}}
    get_scout = recording(bl.blockscout_logs(scout), replies["blockscout"])
    get_ether = recording(bl.etherscan_logs(ether), replies["etherscan"])
    differences = 0
    summary = {}
    for c in bl.for_chain("eth"):
        if not args.dry_run and blacklist.coverage(conn, c.chain, c.contract) is not None:
            print(f"eth {c.token}: already backfilled; the daily job updates it")
            continue
        agree = True
        ours = bl.evm_scan(get_scout, c, c.start_block, to_block)
        theirs = bl.evm_scan(get_ether, c, c.start_block, to_block)
        for ev in c.events:
            a = {e.key for e in ours.events if e.event_name == ev.name}
            b = {e.key for e in theirs.events if e.event_name == ev.name}
            summary[f"{c.token} {ev.name}"] = {"blockscout": len(a), "etherscan": len(b)}
            print(f"eth {c.token} {ev.name}: Blockscout {len(a)}, Etherscan {len(b)}"
                  f"{'' if a == b else f'  DIFFERENT: {len(b - a)} only in Etherscan, {len(a - b)} only in Blockscout'}",
                  flush=True)
            for k in sorted(b - a)[:10]:
                print(f"    only in Etherscan: {k[0]} log {k[1]}")
            for k in sorted(a - b)[:10]:
                print(f"    only in Blockscout: {k[0]} log {k[1]}")
            differences += len(a ^ b)
            agree = agree and a == b
        print(f"eth {c.token}: {ours.calls} Blockscout call(s), {theirs.calls} Etherscan call(s)", flush=True)
        if not agree:
            print(f"eth {c.token}: NOT written, because the providers disagree", flush=True)
        elif not args.dry_run:
            sid, n = blacklist.record_scan(conn, ACTOR, c, c.start_block, to_block, None, ours, "blockscout",
                                           detail={"etherscan_comparison": summary})
            print(f"eth {c.token}: scan {sid}, {n} event(s) written, blocks {c.start_block:,} to {to_block:,}")
    if args.record:
        out = pathlib.Path(args.record)
        out.mkdir(parents=True, exist_ok=True)
        with gzip.open(out / "eth_replay.json.gz", "wt") as f:
            json.dump({"to_block": to_block, "counts": summary, "replies": replies}, f, sort_keys=True)
        print(f"recorded replies to {out / 'eth_replay.json.gz'}")
    print("Blockscout and Etherscan agree on every event." if not differences
          else f"{differences} event(s) differ between Blockscout and Etherscan.")
    return 1 if differences else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chain", choices=("eth", "tron"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--record", help="eth: write both providers' replies here for the replay test")
    ap.add_argument("--raw-dir", default=".raw/blacklist", help="--dry-run: where responses are saved")
    args = ap.parse_args()
    conn = None
    if args.dry_run and args.chain == "tron":
        raw_store = LocalRawStore(args.raw_dir)
    else:
        if not config.DATABASE_URL:
            sys.exit("DATABASE_URL is not set: run this inside Railway with railway ssh (or use --dry-run).")
        import psycopg
        conn = psycopg.connect(config.DATABASE_URL, autocommit=True)
        raw_conn = psycopg.connect(config.DATABASE_URL, autocommit=True)
        raw_store = store.RecordingRawStore(raw_conn, ACTOR, s3_client=BucketRawStore._client_from_env())
    return run_tron(args, conn, raw_store) if args.chain == "tron" else run_eth(args, conn, raw_store)


if __name__ == "__main__":
    sys.exit(main())
