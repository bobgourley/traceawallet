"""The global index of stablecoin blacklist events: parsing, scanning, and its use in
freeze dates and the stablecoin reconciliation. Database parts are in
tests/test_store_pg.py."""
import datetime as dt

import pytest

from taw.adapters.base import AddressSnapshot, TokenBalance, TransferBatch, TransferRecord
from taw.adapters.chains.tron_address import to_base58
from taw.adapters.labels import blacklist as bl
from taw.adapters.labels import freeze
from taw.blacklist import FreezeFound, IndexedEvent
from taw.reconcile import (FROZEN_NO_DESTRUCTION, NOT_RECONCILED, RECONCILED, StablecoinContext, run_checks)

USDT_ETH = bl.CONTRACTS[0]
USDC_ETH = bl.CONTRACTS[1]
USDT_TRON = bl.CONTRACTS[2]
ADDR = "0x" + "ab" * 20
TX = "0x" + "11" * 32


def word(v: int | str) -> str:
    return (v[2:] if isinstance(v, str) else f"{v:x}").rjust(64, "0")


def usdt_log(event: bl.Event, address=ADDR, amount=None, block=100, index=0, tx=TX):
    data = "0x" + word(address) + (word(amount) if amount is not None else "")
    return {"address": USDT_ETH.contract, "topics": [event.topic], "data": data, "blockNumber": hex(block),
            "logIndex": hex(index), "transactionHash": tx}


# -- parsing -----------------------------------------------------------------------------------

def test_ethereum_usdt_events_carry_the_address_in_the_data():
    added, _, destroyed = bl.USDT_EVENTS
    e = bl.evm_event(USDT_ETH, added, usdt_log(added), "s" * 64)
    assert (e.action, e.address, e.amount_raw, e.block_number, e.tx_hash, e.block_time) == \
           ("freeze", ADDR, None, 100, TX, None)
    d = bl.evm_event(USDT_ETH, destroyed, usdt_log(destroyed, amount=465514638375, index=3), "s" * 64)
    assert (d.action, d.address, d.amount_raw, d.log_index) == ("destroy", ADDR, 465514638375, 3)


def test_ethereum_usdc_events_carry_the_address_in_a_topic():
    blk = bl.USDC_EVENTS[0]
    log = {"topics": [blk.topic, "0x" + word(ADDR)], "data": "0x", "blockNumber": "0x10", "logIndex": "0x1",
           "transactionHash": TX.upper().replace("0X", "0x"), "timeStamp": "0x5f5e1000"}
    e = bl.evm_event(USDC_ETH, blk, log, "s" * 64)
    assert (e.address, e.block_number, e.tx_hash) == (ADDR, 16, TX)
    assert e.block_time == dt.datetime.fromtimestamp(0x5f5e1000, dt.timezone.utc)


def test_tron_event_from_trongrid_reply():
    # A real TronGrid record (USDT DestroyedBlackFunds, 2020-06-26), as read on 2026-11-20.
    item = {"block_number": 20973041, "block_timestamp": 1593185253000,
            "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "event_index": 0,
            "event_name": "DestroyedBlackFunds",
            "result": {"0": "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c", "1": "18529513880",
                       "_blackListedUser": "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c", "_balance": "18529513880"},
            "result_type": {"_blackListedUser": "address", "_balance": "uint256"},
            "transaction_id": "0dd60d70bb509edd2523127fd4b9f1e0f0855d23ffc7de6535271580d8778730"}
    e = bl.tron_event(USDT_TRON, bl.USDT_EVENTS[2], item, "s" * 64)
    assert e.address == to_base58("41a614f803b6fd780986a42c78ec9c7f77e6ded13c") and e.address.startswith("T")
    assert (e.action, e.amount_raw, e.block_number, e.log_index) == ("destroy", 18529513880, 20973041, 0)
    assert e.block_time == dt.datetime(2020, 6, 26, 15, 27, 33, tzinfo=dt.timezone.utc)


# -- scanning ----------------------------------------------------------------------------------

def test_evm_scan_halves_a_range_that_hit_the_cap_and_keeps_each_event_once():
    added = bl.USDT_EVENTS[0]
    logs = [usdt_log(added, block=b, tx="0x" + f"{b:064x}") for b in range(1, 1501)]
    asked = []

    def get(contract, topic, lo, hi):
        asked.append((topic, lo, hi))
        if topic != added.topic:
            return [], "e" * 64
        found = [g for g in logs if lo <= int(g["blockNumber"], 16) <= hi]
        return found[:bl.LOG_CAP], "f" * 64            # a provider that cuts at the cap

    result = bl.evm_scan(get, USDT_ETH, 1, 1500)
    assert len(result.events) == 1500 and len({e.key for e in result.events}) == 1500
    assert [(lo, hi) for t, lo, hi in asked if t == added.topic] == [(1, 1500), (1, 750), (751, 1500)]
    assert result.calls == len(asked) == 5               # three for AddedBlackList, one each for the others


class FakeTron:
    """Serves events by time window, TRON_PAGE a page."""

    def __init__(self, events):
        self.events = events
        self.windows = []

    def _pages(self, path, query, max_pages):
        lo, hi = query["min_block_timestamp"], query["max_block_timestamp"]
        self.windows.append((query["event_name"], lo, hi))
        mine = [e for e in self.events if e["event_name"] == query["event_name"] and lo <= e["block_timestamp"] <= hi]
        pages = [mine[i:i + bl.TRON_PAGE] for i in range(0, len(mine), bl.TRON_PAGE)] or [[]]
        for n, page in enumerate(pages[:max_pages]):
            yield page, f"{n:064x}"
        return len(pages) > max_pages


