"""Tron adapter tests. Replayed from responses saved during a live run on
2026-11-17 for TL7me8SQkNmieVngj9sPQGG2PSMENASHug (35 transfers: 10 TRX, 25 USDT),
plus small hand-built responses for paging, retries and edge cases."""
import datetime as dt
import json
from decimal import Decimal

import httpx
import pytest

from taw.adapters.base import AddressSnapshot, ChainAdapter, LocalRawStore, TransferBatch
from taw.adapters.chains.tron_address import to_hex
from taw.adapters.chains.tron_trongrid import TronGridAdapter, TronGridBlocked, TronGridError
from tests.tron_replay import FIXTURES, FakeClock, Replay

ADDR = "TL7me8SQkNmieVngj9sPQGG2PSMENASHug"
USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


@pytest.fixture
def replay():
    return Replay(FIXTURES / ADDR)


def make_adapter(tmp_path, http, clock=None, key="test-key-not-real"):
    clock = clock or FakeClock()
    return TronGridAdapter(LocalRawStore(tmp_path / "raw"), key, http=http,
                           clock=clock.clock, sleep=clock.sleep)


# -- replay of the real address -------------------------------------------------

def test_adapter_satisfies_protocol(tmp_path, replay):
    assert isinstance(make_adapter(tmp_path, replay.client()), ChainAdapter)


def test_snapshot(tmp_path, replay):
    snap = make_adapter(tmp_path, replay.client()).fetch_snapshot(ADDR)
    assert isinstance(snap, AddressSnapshot)
    assert snap.chain == "tron" and snap.address == ADDR
    assert snap.as_of_block == 88_091_753
    assert snap.as_of_time == dt.datetime(2026, 11, 17, 21, 1, 3, tzinfo=dt.timezone.utc)
    assert snap.native_balance_raw == 0 and snap.native_symbol == "TRX"
    assert snap.token_balances == ()
    assert snap.is_contract is False
    assert snap.truncated is False
    assert len(snap.raw_sha256) == 3                      # account, getcontract, getblock


def test_snapshot_accepts_hex_input(tmp_path, replay):
    snap = make_adapter(tmp_path, replay.client()).fetch_snapshot(to_hex(ADDR))
    assert snap.address == ADDR


def test_transfers(tmp_path, replay):
    batch = make_adapter(tmp_path, replay.client()).fetch_transfers(ADDR)
    assert isinstance(batch, TransferBatch)
    assert batch.truncated is False
    t = batch.transfers
    assert len(t) == 35

    native = [x for x in t if x.kind == "native"]
    token = [x for x in t if x.kind == "token"]
    assert len(native) == 10 and len(token) == 25

    # Same figures Tronscan shows for this address.
    def total(rows, inbound):
        return sum((x.amount() for x in rows if (x.to_address if inbound else x.from_address) == ADDR), Decimal(0))
    assert total(native, True) == Decimal("14.000013")
    assert total(native, False) == Decimal("13.310013")
    assert total(token, True) == Decimal("6527.458759")
    assert total(token, False) == Decimal("6527.458759")

    # Newest first; first and last activity.
    times = [x.block_time for x in t]
    assert times == sorted(times, reverse=True)
    assert t[0].tx_hash.startswith("f589fff5289e") and t[0].block_number == 87_394_243
    assert t[-1].tx_hash.startswith("c63601828864") and t[-1].block_number == 87_242_835


def test_token_records_are_complete(tmp_path, replay):
    adapter = make_adapter(tmp_path, replay.client())
    batch = adapter.fetch_transfers(ADDR)
    store_root = tmp_path / "raw"
    for x in batch.transfers:
        assert len(x.tx_hash) == 64 and x.tx_hash == x.tx_hash.lower()
        assert x.from_address.startswith("T") and x.to_address.startswith("T")
        assert (store_root / "raw" / x.raw_sha256[:2] / x.raw_sha256).exists()
        if x.kind == "token":
            assert x.token_contract == USDT                  # identified by contract
            assert x.token_decimals == 6
            assert x.event_index >= 0 and x.block_number > 87_000_000
        else:
            assert x.token_contract is None and x.event_index == 0 and x.token_decimals == 6


def test_truncation_keeps_newest(tmp_path, replay):
    full = make_adapter(tmp_path / "a", Replay(FIXTURES / ADDR).client()).fetch_transfers(ADDR)
    small = make_adapter(tmp_path / "b", replay.client()).fetch_transfers(ADDR, limit=5)
    assert small.truncated is True and small.limit == 5
    assert small.transfers == full.transfers[:5]
    # Events are fetched only for the token transfers that were kept.
    kept_token_txs = {x.tx_hash for x in small.transfers if x.kind == "token"}
    assert sum(1 for c in replay.calls if c.endswith("/events")) == len(kept_token_txs)


