"""Etherscan adapter tests.

Replayed from responses saved during the live cross-check on 2026-11-20 for
0xaab6417993d04a861f1f02c5c4bd2e9145d8f637 on Ethereum at block 26,452,607
(49 transfers: 7 native, 1 internal, 41 USDC/USDT), plus a small fake Etherscan
for paging, errors and edge cases. The live figures were checked against
etherscan.io (docs/STATUS.md) and against Blockscout (tests/test_evm_crosscheck.py).
"""
import datetime as dt
import json
from decimal import Decimal
from urllib.parse import parse_qsl

import httpx
import pytest

from taw.adapters.base import AddressSnapshot, ChainAdapter, LocalRawStore, TransferBatch
from taw.adapters.chains import evm_etherscan as es
from taw.adapters.chains.evm_etherscan import EtherscanAdapter, trace_index
from tests.evm_replay import FIXTURES, FakeClock, Replay

ADDR = "0xaab6417993d04a861f1f02c5c4bd2e9145d8f637"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
KEY = "test-etherscan-key-not-real"


@pytest.fixture
def replay():
    return Replay(FIXTURES / "eth" / ADDR)


def make_adapter(tmp_path, http, clock=None, key=KEY, chain="eth", **kw):
    clock = clock or FakeClock()
    return EtherscanAdapter(chain, LocalRawStore(tmp_path / "raw"), key, http=http,
                            clock=clock.clock, sleep=clock.sleep, **kw)


# -- replay of the real address ---------------------------------------------------

def test_adapter_satisfies_protocol(tmp_path, replay):
    assert isinstance(make_adapter(tmp_path, replay.client()), ChainAdapter)


def test_snapshot(tmp_path, replay):
    snap = make_adapter(tmp_path, replay.client()).fetch_snapshot(ADDR)
    assert isinstance(snap, AddressSnapshot)
    assert snap.chain == "eth" and snap.address == ADDR
    assert snap.as_of_block == 26_452_607
    assert snap.as_of_time == dt.datetime(2026, 11, 20, 0, 5, 59, tzinfo=dt.timezone.utc)
    assert snap.native_symbol == "ETH" and snap.native_decimals == 18
    assert snap.native_balance() == Decimal("0.000428974286563892")    # what etherscan.io shows
    assert snap.token_balances == ()                                    # USDC and USDT spent to zero
    assert snap.is_contract is False and snap.truncated is False
    assert snap.nonce == 22 and snap.eip7702_delegate is None
    # balance (Multicall3), block number, block, code, nonce, token page, 9 stablecoin balanceOf calls
    assert len(snap.raw_sha256) == 15


def test_snapshot_accepts_checksummed_input(tmp_path, replay):
    snap = make_adapter(tmp_path, replay.client()).fetch_snapshot("0xaaB6417993D04A861F1f02C5c4Bd2E9145D8F637")
    assert snap.address == ADDR


def test_transfers(tmp_path, replay):
    batch = make_adapter(tmp_path, replay.client()).fetch_transfers(ADDR)
    assert isinstance(batch, TransferBatch) and batch.truncated is False
    t = batch.transfers
    assert len(t) == 49
    kinds = {k: [x for x in t if x.kind == k] for k in ("native", "internal", "token")}
    assert {k: len(v) for k, v in kinds.items()} == {"native": 7, "internal": 1, "token": 41}

    def total(rows, inbound, contract=None):
        return sum((x.amount() for x in rows if x.token_contract == contract
                    and (x.to_address if inbound else x.from_address) == ADDR), Decimal(0))
    # Same figures etherscan.io shows for this address.
    assert total(kinds["token"], True, USDC) == total(kinds["token"], False, USDC) == Decimal("9280.39")
    assert total(kinds["token"], True, USDT) == total(kinds["token"], False, USDT) == Decimal("2569.517887")
    assert total(kinds["internal"], True) == Decimal("0.25022341")
    assert sum(1 for x in kinds["token"] if x.token_contract == USDC and x.to_address == ADDR) == 17
    assert sum(1 for x in kinds["token"] if x.token_contract == USDC and x.from_address == ADDR) == 16

    # Newest first; first and last activity; first ETH in is etherscan.io's "Funded By".
    assert [x.block_number for x in t] == sorted((x.block_number for x in t), reverse=True)
    assert t[0].tx_hash.startswith("0x4056399ba3f2") and t[0].block_number == 26_442_338
    assert t[-1].tx_hash.startswith("0x7758ec55e6d2") and t[-1].block_number == 25_889_432
    first_eth = min((x for x in kinds["native"] if x.to_address == ADDR), key=lambda x: x.block_number)
    assert first_eth.from_address == "0xc5267f13b9b4ba7bb73bdd53a2a3c944733a2961"
    assert first_eth.tx_hash.startswith("0x36d76b222b02")


