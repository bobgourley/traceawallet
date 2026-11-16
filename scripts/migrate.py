"""Apply migrations/NNNN_*.sql in order, once each.

Uses DATABASE_OWNER_URL if set, otherwise DATABASE_URL. Each file manages its
own transaction (BEGIN ... COMMIT), so a failed file leaves nothing half-applied.
"""
import os
import pathlib
import sys

import psycopg

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"


def main() -> int:
    url = os.environ.get("DATABASE_OWNER_URL") or os.environ.get("DATABASE_URL")
    if not url:
        print("migrate: no DATABASE_OWNER_URL or DATABASE_URL set", file=sys.stderr)
        return 1
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(727001)")  # one migrator at a time
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration ("
            "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migration")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            print(f"migrate: applying {path.name}", flush=True)
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
        print("migrate: up to date", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
