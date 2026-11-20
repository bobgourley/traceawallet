"""Chainlink prices: buckets, judging a round, reads through a metered Blockscout
adapter, and which transfers get a price."""
import datetime as dt
from decimal import Decimal

import httpx
import pytest

from taw import pricing
from taw.adapters.base import TransferRecord
from taw.adapters.prices import chainlink as cl
from taw.adapters.prices.assets import asset_of
from tests.test_budget import Ledger
from tests.test_evm_blockscout import FakeBlockscout, body, make

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 20, 4, 40, tzinfo=UTC)


def round_data(answer, updated, round_id=7):
    words = [round_id, answer % (1 << 256), updated - 30, updated, round_id]
    return "".join(f"{w:064x}" for w in words)


# -- buckets -------------------------------------------------------------------------------

def test_hourly_within_48_hours_daily_before():
    assert cl.bucket_for(dt.datetime(2026, 11, 19, 6, 59, tzinfo=UTC), NOW) == \
        (dt.datetime(2026, 11, 19, 6, tzinfo=UTC), "hour")
    assert cl.bucket_for(dt.datetime(2026, 11, 18, 4, 39, tzinfo=UTC), NOW) == \
        (dt.datetime(2026, 11, 18, tzinfo=UTC), "day")


# -- judging a round -------------------------------------------------------------------------

START = dt.datetime(2026, 11, 1, tzinfo=UTC)
ETH, USDT = cl.FEEDS["ETH"], cl.FEEDS["USDT"]


def test_a_fresh_round_is_a_price():
    status, usd, round_id, updated, age = cl.judge(ETH, round_data(171904770000, int(START.timestamp()) - 600), START)
    assert (status, usd, round_id, age) == ("ok", Decimal("1719.0477"), 7, 600)


def test_a_stablecoin_is_read_not_assumed():
    status, usd, *_ = cl.judge(USDT, round_data(99892361, int(START.timestamp()) - 3600), START)
    assert status == "ok" and usd == Decimal("0.99892361")


def test_older_than_heartbeat_and_a_quarter_is_stale():
    limit = ETH.max_age
    assert limit == 4500
    assert cl.judge(ETH, round_data(1, int(START.timestamp()) - limit), START)[0] == "ok"
    status, usd, _, _, age = cl.judge(ETH, round_data(1, int(START.timestamp()) - limit - 1), START)
    assert status == "stale" and usd is None and age == limit + 1


@pytest.mark.parametrize("out", [None, "", "00" * 10, round_data(5, 0)])
def test_no_feed_yet(out):
    assert cl.judge(ETH, out, START)[:2] == ("no_feed", None)


@pytest.mark.parametrize("answer", [0, -5])
def test_a_non_positive_answer_is_no_price(answer):
    assert cl.judge(ETH, round_data(answer, int(START.timestamp())), START)[:2] == ("invalid", None)


# -- reading through Blockscout ------------------------------------------------------------------

class FeedServer(FakeBlockscout):
    """Answers getblocknobytime and latestRoundData for the feeds."""

    def __init__(self, answers):
        super().__init__()
        self.answers = answers           # feed address -> hex output, or None for a revert

    def handler(self, request):
        q = dict(httpx.QueryParams(request.url.query.decode()))
        if request.method == "GET" and q.get("module") == "block":
            self.requests.append(request)
            self.calls.append(q)
            return body(status="1", message="OK", result={"blockNumber": str(1000 + int(q["timestamp"]) % 997)})
        if request.method == "POST" and b"0xfeaf968c" in request.content:
            import json
            self.requests.append(request)
            to = json.loads(request.content)["params"][0]["to"]
            self.calls.append({"rpc": "eth_call", "to": to})
            out = self.answers.get(to)
            if out is None:
                return body(jsonrpc="2.0", id=1, error={"code": -32000, "message": "execution reverted"})
            return body(jsonrpc="2.0", id=1, result="0x" + out)
        return super().handler(request)


def reader(tmp_path, server, ledger=None):
    adapters = {}

    def adapter_for(chain):
        adapters[chain] = make(tmp_path / chain, server, chain=chain, budget=ledger)
        return adapters[chain]
    return cl.ChainlinkReader(adapter_for)


def test_one_block_lookup_per_bucket_then_one_call_per_asset(tmp_path):
    t = int(START.timestamp()) - 60
    server = FeedServer({ETH.address: round_data(171904770000, t), cl.FEEDS["BTC"].address: round_data(6142955575003, t)})
    ledger = Ledger()
    r = reader(tmp_path, server, ledger)
    eth = r.read("ETH", START, "day")
    btc = r.read("BTC", START, "day")
    assert (eth.status, eth.usd, btc.usd) == ("ok", Decimal("1719.0477"), Decimal("61429.55575003"))
    assert eth.block_number == btc.block_number
    kinds = [c.get("action") or c.get("rpc") for c in server.calls]
    assert kinds == ["getblocknobytime", "eth_call", "eth_call"]
    assert len(ledger.calls) == 3 and all(c == 20 for c, _ in ledger.calls)     # every call metered
    assert {c["timestamp"] for c in server.calls if "timestamp" in c} == {str(int(START.timestamp()))}


def test_trx_is_read_on_optimism_and_a_revert_is_no_feed(tmp_path):
    server = FeedServer({})
    r = reader(tmp_path, server)
    trx = r.read("TRX", START, "day")
    assert trx.feed.chain == "optimism" and trx.status == "no_feed" and trx.usd is None


# -- which transfers get a price -----------------------------------------------------------------

def rec(chain, contract, when, kind="token"):
    return TransferRecord(chain=chain, tx_hash="0x" + "ab" * 32, kind=kind if contract else "native", event_index=0,
                          from_address="a", to_address="b", token_contract=contract, token_symbol=None,
                          token_decimals=6, amount_raw=1, block_number=1, block_time=when, source_slug="x",
                          raw_sha256="0" * 64)


def test_assets():
    when = NOW
    assert asset_of(rec("eth", None, when)) == "ETH"
    assert asset_of(rec("optimism", None, when)) == "ETH"
    assert asset_of(rec("btc", None, when)) == "BTC"
    assert asset_of(rec("tron", None, when)) == "TRX"
    assert asset_of(rec("tron", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", when)) == "USDT"
    assert asset_of(rec("eth", "0xA0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", when)) == "USDC"
    assert asset_of(rec("arbitrum", "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8", when)) is None   # USDC.e: bridged
    assert asset_of(rec("eth", "0x1111111111111111111111111111111111111111", when)) is None


def test_needed_is_distinct_and_newest_first():
    recs = [rec("eth", None, NOW - dt.timedelta(minutes=5)), rec("eth", None, NOW - dt.timedelta(minutes=50)),
            rec("eth", None, NOW - dt.timedelta(days=10)), rec("eth", None, NOW - dt.timedelta(days=10, hours=3)),
            rec("tron", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", NOW - dt.timedelta(days=400)),
            rec("eth", "0x1111111111111111111111111111111111111111", NOW)]
    keys = pricing.needed(recs, NOW)
    assert keys == [("ETH", dt.datetime(2026, 11, 20, 4, tzinfo=UTC), "hour"),
                    ("ETH", dt.datetime(2026, 11, 20, 3, tzinfo=UTC), "hour"),
                    ("ETH", dt.datetime(2026, 11, 10, tzinfo=UTC), "day"),       # both 10-day-old transfers
                    ("USDT", dt.datetime(2025, 10, 16, tzinfo=UTC), "day")]       # the unknown token: none
