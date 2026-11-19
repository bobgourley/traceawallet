"""address_lookup handler and failure handling, with stand-ins for the database."""
import datetime as dt

import httpx
import pytest

from taw import jobs, store
from taw.adapters.base import LocalRawStore
from taw.adapters.chains.tron_trongrid import TronGridAdapter, TronGridBlocked, TronGridError
from taw.worker.__main__ import handle
from taw.worker.lookup import describe_failure, run_lookup
from tests.tron_replay import FIXTURES, FakeClock, Replay

ADDR = "TL7me8SQkNmieVngj9sPQGG2PSMENASHug"


class FakeStore:
    """Mimics store.fresh_snapshot / save_lookup, keyed like the real tables."""

    def __init__(self):
        self.fresh = None
        self.transfers = set()
        self.snapshots = []
        self.reused_events = []

    def fresh_snapshot(self, conn, chain, address):
        return self.fresh

    def record_reused_lookup(self, conn, actor, chain, address, fresh):
        self.reused_events.append((actor, chain, address, fresh.id))

    def save_lookup(self, conn, actor, snap, batch):
        keys = {(t.chain, t.tx_hash, t.kind, t.event_index, t.source_slug) for t in batch.transfers}
        new = len(keys - self.transfers)
        self.transfers |= keys
        self.snapshots.append((actor, snap.as_of_block))
        return store.LookupSaved(len(self.snapshots), 1, len(batch.transfers), new, 3)


class FakeConn:
    """Records the job-table updates."""

    def __init__(self):
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((sql, params))

        class R:
            def fetchone(self_inner):
                return None
        return R()

    def status_set(self):
        return [s.split("status = '")[1].split("'")[0] for s, _ in self.sql if "status = '" in s]


def replay_adapter_for(tmp_path):
    replay = Replay(FIXTURES / ADDR)
    clock = FakeClock()

    def factory(chain, raw_store):
        assert chain == "tron"
        return TronGridAdapter(raw_store, "test-key-not-real", http=replay.client(),
                               clock=clock.clock, sleep=clock.sleep)
    return factory


def job(payload, attempts=1, max_attempts=3, kind="address_lookup"):
    return jobs.Job(id=11, kind=kind, payload=payload, attempts=attempts, max_attempts=max_attempts)


def run(tmp_path, j, adapter_for, fake_store=None):
    conn, logs = FakeConn(), []
    status = handle(j, data_conn=conn, raw_store_factory=lambda: LocalRawStore(tmp_path / "raw"),
                    adapter_for=adapter_for, actor="worker:test", log=logs.append,
                    store_module=fake_store or FakeStore())
    return status, conn, logs


# -- the happy paths ------------------------------------------------------------

def test_lookup_saves_snapshot_and_transfers(tmp_path):
    fake = FakeStore()
    status, conn, logs = run(tmp_path, job({"chain": "tron", "address": ADDR}), replay_adapter_for(tmp_path), fake)
    assert status == "done" and conn.status_set() == ["done"]
    assert fake.snapshots == [("worker:test", 88_091_753)]
    assert len(fake.transfers) == 35
    assert "35 transfers (35 new)" in logs[-1]


def test_second_lookup_adds_nothing(tmp_path):
    fake = FakeStore()
    adapter_for = replay_adapter_for(tmp_path)
    run(tmp_path, job({"chain": "tron", "address": ADDR}), adapter_for, fake)
    status, _, logs = run(tmp_path, job({"chain": "tron", "address": ADDR}), adapter_for, fake)
    assert status == "done" and len(fake.transfers) == 35
    assert "(0 new)" in logs[-1]


def test_recent_fetch_is_reused_without_calling_the_provider(tmp_path):
    fake = FakeStore()
    fake.fresh = store.FreshSnapshot(5, 88_000_000, dt.datetime(2026, 11, 17, 20, 0, tzinfo=dt.timezone.utc), 35)

    def must_not_fetch(chain, raw_store):
        raise AssertionError("provider called despite a fresh snapshot")
    status, conn, logs = run(tmp_path, job({"chain": "tron", "address": ADDR}), must_not_fetch, fake)
    assert status == "done" and "reused snapshot 5" in logs[-1]
    assert fake.snapshots == []
    assert fake.reused_events == [("worker:test", "tron", ADDR, 5)]       # the completion is logged


