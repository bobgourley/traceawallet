"""Bitcoin adapter tests.

Replayed from the live cross-check of 2026-11-20 for
bc1q4t93pz7k8xaqsckmh30we380xw8ggtq4xanvmp at height 976,672 (14 transactions,
154 rows; mempool.space and Blockstream agreed, and mempool.space's own address
page showed the same balance, totals, count and first and last activity), plus a
small fake Esplora server for cases a low-activity wallet does not have: a
coinbase input, unconfirmed and above-height transactions, a count mismatch,
truncation with the earliest transaction fetched, and 429 retries.
"""
import datetime as dt
import json
import pathlib

import httpx
import pytest

from taw.adapters.base import FAILED, NOT_RUN, PASSED, ChainAdapter, LocalRawStore
from taw.adapters.chains.btc_esplora import BLOCKSTREAM, MEMPOOL, BtcEsploraAdapter, EsploraRateLimited
from taw.adapters.chains.evm_crosscheck import compare
from taw.reconcile import run_checks
from tests.tron_replay import FakeClock

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "btc"
ADDR = "bc1q4t93pz7k8xaqsckmh30we380xw8ggtq4xanvmp"
HEIGHT = 976_672


class Replay:
    """Serve saved Esplora responses by URL path."""

    def __init__(self, folder: pathlib.Path):
        self.responses: dict[str, bytes] = {}
        self.calls: list[str] = []
        for line in (folder / "index.jsonl").read_text().splitlines():
            e = json.loads(line)
            if e["http_status"] == 200:
                path = httpx.URL(e["request_url"]).path
                self.responses[path] = (folder / "raw" / e["sha256"][:2] / e["sha256"]).read_bytes()

    def handler(self, request):
        self.calls.append(request.url.path)
        if request.url.path not in self.responses:
            raise AssertionError(f"no saved response for {request.url.path}")
        return httpx.Response(200, content=self.responses[request.url.path])

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def replay_adapter(tmp_path, provider=MEMPOOL, folder="mempool", **kw):
    clock = FakeClock()
    return BtcEsploraAdapter(LocalRawStore(tmp_path / folder), provider=provider, at_height=HEIGHT,
                             http=Replay(FIXTURES / folder / ADDR).client(), clock=clock.clock, sleep=clock.sleep,
                             **kw)


def run(adapter, address=ADDR, limit=1000):
    snap = adapter.fetch_snapshot(address)
    return snap, adapter.fetch_transfers(snap.address, limit=limit)


def checks(snap, batch):
    return {c.name: c for c in run_checks(snap, batch, ()).checks}


# -- replay of the live address -----------------------------------------------------

def test_satisfies_protocol(tmp_path):
    assert isinstance(replay_adapter(tmp_path), ChainAdapter)


def test_snapshot_and_rows(tmp_path):
    snap, batch = run(replay_adapter(tmp_path))
    assert (snap.as_of_block, snap.native_balance_raw, snap.native_decimals) == (HEIGHT, 1_733_405, 8)
    assert snap.as_of_time == dt.datetime(2026, 11, 20, 2, 49, 22, tzinfo=dt.timezone.utc)
    rows = batch.transfers
    assert len(rows) == 154 and len({r.tx_hash for r in rows}) == 14 and batch.truncated is False
    received = [r for r in rows if r.kind == "utxo_out" and r.to_address == ADDR]
    spent = [r for r in rows if r.kind == "utxo_in" and r.from_address == ADDR]
    assert (len(received), sum(r.amount_raw for r in received)) == (15, 5_660_986)
    assert (len(spent), sum(r.amount_raw for r in spent)) == (6, 3_927_581)
    for r in rows:                                  # one side per row, as migration 0003 requires
        assert (r.from_address is None) == (r.kind == "utxo_out") and (r.to_address is None) == (r.kind == "utxo_in")
        assert r.token_contract is None and r.token_decimals == 8 and r.token_symbol == "BTC"
        assert r.tx_hash == r.tx_hash.lower() and len(r.tx_hash) == 64
    first = min(rows, key=lambda r: r.block_number)
    assert first.tx_hash.startswith("aa408205d623") and first.block_time == dt.datetime(2026, 11, 15, 22, 54,
                                                                                     tzinfo=dt.timezone.utc)