def test_every_response_is_stored_and_key_is_not(tmp_path, replay, monkeypatch):
    monkeypatch.setenv("TRONGRID_API_KEY", "test-key-not-real")
    adapter = make_adapter(tmp_path, replay.client(), key="test-key-not-real")
    adapter.fetch_snapshot(ADDR)
    adapter.fetch_transfers(ADDR)
    index = (tmp_path / "raw" / "index.jsonl").read_text()
    assert len(index.splitlines()) == adapter.request_count == 30
    assert "test-key-not-real" not in index


def test_second_run_reuses_saved_transaction_events(tmp_path, replay):
    store = LocalRawStore(tmp_path / "raw")
    clock = FakeClock()
    first = TronGridAdapter(store, "k" * 20, http=replay.client(), clock=clock.clock, sleep=clock.sleep)
    batch1 = first.fetch_transfers(ADDR)
    assert (first.request_count, first.reused_count) == (27, 0)

    replay.calls.clear()
    second = TronGridAdapter(store, "k" * 20, http=replay.client(), clock=clock.clock, sleep=clock.sleep)
    batch2 = second.fetch_transfers(ADDR)
    assert (second.request_count, second.reused_count) == (2, 25)   # only the two list pages
    assert not any(c.endswith("/events") for c in replay.calls)
    assert batch2.transfers == batch1.transfers


def test_missing_saved_body_is_fetched_again(tmp_path, replay):
    store = LocalRawStore(tmp_path / "raw")
    clock = FakeClock()
    TronGridAdapter(store, None, http=replay.client(), clock=clock.clock, sleep=clock.sleep).fetch_transfers(ADDR)
    one_events_body = next(p for p in (tmp_path / "raw" / "raw").rglob("*") if p.is_file()
                           and b'"event_name"' in p.read_bytes())
    one_events_body.unlink()
    again = TronGridAdapter(store, None, http=replay.client(), clock=clock.clock, sleep=clock.sleep)
    again.fetch_transfers(ADDR)
    assert (again.request_count, again.reused_count) == (3, 24)
    assert one_events_body.exists()                                    # saved again, same name


def test_saved_empty_events_are_fetched_again(tmp_path):
    tx = "f" * 64
    store = LocalRawStore(tmp_path / "raw")
    store.save("trongrid", f"https://api.trongrid.io/v1/transactions/{tx}/events",
               {"query": {"only_confirmed": "true"}}, 200, b'{"success": true, "data": []}')
    http, seen = scripted([
        json_response({"success": True, "data": [], "meta": {}}),
        json_response({"success": True, "data": [trc20_item(tx, 3000, 9)], "meta": {}}),
        json_response(events(tx, 780, [(0, "Transfer", positional(A, B, 9))])),
    ])
    clock = FakeClock()
    adapter = TronGridAdapter(store, None, http=http, clock=clock.clock, sleep=clock.sleep)
    batch = adapter.fetch_transfers(A)
    assert adapter.reused_count == 0 and len(seen) == 3
    assert batch.transfers[0].block_number == 780


def test_rate_stays_under_ten_per_second(tmp_path, replay):
    clock = FakeClock()
    adapter = make_adapter(tmp_path, replay.client(), clock=clock)
    adapter.fetch_transfers(ADDR)
    # 27 requests at no more than 9 a second take at least 26/9 seconds.
    assert adapter.request_count == 27
    assert clock.now >= 26 / 9 - 1e-9


# -- hand-built responses -------------------------------------------------------

def json_response(data, status=200, headers=None):
    return httpx.Response(status, content=json.dumps(data).encode(), headers=headers or {})


def scripted(responses):
    """An httpx client that returns the given responses in order."""
    queue = list(responses)
    seen = []

    def handler(request):
        seen.append(request)
        return queue.pop(0)
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


BLOCK = {"block_header": {"raw_data": {"number": 100, "timestamp": 1_700_000_000_000}}}


