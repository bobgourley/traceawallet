"""Worker loop. Claims one job at a time from the Postgres queue.

No job kinds exist yet, so a claimed job is marked failed with a clear message.
Adapters plug in here next.
"""
import os
import socket
import time

from taw import db

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"

CLAIM = """
UPDATE job SET status = 'running', locked_by = %s, locked_at = now(), attempts = attempts + 1
WHERE id = (SELECT id FROM job
            WHERE status = 'queued' AND run_after <= now()
            ORDER BY run_after, id
            FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, kind
"""


def run_once() -> bool:
    with db.connect() as conn:
        row = conn.execute(CLAIM, (WORKER_ID,)).fetchone()
        if row is None:
            return False
        job_id, kind = row
        conn.execute(
            "UPDATE job SET status = 'failed', last_error = %s, finished_at = now() WHERE id = %s",
            (f"no handler for job kind {kind!r} yet", job_id),
        )
        print(f"job {job_id} ({kind}): no handler yet", flush=True)
        return True


def main() -> None:
    print(f"worker {WORKER_ID} started", flush=True)
    while True:
        try:
            if not run_once():
                time.sleep(2)
        except Exception as exc:
            print(f"worker error: {exc!r}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
