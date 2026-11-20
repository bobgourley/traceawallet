"""Busy Bitcoin addresses, timeouts with a Blockstream fallback, and the steps a job
records (live testing, 2026-11-20: bc1q8yja...syvz, 86,570 transactions, failed three
times on a mempool.space history page that stopped answering after 60 s)."""
import datetime as dt
import json

import httpx
import pytest

from taw import jobs
from taw.adapters.base import LocalRawStore
from taw.adapters.chains import btc_esplora as be
from taw.worker import lookup
from tests import report_fixtures as rf
from tests.tron_replay import FakeClock

BUSY = "bc1q8yja3gw33ngd8aunmfr4hj820adc9nlsv0syvz"


def esplora(route, tmp_path, provider=be.MEMPOOL, **kw):
    seen = []

    def handler(request):
        seen.append((request.url.path, request.extensions.get("timeout")))
        return route(request)
    clock = FakeClock()
    a = be.BtcEsploraAdapter(LocalRawStore(tmp_path), provider=provider, clock=clock.clock, sleep=clock.sleep,
                             http=httpx.Client(transport=httpx.MockTransport(handler)), **kw)
    return a, seen


def summary(tx_count, funded=5_000, spent=4_000):
    return {"address": BUSY, "chain_stats": {"tx_count": tx_count, "funded_txo_sum": funded, "spent_txo_sum": spent},
            "mempool_stats": {"tx_count": 0}}


def chain_api(tx_count, hang_history=False):
    def route(request):
        p = request.url.path.split("/api", 1)[1]
        if p == "/blocks/tip/height":
            return httpx.Response(200, text="976794")
        if p == "/block-height/976794":
            return httpx.Response(200, text="00" * 32)
        if p.startswith("/block/"):
            return httpx.Response(200, json={"height": 976794, "timestamp": 1_795_000_000})
        if p == f"/address/{BUSY}":
            return httpx.Response(200, json=summary(tx_count))
        if "/txs/chain" in p:
            if hang_history:
                raise httpx.ReadTimeout("no answer", request=request)
            return httpx.Response(200, json=[])
        if p.endswith("/utxo"):
            return httpx.Response(200, json=[])
        raise AssertionError(p)
    return route


def test_each_kind_of_request_has_its_own_time_limit():
    assert be.timeout_for("/blocks/tip/height").read == 10
    assert be.timeout_for(f"/address/{BUSY}").read == 15
    assert be.timeout_for(f"/address/{BUSY}/txs/chain/abc").read == 30
    assert be.timeout_for(f"/address/{BUSY}/utxo").read == 45
    assert be.timeout_for("/tx/" + "ab" * 32).read == 20
    assert be.timeout_for("/blocks/tip/height").connect == 5


def test_requests_carry_their_limit(tmp_path):
    a, seen = esplora(chain_api(3), tmp_path)
    a.fetch_snapshot(BUSY)
    limits = {path: t["read"] for path, t in seen}
    assert limits[f"/api/address/{BUSY}"] == 15 and limits["/api/blocks/tip/height"] == 10


def test_a_busy_address_gets_a_summary_and_its_history_is_never_requested(tmp_path):
    a, seen = esplora(chain_api(86_570), tmp_path)
    snap = a.fetch_snapshot(BUSY)
    batch = a.fetch_transfers(BUSY)
    assert snap.native_balance_raw == 1_000 and batch.transfers == () and batch.truncated
    assert not any("/txs/chain" in p or p.endswith("/utxo") for p, _ in seen)
    (why,) = [c for c in batch.checks if c.name == "history_not_read"]
    assert why.status == "failed" and why.marks_incomplete
    assert why.details["provider_tx_count"] == 86_570 and why.details["received_sat"] == "5000"
    assert "86,570 transactions" in why.reason


def test_the_limit_for_a_summary_is_where_the_first_transaction_stops_being_reachable():
    assert be.SUMMARY_ONLY_ABOVE == be.EARLIEST_PAGES * be.PAGE == 10_000


