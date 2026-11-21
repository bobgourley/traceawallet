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
            if m == "eth_call" and p[0]["data"][:10] in ("0xe47d6060", "0xfe575a87", "0xfbac3951", "0xe5839836",
                                                         "0xd0516650", "0xbc8c4b4f"):
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
    assert {c.source for c in freeze.CONTRACTS} == {"usdt-contract", "usdc-contract", "paxos-contracts",
                                                    "fdusd-contract", "usd1-contract", "rlusd-contract"}
    readable = {(c.chain, c.token): c.date_readable for c in freeze.CONTRACTS}
    assert readable == {("eth", "USDT"): False, ("eth", "USDC"): True, ("arbitrum", "USDT0"): True,
                        ("arbitrum", "USDC"): True, ("optimism", "USDC"): True, ("eth", "PYUSD"): True,
                        ("eth", "USDP"): True, ("eth", "USDG"): True, ("eth", "FDUSD"): True, ("eth", "USD1"): True,
                        ("eth", "RLUSD"): False, ("eth", "EURC"): True, ("arbitrum", "PYUSD"): True,
                        ("optimism", "USDT0"): False, ("tron", "USDT"): False, ("tron", "USDC"): False}
    assert len({(c.chain, c.contract) for c in freeze.CONTRACTS}) == len(freeze.CONTRACTS)


def test_the_original_tokens_are_always_shown_and_the_added_ones_only_when_they_say_something():
    shown = {(c.chain, c.token) for c in freeze.CONTRACTS if c.always_shown}
    assert shown == {("eth", "USDT"), ("eth", "USDC"), ("arbitrum", "USDT0"), ("arbitrum", "USDC"),
                     ("optimism", "USDC"), ("tron", "USDT"), ("tron", "USDC")}


def test_each_status_function_is_called_by_its_own_selector(tmp_path):
    expected = {"PYUSD": "0xe5839836", "USDP": "0xe5839836", "USDG": "0xe5839836", "FDUSD": "0xd0516650",
                "USD1": "0xd0516650", "RLUSD": "0xbc8c4b4f", "EURC": "0xfe575a87"}
    for token, selector in expected.items():
        server = FreezeServer(FALSE)
        chk = freeze.check_evm(make(tmp_path / token, server), contract("eth", token), ADDR, 23_000_000)
        assert chk.frozen is False
        assert server.calls[0]["data"] == selector + "0" * 24 + ADDR[2:]


def test_fdusd_and_usd1_find_the_account_in_the_second_topic(tmp_path):
    # Freeze(address indexed caller, address indexed account): the frozen account is topic 2.
    topic = "0x" + "0" * 24 + ADDR[2:]
    usd1 = contract("eth", "USD1")
    logs = [{"blockNumber": hex(800), "logIndex": "0x3", "topics": [usd1.events[0], "0x" + "0" * 64, topic],
             "transactionHash": "0x" + "cd" * 32}]
    server = FreezeServer(TRUE, logs)
    chk = freeze.check_evm(make(tmp_path, server), usd1, ADDR, 900)
    flt = next(c["filter"] for c in server.calls if c["rpc"] == "eth_getLogs")
    assert flt["topics"] == [list(usd1.events), None, topic]
    assert chk.frozen_since_block == 800
    assert chk.claim().label == ("frozen by the USD1 issuer: the USD1 contract on Ethereum "
                                 "(0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d) reports frozen = true for this address")


def test_rlusd_uses_its_own_word_and_claims_no_date(tmp_path):
    server = FreezeServer(TRUE)
    chk = freeze.check_evm(make(tmp_path, server), contract("eth", "RLUSD"), ADDR, 900)
    assert [c["rpc"] for c in server.calls] == ["eth_call"] and chk.frozen_since is None
    assert chk.claim().label.startswith("paused by the RLUSD issuer: the RLUSD contract on Ethereum")


def test_paxos_freeze_date_from_freezeaddress(tmp_path):
    topic = "0x" + "0" * 24 + ADDR[2:]
    pyusd = contract("eth", "PYUSD")
    server = FreezeServer(TRUE, [{"blockNumber": hex(700), "logIndex": "0x1", "topics": [pyusd.events[0], topic]}])
    chk = freeze.check_evm(make(tmp_path, server), pyusd, ADDR, 900)
    flt = next(c["filter"] for c in server.calls if c["rpc"] == "eth_getLogs")
    assert flt["topics"] == [list(pyusd.events), topic] and chk.frozen_since_block == 700
    assert chk.claim().label.startswith("frozen by Paxos: the PYUSD contract on Ethereum")


def test_what_each_issuer_can_do_with_a_frozen_balance():
    removal = {(c.chain, c.token): c.removal for c in freeze.CONTRACTS}
    assert removal[("eth", "USDT")] == removal[("tron", "USDT")] == removal[("optimism", "USDT0")] == "destroy it"
    assert removal[("eth", "PYUSD")] == "wipe it from the address" and removal[("eth", "USD1")] == \
        "move it to another address"
    assert removal[("eth", "USDC")] is None and removal[("eth", "EURC")] is None   # Circle's contract cannot
