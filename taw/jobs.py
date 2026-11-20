"""The Postgres job queue: enqueue, claim, finish.

A job is "fetch this address". Its payload holds the chain and the address and
nothing else: no user id, session, IP or anything a person typed besides the
address. Two people asking about the same address share one job through
dedupe_key and the job_one_live index.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from taw.adapters.registry import normalize_address

LOOKUP = "address_lookup"
OFAC_REFRESH = "ofac_refresh"
DAILY = {OFAC_REFRESH: 23}       # kind -> hour (UTC) it runs each day
RETRY_BASE_SECONDS = 30          # waits of 30 s, 2 min, 8 min, ... between attempts
STALE_AFTER = "30 minutes"       # a 'running' job older than this lost its worker


class PayloadError(ValueError):
    pass


def lookup_payload(chain: str, address: str) -> dict[str, str]:
    return {"chain": chain, "address": normalize_address(chain, address)}


def read_lookup_payload(payload: Any) -> tuple[str, str]:
    """(chain, address) from a job payload. Anything beyond those two keys is refused."""
    if not isinstance(payload, dict) or set(payload) != {"chain", "address"}:
        keys = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        raise PayloadError(f"an address_lookup payload must be exactly chain and address, got {keys}")
    chain, address = payload["chain"], payload["address"]
    if not isinstance(chain, str) or not isinstance(address, str):
        raise PayloadError("chain and address must be text")
    return chain, normalize_address(chain, address)


def enqueue_lookup(conn, chain: str, address: str) -> tuple[int, bool]:
    """Queue a lookup. Returns (job id, True) for a new job, or the live job's id
    and False when one is already queued or running for this address."""
    payload = lookup_payload(chain, address)
    dedupe = f"{chain}:{payload['address']}"
    row = conn.execute(
        "INSERT INTO job (kind, dedupe_key, payload) VALUES (%s, %s, %s::jsonb)"
        " ON CONFLICT (kind, dedupe_key) WHERE status IN ('queued', 'running') DO NOTHING RETURNING id",
        (LOOKUP, dedupe, json.dumps(payload)),
    ).fetchone()
    if row:
        return row[0], True
    row = conn.execute(
        "SELECT id FROM job WHERE kind = %s AND dedupe_key = %s AND status IN ('queued', 'running')",
        (LOOKUP, dedupe),
    ).fetchone()
    if row is None:              # finished between the two statements; queue a fresh one
        return enqueue_lookup(conn, chain, address)
    return row[0], False


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: Any
    attempts: int                # including this one
    max_attempts: int


_CLAIM = f"""
UPDATE job SET status = 'running', locked_by = %s, locked_at = now(), attempts = attempts + 1
WHERE id = (SELECT id FROM job
            WHERE (status = 'queued' AND run_after <= now())
               OR (status = 'running' AND locked_at < now() - interval '{STALE_AFTER}')
            ORDER BY run_after, id
            FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, kind, payload, attempts, max_attempts
"""


def claim(conn, worker_id: str) -> Job | None:
    """Claim the next job and commit the claim at once, so other workers see it."""
    with conn.transaction():
        row = conn.execute(_CLAIM, (worker_id,)).fetchone()
    return Job(*row) if row else None


def mark_done(conn, job_id: int) -> None:
    conn.execute("UPDATE job SET status = 'done', last_error = NULL, finished_at = now() WHERE id = %s",
                 (job_id,))


def retry_later(conn, job: Job, message: str) -> int:
    """Put the job back in the queue after a growing wait. Returns the wait in seconds."""
    wait = RETRY_BASE_SECONDS * 4 ** (job.attempts - 1)
    conn.execute(
        "UPDATE job SET status = 'queued', run_after = now() + %s * interval '1 second',"
        " last_error = %s, locked_by = NULL, locked_at = NULL WHERE id = %s",
        (wait, message, job.id),
    )
    return wait


def defer_until(conn, job: Job, when, message: str) -> None:
    """Put the job back to run at `when` without counting this attempt (a provider's
    daily allowance ran out: not the job's fault, and retrying sooner cannot help)."""
    conn.execute(
        "UPDATE job SET status = 'queued', run_after = %s, attempts = GREATEST(attempts - 1, 0),"
        " last_error = %s, locked_by = NULL, locked_at = NULL WHERE id = %s",
        (when, message, job.id),
    )


def mark_failed(conn, job_id: int, message: str) -> None:
    conn.execute("UPDATE job SET status = 'failed', last_error = %s, finished_at = now() WHERE id = %s",
                 (message, job_id))


def ensure_daily(conn, kind: str, *, now=None) -> bool:
    """Queue today's run of a daily job unless one was already queued for today.
    It runs at DAILY[kind]:00 UTC, or at once if no run of that kind ever finished.
    Returns True when a job was queued."""
    import datetime as dt
    now = now or dt.datetime.now(dt.timezone.utc)
    at = dt.datetime.combine(now.date(), dt.time(DAILY[kind]), dt.timezone.utc)
    ever = conn.execute("SELECT 1 FROM job WHERE kind = %s AND status = 'done' LIMIT 1", (kind,)).fetchone()
    when = at if ever else now
    row = conn.execute(
        "INSERT INTO job (kind, dedupe_key, payload, run_after)"
        " SELECT %s, %s, '{}'::jsonb, %s WHERE NOT EXISTS (SELECT 1 FROM job WHERE kind = %s AND dedupe_key = %s)"
        " ON CONFLICT (kind, dedupe_key) WHERE status IN ('queued', 'running') DO NOTHING RETURNING id",
        (kind, f"{kind}:{now.date()}", when, kind, f"{kind}:{now.date()}")).fetchone()
    return row is not None