def test_a_timeout_names_the_provider_the_request_and_the_wait(tmp_path):
    a, _ = esplora(chain_api(500, hang_history=True), tmp_path)
    with pytest.raises(be.EsploraTimeout) as err:
        a.fetch_snapshot(BUSY)
    e = err.value
    assert e.provider == "mempool.space" and "/txs/chain" in e.path and e.waited == 30
    permanent, message = lookup.describe_failure(e)
    assert not permanent and "mempool.space did not answer within 30 s" in message
    assert lookup.failure_kind(e) == "provider_slow"


def test_a_mempool_timeout_falls_back_to_blockstream(tmp_path):
    class Store:
        pass
    calls = []

    def adapter_for(chain, raw_store):
        a, seen = esplora(chain_api(500, hang_history=True), tmp_path)
        good, _ = esplora(chain_api(3), tmp_path / "bs", provider=be.BLOCKSTREAM)
        a.fallback = lambda: (calls.append("fallback"), good)[1]
        return a

    saved = {}

    class FakeStore:
        @staticmethod
        def fresh_snapshot(conn, chain, address):
            return None

        @staticmethod
        def save_lookup(conn, actor, snap, batch, recon=None):
            saved["source"] = snap.source_slug
            from taw.store import LookupSaved
            return LookupSaved(7, 1, len(batch.transfers), len(batch.transfers), 1)
    steps = []
    out = lookup.run_lookup({"chain": "btc", "address": BUSY}, data_conn=None, raw_store=None, actor="t",
                            adapter_for=adapter_for, store_module=FakeStore, progress=steps.append)
    assert calls == ["fallback"] and saved["source"] == "blockstream"
    assert "did not answer within 30 s" in out.message and "read from Blockstream instead" in out.message
    assert steps == ["reading_balances", "reading_balances", "reading_transfers", "checking", "checking_lists"]


def test_blockstream_keeps_the_height_already_read(tmp_path):
    a, _ = esplora(chain_api(3), tmp_path)
    a.head()
    b = a.fallback()
    assert b.source == "blockstream" and b._at_height == 976794
    assert b.fallback() is None


@pytest.mark.parametrize("exc,kind", [
    (be.EsploraRateLimited("x", 429), "provider_busy"),
    (be.EsploraError("x", 500), "provider_error"),
    (httpx.ConnectError("x"), "provider_unreachable"),
    (httpx.ReadTimeout("x"), "provider_slow"),
    (RuntimeError("x"), "service_problem"),
])
def test_failures_have_plain_kinds(exc, kind):
    assert lookup.failure_kind(exc) == kind


def test_the_worker_records_its_steps():
    from taw.worker.__main__ import handle

    class Conn:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=()):
            self.sql.append((sql, params))

            class R:
                def fetchone(self_inner):
                    return None
            return R()
    conn = Conn()

    def run_lookup(payload, **kw):
        for s in ("reading_balances", "reading_transfers", "checking", "checking_lists"):
            kw["progress"](s)
        return lookup.Outcome("ok", reused=False, snapshot_id=5, chain="tron", address="T")
    import taw.worker.__main__ as w
    original = w.lookup.run_lookup
    w.lookup.run_lookup = run_lookup
    try:
        status = handle(jobs.Job(id=3, kind=jobs.LOOKUP, payload={}, attempts=1, max_attempts=3), data_conn=conn,
                        raw_store_factory=lambda: None, adapter_for=None, log=lambda m: None,
                        after=lambda c, a, o: "report built")
    finally:
        w.lookup.run_lookup = original
    assert status == "done"
    steps = [p[0] for s, p in conn.sql if "SET step" in s]
    assert steps == ["reading_balances", "reading_transfers", "checking", "checking_lists", "building_report"]


def test_the_summary_report_is_marked_as_a_summary():
    from taw.report.build import build
    from taw.report.render import render_text
    inp, *_ = rf.case_input("busy-btc-summary-only")
    data = build(inp)
    assert data["summary_only"]["tx_count"] == 86_570 and data["summary"]["history"] == "summary_only"
    text = render_text(data)
    assert "This is a summary only. This address is too busy to read in detail: it has 86,570 transactions" in text
    assert "no transfers were read" in text and "== Where funds went next ==" not in text
    assert "complete as far as we can check" not in text
    assert json.dumps(data["activity"]["assets"]) == "[]"