def test_event_index_is_the_real_position_in_a_transaction_with_many_outputs(tmp_path):
    _, batch = run(replay_adapter(tmp_path))
    big = [r for r in batch.transfers if r.tx_hash.startswith("3dc39002bb69")]
    outs = sorted(r.event_index for r in big if r.kind == "utxo_out")
    ins = sorted(r.event_index for r in big if r.kind == "utxo_in")
    assert len(outs) == 41 and len(ins) == 8                # every input and output of the payout
    assert outs == list(range(41)) and ins == list(range(8))


def test_all_checks_pass_on_the_live_address(tmp_path):
    snap, batch = run(replay_adapter(tmp_path))
    c = checks(snap, batch)
    assert {n: x.status for n, x in c.items()} == {"native_reconciliation": PASSED, "tx_count": PASSED,
                                                    "other_party_rows_capped": PASSED}
    assert c["native_reconciliation"].details == {"received_sat": "5660986", "spent_sat": "3927581",
                                                  "ours_sat": "1733405", "provider_totals_sat": "1733405",
                                                  "unspent_outputs_sat": "1733405"}
    assert c["tx_count"].details["transactions_stored"] == c["tx_count"].details["provider_count"] == 14


def test_capped_transaction_is_recorded(tmp_path):
    """The real 41-output payout, with the other-party cap lowered to 10."""
    snap, batch = run(replay_adapter(tmp_path, other_cap=10))
    big = [r for r in batch.transfers if r.tx_hash.startswith("3dc39002bb69")]
    own = [r for r in big if ADDR in (r.from_address, r.to_address)]
    assert len(big) == len(own) + 10                       # every row of ours, 10 of the others
    c = checks(snap, batch)["other_party_rows_capped"]
    assert c.status == FAILED and c.marks_incomplete is False
    assert any(t.startswith("3dc39002bb69") for t in c.details["capped_transactions"])
    assert checks(snap, batch)["native_reconciliation"].status == PASSED     # our own rows are never capped


def test_mempool_and_blockstream_agree(tmp_path):
    sa, ba = run(replay_adapter(tmp_path / "m"))
    sb, bb = run(replay_adapter(tmp_path / "b", provider=BLOCKSTREAM, folder="blockstream"))
    assert sa.source_slug == "mempool-space" and sb.source_slug == "blockstream"
    assert compare(sa, ba, sb, bb) == []


def test_raw_responses_are_stored(tmp_path):
    adapter = replay_adapter(tmp_path)
    run(adapter)
    index = (tmp_path / "mempool" / "index.jsonl").read_text().splitlines()
    assert len(index) == adapter.request_count
    assert all(json.loads(line)["source"] == "mempool-space" for line in index)


# -- a fake Esplora server -----------------------------------------------------------

A = ADDR
OTHER = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
THIRD = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


def txid(n):
    return f"{n:064x}"


def tx(n, height, vin=(), vout=(), confirmed=True):
    return {"txid": txid(n), "status": {"confirmed": confirmed, "block_height": height if confirmed else None,
                                        "block_time": 1_700_000_000 + (height or 0) if confirmed else None},
            "vin": [{"txid": txid(n + 10_000), "vout": 0, "is_coinbase": False,
                     "prevout": {"scriptpubkey_address": a, "value": v}} for a, v in vin],
            "vout": [{"scriptpubkey_address": a, "value": v} for a, v in vout]}


def coinbase_tx(n, height, vout):
    return {"txid": txid(n), "status": {"confirmed": True, "block_height": height, "block_time": 1_700_000_000 + height},
            "vin": [{"is_coinbase": True, "prevout": None, "txid": "0" * 64, "vout": 4294967295}],
            "vout": [{"scriptpubkey_address": a, "value": v} for a, v in vout]}


