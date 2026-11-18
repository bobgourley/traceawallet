"""Read-only check of what lookups have stored. Writes nothing.

    python scripts/check_lookup.py tron T...

Prints the lookup jobs for the address, row counts, the number of objects in
the bucket and the event log check. Needs DATABASE_URL and the BUCKET_*
variables.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from taw import config  # noqa: E402
from taw.adapters.base import BucketRawStore  # noqa: E402
from taw.adapters.registry import normalize_address  # noqa: E402


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Show what lookups stored for one address")
    ap.add_argument("chain")
    ap.add_argument("address")
    args = ap.parse_args()
    address = normalize_address(args.chain, args.address)

    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = on")
        print(f"Lookup jobs for {args.chain}:{address}")
        for row in conn.execute(
            "SELECT id, status, attempts, max_attempts, locked_by, last_error, created_at, finished_at"
            " FROM job WHERE kind = 'address_lookup' AND dedupe_key = %s ORDER BY id",
            (f"{args.chain}:{address}",),
        ):
            jid, status, att, mx, by, err, created, finished = row
            print(f"  job {jid}: {status}, attempt {att}/{mx}, worker {by}, created {created:%H:%M:%S},"
                  f" finished {finished:%H:%M:%S}" if finished else f"  job {jid}: {status}, attempt {att}/{mx}")
            if err:
                print(f"    last_error: {err}")

        addr_id = conn.execute("SELECT id FROM address WHERE chain = %s AND address = %s",
                               (args.chain, address)).fetchone()
        print()
        print("Rows")
        print(f"  address (all chains)             {one(conn, 'SELECT count(*) FROM address'):>8}")
        print(f"  address ({args.chain})                    {one(conn, 'SELECT count(*) FROM address WHERE chain = %s', args.chain):>8}")
        print(f"  transfer (all)                   {one(conn, 'SELECT count(*) FROM transfer'):>8}")
        if addr_id:
            n = one(conn, "SELECT count(*) FROM transfer WHERE from_address_id = %s OR to_address_id = %s",
                    addr_id[0], addr_id[0])
            print(f"  transfer touching this address   {n:>8}")
            dupes = one(conn, "SELECT count(*) FROM (SELECT 1 FROM transfer GROUP BY chain, tx_hash, kind,"
                              " event_index, source_id HAVING count(*) > 1) d")
            print(f"  duplicate transfer keys          {dupes:>8}")
        print(f"  raw_response                     {one(conn, 'SELECT count(*) FROM raw_response'):>8}")
        print(f"  raw_response distinct bodies     {one(conn, 'SELECT count(DISTINCT sha256) FROM raw_response'):>8}")
        print(f"  address_snapshot                 {one(conn, 'SELECT count(*) FROM address_snapshot'):>8}")
        for sid, block, fetched, n, trunc in conn.execute(
            "SELECT s.id, s.as_of_block, s.fetched_at, s.transfer_count, s.transfers_truncated"
            " FROM address_snapshot s JOIN address a ON a.id = s.address_id"
            " WHERE a.chain = %s AND a.address = %s ORDER BY s.id", (args.chain, address)):
            print(f"    snapshot {sid}: block {block:,}, {n} transfers{' (truncated)' if trunc else ''},"
                  f" fetched {fetched:%Y-%m-%d %H:%M:%S} UTC")

        bucket = os.environ.get("BUCKET_NAME")
        s3 = BucketRawStore._client_from_env()
        objects = 0
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="raw/"):
            objects += page.get("KeyCount", 0)
        print()
        print(f"Bucket objects under raw/          {objects:>8}")
        result = one(conn, "SELECT event_log_verify()")
        print(f"event_log_verify()                 {'NULL (chain intact)' if result is None else f'BROKEN at row {result}'}")
        print(f"event_log rows                     {one(conn, 'SELECT count(*) FROM event_log'):>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