def tron_item(n, name="AddedBlackList", ms=None):
    return {"block_number": 1000 + n, "block_timestamp": ms if ms is not None else 1_600_000_000_000 + n * 17_000,   # spread over a day
            "contract_address": USDT_TRON.contract, "event_index": 0, "event_name": name,
            "result": {"_user": "0x" + f"{n + 1:040x}"}, "result_type": {"_user": "address"},
            "transaction_id": f"{n:064x}"}


def test_tron_scan_splits_a_window_that_needs_too_many_pages():
    many = bl.TRON_MAX_PAGES * bl.TRON_PAGE + 1
    fake = FakeTron([tron_item(n) for n in range(many)])
    start = dt.datetime.fromtimestamp(1_600_000_000, dt.timezone.utc)
    result = bl.tron_scan(fake, USDT_TRON, start, start + dt.timedelta(days=1))
    assert len(result.events) == many
    assert sum(1 for w in fake.windows if w[0] == "AddedBlackList") == 3     # the full window, then two halves


# -- freeze dates from the index -----------------------------------------------------------------

FROZEN_AT = dt.datetime(2024, 5, 1, 12, 0, tzinfo=dt.timezone.utc)


def test_freeze_date_from_the_index():
    usdt = next(c for c in freeze.CONTRACTS if c.chain == "tron" and c.token == "USDT")
    found = FreezeFound(IndexedEvent("freeze", 62_000_000, FROZEN_AT, "aa" * 32, 0, None), 77_000_000)
    chk = freeze._from_index(usdt, "TX", True, None, "s" * 64, lambda *a: found, None)
    assert chk.frozen_since == FROZEN_AT and chk.frozen_since_block == 62_000_000
    assert "complete to block 77,000,000" in chk.date_note and "aa" * 32 in chk.date_note
    assert chk.claim().source_date == FROZEN_AT.date()


def test_freeze_date_says_when_the_index_is_missing_or_has_no_event():
    usdt = next(c for c in freeze.CONTRACTS if c.chain == "eth" and c.token == "USDT")
    none = freeze._from_index(usdt, ADDR, True, 900, "s" * 64, lambda *a: FreezeFound(None, None), None)
    assert none.frozen_since is None and "not available" in none.date_note
    empty = freeze._from_index(usdt, ADDR, True, 900, "s" * 64, lambda *a: FreezeFound(None, 800), None)
    assert empty.frozen_since is None and "no freeze event" in empty.date_note and "800" in empty.date_note


# -- reconciliation ------------------------------------------------------------------------------

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
STABLE = USDT_ETH.contract
B = "0x" + "b" * 40


def snap(balance, block=100):
    return AddressSnapshot("eth", ADDR, block, T0, "ETH", 18, 0, (TokenBalance(STABLE, balance, 6, "USDT"),)
                           if balance else (), False, False, "blockscout", ("0" * 64,), balances_read=(STABLE,))


def received(amount):
    return TransferBatch("eth", ADDR, (TransferRecord("eth", TX, "token", 0, B, ADDR, STABLE, "USDT", 6, amount, 50, T0,
                                                      "blockscout", "0" * 64),), False, 2000)


def burn(amount, block=80):
    return {"tx_hash": "0x" + "dd" * 32, "log_index": 4, "block": block, "time": None, "amount_raw": str(amount)}


def test_a_recorded_destruction_is_an_accounted_movement():
    ctx = StablecoinContext({STABLE: True}, {STABLE: (burn(465_514_638_375),)}, {STABLE: 99})
    result = run_checks(snap(0), received(465_514_638_375), [STABLE], ctx)
    (t,) = result.tokens
    assert t.status == RECONCILED and t.destroyed_raw == 465_514_638_375
    assert t.destructions[0]["tx_hash"] == "0x" + "dd" * 32 and t.as_json()["destroyed_raw"] == "465514638375"
    assert result.history_incomplete is False
    recon = next(c for c in result.checks if c.name == "stablecoin_reconciliation")
    assert recon.details["reconciled_with_destruction"] == 1


def test_frozen_with_zero_balance_and_no_destruction_is_its_own_result():
    ctx = StablecoinContext({STABLE: True}, {}, {STABLE: 99})
    result = run_checks(snap(0), received(500), [STABLE], ctx)
    (t,) = result.tokens
    assert t.status == FROZEN_NO_DESTRUCTION and t.status != RECONCILED
    assert result.history_incomplete is True
    assert result.incomplete_reasons == (
        f"stablecoin {STABLE} is frozen with a zero balance, transfers in minus out 500, and no destruction "
        "event was found (the index of destruction events is complete to block 99)",)


def test_a_destruction_after_the_snapshot_block_does_not_count():
    ctx = StablecoinContext({STABLE: True}, {STABLE: (burn(500, block=101),)}, {STABLE: 200})
    (t,) = run_checks(snap(0), received(500), [STABLE], ctx).tokens
    assert t.status == FROZEN_NO_DESTRUCTION and t.destroyed_raw == 0


def test_not_frozen_and_unexplained_stays_not_reconciled():
    ctx = StablecoinContext({STABLE: False}, {}, {STABLE: 99})
    (t,) = run_checks(snap(0), received(500), [STABLE], ctx).tokens
    assert t.status == NOT_RECONCILED


def test_without_context_nothing_changes():
    (t,) = run_checks(snap(0), received(500), [STABLE]).tokens
    assert t.status == NOT_RECONCILED and t.frozen is None and t.index_to_block is None


@pytest.mark.parametrize("c", bl.CONTRACTS)
def test_every_indexed_contract_is_a_freeze_contract(c):
    assert any(f.chain == c.chain and f.contract == c.contract for f in freeze.CONTRACTS)
