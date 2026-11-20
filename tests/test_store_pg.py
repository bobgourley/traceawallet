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
                              "as_of_block", "transfers_saved", "transfers_reused", "snapshot_reused",
                              "history_incomplete"}
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
    assert count(data, "SELECT count(*) FROM raw_response") == adapter.request_count == 20
    assert count(data, "SELECT count(*) FROM transfer WHERE chain = 'eth' AND kind = 'internal'"
                       " AND event_index >= 33554432") == 1                  # encoded trace position fits
    assert count(data, "SELECT count(*) FROM transfer t JOIN raw_response r ON r.id = t.raw_response_id"
                       " JOIN source s ON s.id = r.source_id WHERE s.slug = 'etherscan'") == 49
    assert count(data, "SELECT count(*) FROM raw_response WHERE request_url LIKE '%%apikey%%'"
                       " OR request_params::text LIKE '%%apikey%%'") == 0
    assert count(data, "SELECT as_of_block FROM address_snapshot") == 26_452_607

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


def test_every_source_shown_to_users_has_its_terms_checked(conns):
    data, _ = conns
    rows = dict(data.execute("SELECT slug, terms_checked_on FROM source").fetchall())
    for slug in ("blockscout", "etherscan", "trongrid", "mempool-space", "blockstream", "goplus", "defillama",
                 "ofac-sdn", "nbctf"):
        assert rows[slug] == dt.date(2026, 11, 19), slug
    resale = {s for (s,) in data.execute("SELECT slug FROM source WHERE resale_permitted")}
    assert resale <= {"ofac-sdn", "usdt-contract", "usdc-contract"}
    assert count(data, "SELECT count(*) FROM event_log WHERE actor = 'migration:0006_source_terms'") == 8


# -- reconciliation (0007) ----------------------------------------------------------

def test_reconciliation_is_stored_on_the_snapshot(conns):
    from taw.adapters.registry import stablecoins_for
    from taw.reconcile import reconcile
    data, raw = conns
    s3 = FakeS3()
    replay = Replay(FIXTURES / ADDR)
    clock = FakeClock()
    adapter = TronGridAdapter(store.RecordingRawStore(raw, ACTOR, s3_client=s3, bucket="test"), "test-key-not-real",
                              http=replay.client(), clock=clock.clock, sleep=clock.sleep)
    snap = adapter.fetch_snapshot(ADDR)
    batch = adapter.fetch_transfers(ADDR)
    recon = reconcile(snap, batch, stablecoins_for("tron"))
    saved = store.save_lookup(data, ACTOR, snap, batch, recon=recon)
    checks, incomplete, reasons = data.execute(
        "SELECT token_reconciliation, history_incomplete, incomplete_reasons FROM address_snapshot WHERE id = %s",
        (saved.snapshot_id,)).fetchone()
    assert incomplete is False and reasons == []
    assert checks == [{"contract": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "symbol": "USDT", "stablecoin": True,
                       "status": "reconciled", "balance_raw": "0", "net_flow_raw": "0", "reason": None}]
    (event,) = data.execute("SELECT payload FROM event_log WHERE action = 'lookup_completed'").fetchone()
    assert event["history_incomplete"] is False
    fresh = store.fresh_snapshot(data, "tron", ADDR)
    assert fresh.history_incomplete is False
    (checks, lo, hi) = data.execute("SELECT checks, internal_from_block, internal_to_block FROM address_snapshot"
                                    " WHERE id = %s", (saved.snapshot_id,)).fetchone()
    assert [(c["name"], c["status"]) for c in checks] == [
        ("stablecoin_reconciliation", "passed"), ("other_token_reconciliation", "not_run"),
        ("native_reconciliation", "not_run"), ("tx_count", "not_run")]
    assert (lo, hi) == (None, None)                            # Tron: internal transfers are not read


