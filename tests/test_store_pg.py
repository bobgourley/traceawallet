"""Store and queue tests against a real Postgres.

Skipped unless TEST_DATABASE_URL points at a server where this user may create
databases. Each test session creates a throwaway database, applies every
migration, and drops it afterwards. Never point this at production.
"""
import datetime as dt
import io
import os
import pathlib
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from taw import jobs, store  # noqa: E402
from taw.adapters.base import TransferBatch, TransferRecord  # noqa: E402
from taw.adapters.chains.tron_trongrid import TronGridAdapter  # noqa: E402
from tests.tron_replay import FIXTURES, FakeClock, Replay  # noqa: E402

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="TEST_DATABASE_URL not set")
MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"
ADDR = "TL7me8SQkNmieVngj9sPQGG2PSMENASHug"
ACTOR = "worker:test"


@pytest.fixture(scope="session")
def db_url():
    name = f"taw_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=name)
    with psycopg.connect(url, autocommit=True) as conn:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(path.read_text())
    yield url
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def conns(db_url):
    data = psycopg.connect(db_url, autocommit=True)
    raw = psycopg.connect(db_url, autocommit=True)
    yield data, raw
    data.close()
    raw.close()


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


def count(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def lookup_once(data, raw, s3):
    replay = Replay(FIXTURES / ADDR)
    clock = FakeClock()
    raw_store = store.RecordingRawStore(raw, ACTOR, s3_client=s3, bucket="test")
    adapter = TronGridAdapter(raw_store, "test-key-not-real", http=replay.client(),
                              clock=clock.clock, sleep=clock.sleep)
    snap = adapter.fetch_snapshot(ADDR)
    batch = adapter.fetch_transfers(ADDR)
    return store.save_lookup(data, ACTOR, snap, batch), adapter


def test_lookup_is_idempotent(conns):
    data, raw = conns
    s3 = FakeS3()
    first, a1 = lookup_once(data, raw, s3)
    assert (first.transfers_seen, first.transfers_new) == (35, 35)
    transfers = count(data, "SELECT count(*) FROM transfer WHERE chain = 'tron'")
    addresses = count(data, "SELECT count(*) FROM address WHERE chain = 'tron'")
    responses = count(data, "SELECT count(*) FROM raw_response")
    assert transfers == 35 and responses == a1.request_count == 30
    assert len(s3.objects) == count(data, "SELECT count(DISTINCT sha256) FROM raw_response")

    second, a2 = lookup_once(data, raw, s3)
    assert (second.transfers_seen, second.transfers_new) == (35, 0)
    assert count(data, "SELECT count(*) FROM transfer WHERE chain = 'tron'") == transfers
    assert count(data, "SELECT count(*) FROM address WHERE chain = 'tron'") == addresses
    assert (a2.request_count, a2.reused_count) == (5, 25)          # events reused, not re-fetched
    assert count(data, "SELECT count(*) FROM raw_response") == responses + 5
    assert count(data, "SELECT count(*) FROM address_snapshot") == 2

    # Every transfer points at a stored response from TronGrid.
    assert count(data, "SELECT count(*) FROM transfer t JOIN raw_response r ON r.id = t.raw_response_id"
                       " JOIN source s ON s.id = r.source_id WHERE s.slug = 'trongrid'") == transfers
    assert count(data, "SELECT event_log_verify() IS NULL") is True


def test_snapshot_rests_on_stored_responses(conns):
    data, raw = conns
    saved, _ = lookup_once(data, raw, FakeS3())
    raw_id, source = data.execute(
        "SELECT s.raw_response_id, src.slug FROM address_snapshot s JOIN source src ON src.id = s.source_id"
        " WHERE s.id = %s", (saved.snapshot_id,)).fetchone()
    assert source == "trongrid"
    assert "/v1/accounts/" in count(data, "SELECT request_url FROM raw_response WHERE id = %s", raw_id)
    linked = [r[0] for r in data.execute(
        "SELECT r.request_url FROM address_snapshot_response l JOIN raw_response r ON r.id = l.raw_response_id"
        " WHERE l.snapshot_id = %s", (saved.snapshot_id,))]
    assert len(linked) == 3 and any("getblock" in u for u in linked)


# table -> a column to "update" to itself
EVIDENCE_TABLES = {"address_snapshot": "id", "address_snapshot_response": "snapshot_id",
                   "transfer": "id", "raw_response": "id", "address": "id"}


@pytest.mark.parametrize("table", EVIDENCE_TABLES)
def test_evidence_tables_are_append_only(conns, table):
    data, raw = conns
    lookup_once(data, raw, FakeS3())
    assert count(data, f"SELECT count(*) FROM {table}") > 0
    column = EVIDENCE_TABLES[table]
    for statement in (f"UPDATE {table} SET {column} = {column}",
                      f"DELETE FROM {table}",
                      f"TRUNCATE {table} CASCADE"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            data.execute(statement)


@pytest.mark.parametrize("table", EVIDENCE_TABLES)
def test_app_role_may_read_and_insert_only(conns, table):
    data, _ = conns
    can = {p: count(data, "SELECT has_table_privilege('taw_app', %s, %s)", table, p)
           for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")}
    assert can == {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False, "TRUNCATE": False}


def test_snapshot_foreign_keys(conns):
    data, _ = conns
    refs = {(r[0], r[1]) for r in data.execute(
        "SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint"
        " WHERE contype = 'f' AND conrelid::regclass::text IN ('address_snapshot', 'address_snapshot_response')")}
    assert ("address_snapshot", "raw_response") in refs
    assert ("address_snapshot", "source") in refs
    assert ("address_snapshot", "address") in refs
    assert ("address_snapshot_response", "raw_response") in refs
    assert ("address_snapshot_response", "address_snapshot") in refs
    assert count(data, "SELECT is_nullable FROM information_schema.columns"
                       " WHERE table_name = 'address_snapshot' AND column_name = 'raw_response_id'") == "NO"


def test_fresh_snapshot_window(conns):
    data, raw = conns
    lookup_once(data, raw, FakeS3())
    assert store.fresh_snapshot(data, "tron", ADDR) is not None
    assert store.fresh_snapshot(data, "tron", ADDR, max_age=dt.timedelta(microseconds=1)) is None
    assert store.fresh_snapshot(data, "tron", "TLgdKbLgoFKGbUMmtTH4RS632pRzeuhE4X") is None


def test_identical_transfers_are_two_rows(conns):
    data, raw = conns
    raw_store = store.RecordingRawStore(raw, ACTOR, s3_client=FakeS3(), bucket="test")
    sha = raw_store.save("trongrid", "https://api.trongrid.io/v1/transactions/x/events", None, 200, b'{"n": 1}')
    when = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)

    def rec(index):
        return TransferRecord("tron", "9" * 64, "token", index, ADDR, "TLgdKbLgoFKGbUMmtTH4RS632pRzeuhE4X",
                              "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "USDT", 6, 5_000_000, 1, when, "trongrid", sha)
    with store.actor_transaction(data, ACTOR):
        ids = store.upsert_addresses(data, "tron", [ADDR, "TLgdKbLgoFKGbUMmtTH4RS632pRzeuhE4X"])
        raw_ids = store.raw_response_ids(data, "trongrid", [sha])
        assert store.insert_transfers(data, [rec(3), rec(5)], ids, raw_ids) == 2
        assert store.insert_transfers(data, [rec(3), rec(5)], ids, raw_ids) == 0
    assert count(data, "SELECT count(*) FROM transfer WHERE tx_hash = %s", "9" * 64) == 2


def test_actor_names_the_worker_in_the_event_log(conns):
    data, _ = conns
    with store.actor_transaction(data, "worker:host-123"):
        assert count(data, "SELECT current_setting('taw.actor')") == "worker:host-123"
        # source is audited; touching it shows who the log names
        data.execute("UPDATE source SET attribution = attribution WHERE slug = 'trongrid'")
    assert count(data, "SELECT current_setting('taw.actor', true)") in ("", None)    # LOCAL: gone after commit
    assert count(data, "SELECT actor FROM event_log ORDER BY id DESC LIMIT 1") == "worker:host-123"
    assert count(data, "SELECT event_log_verify() IS NULL") is True


def test_failed_save_writes_nothing(conns):
    data, raw = conns
    before = count(data, "SELECT count(*) FROM address")
    with pytest.raises(KeyError):
        with store.actor_transaction(data, ACTOR):
            store.upsert_addresses(data, "tron", ["TJPoViEUAcJJxbH1dxYdDFvBpy9ekSqsLW"])
            raise KeyError("boom")
    assert count(data, "SELECT count(*) FROM address") == before


def test_queue(conns):
    data, _ = conns
    job_id, created = jobs.enqueue_lookup(data, "tron", ADDR)
    again, created_again = jobs.enqueue_lookup(data, "tron", ADDR)
    assert created and not created_again and again == job_id
    assert count(data, "SELECT payload FROM job WHERE id = %s", job_id) == {"chain": "tron", "address": ADDR}

    job = jobs.claim(data, "w1")
    assert job.id == job_id and job.attempts == 1
    assert jobs.claim(data, "w2") is None                             # nothing else is due

    assert jobs.retry_later(data, job, "TronGrid kept answering 503") == 30
    assert jobs.claim(data, "w2") is None                             # not due for 30 s
    data.execute("UPDATE job SET run_after = now() WHERE id = %s", (job_id,))
    job = jobs.claim(data, "w2")
    assert job.attempts == 2

    # A worker that died: its claim goes stale and the job can be taken again.
    data.execute("UPDATE job SET locked_at = now() - interval '31 minutes' WHERE id = %s", (job_id,))
    job = jobs.claim(data, "w3")
    assert job.id == job_id and job.attempts == 3

    jobs.mark_failed(data, job_id, "Failed after 3 attempts.")
    new_id, created = jobs.enqueue_lookup(data, "tron", ADDR)          # a finished job does not block
    assert created and new_id != job_id
    jobs.mark_done(data, jobs.claim(data, "w4").id)
