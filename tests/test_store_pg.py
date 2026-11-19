"""Store and queue tests against a real Postgres.

Skipped unless TEST_DATABASE_URL points at a server where this user may create
databases. Each test session creates a throwaway database, applies every
migration, and drops it afterwards. Never point this at production.
"""
import dataclasses
import datetime as dt
import io
import os
import pathlib
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from taw import jobs, store  # noqa: E402
from taw.adapters.base import BucketRawStore, TransferBatch, TransferRecord  # noqa: E402
from taw.adapters.chains.tron_trongrid import TronGridAdapter  # noqa: E402
from tests.tron_replay import FIXTURES, FakeClock, Replay  # noqa: E402

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="TEST_DATABASE_URL not set")
MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"
ADDR = "TL7me8SQkNmieVngj9sPQGG2PSMENASHug"
ACTOR = "worker:test"


@pytest.fixture(scope="session")
def template_db():
    """One database with every migration applied, copied for each test."""
    name = f"taw_tmpl_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    with psycopg.connect(psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=name), autocommit=True) as conn:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(path.read_text())
    yield name
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def db_url(template_db):
    """A fresh, fully migrated database for this test alone."""
    name = f"taw_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"')
    yield psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=name)
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def conns(db_url):
    data = psycopg.connect(db_url, autocommit=True)
    raw = psycopg.connect(db_url, autocommit=True)
    yield data, raw
    data.close()
    raw.close()