def test_every_check_and_the_internal_range_are_stored(conns):
    from taw.adapters.registry import stablecoins_for
    from taw.reconcile import run_checks
    data, raw = conns
    _, adapter = evm_lookup_once(data, raw, FakeS3())
    snap = adapter.fetch_snapshot(EVM_ADDR)                    # replayed again from the store's memo
    batch = adapter.fetch_transfers(EVM_ADDR)
    result = run_checks(snap, batch, stablecoins_for("eth"))
    saved = store.save_lookup(data, ACTOR, snap, batch, recon=result)
    checks, lo, hi, incomplete = data.execute(
        "SELECT checks, internal_from_block, internal_to_block, history_incomplete FROM address_snapshot"
        " WHERE id = %s", (saved.snapshot_id,)).fetchone()
    assert {c["name"]: c["status"] for c in checks} == {
        "stablecoin_reconciliation": "passed", "other_token_reconciliation": "not_run",
        "native_reconciliation": "passed", "tx_count": "passed", "token_list_matches_logs": "passed"}
    native = next(c for c in checks if c["name"] == "native_reconciliation")
    assert native["details"]["expected_raw"] == native["details"]["balance_raw"] == "428974286563892"
    assert (lo, hi, incomplete) == (0, 26_452_607, False)
    with pytest.raises(psycopg.errors.CheckViolation):          # the range must sit inside the snapshot block
        with data.transaction():
            data.execute("INSERT INTO address_snapshot (chain, address_id, as_of_block, as_of_time,"
                         " native_balance_raw, native_decimals, is_contract, tokens_truncated, transfer_count,"
                         " transfers_truncated, source_id, raw_response_id, internal_from_block, internal_to_block)"
                         " SELECT chain, address_id, as_of_block, as_of_time, native_balance_raw, native_decimals,"
                         " is_contract, tokens_truncated, transfer_count, transfers_truncated, source_id,"
                         " raw_response_id, 0, as_of_block + 1 FROM address_snapshot WHERE id = %s",
                         (saved.snapshot_id,))


def test_snapshot_without_reconciliation_stores_nulls_never_complete(conns):
    data, raw = conns
    saved, _ = lookup_once(data, raw, FakeS3())                  # the old call, no reconciliation
    row = data.execute("SELECT token_reconciliation, history_incomplete, incomplete_reasons FROM address_snapshot"
                       " WHERE id = %s", (saved.snapshot_id,)).fetchone()
    assert row == (None, None, None)
    with pytest.raises(psycopg.errors.CheckViolation):
        with data.transaction():
            data.execute("INSERT INTO address_snapshot (chain, address_id, as_of_block, as_of_time,"
                         " native_balance_raw, native_decimals, is_contract, tokens_truncated, transfer_count,"
                         " transfers_truncated, source_id, raw_response_id, history_incomplete)"
                         " SELECT chain, address_id, as_of_block, as_of_time, native_balance_raw, native_decimals,"
                         " is_contract, tokens_truncated, transfer_count, transfers_truncated, source_id,"
                         " raw_response_id, false FROM address_snapshot WHERE id = %s", (saved.snapshot_id,))


def test_goplus_display_rules_and_nbctf_out_of_phase1(conns):
    data, _ = conns
    attribution, license = data.execute("SELECT attribution, license FROM source WHERE slug = 'goplus'").fetchone()
    assert attribution.startswith("Powered by GoPlus") and "flagged by GoPlus" in attribution
    assert "never queried or shown for Bitcoin" in license
    (nbctf,) = data.execute("SELECT license FROM source WHERE slug = 'nbctf'").fetchone()
    assert "Out of Phase 1" in nbctf
    assert count(data, "SELECT count(*) FROM event_log WHERE actor = 'migration:0009_goplus_nbctf'") == 2


# -- Bitcoin -------------------------------------------------------------------------------

