"""Queue one address lookup for the worker.

    python scripts/enqueue.py tron T... [--wait 300]

Needs DATABASE_URL. The job payload is the chain and address only. With --wait,
polls until the job is done or failed and prints the job row.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from taw import config, jobs  # noqa: E402

FIELDS = "id, kind, dedupe_key, payload, status, attempts, max_attempts, run_after, locked_by, last_error, created_at, finished_at"


def show(conn, job_id: int) -> dict:
    cur = conn.execute(f"SELECT {FIELDS} FROM job WHERE id = %s", (job_id,))
    row = dict(zip([d.name for d in cur.description], cur.fetchone()))
    for k, v in row.items():
        print(f"  {k:<13} {v}")
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="Queue one address lookup")
    ap.add_argument("chain")
    ap.add_argument("address")
    ap.add_argument("--wait", type=int, default=0, help="seconds to wait for the worker")
    args = ap.parse_args()
    if not config.DATABASE_URL:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        job_id, created = jobs.enqueue_lookup(conn, args.chain, args.address)
        print(f"job {job_id}: {'queued' if created else 'already queued or running'}")
        deadline = time.monotonic() + args.wait
        status = None
        while args.wait:
            status = conn.execute("SELECT status FROM job WHERE id = %s", (job_id,)).fetchone()[0]
            if status in ("done", "failed") or time.monotonic() > deadline:
                break
            time.sleep(2)
        show(conn, job_id)
        return 0 if status in (None, "done") else 1


if __name__ == "__main__":
    sys.exit(main())
