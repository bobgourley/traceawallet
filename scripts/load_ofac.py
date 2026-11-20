"""Load OFAC's SDN list now, the same way the daily job does. Run it inside Railway:

    railway ssh --service traceawallet-worker -- python scripts/load_ofac.py

No API key is involved; the file is public. Prints the counts only.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from taw import config, store  # noqa: E402
from taw.adapters.base import BucketRawStore  # noqa: E402
from taw.worker.lists import refresh_ofac  # noqa: E402

ACTOR = "script:load_ofac"


def main() -> int:
    if not config.DATABASE_URL:
        sys.exit("DATABASE_URL is not set: run this inside Railway with railway ssh.")
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as data, \
            psycopg.connect(config.DATABASE_URL, autocommit=True) as raw:
        raw_store = store.RecordingRawStore(raw, ACTOR, s3_client=BucketRawStore._client_from_env())
        print(refresh_ofac(data_conn=data, raw_store=raw_store, actor=ACTOR))
    return 0


if __name__ == "__main__":
    sys.exit(main())