def test_backs_off_on_429_and_503(tmp_path):
    http, seen = scripted([
        json_response({"Error": "The key exceeds the frequency limit"}, 429),
        json_response({}, 503, {"Retry-After": "7"}),
        json_response(BLOCK),
    ])
    clock = FakeClock()
    adapter = make_adapter(tmp_path, http, clock=clock)
    assert adapter.latest_block()[0] == 100
    assert len(seen) == 3
    assert any(1.0 <= s <= 1.5 for s in clock.sleeps)       # first backoff: 1 s plus jitter
    assert any(7.0 <= s <= 7.5 for s in clock.sleeps)       # Retry-After honoured
    statuses = [json.loads(l)["http_status"] for l in (tmp_path / "raw" / "index.jsonl").read_text().splitlines()]
    assert statuses == [429, 503, 200]                       # error responses are stored too


def test_gives_up_after_max_attempts(tmp_path):
    http, seen = scripted([json_response({}, 503) for _ in range(6)])
    with pytest.raises(TronGridError) as err:
        make_adapter(tmp_path, http).latest_block()
    assert err.value.status == 503 and len(seen) == 6


def test_stops_at_once_on_403(tmp_path):
    http, seen = scripted([json_response({}, 403), json_response(BLOCK)])
    with pytest.raises(TronGridBlocked):
        make_adapter(tmp_path, http).latest_block()
    assert len(seen) == 1


def test_key_sent_only_in_header(tmp_path):
    http, seen = scripted([json_response(BLOCK)])
    make_adapter(tmp_path, http, key="test-key-not-real").latest_block()
    req = seen[0]
    assert req.headers["TRON-PRO-API-KEY"] == "test-key-not-real"
    assert "test-key-not-real" not in str(req.url) and b"test-key-not-real" not in req.content


A = "TL7me8SQkNmieVngj9sPQGG2PSMENASHug"
B = "TLgdKbLgoFKGbUMmtTH4RS632pRzeuhE4X"
TOKEN = USDT


def trc20_item(tx, ts, value, frm=A, to=B, decimals=6):
    return {"transaction_id": tx, "block_timestamp": ts, "from": frm, "to": to, "type": "Transfer",
            "value": str(value), "token_info": {"address": TOKEN, "symbol": "USDT", "decimals": decimals}}


def events(tx, block, evs):
    return {"success": True, "data": [
        {"block_number": block, "contract_address": TOKEN, "event_index": i, "event_name": name,
         "result": res, "transaction_id": tx} for i, name, res in evs]}


def positional(frm, to, value):
    return {"0": "0x" + to_hex(frm)[2:], "1": "0x" + to_hex(to)[2:], "2": str(value)}


def test_trc20_paging_and_log_index(tmp_path):
    tx1, tx2 = "a" * 64, "b" * 64
    http, seen = scripted([
        json_response({"success": True, "data": [], "meta": {}}),                       # no TRX txs
        json_response({"success": True, "data": [trc20_item(tx1, 2000, 5)],
                       "meta": {"fingerprint": "next-page"}}),
        json_response({"success": True, "data": [trc20_item(tx2, 1000, 7)], "meta": {}}),
        # tx1: an Approval first, then two transfers; ours is the second, named _from/_to/_value
        json_response(events(tx1, 501, [
            (0, "Approval", positional(A, B, 5)),
            (1, "Transfer", {**positional(B, A, 5), "_from": "x"}),
            (2, "Transfer", positional(A, B, 5)),
        ])),
        json_response(events(tx2, 400, [(0, "Transfer", positional(A, B, 7))])),
    ])
    batch = make_adapter(tmp_path, http).fetch_transfers(A)
    assert [r.tx_hash for r in batch.transfers] == [tx1, tx2]
    assert (batch.transfers[0].event_index, batch.transfers[0].block_number) == (2, 501)
    assert (batch.transfers[1].event_index, batch.transfers[1].block_number) == (0, 400)
    assert seen[2].url.params["fingerprint"] == "next-page"
    assert batch.truncated is False


def test_identical_transfers_in_one_transaction_stay_separate(tmp_path):
    """Two transfers with the same token, from, to and amount in one transaction
    are two movements of value. Each must keep its own log index."""
    tx = "c" * 64
    http, seen = scripted([
        json_response({"success": True, "data": [], "meta": {}}),
        json_response({"success": True, "data": [trc20_item(tx, 3000, 9), trc20_item(tx, 3000, 9)], "meta": {}}),
        json_response(events(tx, 777, [
            (0, "Approval", positional(A, B, 9)),
            (3, "Transfer", positional(A, B, 9)),
            (5, "Transfer", positional(A, B, 9)),
        ])),
    ])
    batch = make_adapter(tmp_path, http).fetch_transfers(A)
    assert len(batch.transfers) == 2
    assert sorted(r.event_index for r in batch.transfers) == [3, 5]
    assert {r.block_number for r in batch.transfers} == {777}
    assert sum(1 for r in seen if r.url.path.endswith("/events")) == 1     # one events call per transaction
    keys = {(r.chain, r.tx_hash, r.kind, r.event_index) for r in batch.transfers}
    assert len(keys) == 2                                                  # distinct under the table's unique key