class FakeEsplora:
    def __init__(self, tip=1000):
        self.tip = tip
        self.txs: list[dict] = []          # newest first, as Esplora lists them
        self.mempool_count = 0
        self.extra_count = 0               # added to the provider's tx_count
        self.utxos: list[dict] | None = None
        self.first: list[httpx.Response] = []
        self.calls: list[str] = []

    def stats(self):
        funded = sum(o["value"] for t in self.txs if t["status"]["confirmed"] for o in t["vout"]
                     if o.get("scriptpubkey_address") == A)
        spent = sum(i["prevout"]["value"] for t in self.txs if t["status"]["confirmed"] for i in t["vin"]
                    if i.get("prevout") and i["prevout"].get("scriptpubkey_address") == A)
        n = sum(1 for t in self.txs if t["status"]["confirmed"]) + self.extra_count
        return {"address": A, "chain_stats": {"funded_txo_sum": funded, "spent_txo_sum": spent, "tx_count": n},
                "mempool_stats": {"tx_count": self.mempool_count}}

    def handler(self, request):
        p = request.url.path.removeprefix("/api")
        self.calls.append(p)
        if self.first:
            return self.first.pop(0)
        if p == "/blocks/tip/height":
            return httpx.Response(200, text=str(self.tip))
        if p.startswith("/block-height/"):
            return httpx.Response(200, text=f"hash{p.split('/')[-1]}")
        if p.startswith("/block/hash"):
            h = int(p.removeprefix("/block/hash"))
            return httpx.Response(200, json={"height": h, "timestamp": 1_700_000_000 + h})
        if p == f"/address/{A}":
            return httpx.Response(200, json=self.stats())
        if p.startswith(f"/address/{A}/txs/chain"):
            confirmed = [t for t in self.txs if t["status"]["confirmed"]]
            rest = p.removeprefix(f"/address/{A}/txs/chain").strip("/")
            start = 0 if not rest else [t["txid"] for t in confirmed].index(rest) + 1
            return httpx.Response(200, json=confirmed[start:start + 25])
        if p == f"/address/{A}/utxo":
            if self.utxos is not None:
                return httpx.Response(200, json=self.utxos)
            spent_ids = {(i["txid"], i["vout"]) for t in self.txs for i in t["vin"] if not i.get("is_coinbase")}
            out = []
            for t in self.txs:
                if not t["status"]["confirmed"]:
                    continue
                for j, o in enumerate(t["vout"]):
                    if o.get("scriptpubkey_address") == A and (t["txid"], j) not in spent_ids:
                        out.append({"txid": t["txid"], "vout": j, "value": o["value"], "status": t["status"]})
            return httpx.Response(200, json=out)
        raise AssertionError(f"unexpected path {p}")

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def make(tmp_path, fake, **kw):
    clock = FakeClock()
    return BtcEsploraAdapter(LocalRawStore(tmp_path), http=fake.client(), clock=clock.clock, sleep=clock.sleep, **kw)


def test_coinbase_input_is_skipped(tmp_path):
    fake = FakeEsplora()
    fake.txs = [coinbase_tx(1, 900, [(A, 625_000_000), (OTHER, 1)])]
    snap, batch = run(make(tmp_path, fake))
    assert sorted((r.kind, r.event_index, r.to_address) for r in batch.transfers) == [
        ("utxo_out", 0, A), ("utxo_out", 1, OTHER)]                       # no utxo_in for the coinbase
    assert checks(snap, batch)["native_reconciliation"].status == PASSED


def test_op_return_output_is_skipped_and_positions_kept(tmp_path):
    fake = FakeEsplora()
    t = tx(2, 900, vin=[(OTHER, 10_000)], vout=[(A, 6_000), (None, 0), (OTHER, 3_000)])
    fake.txs = [t]
    _, batch = run(make(tmp_path, fake))
    assert sorted((r.kind, r.event_index) for r in batch.transfers) == [("utxo_in", 0), ("utxo_out", 0), ("utxo_out", 2)]