def test_btc_lookup_is_stored_and_idempotent(conns):
    from taw.adapters.chains.btc_esplora import BtcEsploraAdapter
    from taw.reconcile import run_checks
    from tests.test_btc_esplora import ADDR as BTC_ADDR, FIXTURES as BTC_FIXTURES, HEIGHT, Replay as BtcReplay
    data, raw = conns
    s3 = FakeS3()

    def once():
        clock = FakeClock()
        adapter = BtcEsploraAdapter(store.RecordingRawStore(raw, ACTOR, s3_client=s3, bucket="test"), at_height=HEIGHT,
                                    http=BtcReplay(BTC_FIXTURES / "mempool" / BTC_ADDR).client(),
                                    clock=clock.clock, sleep=clock.sleep)
        snap = adapter.fetch_snapshot(BTC_ADDR)
        batch = adapter.fetch_transfers(BTC_ADDR)
        return store.save_lookup(data, ACTOR, snap, batch, recon=run_checks(snap, batch, ()))

    first = once()
    assert (first.transfers_seen, first.transfers_new) == (154, 154)
    assert count(data, "SELECT count(*) FROM transfer WHERE chain = 'btc' AND kind = 'utxo_in'"
                       " AND to_address_id IS NULL AND from_address_id IS NOT NULL") == 43
    assert count(data, "SELECT count(*) FROM transfer WHERE chain = 'btc' AND kind = 'utxo_out'"
                       " AND from_address_id IS NULL AND to_address_id IS NOT NULL") == 111
    assert count(data, "SELECT count(*) FROM address WHERE chain = 'btc' AND address = %s", BTC_ADDR) == 1
    checks, inc = data.execute("SELECT checks, history_incomplete FROM address_snapshot WHERE id = %s",
                               (first.snapshot_id,)).fetchone()
    assert {c["name"]: c["status"] for c in checks} == {"native_reconciliation": "passed", "tx_count": "passed",
                                                        "other_party_rows_capped": "passed"}
    assert inc is False
    second = once()
    assert (second.transfers_seen, second.transfers_new) == (154, 0)
    assert count(data, "SELECT count(*) FROM transfer WHERE chain = 'btc'") == 154


# -- Blockscout's daily budget (0010) ------------------------------------------------------

def test_budget_ledger_splits_worker_and_scripts(conns):
    from taw.budget import BudgetExhausted, PostgresBudget
    data, _ = conns
    now = lambda: dt.datetime(2026, 11, 19, 12, tzinfo=dt.timezone.utc)   # noqa: E731
    worker = PostgresBudget(data, "worker", allowance=1_000, now=now)
    scripts = PostgresBudget(data, "scripts", allowance=1_000, now=now)
    for _ in range(15):                                   # 300 credits: scripts' 30%
        scripts.before_call(20)
        scripts.after_call(20, None)
    with pytest.raises(BudgetExhausted, match="kept for the production worker"):
        scripts.before_call(20)
    worker.before_call(20)
    worker.after_call(20, 680)                            # the provider's own count is kept
    assert worker.used() == {"worker": 20, "scripts": 300}
    assert count(data, "SELECT calls FROM provider_budget WHERE consumer = 'scripts'") == 15
    assert worker.remaining() == 680
    tomorrow = PostgresBudget(data, "scripts", allowance=1_000,
                              now=lambda: dt.datetime(2026, 11, 20, 0, 1, tzinfo=dt.timezone.utc))
    assert tomorrow.used() == {} and tomorrow.remaining() is None    # yesterday's count is not today's
    tomorrow.before_call(20)                              # a new day starts from nothing


def test_range_store_round_trip(conns):
    from taw.budget import PostgresRangeStore
    data, _ = conns
    ranges = PostgresRangeStore(data)
    at = dt.datetime(2026, 11, 19, 12, tzinfo=dt.timezone.utc)
    assert ranges.get("eth") is None
    ranges.put("eth", 400, 1_000, at)
    ranges.put("eth", 0, 1_050, at)
    assert ranges.get("eth") == (0, 1_050, at)
    ranges.drop("eth")
    assert ranges.get("eth") is None
    with pytest.raises(psycopg.errors.CheckViolation):
        ranges.put("eth", 10, 5, at)


def test_app_role_may_keep_the_budget(conns):
    data, _ = conns
    for table in ("provider_budget", "provider_status", "blockscout_internal_range"):
        can = {p: count(data, "SELECT has_table_privilege('taw_app', %s, %s)", table, p)
               for p in ("SELECT", "INSERT", "UPDATE")}
        assert can == {"SELECT": True, "INSERT": True, "UPDATE": True}