def test_records_are_complete_and_lower_case(tmp_path, replay):
    batch = make_adapter(tmp_path, replay.client()).fetch_transfers(ADDR)
    for x in batch.transfers:
        assert x.tx_hash == x.tx_hash.lower() and len(x.tx_hash) == 66
        assert x.from_address == x.from_address.lower() and x.to_address == x.to_address.lower()
        assert (tmp_path / "raw" / "raw" / x.raw_sha256[:2] / x.raw_sha256).exists()
        if x.kind == "token":
            assert x.token_contract in (USDC, USDT) and x.token_decimals == 6
        else:
            assert x.token_contract is None and x.token_decimals == 18
    internal = next(x for x in batch.transfers if x.kind == "internal")
    assert internal.event_index >= 32 ** 5                          # an encoded trace position


def test_token_event_index_is_the_real_log_index(tmp_path, replay):
    batch = make_adapter(tmp_path, replay.client()).fetch_transfers(ADDR)
    logs = {}
    for e in map(json.loads, (FIXTURES / "eth" / ADDR / "index.jsonl").read_text().splitlines()):
        if e["request_params"]["query"]["action"] == "getLogs":
            body = json.loads((FIXTURES / "eth" / ADDR / "raw" / e["sha256"][:2] / e["sha256"]).read_bytes())
            for log in body["result"]:
                logs[log["transactionHash"]] = int(log["logIndex"], 16)
    for x in batch.transfers:
        if x.kind == "token":
            assert x.event_index == logs[x.tx_hash]


