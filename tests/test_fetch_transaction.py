"""Reading one transaction by hash (fetch_transaction), for matching. Canned replies
in each provider's shape; the Ethereum path also ran live when the case 3 fixture
was recorded (tests/fixtures/reports/wdpa-2-26-cv-00087-usdt)."""
import json

import httpx

from taw.adapters.base import LocalRawStore
from taw.adapters.chains.btc_esplora import BtcEsploraAdapter
from taw.adapters.chains.evm_etherscan import TRANSFER_TOPIC, EtherscanAdapter
from taw.adapters.chains.tron_address import to_hex
from taw.adapters.chains.tron_trongrid import TronGridAdapter
from tests.tron_replay import FakeClock

TX = "ab" * 32
USDT_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
A = "TC66d59uus8AWhYDvAgWM55T8NRa7KNWAN"
B = "TH4qXPSp3S5g3HnDr1pgnEq4XAoJh7McvZ"


def client(route, base_url=None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        return httpx.Response(200, json=route(request.url.path, dict(request.url.params), body))
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=base_url or "")


def tron(route, tmp_path):
    clock = FakeClock()
    return TronGridAdapter(LocalRawStore(tmp_path), None, http=client(route, "https://api.trongrid.io"),
                           clock=clock.clock, sleep=clock.sleep)


def test_tron_token_transfer_from_events(tmp_path):
    def route(path, params, body):
        if path == "/walletsolidity/gettransactionbyid":
            return {"txID": TX, "ret": [{"contractRet": "SUCCESS"}],
                    "raw_data": {"contract": [{"type": "TriggerSmartContract"}]}}
        if path == "/walletsolidity/gettransactioninfobyid":
            return {"blockNumber": 66_000_000, "blockTimeStamp": 1_730_000_000_000}
        if path == f"/v1/transactions/{TX}/events":
            return {"data": [{"event_name": "Transfer", "contract_address": USDT_TRON, "event_index": 2,
                              "block_number": 66_000_000, "block_timestamp": 1_730_000_000_000,
                              "result": {"0": "0x" + to_hex(A)[2:], "1": "0x" + to_hex(B)[2:], "2": "5000000"}}]}
        if path == "/wallet/triggerconstantcontract":
            sel = body["function_selector"]
            word = (6).to_bytes(32, "big").hex() if sel == "decimals()" else (
                (32).to_bytes(32, "big").hex() + (4).to_bytes(32, "big").hex() + b"USDT".hex().ljust(64, "0"))
            return {"result": {"result": True}, "constant_result": [word]}
        raise AssertionError(path)
    f = tron(route, tmp_path).fetch_transaction("0x" + TX)
    assert f.found and f.succeeded and f.block_number == 66_000_000
    (r,) = f.records
    assert (r.kind, r.from_address, r.to_address, r.token_contract, r.amount_raw, r.token_decimals, r.event_index) == \
           ("token", A, B, USDT_TRON, 5_000_000, 6, 2)


def test_tron_unknown_and_failed(tmp_path):
    f = tron(lambda path, params, body: {}, tmp_path).fetch_transaction(TX)
    assert not f.found and "does not know" in f.note

    def failed(path, params, body):
        if path == "/walletsolidity/gettransactionbyid":
            return {"txID": TX, "ret": [{"contractRet": "REVERT"}], "raw_data": {"contract": []}}
        return {"blockNumber": 1, "blockTimeStamp": 1_700_000_000_000}
    g = tron(failed, tmp_path).fetch_transaction(TX)
    assert g.found and g.succeeded is False and g.records == () and "REVERT" in g.note


def test_bitcoin_inputs_and_outputs(tmp_path):
    txid = "cd" * 32

    def route(path, params, body):
        assert path == f"/api/tx/{txid}"
        return {"txid": txid, "status": {"confirmed": True, "block_height": 800_000, "block_time": 1_690_000_000},
                "vin": [{"prevout": {"scriptpubkey_address": "3PayerAddressxxxxxxxxxxxxxxxxxxxx", "value": 150_000}}],
                "vout": [{"scriptpubkey_address": "bc1qpaid", "value": 100_000},
                         {"scriptpubkey_address": "3PayerAddressxxxxxxxxxxxxxxxxxxxx", "value": 49_000},
                         {"scriptpubkey_type": "op_return", "value": 0}]}
    clock = FakeClock()
    a = BtcEsploraAdapter(LocalRawStore(tmp_path), http=client(route), clock=clock.clock, sleep=clock.sleep)
    f = a.fetch_transaction(txid)
    assert f.found and f.block_number == 800_000
    assert [(r.kind, r.event_index, r.amount_raw) for r in f.records] == [
        ("utxo_in", 0, 150_000), ("utxo_out", 0, 100_000), ("utxo_out", 1, 49_000)]


def test_evm_native_and_token_from_the_receipt(tmp_path):
    h = "0x" + "ef" * 32
    frm, to, token = "0x" + "1" * 40, "0x" + "2" * 40, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

    def route(path, params, body):
        action = params.get("action")
        if action == "eth_getTransactionByHash":
            return {"jsonrpc": "2.0", "result": {"hash": h, "blockNumber": "0x10", "from": frm, "to": token,
                                                 "value": "0x5"}}
        if action == "eth_getTransactionReceipt":
            return {"jsonrpc": "2.0", "result": {"status": "0x1", "logs": [
                {"address": token, "topics": [TRANSFER_TOPIC, "0x" + frm[2:].rjust(64, "0"), "0x" + to[2:].rjust(64, "0")],
                 "data": "0x" + (2_500_000).to_bytes(32, "big").hex(), "logIndex": "0x3", "transactionHash": h}]}}
        if action == "eth_getBlockByNumber":
            return {"jsonrpc": "2.0", "result": {"number": "0x10", "timestamp": "0x6500"}}
        if action == "eth_call":
            data = params["data"]
            if data.startswith("0x313ce567"):                                     # decimals()
                return {"jsonrpc": "2.0", "result": "0x" + (6).to_bytes(32, "big").hex()}
            return {"jsonrpc": "2.0", "result": "0x" + (32).to_bytes(32, "big").hex() + (4).to_bytes(32, "big").hex()
                    + b"USDC".hex().ljust(64, "0")}
        raise AssertionError(params)
    clock = FakeClock()
    a = EtherscanAdapter("eth", LocalRawStore(tmp_path), None, http=client(route), clock=clock.clock, sleep=clock.sleep)
    f = a.fetch_transaction(h)
    assert f.found and f.succeeded and f.block_number == 16
    kinds = {(r.kind, r.to_address, r.amount_raw, r.token_decimals) for r in f.records}
    assert kinds == {("native", token, 5, 18), ("token", to, 2_500_000, 6)}
    assert "internal transfers" in f.note