def test_deferred_job_keeps_its_attempts(conns):
    data, _ = conns
    job_id, _ = jobs.enqueue_lookup(data, "eth", EVM_ADDR)
    job = jobs.claim(data, "w1")
    assert job.attempts == 1
    jobs.defer_until(data, job, dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1), "used up")
    assert jobs.claim(data, "w2") is None                 # not due until the reset
    assert count(data, "SELECT attempts FROM job WHERE id = %s", job_id) == 0
    data.execute("UPDATE job SET run_after = now() WHERE id = %s", (job_id,))
    assert jobs.claim(data, "w2").attempts == 1           # the deferred try did not count


# -- prices (0011) ---------------------------------------------------------------------------

class StubPriceReader:
    """Saves a raw response per read, like the real reader, and answers ETH with a
    price and everything else as stale."""

    def __init__(self, raw_store):
        self.raw_store, self.reads = raw_store, []

    def read(self, asset, start, size):
        from decimal import Decimal
        from taw.adapters.prices import chainlink as cl
        self.reads.append((asset, start, size))
        n = len(self.reads)
        sha = self.raw_store.save("blockscout", "https://api.blockscout.com/1/json-rpc", {"read": n}, 200,
                                  f'{{"result": "{n}"}}'.encode())
        block_sha = self.raw_store.save("blockscout", "https://api.blockscout.com/v2/api", {"block": n}, 200,
                                        f'{{"result": {{"blockNumber": "{n}"}}}}'.encode())
        ok = asset == "ETH"
        return cl.PriceRead(asset, start, size, "ok" if ok else "stale", Decimal("1719.0477") if ok else None,
                            cl.FEEDS[asset], 1000 + n, 7, start - dt.timedelta(seconds=60), 60, sha, block_sha)


def price_records():
    from taw.adapters.base import TransferRecord
    now = dt.datetime(2026, 11, 20, 4, 40, tzinfo=dt.timezone.utc)

    def rec(chain, contract, hours_ago):
        return TransferRecord(chain=chain, tx_hash="0x" + "ab" * 32, kind="token" if contract else "native",
                              event_index=0, from_address="a", to_address="b", token_contract=contract,
                              token_symbol=None, token_decimals=6, amount_raw=1, block_number=1,
                              block_time=now - dt.timedelta(hours=hours_ago), source_slug="x", raw_sha256="0" * 64)
    return now, [rec("eth", None, 1), rec("eth", None, 300), rec("tron", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", 300)]


def test_prices_are_read_once_and_kept(conns):
    from taw import pricing
    data, raw = conns
    reader = StubPriceReader(store.RecordingRawStore(raw, ACTOR, s3_client=FakeS3(), bucket="test"))
    now, recs = price_records()
    first = pricing.fill(data, ACTOR, recs, reader, now=now)
    assert (first.needed, first.read, first.priced, first.reused) == (3, 3, 2, 0)
    rows = data.execute("SELECT asset, bucket_size, status::text, usd IS NULL, source_id = (SELECT id FROM source"
                        " WHERE slug = 'chainlink') FROM price ORDER BY id").fetchall()
    assert sorted(rows) == [("ETH", "day", "ok", False, True), ("ETH", "hour", "ok", False, True),
                            ("USDT", "day", "stale", True, True)]
    again = pricing.fill(data, ACTOR, recs, reader, now=now)
    assert (again.read, again.reused) == (0, 3) and len(reader.reads) == 3        # nothing read twice
    for statement in ("UPDATE price SET usd = 1", "DELETE FROM price", "TRUNCATE price"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            data.execute(statement)


def test_price_cap_leaves_the_oldest_for_later(conns):
    from taw import pricing
    data, raw = conns
    reader = StubPriceReader(store.RecordingRawStore(raw, ACTOR, s3_client=FakeS3(), bucket="test"))
    now, recs = price_records()
    out = pricing.fill(data, ACTOR, recs, reader, now=now, cap=1)
    assert (out.read, out.left_for_later) == (1, 2) and reader.reads[0][2] == "hour"   # newest first
    assert "left for a later lookup" in out.summary()


def test_price_rows_must_be_consistent(conns):
    data, _ = conns
    good = ("ETH", dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc), "day", "ok", 1719)
    bad = [("ETH", dt.datetime(2026, 11, 1, 3, tzinfo=dt.timezone.utc), "day", "ok", 1719),   # not a day start
           ("ETH", dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc), "day", "stale", 1719),   # price on a stale read
           ("ETH", dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc), "day", "ok", None)]      # ok with no price
    raw_id = count(data, "INSERT INTO raw_response (source_id, sha256, bucket_key, request_url, http_status, byte_size)"
                         " VALUES ((SELECT id FROM source WHERE slug = 'blockscout'), repeat('a', 64), 'k', 'u', 200, 1)"
                         " RETURNING id")
    sql = ("INSERT INTO price (asset, bucket_start, bucket_size, status, usd, feed_chain, feed_address, block_number,"
           " max_age_seconds, source_id, raw_response_id, block_raw_response_id) VALUES (%s, %s, %s, %s, %s, 'eth',"
           " '0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419', 1, 4500, (SELECT id FROM source WHERE slug = 'chainlink'),"
           " %s, %s)")
    data.execute(sql, (*good, raw_id, raw_id))
    for row in bad:
        with pytest.raises(psycopg.errors.CheckViolation):
            data.execute(sql, (*row, raw_id, raw_id))


