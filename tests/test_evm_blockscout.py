"""Blockscout adapter tests, against a fake that answers like Blockscout: Etherscan-style
GETs with chain_id, JSON-RPC POSTs in place of module=proxy, a cap on logs per query,
internal rows with transactionHash and index, and status "2" for unfinished indexing.
Replay tests from a live run are added once a PRO API key exists (docs/STATUS.md)."""
import json

import httpx
import pytest

from taw.adapters.base import ChainAdapter, LocalRawStore
from taw.adapters.chains import evm_etherscan as es
from taw.adapters.chains.evm_blockscout import BlockscoutAdapter, BlockscoutIncomplete
from tests.evm_replay import FakeClock
from tests.test_evm_etherscan import A, B, C, TOKEN, FakeEtherscan, body, h, itx, log, ttx, tx

KEY = "proapi_test_key_not_real"


class FakeBlockscout(FakeEtherscan):
    def __init__(self, log_cap=1000, **kw):
        super().__init__(**kw)
        self.log_cap = log_cap
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
            if m == "eth_call":
                got = self.eth_calls.get((p[0]["to"], p[0]["data"][:10]), "0x")
                if isinstance(got, dict):
                    return body(jsonrpc="2.0", id=1, error=got)
                return self.rpc(got)
            raise AssertionError(f"unexpected rpc {m}")
        q = dict(httpx.QueryParams(request.url.query.decode()))
        assert "chainid" not in q and "chain_id" in q
        if q.get("module") == "proxy":
            return body(status="0", message="Unknown module", result=None)
        if q.get("action") == "getLogs" and not self.first:
            # Blockscout ignores page/offset and returns at most log_cap logs, oldest first.
            self.calls.append(q)
            lo, hi = int(q["fromBlock"]), int(q["toBlock"])
            rows = [lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi and lg["topics"][0] == q["topic0"]
                    and ("topic1" not in q or lg["topics"][1] == q["topic1"])
                    and ("topic2" not in q or lg["topics"][2] == q["topic2"])]
            rows.sort(key=lambda lg: (int(lg["blockNumber"], 16), int(lg["logIndex"], 16)))
            return body(status="1" if rows else "0", message="OK", result=rows[:self.log_cap])
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
    return BlockscoutAdapter(chain, LocalRawStore(tmp_path / "raw"), KEY, http=fake.client(),
                             clock=clock.clock, sleep=clock.sleep, **kw)


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
    assert '"method": "eth_call"' in index                          # the JSON-RPC body is the stored request


def test_internal_rows_use_blockscout_field_names(tmp_path):
    fake = FakeBlockscout()
    fake.lists["txlistinternal"] = [bs_internal(5, 950, C, A, 7, index=3), bs_internal(5, 950, C, A, 8, index=4)]
    batch = make(tmp_path, fake).fetch_transfers(A)
    assert [(r.tx_hash, r.event_index, r.amount_raw) for r in batch.transfers] == [(h(5), 4, 8), (h(5), 3, 7)]


def test_not_finished_is_incomplete_never_an_answer(tmp_path):
    fake = FakeBlockscout()
    fake.lists["txlist"] = [tx(1, 900, B, A, 5)]
    adapter = make(tmp_path, fake)
    adapter.head()
    fake.first = [body(status="2", message="Some internal transactions within this block range have not yet "
                                            "been processed", result=[])]
    # txlist is served normally, then txlistinternal answers status 2
    fake.first.insert(0, body(status="1", message="OK", result=[tx(1, 900, B, A, 5)]))
    with pytest.raises(BlockscoutIncomplete) as err:
        adapter.fetch_transfers(A)
    assert err.value.provider == "Blockscout" and "not yet been processed" in str(err.value)


def test_logs_are_paged_by_block_range_under_the_cap(tmp_path):
    fake = FakeBlockscout(log_cap=3)
    fake.logs = [log(200 + i, b, i, B, A, 1 + i) for i, b in enumerate([5, 6, 6, 7, 8, 9, 9])]
    adapter = make(tmp_path, fake)
    adapter.logs_window = 3
    batch = adapter.fetch_transfers(A)
    assert sorted(r.amount_raw for r in batch.transfers) == [1, 2, 3, 4, 5, 6, 7]    # every log, once
    starts = [c["fromBlock"] for c in fake.calls if c.get("action") == "getLogs" and "topic2" in c]
    assert starts == ["0", "6", "7", "9"]       # each full answer moves the edge to its last block


def test_a_block_with_more_logs_than_the_cap_is_an_error(tmp_path):
    """If one block fills the cap we cannot tell whether more exist: refuse, never guess."""
    fake = FakeBlockscout(log_cap=2)
    fake.logs = [log(300 + i, 6, i, B, A, 1) for i in range(3)]
    adapter = make(tmp_path, fake)
    adapter.logs_window = 2
    with pytest.raises(es.EtherscanError, match="cannot page past"):
        adapter.fetch_transfers(A)


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


def test_rate_limit_is_retried_then_succeeds(tmp_path):
    fake = FakeBlockscout()
    fake.first = [body(status="0", message="Too many requests. Increase limits now at https://dev.blockscout.com")]
    assert make(tmp_path, fake).head().number == 1000


def test_reverted_eth_call_is_not_an_error(tmp_path):
    fake = FakeBlockscout()
    fake.lists["tokentx"] = [ttx(9, 900, B, A, 1)]
    fake.eth_calls[(TOKEN, es.SEL_BALANCE_OF)] = {"code": 3, "message": "execution reverted"}
    snap = make(tmp_path, fake).fetch_snapshot(A)
    assert snap.token_balances == ()


def test_pinned_block_skips_the_tip(tmp_path):
    fake = FakeBlockscout()
    adapter = make(tmp_path, fake, at_block=777)
    assert adapter.head().number == 777
    assert not any(c.get("rpc") == "eth_blockNumber" for c in fake.calls)


def test_null_padded_topics_are_not_mistaken_for_nfts(tmp_path):
    """Blockscout answers topics as [topic0, from, to, null]. That is an ERC-20 transfer."""
    fake = FakeBlockscout()
    padded = log(400, 950, 7, B, A, 5)
    padded["topics"] = padded["topics"] + [None]
    nft = log(401, 951, 8, B, A, 0, contract="0x" + "9" * 40, token_id=12)
    fake.logs = [padded, nft]
    batch = make(tmp_path, fake).fetch_transfers(A)
    assert [(r.tx_hash, r.event_index, r.amount_raw) for r in batch.transfers] == [(h(400), 7, 5)]
