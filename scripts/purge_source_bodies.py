"""Delete one source's stored response bodies from the bucket. Dry run by default.

    python scripts/purge_source_bodies.py etherscan              # dry run: counts only, changes nothing
    python scripts/purge_source_bodies.py etherscan --execute    # really delete

Why: a provider's terms may require us to destroy its data (Etherscan's do when
the agreement ends). The raw_response rows stay: their sha256, request, status
and fetch time remain the record that the response existed and what it hashed
to. Only the body objects go.

Rules:
  - Bodies are stored by sha256 alone, so two sources that returned identical
    bytes share one object. An object is deleted only when every raw_response
    row pointing at it belongs to this source; shared objects are kept and
    listed.
  - A real run writes one event_log row (action raw_bodies_purged) naming the
    source, the deleted shas and the kept ones, so nothing disappears silently
    (design rule 5). The row is written before the deletes start: if a delete
    fails, the log still says what was attempted.
  - Afterwards, BucketRawStore.body() raises RawMissing for those shas.

Needs DATABASE_URL and the BUCKET_* variables. Run inside Railway:

    railway ssh --service traceawallet-worker -- python scripts/purge_source_bodies.py etherscan
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ACTION = "raw_bodies_purged"


@dataclass(frozen=True)
class Plan:
    source_slug: str
    rows: int                      # raw_response rows from this source (all stay)
    delete: list[str]              # shas whose objects only this source uses
    shared: list[str]              # shas also used by another source: kept


def plan(conn, source_slug: str) -> Plan:
    row = conn.execute("SELECT id FROM source WHERE slug = %s", (source_slug,)).fetchone()
    if row is None:
        raise SystemExit(f"no source with slug {source_slug!r}")
    sid = row[0]
    rows = conn.execute("SELECT count(*) FROM raw_response WHERE source_id = %s", (sid,)).fetchone()[0]
    found = conn.execute(
        "SELECT r.sha256, bool_or(o.source_id IS NOT NULL) AS shared"
        " FROM raw_response r"
        " LEFT JOIN raw_response o ON o.sha256 = r.sha256 AND o.source_id <> r.source_id"
        " WHERE r.source_id = %s GROUP BY r.sha256 ORDER BY r.sha256",
        (sid,),
    ).fetchall()
    delete = [sha.strip() for sha, shared in found if not shared]
    shared = [sha.strip() for sha, shared in found if shared]
    return Plan(source_slug, rows, delete, shared)


def present(s3, bucket: str, shas: list[str]) -> list[str]:
    """The shas whose objects are in the bucket now."""
    from taw.adapters.base import bucket_key_for

    out = []
    for sha in shas:
        try:
            s3.head_object(Bucket=bucket, Key=bucket_key_for(sha))
            out.append(sha)
        except Exception as exc:
            code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
            if code not in ("404", "NoSuchKey", "NotFound"):
                raise
    return out


def execute(conn, s3, bucket: str, p: Plan, actor: str) -> int:
    """Log, then delete. Returns how many objects were deleted."""
    from taw.adapters.base import bucket_key_for
    from taw.store import actor_transaction

    with actor_transaction(conn, actor):
        conn.execute(
            "INSERT INTO event_log (actor, action, entity, entity_id, payload)"
            " VALUES (%s, %s, 'source', %s, %s::jsonb)",
            (actor, ACTION, p.source_slug,
             json.dumps({"source": p.source_slug, "raw_response_rows_kept": p.rows,
                         "deleted_sha256": p.delete, "kept_shared_sha256": p.shared}, sort_keys=True)),
        )
    for sha in p.delete:
        s3.delete_object(Bucket=bucket, Key=bucket_key_for(sha))
    return len(p.delete)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", help="source slug, e.g. etherscan")
    ap.add_argument("--execute", action="store_true", help="really delete (default: dry run)")
    ap.add_argument("--actor", default="script:purge_source_bodies")
    args = ap.parse_args(argv)

    import psycopg

    from taw import config
    from taw.adapters.base import BucketRawStore

    bucket = os.environ.get("BUCKET_NAME")
    s3 = BucketRawStore._client_from_env()
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        p = plan(conn, args.source)
        here = present(s3, bucket, p.delete)
        print(f"Source {p.source_slug}: {p.rows} raw_response rows (kept either way)")
        print(f"  distinct bodies only this source uses   {len(p.delete):>8}  ({len(here)} in the bucket now)")
        print(f"  bodies shared with another source (kept) {len(p.shared):>7}")
        if not args.execute:
            print("Dry run: nothing deleted, nothing logged. Add --execute to delete.")
            return 0
        n = execute(conn, s3, bucket, p, args.actor)
        print(f"Deleted {n} objects; event_log row '{ACTION}' written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