def test_identical_transfers_without_matching_events_stay_separate(tmp_path):
    """Fallback path: when no event matches, positions within the transaction are used."""
    tx = "d" * 64
    http, _ = scripted([
        json_response({"success": True, "data": [], "meta": {}}),
        json_response({"success": True, "data": [trc20_item(tx, 3000, 9), trc20_item(tx, 3000, 9)], "meta": {}}),
        json_response(events(tx, 778, [(0, "Approval", positional(A, B, 9))])),
    ])
    batch = make_adapter(tmp_path, http).fetch_transfers(A)
    # numbered after the highest real log index (0), never reusing it
    assert sorted(r.event_index for r in batch.transfers) == [1, 2]


def test_fallback_index_never_collides_with_a_matched_one(tmp_path):
    """One transfer matches log index 1; the other has no event and falls back.
    The fallback must not also be 1, or the database would merge them."""
    tx = "e" * 64
    http, _ = scripted([
        json_response({"success": True, "data": [], "meta": {}}),
        json_response({"success": True, "data": [trc20_item(tx, 3000, 9), trc20_item(tx, 3000, 4)], "meta": {}}),
        json_response(events(tx, 779, [(0, "Approval", positional(A, B, 9)),
                                       (1, "Transfer", positional(A, B, 9))])),
    ])
    batch = make_adapter(tmp_path, http).fetch_transfers(A)
    by_amount = {r.amount_raw: r.event_index for r in batch.transfers}
    assert by_amount == {9: 1, 4: 2}


def test_failed_and_non_transfer_trx_transactions_are_skipped(tmp_path):
    def tx(txid, kind, ret):
        return {"txID": txid, "blockNumber": 9, "block_timestamp": 1000, "ret": [{"contractRet": ret}],
                "raw_data": {"contract": [{"type": kind, "parameter": {"value": {
                    "owner_address": to_hex(A), "to_address": to_hex(B), "amount": 1}}}]}}
    http, _ = scripted([
        json_response({"success": True, "data": [
            tx("1" * 64, "TransferContract", "SUCCESS"),
            tx("2" * 64, "TransferContract", "OUT_OF_ENERGY"),
            tx("3" * 64, "TriggerSmartContract", "SUCCESS"),
            tx("4" * 64, "TransferAssetContract", "SUCCESS"),
        ], "meta": {}}),
        json_response({"success": True, "data": [], "meta": {}}),
    ])
    batch = make_adapter(tmp_path, http).fetch_transfers(A)
    assert [r.tx_hash for r in batch.transfers] == ["1" * 64]


def test_snapshot_reads_token_metadata_and_contract_flag(tmp_path):
    symbol_abi = ("20".rjust(64, "0") + "4".rjust(64, "0") + "55534454".ljust(64, "0"))
    http, _ = scripted([
        json_response({"success": True, "data": [{"balance": 2_500_000, "trc20": [{TOKEN: "1234567"}]}]}),
        json_response({"contract_address": "41aa", "bytecode": "60"}),
        json_response({"result": {"result": True}, "constant_result": ["6".rjust(64, "0")]}),
        json_response({"result": {"result": True}, "constant_result": [symbol_abi]}),
        json_response(BLOCK),
    ])
    snap = make_adapter(tmp_path, http).fetch_snapshot(A)
    assert snap.is_contract is True
    assert snap.native_balance() == Decimal("2.5")
    (bal,) = snap.token_balances
    assert (bal.contract, bal.amount_raw, bal.decimals, bal.symbol) == (TOKEN, 1234567, 6, "USDT")
    assert bal.amount() == Decimal("1.234567")
    assert snap.as_of_block == 100 and len(snap.raw_sha256) == 5


def test_unknown_address_has_zero_balance(tmp_path):
    http, _ = scripted([json_response({"success": True, "data": []}), json_response({}), json_response(BLOCK)])
    snap = make_adapter(tmp_path, http).fetch_snapshot(A)
    assert snap.native_balance_raw == 0 and snap.token_balances == () and snap.is_contract is False


def test_success_false_is_an_error(tmp_path):
    http, _ = scripted([json_response({"success": False, "error": "bad address"})])
    with pytest.raises(TronGridError, match="bad address"):
        make_adapter(tmp_path, http).fetch_snapshot(A)
