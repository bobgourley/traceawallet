"""Worker loop. Claims one job at a time from the Postgres queue and runs it.

Job kinds:
  address_lookup   taw/worker/lookup.py

A failing job is retried after a growing wait until max_attempts, then marked
failed with a plain-language last_error. Some failures (a TronGrid 403, a bad
address) fail the job at once.
"""
from __future__ import annotations

import os
import socket
import time
from typing import Callable

import psycopg

from taw import config, jobs, store
from taw.adapters import registry
from taw.adapters.base import BucketRawStore
from taw.worker import lookup

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
ACTOR = f"worker:{WORKER_ID}"


def connect() -> psycopg.Connection:
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(config.DATABASE_URL, autocommit=True)


def handle(job: jobs.Job, *, data_conn, raw_store_factory: Callable, adapter_for: Callable,
           actor: str = ACTOR, log=print, store_module=store) -> str:
    """Run one claimed job and record its outcome on the job row. Returns the status."""
    if job.attempts > job.max_attempts:     # reclaimed after the worker died too many times
        jobs.mark_failed(data_conn, job.id, f"Gave up after {job.max_attempts} attempts: the worker "
                                            "stopped partway through each time.")
        log(f"job {job.id}: failed, too many attempts")
        return "failed"
    if job.kind != jobs.LOOKUP:
        jobs.mark_failed(data_conn, job.id, f"No handler for job kind {job.kind!r}.")
        log(f"job {job.id}: no handler for {job.kind!r}")
        return "failed"
    try:
        outcome = lookup.run_lookup(job.payload, data_conn=data_conn, raw_store=raw_store_factory(),
                                    actor=actor, adapter_for=adapter_for, store_module=store_module)
    except Exception as exc:
        resets_at = lookup.allowance_reset(exc)
        if resets_at is not None:
            message = (f"{getattr(exc, 'provider', 'The provider')}'s daily allowance used up, resets at 00:00 UTC. "
                       f"The lookup will run again after {resets_at:%Y-%m-%d %H:%M} UTC; this attempt is not counted.")
            jobs.defer_until(data_conn, job, resets_at, message)
            log(f"job {job.id}: deferred to {resets_at:%Y-%m-%d %H:%M} UTC: {message}")
            return "queued"
        permanent, message = lookup.describe_failure(exc)
        if permanent or job.attempts >= job.max_attempts:
            final = message if permanent else f"Failed after {job.attempts} attempts. Last error: {message}"
            jobs.mark_failed(data_conn, job.id, final)
            log(f"job {job.id}: failed: {final}")
            return "failed"
        wait = jobs.retry_later(data_conn, job, message)
        log(f"job {job.id}: attempt {job.attempts} of {job.max_attempts} failed, retrying in {wait}s: {message}")
        return "queued"
    jobs.mark_done(data_conn, job.id)
    log(f"job {job.id}: done: {outcome.message}")
    return "done"


def run_once(s3_client=None) -> bool:
    with connect() as data_conn, connect() as raw_conn:
        job = jobs.claim(data_conn, WORKER_ID)
        if job is None:
            return False
        handle(job, data_conn=data_conn,
               raw_store_factory=lambda: store.RecordingRawStore(raw_conn, ACTOR, s3_client=s3_client),
               adapter_for=lambda chain, raw_store: registry.adapter_for(chain, raw_store, state_conn=raw_conn),
               log=lambda m: print(m, flush=True))
        return True


def main() -> None:
    print(f"worker {WORKER_ID} started", flush=True)
    s3_client = None
    while True:
        try:
            if s3_client is None and os.environ.get("BUCKET_ENDPOINT"):
                s3_client = BucketRawStore._client_from_env()
            if not run_once(s3_client):
                time.sleep(2)
        except Exception as exc:
            print(f"worker error: {exc!r}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