def test_hex_address_in_payload_is_normalized(tmp_path):
    fake = FakeStore()
    from taw.adapters.chains.tron_address import to_hex
    status, _, _ = run(tmp_path, job({"chain": "tron", "address": to_hex(ADDR)}), replay_adapter_for(tmp_path), fake)
    assert status == "done"


# -- payload rules --------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"chain": "tron", "address": ADDR, "user_id": "user_123"},
    {"chain": "tron", "address": ADDR, "session": "abc"},
    {"chain": "tron"},
    ["tron", ADDR],
])
def test_payload_must_be_exactly_chain_and_address(tmp_path, payload):
    status, conn, _ = run(tmp_path, job(payload), replay_adapter_for(tmp_path))
    assert status == "failed"
    message = conn.sql[-1][1][0]
    assert message.startswith("This lookup cannot run: an address_lookup payload must be exactly chain and address")


def test_lookup_payload_holds_nothing_else():
    assert jobs.lookup_payload("tron", ADDR) == {"chain": "tron", "address": ADDR}


def test_bad_address_and_unsupported_chain_fail_at_once(tmp_path):
    status, conn, _ = run(tmp_path, job({"chain": "tron", "address": "TnotAnAddress"}), replay_adapter_for(tmp_path))
    assert status == "failed" and "cannot run" in conn.sql[-1][1][0]
    status, conn, _ = run(tmp_path, job({"chain": "btc", "address": "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"}),
                          replay_adapter_for(tmp_path))
    assert status == "failed" and "not built yet" in conn.sql[-1][1][0]


# -- failures and retries -------------------------------------------------------

def raising(exc):
    def factory(chain, raw_store):
        class A:
            def fetch_snapshot(self, address):
                raise exc
        return A()
    return factory


def test_403_fails_the_job_at_once(tmp_path):
    status, conn, _ = run(tmp_path, job({"chain": "tron", "address": ADDR}, attempts=1),
                          raising(TronGridBlocked("403", 403)))
    assert status == "failed" and conn.status_set() == ["failed"]
    assert "HTTP 403" in conn.sql[-1][1][0] and "Not retried" in conn.sql[-1][1][0]


def test_rate_limit_is_retried_with_growing_waits(tmp_path):
    waits = []
    for attempt in (1, 2):
        status, conn, logs = run(tmp_path, job({"chain": "tron", "address": ADDR}, attempts=attempt),
                                 raising(TronGridError("still 503", 503)))
        assert status == "queued" and conn.status_set() == ["queued"]
        waits.append(conn.sql[-1][1][0])
        assert "kept answering 503 (service unavailable)" in conn.sql[-1][1][1]
    assert waits == [30, 120]


def test_last_attempt_marks_failed_in_plain_language(tmp_path):
    status, conn, _ = run(tmp_path, job({"chain": "tron", "address": ADDR}, attempts=3, max_attempts=3),
                          raising(httpx.ConnectTimeout("timed out")))
    assert status == "failed"
    assert conn.sql[-1][1][0] == ("Failed after 3 attempts. Last error: Could not reach the data provider "
                                  "(ConnectTimeout).")


def test_job_reclaimed_too_often_is_failed(tmp_path):
    status, conn, _ = run(tmp_path, job({"chain": "tron", "address": ADDR}, attempts=4, max_attempts=3),
                          replay_adapter_for(tmp_path))
    assert status == "failed" and "stopped partway" in conn.sql[-1][1][0]


def test_unknown_job_kind_fails(tmp_path):
    status, conn, _ = run(tmp_path, job({}, kind="list_refresh"), replay_adapter_for(tmp_path))
    assert status == "failed" and "No handler" in conn.sql[-1][1][0]


def test_missing_api_key_fails_at_once(monkeypatch, tmp_path):
    from taw.adapters import registry
    monkeypatch.delenv("TRONGRID_API_KEY", raising=False)
    status, conn, _ = run(tmp_path, job({"chain": "tron", "address": ADDR}), registry.adapter_for)
    assert status == "failed" and "TRONGRID_API_KEY is not set" in conn.sql[-1][1][0]