def test_unconfirmed_and_above_height_transactions_are_ignored_and_counted(tmp_path):
    fake = FakeEsplora(tip=1000)
    fake.txs = [tx(1, 990, vin=[(OTHER, 7_000)], vout=[(A, 5_000)])]
    fake.mempool_count = 2
    adapter = make(tmp_path, fake, at_height=980)                         # a block arrived after the snapshot
    fake.txs = [tx(1, 990, vin=[(OTHER, 7_000)], vout=[(A, 5_000)]),
                tx(2, 970, vin=[(OTHER, 9_000)], vout=[(A, 8_000)])]
    snap, batch = run(adapter)
    assert snap.native_balance_raw == 8_000                                # the 990 transaction is not counted
    assert {r.tx_hash for r in batch.transfers} == {txid(2)}
    c = checks(snap, batch)
    assert c["tx_count"].status == PASSED and c["native_reconciliation"].status == PASSED
    assert (c["tx_count"].details["unconfirmed_ignored"], c["tx_count"].details["above_snapshot_height_ignored"]) \
        == (2, 1)


def test_count_mismatch_is_flagged(tmp_path):
    fake = FakeEsplora()
    fake.txs = [tx(1, 900, vin=[(OTHER, 7_000)], vout=[(A, 5_000)])]
    fake.extra_count = 1                                                   # the provider counts one we never saw
    snap, batch = run(make(tmp_path, fake))
    result = run_checks(snap, batch, ())
    c = {x.name: x for x in result.checks}["tx_count"]
    assert c.status == FAILED and c.reason == "1 transactions were stored but the provider counts 2"
    assert result.history_incomplete is True


def test_balance_mismatch_with_unspent_outputs_is_flagged(tmp_path):
    fake = FakeEsplora()
    fake.txs = [tx(1, 900, vin=[(OTHER, 7_000)], vout=[(A, 5_000)])]
    fake.utxos = []                                                        # the provider lists no unspent output
    snap, batch = run(make(tmp_path, fake))
    c = checks(snap, batch)["native_reconciliation"]
    assert c.status == FAILED and c.details["unspent_outputs_sat"] == "0"


def test_truncated_history_fetches_the_earliest_and_is_never_reconciled(tmp_path):
    fake = FakeEsplora()
    fake.txs = [tx(n, 900 - n, vin=[(OTHER, 1_000)], vout=[(A, 1_000)]) for n in range(1, 61)]   # 60, 3 pages
    snap, batch = run(make(tmp_path, fake), limit=10)
    assert batch.truncated is True
    stored = sorted({r.block_number for r in batch.transfers}, reverse=True)
    assert stored[:10] == list(range(899, 889, -1)) and stored[-1] == 840    # newest 10, then the earliest
    c = checks(snap, batch)
    assert c["native_reconciliation"].status == NOT_RUN and c["tx_count"].status == NOT_RUN
    assert c["earliest_transaction"].status == PASSED and c["earliest_transaction"].details["txid"] == txid(60)


def test_earliest_out_of_reach_is_reported(tmp_path):
    fake = FakeEsplora()
    fake.txs = [tx(n, 900 - n, vin=[(OTHER, 1_000)], vout=[(A, 1_000)]) for n in range(1, 101)]
    snap, batch = run(make(tmp_path, fake, earliest_pages=1), limit=10)
    result = run_checks(snap, batch, ())
    c = {x.name: x for x in result.checks}["earliest_transaction"]
    assert c.status == FAILED and "first activity is unknown" in c.reason and result.history_incomplete is True


def test_backs_off_on_429(tmp_path):
    fake = FakeEsplora()
    fake.first = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(503)]
    clock = FakeClock()
    adapter = BtcEsploraAdapter(LocalRawStore(tmp_path), http=fake.client(), clock=clock.clock, sleep=clock.sleep)
    assert adapter.head().height == 1000
    assert any(7.0 <= s <= 7.5 for s in clock.sleeps)


def test_gives_up_after_repeated_429(tmp_path):
    fake = FakeEsplora()
    fake.first = [httpx.Response(429) for _ in range(6)]
    with pytest.raises(EsploraRateLimited) as err:
        make(tmp_path, fake).head()
    assert err.value.provider == "mempool.space"


def test_paced_at_two_a_second(tmp_path):
    fake = FakeEsplora()
    fake.txs = [tx(1, 900, vin=[(OTHER, 7_000)], vout=[(A, 5_000)])]
    clock = FakeClock()
    adapter = BtcEsploraAdapter(LocalRawStore(tmp_path), http=fake.client(), clock=clock.clock, sleep=clock.sleep)
    run(adapter)
    assert clock.now >= (adapter.request_count - 1) / 2.0 - 1e-9
