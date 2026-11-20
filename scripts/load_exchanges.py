"""Load the exchange wallet lists now (as the daily job does), then print row counts
and database size. Run it inside Railway:

    railway ssh --service traceawallet-worker -- python scripts/load_exchanges.py
"""
from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from taw import config, store  # noqa: E402
from taw.adapters.base import BucketRawStore  # noqa: E402
from taw.worker.lists import refresh_exchanges  # noqa: E402

ACTOR = "script:load_exchanges"
SIZES = """
SELECT pg_size_pretty(pg_database_size(current_database())),
       pg_size_pretty(pg_total_relation_size('label')), pg_size_pretty(pg_total_relation_size('event_log')),
       pg_size_pretty(pg_total_relation_size('address')),
       (SELECT count(*) FROM label), (SELECT count(*) FROM event_log), (SELECT count(*) FROM address)
"""


def report(conn, when: str) -> None:
    db, lab, ev, addr, n_lab, n_ev, n_addr = conn.execute(SIZES).fetchone()
    print(f"{when}: database {db}; label {n_lab:,} rows ({lab}); event_log {n_ev:,} rows ({ev}); "
          f"address {n_addr:,} rows ({addr})")
    for slug, n in conn.execute("SELECT s.slug, count(*) FROM label_current l JOIN source s ON s.id = l.source_id"
                                " GROUP BY s.slug ORDER BY s.slug").fetchall():
        print(f"  current labels from {slug}: {n:,}")


def main() -> int:
    if not config.DATABASE_URL:
        sys.exit("DATABASE_URL is not set: run this inside Railway with railway ssh.")
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as data, \
            psycopg.connect(config.DATABASE_URL, autocommit=True) as raw:
        report(data, "before")
        started = time.monotonic()
        raw_store = store.RecordingRawStore(raw, ACTOR, s3_client=BucketRawStore._client_from_env())
        print(refresh_exchanges(data_conn=data, raw_store=raw_store, actor=ACTOR))
        print(f"took {time.monotonic() - started:.0f} s")
        report(data, "after")
    return 0


if __name__ == "__main__":
    sys.exit(main())
