"""Recompute each label load's digest and compare it with its event_log entry.

    railway ssh --service traceawallet-worker -- python scripts/verify_label_loads.py [load_id]

A file load (OFAC, the exchange file, OKX) writes one event_log entry instead of a
copy of every row: source, file sha256, row count and a sha256 over the rows
(taw/labels.py, DIGEST_METHOD). Exits 1 if any load no longer matches.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from taw import config, labels  # noqa: E402


def main() -> int:
    if not config.DATABASE_URL:
        sys.exit("DATABASE_URL is not set: run this inside Railway with railway ssh.")
    load_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        results = labels.verify_loads(conn, load_id)
    for lid, ok, what in results:
        print(f"load {lid}: {'OK' if ok else 'MISMATCH'}: {what}")
    bad = sum(1 for _, ok, _ in results if not ok)
    print(f"{len(results)} load(s) checked, {bad} mismatch(es)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