# -- labels: OFAC (0012) -------------------------------------------------------------------------

def ofac_publication(raw):
    import pathlib
    from taw.adapters.labels import ofac
    body = (pathlib.Path(__file__).parent / "fixtures" / "ofac" / "SDN_sample.xml").read_bytes()
    raw_store = store.RecordingRawStore(raw, ACTOR, s3_client=FakeS3(), bucket="test")
    sha = raw_store.save("ofac-sdn", ofac.URL, None, 200, body)
    published, count, rows = ofac.parse(body)
    evm = ["arbitrum", "base", "bsc", "eth", "optimism", "polygon"]
    found, unmapped = ofac.claims(published, rows, evm, sha)
    from taw.adapters.labels.base import Publication
    return Publication("ofac-sdn", published, sha, found, unmapped=len(unmapped)), raw_store


def test_ofac_load_is_idempotent_and_matches_evm_without_case(conns):
    from taw import labels
    data, raw = conns
    pub, _ = ofac_publication(raw)
    first = labels.load_publication(data, ACTOR, pub)
    assert first.added == first.claims == len(pub.claims) and first.withdrawn == 0
    again = labels.load_publication(data, ACTOR, pub)
    assert (again.added, again.changed, again.withdrawn) == (0, 0, 0)
    assert count(data, "SELECT count(*) FROM label_load") == 2
    eth = next(c for c in pub.claims if c.chain == "eth")
    shouty = "0x" + eth.address[2:].upper()
    found = labels.current(data, "eth", [shouty])[shouty]
    assert found and all(f["source"] == "ofac-sdn" and f["category"] == "sanctioned" for f in found)
    assert found[0]["source_date"] == dt.date(2026, 11, 19)


def test_ofac_delisting_withdraws_and_relisting_supersedes(conns):
    import dataclasses
    from taw import labels
    data, raw = conns
    pub, _ = ofac_publication(raw)
    labels.load_publication(data, ACTOR, pub)
    gone = pub.claims[0]
    later = dataclasses.replace(pub, source_date=dt.date(2026, 11, 20), claims=pub.claims[1:])
    out = labels.load_publication(data, ACTOR, later)
    assert out.withdrawn == 1 and out.added == 0
    assert count(data, "SELECT count(*) FROM label_current WHERE claim_key = %s", gone.claim_key) == 0
    row = data.execute("SELECT status::text, source_date, supersedes_id IS NOT NULL FROM label"
                       " WHERE claim_key = %s ORDER BY id DESC LIMIT 1", (gone.claim_key,)).fetchone()
    assert row == ("withdrawn", dt.date(2026, 11, 20), True)
    back = labels.load_publication(data, ACTOR, pub)
    assert back.changed == 1                                       # relisted: supersedes the withdrawal
    assert count(data, "SELECT count(*) FROM label WHERE claim_key = %s", gone.claim_key) == 3
    renamed = dataclasses.replace(pub.claims[1], label=pub.claims[1].label + " (corrected)")
    out = labels.sync(data, ACTOR, "ofac-sdn", [renamed, *pub.claims[2:], pub.claims[0]],
                      raw_sha256=pub.raw_sha256, source_date=pub.source_date)
    assert (out.changed, out.withdrawn) == (1, 0)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        data.execute("DELETE FROM label_load")