class NoSuchKey(Exception):
    """Shaped like botocore's ClientError for a missing object."""

    def __init__(self, key):
        super().__init__(key)
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise NoSuchKey(Key)
        return {"ContentLength": len(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


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


def test_missing_bucket_object_is_fetched_again_and_restored(conns):
    data, raw = conns
    s3 = FakeS3()
    lookup_once(data, raw, s3)
    events_keys = [k for k in s3.objects
                   if b'"event_name"' in s3.objects[k]]                # the saved events bodies
    lost = events_keys[0]
    del s3.objects[lost]                                               # the bucket lost one file

    second, adapter = lookup_once(data, raw, s3)
    assert second.transfers_new == 0
    assert (adapter.request_count, adapter.reused_count) == (6, 24)    # 5 as usual + the lost one
    assert lost in s3.objects                                          # restored under the same key


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


def lookup_events(conn):
    return [dict(actor=a, entity=e, entity_id=i, **p) for a, e, i, p in conn.execute(
        "SELECT actor, entity, entity_id, payload FROM event_log WHERE action = 'lookup_completed' ORDER BY id")]


def test_completed_lookups_are_logged(conns):
    data, raw = conns
    s3 = FakeS3()
    first, _ = lookup_once(data, raw, s3)
    second, _ = lookup_once(data, raw, s3)
    events = lookup_events(data)
    assert len(events) == 2
    for event, saved in zip(events, (first, second)):
        assert event["actor"] == ACTOR
        assert event["entity"] == "address_snapshot" and event["entity_id"] == str(saved.snapshot_id)
        assert event["chain"] == "tron" and event["address"] == ADDR
        assert event["snapshot_id"] == saved.snapshot_id
        assert event["as_of_block"] == 88_091_753
        assert event["snapshot_reused"] is False
    assert (events[0]["transfers_saved"], events[0]["transfers_reused"]) == (35, 0)
    assert (events[1]["transfers_saved"], events[1]["transfers_reused"]) == (0, 35)
    assert set(events[0]) == {"actor", "entity", "entity_id", "chain", "address", "snapshot_id",
                              "as_of_block", "transfers_saved", "transfers_reused", "snapshot_reused"}
    assert count(data, "SELECT event_log_verify() IS NULL") is True


def test_reused_lookup_is_logged(conns):
    data, raw = conns
    saved, _ = lookup_once(data, raw, FakeS3())
    fresh = store.fresh_snapshot(data, "tron", ADDR)
    store.record_reused_lookup(data, "worker:other", "tron", ADDR, fresh)
    event = lookup_events(data)[-1]
    assert event["actor"] == "worker:other" and event["snapshot_reused"] is True
    assert event["snapshot_id"] == saved.snapshot_id
    assert (event["transfers_saved"], event["transfers_reused"]) == (0, 35)
    assert count(data, "SELECT event_log_verify() IS NULL") is True


def test_failed_save_logs_nothing(conns):
    data, raw = conns
    replay = Replay(FIXTURES / ADDR)
    clock = FakeClock()
    adapter = TronGridAdapter(store.RecordingRawStore(raw, ACTOR, s3_client=FakeS3(), bucket="test"),
                              "test-key-not-real", http=replay.client(), clock=clock.clock, sleep=clock.sleep)
    snap = adapter.fetch_snapshot(ADDR)
    batch = adapter.fetch_transfers(ADDR)
    # the last transfer points at a response that was never recorded: the save must fail whole
    broken = dataclasses.replace(batch, transfers=batch.transfers[:-1] + (
        dataclasses.replace(batch.transfers[-1], raw_sha256="0" * 64),))
    with pytest.raises(RuntimeError, match="never recorded"):
        store.save_lookup(data, ACTOR, snap, broken)
    assert lookup_events(data) == []
    assert count(data, "SELECT count(*) FROM transfer") == 0


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


# -- EVM (Etherscan) ----------------------------------------------------------------

EVM_ADDR = "0xaab6417993d04a861f1f02c5c4bd2e9145d8f637"


def evm_lookup_once(data, raw, s3):
    from taw.adapters.chains.evm_etherscan import EtherscanAdapter
    from tests.evm_replay import FIXTURES as EVM_FIXTURES, Replay as EvmReplay

    clock = FakeClock()
    raw_store = store.RecordingRawStore(raw, ACTOR, s3_client=s3, bucket="test")
    adapter = EtherscanAdapter("eth", raw_store, "test-etherscan-key-not-real",
                               http=EvmReplay(EVM_FIXTURES / "eth" / EVM_ADDR).client(),
                               clock=clock.clock, sleep=clock.sleep)
    snap = adapter.fetch_snapshot(EVM_ADDR)
    batch = adapter.fetch_transfers(EVM_ADDR)
    return store.save_lookup(data, ACTOR, snap, batch), adapter


def test_evm_lookup_is_stored_and_idempotent(conns):
    data, raw = conns
    s3 = FakeS3()
    first, adapter = evm_lookup_once(data, raw, s3)
    assert (first.transfers_seen, first.transfers_new) == (49, 49)
    assert count(data, "SELECT count(*) FROM raw_response") == adapter.request_count == 18
    assert count(data, "SELECT count(*) FROM transfer WHERE chain = 'eth' AND kind = 'internal'"
                       " AND event_index >= 33554432") == 1                  # encoded trace position fits
    assert count(data, "SELECT count(*) FROM transfer t JOIN raw_response r ON r.id = t.raw_response_id"
                       " JOIN source s ON s.id = r.source_id WHERE s.slug = 'etherscan'") == 49
    assert count(data, "SELECT count(*) FROM raw_response WHERE request_url LIKE '%%apikey%%'"
                       " OR request_params::text LIKE '%%apikey%%'") == 0
    assert count(data, "SELECT as_of_block FROM address_snapshot") == 26_451_986

    second, _ = evm_lookup_once(data, raw, s3)
    assert (second.transfers_seen, second.transfers_new) == (49, 0)
    assert count(data, "SELECT count(*) FROM transfer WHERE chain = 'eth'") == 49
    assert count(data, "SELECT event_log_verify() IS NULL") is True



# -- Etherscan terms: purging bodies by source, the source row, chain rows ----------

def _purge_module():
    import importlib.util
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "purge_source_bodies.py"
    spec = importlib.util.spec_from_file_location("purge_source_bodies", path)
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = mod                 # @dataclass looks its module up here
    spec.loader.exec_module(mod)
    return mod


def test_etherscan_bodies_can_be_purged_and_rows_stay(conns):
    from taw.adapters.base import RawMissing, bucket_key_for
    purge = _purge_module()
    data, raw = conns
    s3 = FakeS3()
    lookup_once(data, raw, s3)                                     # Tron
    evm_lookup_once(data, raw, s3)                                 # Ethereum
    # A Tron response whose bytes happen to equal an Etherscan one: one shared object.
    shared_body = next(v for k, v in s3.objects.items()
                       if count(data, "SELECT count(*) FROM raw_response r JOIN source s ON s.id = r.source_id"
                                      " WHERE s.slug = 'etherscan' AND r.sha256 = %s", k.split("/")[-1]))
    rec = store.RecordingRawStore(raw, ACTOR, s3_client=s3, bucket="test")
    shared_sha = rec.save("trongrid", "https://api.trongrid.io/test", None, 200, shared_body)

    rows_before = count(data, "SELECT count(*) FROM raw_response")
    tron_keys = {bucket_key_for(sha.strip()) for (sha,) in data.execute(
        "SELECT r.sha256 FROM raw_response r JOIN source s ON s.id = r.source_id WHERE s.slug = 'trongrid'")}
    objects_before = len(s3.objects)

    p = purge.plan(data, "etherscan")
    assert shared_sha in p.shared and shared_sha not in p.delete
    assert len(p.delete) + len(p.shared) == count(
        data, "SELECT count(DISTINCT sha256) FROM raw_response r JOIN source s ON s.id = r.source_id"
              " WHERE s.slug = 'etherscan'")
    assert purge.present(s3, "test", p.delete) == p.delete          # dry run reads only
    assert len(s3.objects) == objects_before

    deleted = purge.execute(data, s3, "test", p, "script:test")
    assert deleted == len(p.delete) and len(s3.objects) == objects_before - deleted
    assert tron_keys <= set(s3.objects)                             # other sources untouched
    assert bucket_key_for(shared_sha) in s3.objects                 # shared object kept
    assert count(data, "SELECT count(*) FROM raw_response") == rows_before   # every row and hash stays
    assert count(data, "SELECT count(*) FROM transfer WHERE chain = 'eth'") == 49
    with pytest.raises(RawMissing):
        BucketRawStore(data, s3_client=s3, bucket="test").body(p.delete[0])

    (payload,) = data.execute("SELECT payload FROM event_log WHERE action = 'raw_bodies_purged'").fetchone()
    assert payload["source"] == "etherscan" and sorted(payload["deleted_sha256"]) == sorted(p.delete)
    assert payload["kept_shared_sha256"] == [shared_sha]
    assert count(data, "SELECT event_log_verify() IS NULL") is True


def test_etherscan_source_row_records_the_terms(conns):
    data, _ = conns
    row = data.execute("SELECT license, attribution, terms_checked_on, resale_permitted FROM source"
                       " WHERE slug = 'etherscan'").fetchone()
    license, attribution, checked_on, resale = row
    assert checked_on == dt.date(2026, 11, 19) and resale is False
    assert attribution == "Powered by Etherscan.io APIs"
    assert "nothing from it may feed a public API" in license
    assert count(data, "SELECT count(*) FROM event_log WHERE actor = 'migration:0005_etherscan_terms'"
                       " AND entity = 'source'") == 1


def test_chains_outside_phase1_keep_their_rows(conns):
    data, _ = conns
    rows = dict(data.execute("SELECT id, evm_chain_id FROM chain WHERE id IN ('bsc', 'optimism', 'base')").fetchall())
    assert rows == {"bsc": 56, "optimism": 10, "base": 8453}