def test_unexpected_errors_are_retryable():
    permanent, message = describe_failure(KeyError("block_header"))
    assert permanent is False and message == "Unexpected error (KeyError): 'block_header'"


def test_run_lookup_passes_actor_to_store(tmp_path):
    fake = FakeStore()
    out = run_lookup({"chain": "tron", "address": ADDR}, data_conn=None,
                     raw_store=LocalRawStore(tmp_path / "raw"), actor="worker:abc",
                     adapter_for=replay_adapter_for(tmp_path), store_module=fake)
    assert out.reused is False and fake.snapshots[0][0] == "worker:abc"


# -- EVM chains (Etherscan) -------------------------------------------------------

EVM_ADDR = "0xaab6417993d04a861f1f02c5c4bd2e9145d8f637"


def evm_adapter_for(http):
    from taw.adapters.chains.evm_etherscan import EtherscanAdapter
    clock = FakeClock()

    def factory(chain, raw_store):
        return EtherscanAdapter(chain, raw_store, "test-etherscan-key-not-real", http=http,
                                clock=clock.clock, sleep=clock.sleep)
    return factory


def test_eth_lookup_saves_snapshot_and_transfers(tmp_path):
    from tests.evm_replay import FIXTURES as EVM_FIXTURES, Replay as EvmReplay
    fake = FakeStore()
    http = EvmReplay(EVM_FIXTURES / "eth" / EVM_ADDR).client()
    status, conn, logs = run(tmp_path, job({"chain": "eth", "address": EVM_ADDR}), evm_adapter_for(http), fake)
    assert status == "done" and conn.status_set() == ["done"]
    assert fake.snapshots == [("worker:test", 26_451_986)]
    assert len(fake.transfers) == 49 and "49 transfers (49 new)" in logs[-1]


def test_chain_outside_the_free_plan_fails_at_once(tmp_path):
    body = (b'{"status":"0","message":"NOTOK","result":"Free API access is not supported for this chain. '
            b'Please upgrade your api plan for full chain coverage. https://etherscan.io/apis"}')
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=body)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    status, conn, _ = run(tmp_path, job({"chain": "bsc", "address": EVM_ADDR}, attempts=1), evm_adapter_for(http))
    assert status == "failed" and conn.status_set() == ["failed"]
    assert conn.sql[-1][1][0] == ("Etherscan's free plan does not cover BNB Smart Chain, so this lookup cannot run. "
                                  "Not retried. It needs a paid Etherscan plan.")
    assert len(seen) == 1                                          # one call, no retries


def test_bad_evm_address_fails_at_once(tmp_path):
    bad = "0xaaB6417993D04A861F1f02c5c4Bd2E9145D8F637"                 # one letter in the wrong case
    status, conn, _ = run(tmp_path, job({"chain": "eth", "address": bad}), evm_adapter_for(None))
    assert status == "failed" and "checksum mismatch" in conn.sql[-1][1][0]


def test_missing_etherscan_key_fails_at_once(monkeypatch, tmp_path):
    from taw.adapters import registry
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    status, conn, _ = run(tmp_path, job({"chain": "polygon", "address": EVM_ADDR}), registry.adapter_for)
    assert status == "failed" and "ETHERSCAN_API_KEY is not set" in conn.sql[-1][1][0]


@pytest.mark.parametrize("exc_name, permanent, words", [
    ("EtherscanKeyRejected", True, "rejected our API key"),
    ("EtherscanDailyLimit", True, "daily call allowance"),
    ("EtherscanRateLimited", False, "calling too fast"),
    ("EtherscanError", False, "Etherscan returned an error"),
])
def test_etherscan_failure_wording(exc_name, permanent, words):
    from taw.adapters.chains import evm_etherscan as es
    got_permanent, message = describe_failure(getattr(es, exc_name)("x"))
    assert got_permanent is permanent and words in message


def test_one_job_per_chain():
    assert jobs.lookup_payload("polygon", EVM_ADDR.upper().replace("0X", "0x")) == {"chain": "polygon", "address": EVM_ADDR}