def test_daily_job_is_queued_once_a_day(conns):
    data, _ = conns
    now = dt.datetime(2026, 11, 20, 5, tzinfo=dt.timezone.utc)
    assert jobs.ensure_daily(data, jobs.OFAC_REFRESH, now=now)
    assert not jobs.ensure_daily(data, jobs.OFAC_REFRESH, now=now)
    run_after = count(data, "SELECT run_after FROM job WHERE kind = %s", jobs.OFAC_REFRESH)
    assert run_after == now                                        # never ran before: at once
    data.execute("UPDATE job SET status = 'done' WHERE kind = %s", (jobs.OFAC_REFRESH,))
    assert not jobs.ensure_daily(data, jobs.OFAC_REFRESH, now=now)  # today's already done
    tomorrow = now + dt.timedelta(days=1)
    assert jobs.ensure_daily(data, jobs.OFAC_REFRESH, now=tomorrow)
    assert count(data, "SELECT max(run_after) FROM job WHERE kind = %s", jobs.OFAC_REFRESH) == \
        dt.datetime(2026, 11, 21, 23, tzinfo=dt.timezone.utc)


# -- labels: stablecoin freeze status (0013) -------------------------------------------------------

def test_freeze_status_labels_and_checks(conns):
    from taw.adapters.labels import freeze
    from taw.worker.enrich import save_freeze_checks
    data, raw = conns
    raw_store = store.RecordingRawStore(raw, ACTOR, s3_client=FakeS3(), bucket="test")
    addr = "0xc1b68434d0ef9a78e7f62e3b9850b70b3d0174bf"
    usdt, usdc = (next(c for c in freeze.CONTRACTS if c.chain == "eth" and c.token == t) for t in ("USDT", "USDC"))

    def checks(usdt_status, usdc_status, n):
        sha = [raw_store.save("blockscout", "https://api.blockscout.com/1/json-rpc", {"call": f"{n}-{i}"}, 200,
                              f'{{"n": "{n}-{i}"}}'.encode()) for i in range(2)]
        return [freeze.FreezeCheck(usdt, addr, usdt_status, 900 + n, sha[0]),
                freeze.FreezeCheck(usdc, addr, usdc_status, 900 + n, sha[1])]

    save_freeze_checks(data, ACTOR, "eth", addr, checks(True, False, 1))
    rows = data.execute("SELECT s.slug, l.category, l.status::text FROM label l JOIN source s ON s.id = l.source_id"
                        " ORDER BY l.id").fetchall()
    assert rows == [("usdt-contract", "frozen", "asserted")]
    assert count(data, "SELECT count(*) FROM label_check") == 2
    assert count(data, "SELECT result FROM label_check WHERE subject = %s", usdc.contract) == "not_frozen"

    save_freeze_checks(data, ACTOR, "eth", addr, checks(None, False, 2))        # no answer: nothing withdrawn
    assert count(data, "SELECT count(*) FROM label") == 1
    save_freeze_checks(data, ACTOR, "eth", addr, checks(False, False, 3))       # released
    assert data.execute("SELECT status::text, supersedes_id IS NOT NULL FROM label ORDER BY id DESC LIMIT 1"
                        ).fetchone() == ("withdrawn", True)
    assert count(data, "SELECT count(*) FROM label_current") == 0
    assert count(data, "SELECT count(*) FROM label_check") == 6
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        data.execute("UPDATE label_check SET result = 'x'")


# -- labels: GoPlus --------------------------------------------------------------------------------

