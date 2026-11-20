"""Blockscout adapter tests.

Replayed from the live cross-check on 2026-11-20 (tests/fixtures/evm/blockscout-rest-*),
plus a fake that answers like Blockscout: Etherscan-style GETs with chain_id,
JSON-RPC POSTs in place of module=proxy, the REST token-transfer lists, the
address summary, internal rows with transactionHash and index, and status "2"
for ranges it has not finished indexing.
"""
import datetime as dt
import json

import httpx
import pytest

from taw.adapters.base import FAILED, PASSED, ChainAdapter, LocalRawStore
from taw.adapters.chains import evm_blockscout as bs
from taw.adapters.chains import evm_etherscan as es
from taw.adapters.chains.evm_blockscout import BlockscoutAdapter, BlockscoutIncomplete
from tests.evm_replay import FIXTURES, FakeClock, Replay
from tests.test_evm_etherscan import A, B, C, TOKEN, FakeEtherscan, body, h, itx, log, ttx, tx

KEY = "proapi_test_key_not_real"
ADDR = "0xaab6417993d04a861f1f02c5c4bd2e9145d8f637"


@pytest.fixture(autouse=True)
def fresh_range_cache():
    bs.clear_range_cache()
    yield
    bs.clear_range_cache()


def rest_item(n, block, index, frm, to, value, contract=TOKEN, decimals="6", symbol="TKN"):
    return {"transaction_hash": h(n), "log_index": index, "block_number": block, "token_type": "ERC-20",
            "timestamp": dt.datetime.fromtimestamp(1_700_000_000 + block, dt.timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
            "from": {"hash": frm}, "to": {"hash": to}, "type": "token_transfer",
            "token": {"address_hash": contract, "symbol": symbol},
            "total": {"decimals": decimals, "value": str(value)}}


class FakeBlockscout(FakeEtherscan):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.rest: list[dict] = []                  # token-transfer items for the REST lists
        self.withdrawal_flag = False
        self.receipts: dict[str, list[dict]] = {}   # tx hash -> logs in its receipt
        self.requests: list[httpx.Request] = []

    def handler(self, request):
        self.requests.append(request)
        if request.method == "POST":
            rpc = json.loads(request.content)
            self.calls.append({"rpc": rpc["method"], "params": rpc["params"]})
            if self.first:
                return self.first.pop(0)
            m, p = rpc["method"], rpc["params"]
            if m == "eth_blockNumber":
                return self.rpc(hex(self.latest))
            if m == "eth_getBlockByNumber":
                return self.rpc({"number": p[0], "timestamp": hex(1_700_000_000 + int(p[0], 16))})
            if m == "eth_getCode":
                return self.rpc(self.code)
            if m == "eth_getTransactionCount":
                return self.rpc(hex(self.nonce))
            if m == "eth_getTransactionReceipt":
                logs = self.receipts.get(p[0], [])
                return self.rpc({"status": "0x1", "blockNumber": logs[0]["blockNumber"] if logs else "0x0",
                                 "logs": logs})
            if m == "eth_call":
                got = self.eth_calls.get((p[0]["to"], p[0]["data"][:10]), "0x")
                if isinstance(got, dict):
                    return body(jsonrpc="2.0", id=1, error=got)
                return self.rpc(got)
            raise AssertionError(f"unexpected rpc {m}")
        path = request.url.path
        q = dict(httpx.QueryParams(request.url.query.decode()))
        if "/api/v2/" in path:
            self.calls.append({"rest": path, **q})
            if self.first:
                return self.first.pop(0)
            rest_path = path.split("/api/v2", 1)[1]
            if rest_path.startswith("/addresses/") and rest_path.endswith("/token-transfers"):
                who = rest_path.split("/")[2]
                items = sorted((i for i in self.rest if who in (i["from"]["hash"], i["to"]["hash"])),
                               key=lambda i: (i["block_number"], i["log_index"]), reverse=True)
                page = int(q.get("page", 1))
                nxt = {"page": page + 1} if len(items) > page * 50 else None
                return body(items=items[(page - 1) * 50:page * 50], next_page_params=nxt)
            if rest_path.startswith("/transactions/"):
                txh = rest_path.split("/")[2]
                return body(items=[{**i, "timestamp": None} for i in self.rest if i["transaction_hash"] == txh],
                            next_page_params=None)
            if rest_path.startswith("/addresses/") and rest_path.count("/") == 2:
                return body(has_beacon_chain_withdrawals=self.withdrawal_flag)
            raise AssertionError(f"unexpected REST path {path}")
        assert "chainid" not in q and "chain_id" in q
        if q.get("module") == "proxy":
            return body(status="0", message="Unknown module", result=None)
        return super().handler(request)


def bs_internal(n, block, frm, to, value, index):
    """An internal row the way Blockscout returns it."""
    row = itx(n, block, frm, to, value)
    row["transactionHash"] = row.pop("hash")
    row.pop("traceId")
    row["index"] = str(index)
    row["callType"] = "call"
    return row


def make(tmp_path, fake, chain="eth", **kw):
    clock = FakeClock()
    adapter = BlockscoutAdapter(chain, LocalRawStore(tmp_path / "raw"), KEY, http=fake.client(),
                                clock=clock.clock, sleep=clock.sleep, **kw)
    adapter.tail_window, adapter.tail_span, adapter.range_step = 10, 400, 10
    return adapter


def with_token(fake, n, block, index, frm, to, value):
    """The same transfer in both of Blockscout's token lists."""
    fake.rest.append(rest_item(n, block, index, frm, to, value))
    fake.lists["tokentx"].append(ttx(n, block, frm, to, value))


# -- replay of the live cross-check ---------------------------------------------------

def replay_adapter(tmp_path, chain, address, block):
    clock = FakeClock()
    return BlockscoutAdapter(chain, LocalRawStore(tmp_path / "raw"), KEY,
                             http=Replay(FIXTURES / f"blockscout-rest-{chain}" / address).client(), at_block=block,
                             clock=clock.clock, sleep=clock.sleep)


def test_replay_ethereum(tmp_path):
    adapter = replay_adapter(tmp_path, "eth", ADDR, 26_452_607)
    snap = adapter.fetch_snapshot(ADDR)
    batch = adapter.fetch_transfers(ADDR)
    assert snap.nonce == 22 and snap.native_balance_raw == 428974286563892
    assert {k: sum(1 for t in batch.transfers if t.kind == k) for k in ("native", "internal", "token")} == \
        {"native": 7, "internal": 1, "token": 41}
    assert batch.internal_range == (23_397_898, 26_452_607)
    assert (batch.sent_tx_count, batch.fees_paid_raw, batch.withdrawals_raw) == (22, 474472886768108, 0)
    (check,) = batch.checks
    assert (check.name, check.status) == ("token_lists_agree", PASSED)
    assert check.details["rest_transfers"] == check.details["token_list_rows"] == 41
    first = next(t for t in batch.transfers if t.tx_hash.startswith("0x7758ec55"))
    assert first.event_index == 32                       # the transfer to this wallet, per both receipts


def test_replay_arbitrum(tmp_path):
    addr = "0x9be85deda2de03605af25fe6f33652559519e377"
    adapter = replay_adapter(tmp_path, "arbitrum", addr, 525_767_251)
    snap = adapter.fetch_snapshot(addr)
    batch = adapter.fetch_transfers(addr)
    assert snap.nonce == 26 and batch.sent_tx_count == 26
    assert sum(1 for t in batch.transfers if t.kind == "token") == 52       # two REST pages
    assert batch.internal_range == (524_324_867, 524_324_867)                # scattered gaps: one block


# -- the fake --------------------------------------------------------------------------

def test_satisfies_protocol_and_serves_the_free_tier_chains(tmp_path):
    for chain in ("eth", "arbitrum", "optimism"):
        adapter = make(tmp_path, FakeBlockscout(), chain=chain)
        assert isinstance(adapter, ChainAdapter) and adapter.source == "blockscout"
    for chain in ("bsc", "polygon", "base"):
        with pytest.raises(ValueError):
            make(tmp_path, FakeBlockscout(), chain=chain)


@pytest.mark.parametrize("status, text", [
    (402, '{"error":"Featured chain 1 requires one of the following plans: Builder, Business, Pro","source":"internal"}'),
    (404, '{"error":"Network not supported","source":"internal"}'),
])
def test_paid_or_missing_chain_fails_at_once(tmp_path, status, text):
    fake = FakeBlockscout()
    fake.first = [httpx.Response(status, content=text.encode())]
    with pytest.raises(es.EtherscanChainNotCovered) as err:
        make(tmp_path, fake).head()
    assert err.value.provider == "Blockscout" and err.value.chain_name == "Ethereum"
    assert len(fake.requests) == 1


def test_rpc_by_post_and_key_never_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", KEY)
    fake = FakeBlockscout()
    fake.lists["txlist"] = [tx(1, 900, B, A, 5)]
    with_token(fake, 2, 950, 4, B, A, 7)
    adapter = make(tmp_path, fake)
    snap = adapter.fetch_snapshot(A)
    adapter.fetch_transfers(A)
    assert snap.as_of_block == 1000 and snap.source_slug == "blockscout"
    posts = [r for r in fake.requests if r.method == "POST"]
    gets = [r for r in fake.requests if r.method == "GET"]
    assert posts and all(r.headers["authorization"] == f"Bearer {KEY}" for r in posts)
    assert all(r.url.path == "/1/json-rpc" for r in posts)
    assert gets and all(dict(r.url.params)["apikey"] == KEY for r in gets)
    assert not any(dict(r.url.params).get("module") == "proxy" for r in gets)
    index = (tmp_path / "raw" / "index.jsonl").read_text()
    assert KEY not in index and "authorization" not in index.lower() and "apikey" not in index
    assert '"method": "eth_call"' in index


def test_tokens_come_from_the_rest_list_with_their_log_index(tmp_path):
    fake = FakeBlockscout()
    with_token(fake, 5, 950, 17, B, A, 9)
    fake.rest.append(rest_item(6, 1001, 2, B, A, 9))      # after the snapshot block: left out
    batch = make(tmp_path, fake).fetch_transfers(A)
    assert [(t.tx_hash, t.event_index, t.token_decimals, t.token_symbol) for t in batch.transfers] == \
        [(h(5), 17, 6, "TKN")]
    assert not any(c.get("action") == "getLogs" for c in fake.calls)


def test_a_transfer_only_in_the_token_list_is_filled_from_the_receipt(tmp_path):
    """Blockscout's REST list leaves out tokens it judges to be spam. The token list has
    them; the receipt gives the real log index; the row is stored, no filtering."""
    fake = FakeBlockscout()
    with_token(fake, 5, 950, 17, B, A, 9)
    fake.lists["tokentx"].append(ttx(6, 960, B, A, 4))   # spam airdrop: not in the REST list
    fake.receipts[h(6)] = [log(6, 960, 67, B, A, 4)]
    batch = make(tmp_path, fake).fetch_transfers(A)
    (check,) = batch.checks
    assert (check.name, check.status) == ("token_lists_agree", PASSED)
    assert check.details == {"rest_transfers": 1, "token_list_rows": 2, "only_in_rest": 0, "only_in_token_list": 1,
                             "filled_from_receipts": 1, "unfilled": 0, "compared_above_block": None}
    spam = next(t for t in batch.transfers if t.tx_hash == h(6))
    assert (spam.event_index, spam.amount_raw, spam.token_decimals, spam.block_number) == (67, 4, 6, 960)


def test_a_gap_the_receipt_cannot_fill_marks_incomplete(tmp_path):
    fake = FakeBlockscout()
    with_token(fake, 5, 950, 17, B, A, 9)
    fake.lists["tokentx"].append(ttx(6, 960, B, A, 4))   # and the receipt has no such log
    (check,) = make(tmp_path, fake).fetch_transfers(A).checks
    assert (check.name, check.status) == ("token_lists_agree", FAILED)
    assert (check.details["filled_from_receipts"], check.details["unfilled"]) == (0, 1)


def test_a_transfer_only_in_the_rest_list_is_confirmed_from_the_receipt(tmp_path):
    fake = FakeBlockscout()
    fake.rest.append(rest_item(7, 970, 3, B, A, 5))        # the token list lacks it
    fake.receipts[h(7)] = [log(7, 970, 3, B, A, 5)]
    (check,) = make(tmp_path, fake).fetch_transfers(A).checks
    assert check.status == PASSED and check.details["filled_from_receipts"] == 1


def test_rest_list_pages_and_truncates_newest_first(tmp_path):
    fake = FakeBlockscout()
    for i in range(120):
        with_token(fake, 1000 + i, 100 + i, 0, B, A, 1)
    batch = make(tmp_path, fake).fetch_transfers(A, limit=60)
    assert batch.truncated is True
    assert [t.block_number for t in batch.transfers[:3]] == [219, 218, 217]
    oldest = batch.transfers[-1]
    assert (oldest.block_number, oldest.event_index) == (100, 0)            # the first transfer, added back
    assert oldest.block_time == dt.datetime.fromtimestamp(1_700_000_100, dt.timezone.utc)   # from the block
    assert any(c.get("rest", "").startswith("/1/api/v2/transactions/") for c in fake.calls)


def test_per_transaction_list_is_filtered_to_this_address(tmp_path):
    fake = FakeBlockscout()
    for i in range(5):
        with_token(fake, 2000 + i, 300 + i, 0, B, A, 1)
    fake.rest.append(rest_item(2000, 300, 1, B, C, 50))  # same transaction, someone else's transfer
    batch = make(tmp_path, fake).fetch_transfers(A, limit=2)
    oldest = [t for t in batch.transfers if t.tx_hash == h(2000)]
    assert [(t.to_address, t.event_index) for t in oldest] == [(A, 0)]


def test_internal_rows_use_blockscout_field_names(tmp_path):
    fake = FakeBlockscout()
    fake.lists["txlistinternal"] = [bs_internal(5, 950, C, A, 7, index=3), bs_internal(5, 950, C, A, 8, index=4)]
    batch = make(tmp_path, fake).fetch_transfers(A)
    assert [(r.tx_hash, r.event_index, r.amount_raw) for r in batch.transfers] == [(h(5), 4, 8), (h(5), 3, 7)]
    assert batch.internal_range == (0, 1000)


# -- the finished internal-transaction range ---------------------------------------------

def test_internal_read_only_over_the_finished_range(tmp_path):
    fake = FakeBlockscout()
    fake.unfinished = [(0, 400)]                          # old blocks not yet processed
    fake.lists["txlistinternal"] = [bs_internal(7, 300, C, A, 5, index=1), bs_internal(8, 700, C, A, 6, index=1)]
    batch = make(tmp_path, fake).fetch_transfers(A)
    lo, hi = batch.internal_range
    assert 400 < lo <= 410 and hi == 1000
    assert [t.amount_raw for t in batch.transfers if t.kind == "internal"] == [6]     # block 300: not read


def test_newest_unfinished_blocks_are_left_out_too(tmp_path):
    fake = FakeBlockscout()
    fake.unfinished = [(950, 2000)]                       # indexing lags the tip
    batch = make(tmp_path, fake).fetch_transfers(A)
    lo, hi = batch.internal_range
    assert lo == 0 and 940 <= hi < 950


def test_range_is_cached_per_chain(tmp_path):
    fake = FakeBlockscout()
    fake.unfinished = [(0, 400)]
    make(tmp_path / "a", fake).fetch_transfers(A)
    probes = sum(1 for c in fake.calls if c.get("action") == "txlistinternal" and c.get("offset") == "1")
    make(tmp_path / "b", fake).fetch_transfers(A)
    again = sum(1 for c in fake.calls if c.get("action") == "txlistinternal" and c.get("offset") == "1")
    assert probes > 3 and again == probes


def test_not_finished_in_the_read_range_is_never_an_answer(tmp_path):
    """The range was finished when probed, then a page answers status 2: stop, save nothing."""
    fake = FakeBlockscout()
    adapter = make(tmp_path, fake)
    adapter._internal_range = lambda address, head: (0, head)
    fake.unfinished = [(0, 10)]
    with pytest.raises(BlockscoutIncomplete) as err:
        adapter.fetch_transfers(A)
    assert err.value.provider == "Blockscout" and "not yet been processed" in str(err.value)


# -- nonce, withdrawals, errors ----------------------------------------------------------------

def test_nonce_sent_count_and_withdrawal_flag(tmp_path):
    fake = FakeBlockscout()
    fake.nonce = 3
    fake.lists["txlist"] = [tx(1, 900, A, B, 5), tx(2, 901, A, B, 0), tx(3, 902, A, B, 7, is_error="1")]
    adapter = make(tmp_path, fake)
    assert adapter.fetch_snapshot(A).nonce == 3
    batch = adapter.fetch_transfers(A)
    assert batch.sent_tx_count == 3                       # zero-value and failed ones count
    assert batch.withdrawals_raw == 0                     # has_beacon_chain_withdrawals false: one call


@pytest.mark.parametrize("message, error", [
    ("Too many requests. Increase limits now at https://dev.blockscout.com", es.EtherscanRateLimited),
    ("Proceed with API key or make a X402 payment to continue", es.EtherscanKeyRejected),
])
def test_blockscout_error_bodies(tmp_path, message, error):
    fake = FakeBlockscout()
    fake.first = [body(status="0", message=message)] * (6 if error is es.EtherscanRateLimited else 1)
    with pytest.raises(error) as err:
        make(tmp_path, fake).head()
    assert err.value.provider == "Blockscout" and err.value.key_env == "BLOCKSCOUT_API_KEY"


def test_server_errors_are_retried(tmp_path):
    fake = FakeBlockscout()
    fake.first = [httpx.Response(500, content=b'{"error":"Internal server error","source":"upstream"}')]
    assert make(tmp_path, fake).head().number == 1000


def test_reverted_eth_call_is_not_an_error(tmp_path):
    fake = FakeBlockscout()
    fake.lists["tokentx"] = [ttx(9, 900, B, A, 1)]
    fake.eth_calls[(TOKEN, es.SEL_BALANCE_OF)] = {"code": 3, "message": "execution reverted"}
    snap = make(tmp_path, fake).fetch_snapshot(A)
    assert snap.token_balances == ()


def test_eip7702_account_is_not_a_contract(tmp_path):
    fake = FakeBlockscout()
    fake.code = "0xef0100" + "c" * 40
    snap = make(tmp_path, fake).fetch_snapshot(A)
    assert snap.is_contract is False and snap.eip7702_delegate == "0x" + "c" * 40


def test_pinned_block_skips_the_tip(tmp_path):
    fake = FakeBlockscout()
    adapter = make(tmp_path, fake, at_block=777)
    assert adapter.head().number == 777
    assert not any(c.get("rpc") == "eth_blockNumber" for c in fake.calls)



def test_optimism_reads_the_nonce_at_the_regenesis(tmp_path):
    fake = FakeBlockscout()
    fake.nonce = 12
    snap = make(tmp_path, fake, chain="optimism").fetch_snapshot(A)
    reads = [c["params"][1] for c in fake.calls if c.get("rpc") == "eth_getTransactionCount"]
    assert reads == [hex(1002), "0x0"]           # the snapshot block (10 confirmations), then block 0
    assert (snap.nonce, snap.nonce_before_history, snap.history_start_block) == (12, 12, 0)


def test_replay_optimism_regenesis_and_spam_gap(tmp_path):
    """Live lookup of 2026-11-20 on an Optimism wallet active before the 2021 regenesis
    that received a spam token Blockscout's REST list leaves out."""
    from taw.adapters.registry import stablecoins_for
    from taw.reconcile import run_checks
    addr = "0x5cb8fc049a053df6a05fc40b00ac3e99568fa33e"
    clock = FakeClock()
    adapter = BlockscoutAdapter("optimism", LocalRawStore(tmp_path / "raw"), KEY,
                                http=Replay(FIXTURES / "blockscout-rest-optimism" / addr).client(),
                                clock=clock.clock, sleep=clock.sleep)
    snap = adapter.fetch_snapshot(addr)
    batch = adapter.fetch_transfers(addr)
    spam = next(t for t in batch.transfers if t.tx_hash.startswith("0x2daa322da6"))
    assert (spam.event_index, spam.token_contract) == (67, "0xeedc20720b5f91aeadac720e4793041366c70f40")
    result = run_checks(snap, batch, stablecoins_for("optimism"))
    by_name = {c.name: c for c in result.checks}
    assert by_name["tx_count"].status == PASSED
    assert by_name["tx_count"].details["transactions_before_history"] == 7
    assert by_name["token_lists_agree"].details["filled_from_receipts"] == 1
    assert result.history_incomplete is False
