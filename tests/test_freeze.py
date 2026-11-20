"""Stablecoin freeze status: what is called, when a date is read, and the wording."""
import json

import httpx

from taw.adapters.chains.tron_address import to_hex
from taw.adapters.labels import freeze
from tests.test_budget import Ledger
from tests.test_evm_blockscout import FakeBlockscout, body, make

ADDR = "0xc1b68434d0ef9a78e7f62e3b9850b70b3d0174bf"
TRON_ADDR = "TC66d59uus8AWhYDvAgWM55T8NRa7KNWAN"
TRUE, FALSE = "0x" + "0" * 63 + "1", "0x" + "0" * 64


def contract(chain, token):
    return next(c for c in freeze.CONTRACTS if c.chain == chain and c.token == token)


class FreezeServer(FakeBlockscout):
    def __init__(self, status, logs=()):
        super().__init__()
        self.status, self.logs_out = status, list(logs)

    def handler(self, request):
        if request.method == "POST":
            rpc = json.loads(request.content)
            m, p = rpc["method"], rpc["params"]
            if m == "eth_call" and p[0]["data"][:10] in ("0xe47d6060", "0xfe575a87", "0xfbac3951"):
                self.requests.append(request)
                self.calls.append({"rpc": m, "data": p[0]["data"], "tag": p[1]})
                if self.status is None:
                    return body(jsonrpc="2.0", id=1, error={"code": -32000, "message": "execution reverted"})
                return self.rpc(self.status)
            if m == "eth_getLogs":
                self.requests.append(request)
                self.calls.append({"rpc": m, "filter": p[0]})
                return self.rpc(self.logs_out)
        return super().handler(request)


def test_status_is_read_at_the_snapshot_block_and_metered(tmp_path):
    server, ledger = FreezeServer(FALSE), Ledger()
    chk = freeze.check_evm(make(tmp_path, server, budget=ledger), contract("eth", "USDT"), ADDR, 23_000_000)
    assert chk.frozen is False and chk.result == "not_frozen" and chk.claim() is None
    assert server.calls == [{"rpc": "eth_call", "data": "0xe47d6060" + "0" * 24 + ADDR[2:], "tag": hex(23_000_000)}]
    assert len(ledger.calls) == 1


def test_eth_usdt_frozen_date_needs_the_index(tmp_path):
    server = FreezeServer(TRUE)
    chk = freeze.check_evm(make(tmp_path, server), contract("eth", "USDT"), ADDR, 23_000_000)
    assert chk.frozen and chk.frozen_since is None and "index" in chk.date_note and "not available" in chk.date_note
    assert [c["rpc"] for c in server.calls] == ["eth_call"]                      # no log search
    claim = chk.claim()
    assert claim.category == "frozen" and claim.source_date is None
    assert claim.label == ("frozen by Tether: the USDT contract on Ethereum "
                           "(0xdac17f958d2ee523a2206206994597c13d831ec7) reports isBlackListed = true for this address")


def test_usdc_freeze_date_from_its_indexed_event(tmp_path):
    topic = "0x" + "0" * 24 + ADDR[2:]
    usdc = contract("eth", "USDC")
    logs = [{"blockNumber": hex(500), "logIndex": "0x1", "topics": [usdc.events[0], topic]},     # frozen
            {"blockNumber": hex(600), "logIndex": "0x0", "topics": [usdc.events[1], topic]},     # released
            {"blockNumber": hex(700), "logIndex": "0x2", "topics": [usdc.events[0], topic]}]     # frozen again
    server = FreezeServer(TRUE, logs)
    chk = freeze.check_evm(make(tmp_path, server), usdc, ADDR, 900)
    assert chk.frozen_since_block == 700 and chk.frozen_since.timestamp() == 1_700_000_000 + 700
    flt = next(c["filter"] for c in server.calls if c["rpc"] == "eth_getLogs")
    assert flt["toBlock"] == hex(900) and flt["topics"] == [list(usdc.events), topic]
    assert chk.claim().source_date == chk.frozen_since.date()


def test_no_answer_is_not_a_status(tmp_path):
    chk = freeze.check_evm(make(tmp_path, FreezeServer(None)), contract("eth", "USDC"), ADDR, 900)
    assert chk.frozen is None and chk.result == "no_answer" and chk.claim() is None


def test_tron_passes_the_address_as_the_parameter():
    seen = []

    class Tron:
        def _constant_call(self, c, selector, parameter=""):
            seen.append((c, selector, parameter))
            return "0" * 63 + "1", "f" * 64
    chk = freeze.check_tron(Tron(), contract("tron", "USDT"), TRON_ADDR)
    assert seen == [("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "isBlackListed(address)", "0" * 24 + to_hex(TRON_ADDR)[2:])]
    assert chk.frozen and chk.block is None and "not available" in chk.date_note
    assert chk.claim().label.startswith("frozen by Tether: the USDT contract on Tron (TR7NH")


def test_every_contract_is_in_a_known_source():
    assert {c.source for c in freeze.CONTRACTS} == {"usdt-contract", "usdc-contract"}
    assert {(c.chain, c.date_readable) for c in freeze.CONTRACTS if c.token != "USDT"} == {
        ("eth", True), ("arbitrum", True), ("optimism", True), ("tron", False)}
