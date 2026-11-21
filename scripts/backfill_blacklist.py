"""Backfill the global index of stablecoin blacklist events (taw/blacklist.py), once
per chain. After that the daily blacklist_refresh job keeps it up to date.

    railway ssh --service traceawallet-worker -- python scripts/backfill_blacklist.py tron
    railway ssh --service traceawallet-worker -- python scripts/backfill_blacklist.py eth [--record DIR]

eth: through Blockscout (metered as "backfill": it may use the allowance down to
the worker's reserve, taw/budget.py). USDT through its owner multisig, USDC window
by window (taw/blacklist_backfill.py). Every answered reply is saved before the
next request, so a stopped run carries on where it stopped; --until-done waits for
the daily reset and carries on by itself. The worker runs the same backfill as the
blacklist_backfill job, in slices, so this is only needed by hand. Then the same
range from Etherscan's
getLogs, compared event by event (transaction, log position, address, amount).
Any difference is printed and the script exits 1; the contract's events are not
written: the index is never filled in from Etherscan and never written short.
--record DIR also writes the saved Blockscout replies and Etherscan's, cut to the
fields the parser reads, for tests/test_blacklist_replay.py.

    railway ssh --service traceawallet-worker -- python scripts/backfill_blacklist.py eth --until-done

--dry-run scans and prints counts without writing the index. Responses go to a
local folder instead of the bucket, so a dry run can be done from a laptop with
`railway run` before the migration is deployed.
"""
from __future__ import annotations

import argparse
import datetime as dt
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
    """taw.blacklist_backfill.backfill_eth, with no time limit. The worker runs the same
    thing as the blacklist_backfill job, in slices; this is for running it by hand."""
    from taw import blacklist_backfill as bf
    from taw.adapters.chains.evm_blockscout import BlockscoutAdapter
    from taw.adapters.chains.evm_etherscan import EtherscanAdapter
    from taw.budget import BudgetExhausted, next_reset, script_budget_or_exit
    budget, ranges = script_budget_or_exit("backfill")
    scout = BlockscoutAdapter("eth", raw_store, os.environ["BLOCKSCOUT_API_KEY"], budget=budget, range_store=ranges)
    ether = EtherscanAdapter("eth", raw_store, os.environ["ETHERSCAN_API_KEY"])
    if args.record_only:
        return record_only(args, conn, ether)
    replies: dict = {}
    while True:
        try:
            out = bf.backfill_eth(conn, ACTOR, scout, ether, log=lambda m: print(m, flush=True), record=replies)
            break
        except BudgetExhausted as exc:
            print(f"eth: stopped for today, progress saved ({exc})", flush=True)
            if not args.until_done:
                return 0
            wake = next_reset() + dt.timedelta(minutes=10)
            print(f"eth: waiting until {wake:%Y-%m-%d %H:%M} UTC", flush=True)
            time.sleep(max(60.0, (wake - dt.datetime.now(dt.timezone.utc)).total_seconds()))
    print(out.message(), flush=True)
    if args.record:
        write_record(pathlib.Path(args.record), conn, out.summary, replies)
    return 1 if out.differ else 0


def write_record(out: pathlib.Path, conn, summary: dict, replies: dict) -> None:
    from taw import blacklist_backfill as bf
    to_block = conn.execute("SELECT max(to_block) FROM blacklist_scan WHERE chain = 'eth'").fetchone()[0]
    out.mkdir(parents=True, exist_ok=True)
    with gzip.open(out / "eth_replay.json.gz", "wt") as f:
        json.dump({"to_block": to_block, "counts": summary, "blockscout_parts": bf.saved_parts(conn),
                   "etherscan": replies}, f, sort_keys=True)
    print(f"recorded replies to {out / 'eth_replay.json.gz'}")


def record_only(args, conn, ether) -> int:
    """After the backfill: the saved Blockscout replies, and Etherscan's events read again
    for each written scan's range (Etherscan's free API; nothing is written)."""
    from taw import blacklist_backfill as bf
    replies: dict = {}
    summary = {}

    def get(contract, topic, lo, hi):
        logs, sha = bl.etherscan_logs(ether)(contract, topic, lo, hi)
        replies[f"{contract}|{topic}|{lo}|{hi}"] = [{k: g[k] for k in bf.LOG_FIELDS if k in g} for g in logs]
        return logs, sha
    for c in bl.for_chain("eth"):
        row = conn.execute("SELECT from_block, to_block FROM blacklist_scan WHERE chain = 'eth' AND contract = %s"
                           " ORDER BY id LIMIT 1", (c.contract,)).fetchone()
        if row is None:
            print(f"eth {c.token}: not backfilled yet")
            continue
        theirs = bl.evm_scan(get, c, row[0], row[1])
        for ev in c.events:
            summary[f"{c.token} {ev.name}"] = {"etherscan": sum(1 for e in theirs.events if e.event_name == ev.name)}
    write_record(pathlib.Path(args.record_only), conn, summary, replies)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chain", choices=("eth", "tron"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--record", help="eth: write both providers' replies here for the replay test")
    ap.add_argument("--record-only", help="eth, after the backfill: write the replay fixture here and nothing else")
    ap.add_argument("--until-done", action="store_true",
                    help="eth: when the day's allowance is used, wait for the reset and carry on")
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