def test_goplus_step_labels_checks_and_ledger(conns, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from taw.adapters import registry
    from taw.adapters.labels import goplus
    from taw.budget import PostgresBudget
    from taw.worker.enrich import goplus_step
    from tests.test_goplus import CLEAN
    data, raw = conns
    replies = [{**CLEAN, "phishing_activities": "1", "data_source": "SlowMist"}, CLEAN]

    def handler(request):
        return httpx.Response(200, json={"code": 1, "message": "ok", "result": replies[0]})

    def goplus_for(raw_store, *, state_conn=None):
        return goplus.GoPlus(raw_store, None, None, budget=PostgresBudget(state_conn, "worker", provider="goplus"),
                             http=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(registry, "goplus_for", goplus_for)
    raw_store = store.RecordingRawStore(raw, ACTOR, s3_client=FakeS3(), bucket="test")
    step = goplus_step(data, raw, ACTOR)
    snap = SimpleNamespace(chain="eth", address=EVM_ADDR)
    assert step(snap, None, raw_store) == "GoPlus: 1 flag(s) (phishing_activities)"
    assert count(data, "SELECT label FROM label_current") == 'flagged by GoPlus: phishing_activities = "1" (data source: SlowMist)'
    replies.pop(0)
    assert step(snap, None, raw_store) == "GoPlus: 0 flag(s)"
    assert count(data, "SELECT count(*) FROM label_current") == 0                     # withdrawn, not deleted
    assert count(data, "SELECT count(*) FROM label") == 2
    assert [r for (r,) in data.execute("SELECT result FROM label_check ORDER BY id")] == ["flags", "no_flags"]
    assert count(data, "SELECT calls FROM provider_budget WHERE provider = 'goplus' AND consumer = 'worker'") == 2
    assert step(SimpleNamespace(chain="btc", address="1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"), None, raw_store) \
        .startswith("GoPlus: not asked")


# -- applying labels to a lookup -----------------------------------------------------------------

def test_counterparties_get_only_local_labels(conns):
    from taw import labels
    from taw.adapters.labels.base import LabelClaim
    from tests.test_apply_labels import rec
    data, raw = conns
    me, payer, dest = EVM_ADDR, "0x" + "bb" * 20, "0x" + "cc" * 20
    raw_store = store.RecordingRawStore(raw, ACTOR, s3_client=FakeS3(), bucket="test")
    ofac_sha = raw_store.save("ofac-sdn", "https://sanctionslistservice.ofac.treas.gov/x", None, 200, b"sdn")
    gp_sha = raw_store.save("goplus", "https://api.gopluslabs.io/x", None, 200, b"gp")

    def claim(addr, cat, text, key, sha):
        return LabelClaim(chain="eth", address=addr, category=cat, label=text, claim_key=key,
                          source_date=dt.date(2026, 11, 19), raw_sha256=sha)
    labels.sync(data, ACTOR, "ofac-sdn", [claim(dest, "sanctioned", "listed by OFAC on the SDN list: X", "sdn:1", ofac_sha),
                                          claim(me, "sanctioned", "listed by OFAC on the SDN list: Y", "sdn:2", ofac_sha)],
                raw_sha256=ofac_sha, source_date=dt.date(2026, 11, 19))
    for addr in (me, payer):          # GoPlus said something about both, from earlier lookups
        labels.sync(data, ACTOR, "goplus", [claim(addr, "malicious_flag", 'flagged by GoPlus: mixer = "1"', "goplus:mixer",
                                                  gp_sha)], raw_sha256=gp_sha, source_date=None, only_address=("eth", addr))
    applied = labels.apply_to_lookup(data, "eth", me, [rec("eth", "native", payer, me), rec("eth", "native", me, dest)])
    assert sorted(f["source"] for f in applied.address) == ["goplus", "ofac-sdn"]           # every source
    assert applied.counterparties == {dest: {"roles": ["destination"], "labels": applied.counterparties[dest]["labels"]}}
    assert [f["source"] for f in applied.counterparties[dest]["labels"]] == ["ofac-sdn"]
    assert payer not in applied.counterparties                                              # its GoPlus flag is not local
    assert applied.summary() == "labels: 2 on the address; 1 of 2 first-hop address(es) carry an OFAC or exchange label"


# -- labels: exchange wallet lists (0014) ------------------------------------------------------------

def okx_publication(raw_store, month, keep=None, extra=()):
    import dataclasses
    import pathlib
    from taw.adapters.labels import exchanges
    from taw.adapters.labels.base import Publication
    body = (pathlib.Path(__file__).parent / "fixtures" / "okx" / "por_sample.zip").read_bytes()
    sha = raw_store.save("okx-por", exchanges.okx_url(month), None, 200, body + month.isoformat().encode())
    claims, detail = exchanges.parse_okx(body, month, sha)
    eth = next(c for c in claims if c.chain == "eth")
    claims = (claims if keep is None else claims[:keep]) + [dataclasses.replace(eth, address=a) for a in extra]
    return Publication("okx-por", month, sha, claims, detail=detail)


def test_okx_monthly_refresh_adds_and_withdraws_without_rewriting(conns):
    from taw import labels
    data, raw = conns
    raw_store = store.RecordingRawStore(raw, ACTOR, s3_client=FakeS3(), bucket="test")
    nov, dec = dt.date(2026, 11, 1), dt.date(2026, 12, 1)
    first = labels.load_publication(data, ACTOR, okx_publication(raw_store, nov))
    assert first.added == first.claims == 11
    new_addr = "0x" + "ab" * 20
    dec_pub = okx_publication(raw_store, dec, keep=10, extra=[new_addr])           # one gone, one new
    out = labels.load_publication(data, ACTOR, dec_pub)
    assert (out.added, out.changed, out.withdrawn) == (1, 0, 1)
    assert count(data, "SELECT count(*) FROM label") == 13                         # 11 + 1 new + 1 withdrawal
    kept = data.execute("SELECT label, source_date FROM label_current WHERE claim_key LIKE 'okx:%%'"
                        " ORDER BY id LIMIT 1").fetchone()
    assert "on 2026-11-01" in kept[0] and kept[1] == nov                           # first-listed date kept
    newest = labels.current(data, "eth", [new_addr])[new_addr]
    assert [f["label"][:44] for f in newest] == ["listed by OKX as its wallet on 2026-12-01 (t"]
    assert count(data, "SELECT count(*) FROM label_load WHERE source_id = (SELECT id FROM source"
                       " WHERE slug = 'okx-por')") == 2


def test_exchange_refresh_job(conns):
    from taw.adapters.labels.exchanges import NotPublished
    from taw.worker.lists import refresh_exchanges
    data, raw = conns
    raw_store = store.RecordingRawStore(raw, ACTOR, s3_client=FakeS3(), bucket="test")

    class NoOkx:
        def load(self, month):
            raise NotPublished("none")
    first = refresh_exchanges(data_conn=data, raw_store=raw_store, actor=ACTOR, today=dt.date(2026, 11, 20),
                              okx=NoOkx())
    assert first.startswith("exchange file: ") and "OKX 2026-11: not published yet" in first
    assert count(data, "SELECT count(*) FROM label_current WHERE source_id = (SELECT id FROM source"
                       " WHERE slug = 'exchange-por')") > 50
    again = refresh_exchanges(data_conn=data, raw_store=raw_store, actor=ACTOR, today=dt.date(2026, 11, 21),
                              okx=NoOkx())
    assert again.startswith("exchange file unchanged")

    class FixtureOkx:
        def load(self, month):
            return okx_publication(raw_store, month)
    loaded = refresh_exchanges(data_conn=data, raw_store=raw_store, actor=ACTOR, today=dt.date(2026, 11, 22),
                               okx=FixtureOkx())
    assert "OKX 2026-11: 11 claim(s): 11 added" in loaded
    assert "OKX 2026-11: already loaded" in refresh_exchanges(data_conn=data, raw_store=raw_store, actor=ACTOR,
                                                              today=dt.date(2026, 11, 23), okx=FixtureOkx())