def test_key_is_never_stored(tmp_path, replay, monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", KEY)
    adapter = make_adapter(tmp_path, replay.client())
    adapter.fetch_snapshot(ADDR)
    adapter.fetch_transfers(ADDR)
    assert all(q.get("apikey") == KEY for q in replay.calls)           # it was sent ...
    index = (tmp_path / "raw" / "index.jsonl").read_text()
    assert len(index.splitlines()) == adapter.request_count == 20
    assert KEY not in index and "apikey" not in index                  # ... and never stored
    for body in (tmp_path / "raw" / "raw").rglob("*"):
        if body.is_file():
            assert KEY.encode() not in body.read_bytes()


def test_rate_stays_under_three_per_second(tmp_path, replay):
    clock = FakeClock()
    adapter = make_adapter(tmp_path, replay.client(), clock=clock)
    adapter.fetch_snapshot(ADDR)
    adapter.fetch_transfers(ADDR)
    assert adapter.request_count == 20
    assert clock.now >= 19 / 2.5 - 1e-9          # 2.5 a second; Etherscan's free plan allows 3


# -- a fake Etherscan -------------------------------------------------------------

TOPIC = es.TRANSFER_TOPIC
A = ADDR
B = "0x" + "b" * 40
C = "0x" + "c" * 40
TOKEN = "0x" + "7" * 40
NFT = "0x" + "9" * 40
T0 = 1_700_000_000


def h(n):
    return "0x" + f"{n:064x}"


def pad(a):
    return "0x" + "0" * 24 + a[2:]


def tx(n, block, frm, to, value, is_error="0"):
    return {"hash": h(n), "blockNumber": str(block), "timeStamp": str(T0 + block), "from": frm, "to": to,
            "value": str(value), "isError": is_error, "txreceipt_status": "0" if is_error == "1" else "1",
            "contractAddress": ""}


def itx(n, block, frm, to, value, trace="0_1", is_error="0"):
    return {"hash": h(n), "blockNumber": str(block), "timeStamp": str(T0 + block), "from": frm, "to": to,
            "value": str(value), "isError": is_error, "traceId": trace, "type": "call", "contractAddress": ""}


def ttx(n, block, frm, to, value, contract=TOKEN, dec="6", sym="TKN"):
    return {"hash": h(n), "blockNumber": str(block), "timeStamp": str(T0 + block), "from": frm, "to": to,
            "value": str(value), "contractAddress": contract, "tokenDecimal": dec, "tokenSymbol": sym}


def log(n, block, index, frm, to, value, contract=TOKEN, token_id=None):
    topics = [TOPIC, pad(frm), pad(to)] + ([hex(token_id)] if token_id is not None else [])
    return {"address": contract, "topics": topics,
            "data": "0x" if token_id is not None else "0x" + f"{value:064x}",
            "blockNumber": hex(block), "timeStamp": hex(T0 + block), "logIndex": hex(index),
            "transactionHash": h(n), "transactionIndex": "0x0"}


def body(status_code=200, **data):
    return httpx.Response(status_code, content=json.dumps(data).encode())


def notok(text):
    return body(status="0", message="NOTOK", result=text)


class FakeEtherscan:
    """Pages, sorts and filters like Etherscan, including the 10,000-row window."""

    def __init__(self, latest=1012, window=10_000):
        self.latest, self.window = latest, window
        self.lists = {"txlist": [], "txlistinternal": [], "tokentx": [], "txsBeaconWithdrawal": []}
        self.nonce = 0
        self.unfinished: list[tuple[int, int]] = []    # block ranges whose internal txs are "not yet processed"
        self.logs: list[dict] = []
        self.eth_calls: dict[tuple[str, str], object] = {
            (es.MULTICALL3, es.SEL_GET_ETH_BALANCE): "0x" + "0" * 64,     # no ETH
            (TOKEN, es.SEL_DECIMALS): "0x" + f"{6:064x}",
        }
        self.code = "0x"
        self.first: list[httpx.Response] = []      # served before anything else, in order
        self.calls: list[dict] = []

    def rpc(self, result):
        return body(jsonrpc="2.0", id=1, result=result)

    def page(self, rows, q):
        page, offset = int(q["page"]), int(q["offset"])
        if page * offset > self.window:
            return body(status="0", message=f"Result window is too large, PageNo x Offset size must be less "
                                            f"than or equal to {self.window}", result=None)
        chunk = rows[(page - 1) * offset:page * offset]
        return body(status="1" if chunk else "0", message="OK" if chunk else "No transactions found", result=chunk)

    def handler(self, request):
        q = dict(parse_qsl(request.url.query.decode()))
        self.calls.append(q)
        if self.first:
            return self.first.pop(0)
        action = q["action"]
        if action == "eth_blockNumber":
            return self.rpc(hex(self.latest))
        if action == "eth_getBlockByNumber":
            return self.rpc({"number": q["tag"], "timestamp": hex(T0 + int(q["tag"], 16))})
        if action == "eth_getCode":
            return self.rpc(self.code)
        if action == "eth_getTransactionCount":
            return self.rpc(hex(self.nonce))
        if action == "eth_call":
            got = self.eth_calls.get((q["to"], q["data"][:10]), "0x")
            if isinstance(got, dict):
                return body(jsonrpc="2.0", id=1, error=got)
            return self.rpc(got)
        if action in self.lists:
            lo, hi = int(q["startblock"]), int(q["endblock"])
            rows = [r for r in self.lists[action] if lo <= int(r["blockNumber"]) <= hi]
            rows.sort(key=lambda r: int(r["blockNumber"]), reverse=q.get("sort") == "desc")
            if action == "txlistinternal" and any(a <= hi and lo <= b for a, b in self.unfinished):
                return body(status="2", message="Some internal transactions within this block range have not yet "
                                                "been processed", result=rows[:int(q["offset"])])
            return self.page(rows, q)
        if action == "getLogs":
            lo, hi = int(q["fromBlock"]), int(q["toBlock"])
            rows = [lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi and lg["topics"][0] == q["topic0"]
                    and ("topic1" not in q or lg["topics"][1] == q["topic1"])
                    and ("topic2" not in q or lg["topics"][2] == q["topic2"])]
            rows.sort(key=lambda lg: (int(lg["blockNumber"], 16), int(lg["logIndex"], 16)))
            return self.page(rows, q)
        raise AssertionError(f"unexpected call {q}")

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


# -- what is skipped, what is kept apart ------------------------------------------

def test_failed_transactions_are_skipped(tmp_path):
    fake = FakeEtherscan()
    fake.lists["txlist"] = [tx(1, 900, B, A, 5), tx(2, 901, A, B, 7, is_error="1"), tx(3, 902, A, B, 0)]
    fake.lists["txlistinternal"] = [itx(4, 903, C, A, 9), itx(5, 904, C, A, 11, is_error="1")]
    batch = make_adapter(tmp_path, fake.client()).fetch_transfers(A)
    # the failed transaction, the zero-value call and the failed internal call moved nothing
    assert [(r.kind, r.tx_hash) for r in batch.transfers] == [("internal", h(4)), ("native", h(1))]


def test_two_identical_transfers_in_one_transaction_stay_apart(tmp_path):
    fake = FakeEtherscan()
    fake.lists["tokentx"] = [ttx(7, 950, A, B, 9), ttx(7, 950, A, B, 9)]
    fake.logs = [log(7, 950, 3, A, B, 9), log(7, 950, 5, A, B, 9)]
    adapter = make_adapter(tmp_path, fake.client())
    batch = adapter.fetch_transfers(A)
    assert sorted(r.event_index for r in batch.transfers) == [3, 5]
    assert len({(r.tx_hash, r.kind, r.event_index) for r in batch.transfers}) == 2   # two rows in the table
    assert adapter.unmatched_token_rows == 0
    assert {(r.token_decimals, r.token_symbol) for r in batch.transfers} == {(6, "TKN")}  # from the token list


def test_nft_transfers_are_ignored(tmp_path):
    fake = FakeEtherscan()
    fake.lists["tokentx"] = [ttx(8, 960, B, A, 4)]
    fake.logs = [log(8, 960, 0, B, A, 4), log(9, 961, 1, B, A, 0, contract=NFT, token_id=1234)]
    batch = make_adapter(tmp_path, fake.client()).fetch_transfers(A)
    assert [(r.tx_hash, r.token_contract) for r in batch.transfers] == [(h(8), TOKEN)]


def test_transfer_to_self_is_one_record(tmp_path):
    fake = FakeEtherscan()
    fake.lists["tokentx"] = [ttx(10, 970, A, A, 4)]
    fake.logs = [log(10, 970, 2, A, A, 4)]                       # found by both the sender and recipient query
    batch = make_adapter(tmp_path, fake.client()).fetch_transfers(A)
    assert len(batch.transfers) == 1


def test_token_list_row_without_a_log_is_counted(tmp_path):
    fake = FakeEtherscan()
    fake.lists["tokentx"] = [ttx(11, 980, B, A, 4)]               # e.g. a token that does not index from/to
    adapter = make_adapter(tmp_path, fake.client())
    assert adapter.fetch_transfers(A).transfers == ()
    assert adapter.unmatched_token_rows == 1


def test_decimals_come_from_the_contract_when_the_list_lacks_them(tmp_path):
    fake = FakeEtherscan()
    fake.logs = [log(12, 990, 0, B, A, 4)]                        # no tokentx row at all
    fake.eth_calls[(TOKEN, es.SEL_DECIMALS)] = "0x" + f"{8:064x}"
    fake.eth_calls[(TOKEN, es.SEL_SYMBOL)] = "0x" + f"{32:064x}" + f"{3:064x}" + b"ABC".hex().ljust(64, "0")
    (rec,) = make_adapter(tmp_path, fake.client()).fetch_transfers(A).transfers
    assert (rec.token_decimals, rec.token_symbol) == (8, "ABC")


def test_internal_event_index_is_the_trace_position():
    assert trace_index("0") == 32 ** 5
    assert trace_index("0_1_1") == 1 * 32 ** 5 + 2 * 32 ** 4 + 2 * 32 ** 3
    assert trace_index("0_1") < trace_index("0_1_0") < trace_index("0_2")     # trace order kept
    assert trace_index("0_1_1_1_1_1") < 2 ** 31                                # fits the int column
    assert trace_index(None) is None and trace_index("0_31") is None and trace_index("x") is None


def test_internal_without_trace_falls_back_to_position(tmp_path):
    fake = FakeEtherscan()
    fake.lists["txlistinternal"] = [itx(13, 995, B, A, 1, trace=""), itx(13, 995, C, A, 2, trace=""),
                                    itx(14, 996, B, A, 3, trace="0_2")]
    batch = make_adapter(tmp_path, fake.client()).fetch_transfers(A)
    by_amount = {r.amount_raw: r.event_index for r in batch.transfers}
    assert by_amount == {1: 0, 2: 1, 3: trace_index("0_2")}


# -- errors that arrive as HTTP 200 ----------------------------------------------------

def test_rate_limit_in_a_200_body_is_retried(tmp_path):
    fake = FakeEtherscan()
    fake.first = [notok("Max calls per sec rate limit reached (3/sec)"), body(503), httpx.Response(429, headers={"Retry-After": "7"})]
    clock = FakeClock()
    adapter = make_adapter(tmp_path, fake.client(), clock=clock)
    assert adapter.head().number == 1000                         # 1012 latest, 12 confirmations
    assert adapter.request_count == 5                            # three refusals, then blockNumber and the block
    assert any(1.0 <= s <= 1.5 for s in clock.sleeps)            # first backoff: 1 s plus jitter
    assert any(7.0 <= s <= 7.5 for s in clock.sleeps)            # Retry-After honoured
    statuses = [json.loads(line)["http_status"] for line in (tmp_path / "raw" / "index.jsonl").read_text().splitlines()]
    assert statuses == [200, 503, 429, 200, 200]                 # refusals are stored too


def test_gives_up_after_max_attempts(tmp_path):
    fake = FakeEtherscan()
    fake.first = [notok("Max calls per sec rate limit reached (3/sec)") for _ in range(6)]
    with pytest.raises(es.EtherscanRateLimited):
        make_adapter(tmp_path, fake.client()).head()
    assert len(fake.calls) == 6


@pytest.mark.parametrize("text, error", [
    ("Free API access is not supported for this chain. Please upgrade your api plan for full chain coverage. "
     "https://etherscan.io/apis", es.EtherscanChainNotCovered),
    ("Invalid API Key (#err2)", es.EtherscanKeyRejected),
    ("Max daily rate limit reached. 100000 (100K) API calls/day max", es.EtherscanDailyLimit),
    ("Sorry, it looks like you are trying to access an API Pro endpoint. Contact us to upgrade to API Pro.",
     es.EtherscanPaidEndpoint),
    ("Error! Invalid address format", es.EtherscanError),
])
def test_errors_in_a_200_body_stop_at_once(tmp_path, text, error):
    fake = FakeEtherscan()
    fake.first = [notok(text)]
    with pytest.raises(error):
        make_adapter(tmp_path, fake.client(), chain="bsc").head()
    assert len(fake.calls) == 1


def test_not_covered_names_the_chain(tmp_path):
    fake = FakeEtherscan()
    fake.first = [notok("Free API access is not supported for this chain. Please upgrade your api plan")]
    with pytest.raises(es.EtherscanChainNotCovered) as err:
        make_adapter(tmp_path, fake.client(), chain="base").head()
    assert err.value.chain_name == "Base"


def test_empty_list_with_status_0_is_not_an_error(tmp_path):
    fake = FakeEtherscan()                                       # every list answers "No transactions found"
    batch = make_adapter(tmp_path, fake.client()).fetch_transfers(A)
    assert batch.transfers == () and batch.truncated is False


# -- paging ------------------------------------------------------------------------------

def test_paging_past_the_window(tmp_path):
    """Two rows a page and a window of four: after two pages the block edge must move."""
    fake = FakeEtherscan(window=4)
    blocks = [10, 9, 8, 8, 7, 6, 5]
    fake.lists["txlist"] = [tx(100 + i, b, B, A, 1 + i) for i, b in enumerate(blocks)]
    adapter = make_adapter(tmp_path, fake.client(), page_size=2, window=4)
    batch = adapter.fetch_transfers(A)
    assert sorted(r.amount_raw for r in batch.transfers) == [1, 2, 3, 4, 5, 6, 7]     # every row, once
    calls = [(c["endblock"], c["page"]) for c in fake.calls if c.get("action") == "txlist"]
    assert calls == [("1000", "1"), ("1000", "2"), ("8", "1"), ("8", "2"), ("6", "1"), ("6", "2")]
    assert not any("window" in json.dumps(c) for c in fake.calls)     # never asked past the window


def test_logs_page_past_the_window_oldest_first(tmp_path):
    fake = FakeEtherscan(window=4)
    fake.logs = [log(200 + i, b, i, B, A, 1 + i) for i, b in enumerate([5, 6, 6, 7, 8, 9])]
    batch = make_adapter(tmp_path, fake.client(), page_size=2, window=4).fetch_transfers(A)
    assert sorted(r.amount_raw for r in batch.transfers) == [1, 2, 3, 4, 5, 6]
    starts = [c["fromBlock"] for c in fake.calls if c.get("action") == "getLogs" and "topic2" in c]
    assert starts[:3] == ["0", "0", "7"]       # pages [5,6] [6,7], then the edge moves to block 7


def test_a_block_bigger_than_the_window_is_an_error(tmp_path):
    fake = FakeEtherscan(window=4)
    fake.lists["txlist"] = [tx(300 + i, 50, B, A, 1) for i in range(5)]
    with pytest.raises(es.EtherscanError, match="cannot page past"):
        make_adapter(tmp_path, fake.client(), page_size=2, window=4).fetch_transfers(A)


def test_truncation_keeps_newest_and_adds_the_first_transfers(tmp_path):
    fake = FakeEtherscan()
    funder = C
    fake.lists["txlist"] = [tx(400, 100, funder, A, 50)] + [tx(401 + i, 101 + i, A, B, 1) for i in range(6)]
    adapter = make_adapter(tmp_path, fake.client(), page_size=2)
    batch = adapter.fetch_transfers(A, limit=3)
    assert batch.truncated is True and batch.limit == 3
    assert [r.block_number for r in batch.transfers[:3]] == [106, 105, 104]       # the newest three
    assert batch.transfers[-1].tx_hash == h(400) and batch.transfers[-1].from_address == funder
    assert len(batch.transfers) == 4                                             # plus the first transfer
    assert any(c.get("action") == "txlist" and c.get("sort") == "asc" for c in fake.calls)


def test_truncation_adds_the_first_token_transfer(tmp_path):
    fake = FakeEtherscan()
    fake.lists["tokentx"] = [ttx(500 + i, 200 + i, B, A, 1 + i) for i in range(5)]
    fake.logs = [log(500 + i, 200 + i, 0, B, A, 1 + i) for i in range(5)]
    batch = make_adapter(tmp_path, fake.client(), page_size=2).fetch_transfers(A, limit=2)
    assert batch.truncated is True
    assert [r.block_number for r in batch.transfers] == [204, 203, 200]
    assert batch.transfers[-1].event_index == 0


# -- snapshot ----------------------------------------------------------------------------

def test_snapshot_balances_contract_flag_and_reverts(tmp_path):
    fake = FakeEtherscan()
    other, spam = "0x" + "1" * 40, "0x" + "2" * 40
    fake.code = "0x6080"
    fake.lists["tokentx"] = [ttx(600, 900, B, A, 5_000_000, contract=USDT, sym="USDT"),
                             ttx(601, 901, B, A, 1, contract=other), ttx(602, 902, B, A, 1, contract=spam)]
    fake.eth_calls[(es.MULTICALL3, es.SEL_GET_ETH_BALANCE)] = "0x" + f"{2 * 10 ** 18:064x}"
    fake.eth_calls[(USDT, es.SEL_BALANCE_OF)] = "0x" + f"{5_000_000:064x}"
    fake.eth_calls[(other, es.SEL_BALANCE_OF)] = "0x" + f"{0:064x}"
    fake.eth_calls[(spam, es.SEL_BALANCE_OF)] = {"code": 3, "message": "execution reverted"}
    adapter = make_adapter(tmp_path, fake.client())
    snap = adapter.fetch_snapshot(A)
    assert snap.is_contract is True and snap.native_balance() == Decimal(2) and snap.as_of_block == 1000
    assert [(b.contract, b.amount(), b.symbol) for b in snap.token_balances] == [(USDT, Decimal(5), "USDT")]
    assert snap.truncated is False
    first = json.loads((tmp_path / "raw" / "raw" / snap.raw_sha256[0][:2] / snap.raw_sha256[0]).read_bytes())
    assert int(first["result"], 16) == 2 * 10 ** 18                # the balance response comes first
    # every read was pinned to the snapshot block
    pinned = [c["tag"] for c in fake.calls if c.get("action") in ("eth_getCode", "eth_call")
              and c["data" if c["action"] == "eth_call" else "action"] != es.SEL_DECIMALS]
    assert set(pinned) - {"latest"} == {hex(1000)}


def test_snapshot_is_truncated_when_more_tokens_than_checked(tmp_path):
    fake = FakeEtherscan()
    fake.lists["tokentx"] = [ttx(700 + i, 900, B, A, 1, contract="0x" + f"{i + 1:040x}") for i in range(es.TOP_TOKENS + 2)]
    snap = make_adapter(tmp_path, fake.client()).fetch_snapshot(A)
    assert snap.truncated is True
    checked = [c["to"] for c in fake.calls if c.get("action") == "eth_call" and c["data"].startswith(es.SEL_BALANCE_OF)]
    assert len(checked) == len(es.CHAINS["eth"].stablecoins) + es.TOP_TOKENS


def test_snapshot_and_transfers_share_one_block(tmp_path):
    fake = FakeEtherscan()
    adapter = make_adapter(tmp_path, fake.client())
    snap = adapter.fetch_snapshot(A)
    fake.latest = 5000                                           # the chain moves on
    adapter.fetch_transfers(A)
    ends = {c.get("endblock") for c in fake.calls if c.get("action") in ("txlist", "txlistinternal", "tokentx")}
    assert ends == {str(snap.as_of_block)}
    assert sum(1 for c in fake.calls if c["action"] == "eth_blockNumber") == 1


def test_bare_0x_is_zero():
    """Etherscan writes log index 0 as "0x" (seen on Arbitrum, 2026-11-19)."""
    assert es._int("0x") == 0 and es._int("0x1f") == 31 and es._int("17") == 17 and es._int("") == 0
