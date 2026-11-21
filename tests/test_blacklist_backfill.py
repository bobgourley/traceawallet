"""The resumable Ethereum blacklist backfill (taw/blacklist_backfill.py): windows that
shrink on timeouts and resume where they stopped, USDT through its owner multisig,
and the backfill's share of the Blockscout allowance. No network: every provider is
a fake serving a made-up history."""
import datetime as dt

import httpx
import pytest

from taw import blacklist_backfill as bf
from taw import budget as b
from taw.adapters.labels import blacklist as bl

USDT = bl.CONTRACTS[0]
USDC = bl.CONTRACTS[1]
ADDED, REMOVED, DESTROYED = bl.USDT_EVENTS
MS = bf.OWNER_MULTISIG


def word(v) -> str:
    return (v[2:] if isinstance(v, str) else f"{v:x}").rjust(64, "0")


def submit_input(destination: str, call: str) -> str:
    """submitTransaction(destination, 0, call) as a transaction input."""
    data = call[2:] if call.startswith("0x") else call
    padded = data + "0" * (-len(data) % 64)
    return bf.SUBMIT_SELECTOR + word(destination) + word(0) + word(0x60) + word(len(data) // 2) + padded


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


class Chain:
    """A made-up multisig history. Each proposal: (block, call selector or None for
    another contract, target address, executed?, failed submission?)."""

    def __init__(self, proposals, destroy_amounts=None, extra_destroy=None):
        self.txs, self.exec_logs, self.usdt_logs = [], [], []
        number = 0
        for i, (block, sel, target, executed, failed) in enumerate(proposals):
            dest = USDT.contract if sel else addr(0xbeef)
            call = "0x" + (sel or "a9059cbb") + word(target)
            self.txs.append({"hash": f"0x{i:064x}", "blockNumber": str(block), "transactionIndex": "0", "to": MS,
                             "input": submit_input(dest, call), "isError": "1" if failed else "0"})
            if failed:
                continue
            if executed:
                tx = f"0x{0xe000 + i:064x}"
                self.exec_logs.append({"address": MS, "topics": [bf.EXECUTION_TOPIC, "0x" + word(number)], "data": "0x",
                                       "blockNumber": hex(block + 1), "logIndex": hex(4), "transactionHash": tx})
                event = bf.USDT_CALLS.get(sel or "")
                if event is not None:
                    data = "0x" + word(target) + (word((destroy_amounts or {}).get(target, 7)) if event is DESTROYED
                                                 else "")
                    self.usdt_logs.append({"address": USDT.contract, "topics": [event.topic], "data": data,
                                           "blockNumber": hex(block + 1), "logIndex": hex(3), "transactionHash": tx})
            number += 1
        self.count = number
        if extra_destroy:
            self.usdt_logs.append(extra_destroy)
        self.requests = []

    def txlist(self, lo, hi):
        self.requests.append(("txlist", lo, hi))
        rows = [t for t in self.txs if lo <= int(t["blockNumber"]) <= hi]
        return rows[:bf.TX_PAGE], "t" * 64

    def get_logs(self, contract, topic, lo, hi):
        self.requests.append((contract, topic, lo, hi))
        pool = self.exec_logs if contract == MS else self.usdt_logs
        return [g for g in pool if g["topics"][0] == topic and lo <= int(g["blockNumber"], 16) <= hi], "l" * 64

    def counted(self, hi):
        return self.count


def run(chain, store=None, hi=USDT.start_block + 50_000):
    store = store or bf.MemoryParts()
    return bf.usdt_through_owner(store, None, USDT, hi, txlist=chain.txlist, get_logs=chain.get_logs,
                                 count=chain.counted), store


S = USDT.start_block


# -- windows ------------------------------------------------------------------------------------

def test_a_window_that_times_out_is_halved_and_the_answers_are_saved_in_order():
    asked, saved = [], []

    def get(contract, topic, lo, hi):
        asked.append((lo, hi))
        if hi - lo + 1 > 4_000:
            raise httpx.ReadTimeout("no answer")
        return [], "s" * 64
    calls = bl.walk_windows(get, USDT.contract, ADDED.topic, 1, 10_000, lambda a, b, logs, sha: saved.append((a, b)),
                            span=8_000)
    assert asked[0] == (1, 8_000) and asked[1] == (1, 4_000)
    assert saved[0] == (1, 4_000) and saved[-1][1] == 10_000
    assert all(x[1] + 1 == y[0] for x, y in zip(saved, saved[1:]))      # no gaps, no overlaps
    assert calls == len(asked)


def test_windows_grow_after_answers_in_a_row():
    asked = []
    bl.walk_windows(lambda c, t, lo, hi: (asked.append(hi - lo + 1), ([], "s" * 64))[1], USDT.contract, ADDED.topic,
                    1, 100_000, lambda *a: None, span=8_000, grow_after=3)
    assert asked[:4] == [8_000, 8_000, 8_000, 12_000]


def test_a_full_window_is_halved_rather_than_saved():
    logs = [{"blockNumber": hex(n)} for n in range(1, 1501)]
    saved = []

    def get(contract, topic, lo, hi):
        return [g for g in logs if lo <= int(g["blockNumber"], 16) <= hi], "s" * 64
    bl.walk_windows(get, USDT.contract, ADDED.topic, 1, 1_500, lambda a, b, got, sha: saved.append(len(got)),
                    span=1_500)
    assert sum(saved) == 1_500 and max(saved) < bl.LOG_CAP


def test_a_single_block_that_times_out_is_not_hidden():
    def get(*a):
        raise httpx.ReadTimeout("no answer")
    with pytest.raises(httpx.ReadTimeout):
        bl.walk_windows(get, USDT.contract, ADDED.topic, 5, 5, lambda *a: None)


def test_reading_resumes_after_the_last_saved_window():
    store, asked = bf.MemoryParts(), []

    def get(contract, topic, lo, hi):
        asked.append((lo, hi))
        if len(asked) == 3:
            raise b.BudgetExhausted("used", dt.datetime(2026, 11, 22, tzinfo=dt.timezone.utc), "backfill")
        return [], "s" * 64
    with pytest.raises(b.BudgetExhausted):
        bf.read_logs(store, get, USDC.contract, "Blacklisted", USDC.events[0].topic, 1, 50_000, span=10_000)
    asked.clear()
    bf.read_logs(store, lambda *a: (asked.append(a[2:]), ([], "s" * 64))[1], USDC.contract, "Blacklisted",
                 USDC.events[0].topic, 1, 50_000, span=10_000)
    assert asked[0] == (20_001, 30_000)                  # the two saved windows are not asked again


def test_etherscan_writes_zero_as_0x():
    assert bl._int("0x") == 0 and bl._int("0x1f") == 31 and bl._int("42") == 42


# -- the owner route ----------------------------------------------------------------------------

def test_the_call_inside_a_proposal_is_read():
    dest, call = bf.submitted_call(submit_input(USDT.contract, "0x0ecb93c0" + word(addr(9))))
    assert dest == USDT.contract and call[:8] == "0ecb93c0" and bf.first_address(call) == addr(9)


def test_short_call_data_reads_as_zeros_on_the_right():
    # 2017: destroyBlackFunds sent with its argument one byte short; the EVM reads the gap as zero.
    assert bf.first_address("f3bdc228" + "0" * 62) == "0x" + "0" * 40
    assert bf.first_address("f3bdc228" + "0" * 24 + "ab" * 19) == "0x" + "ab" * 19 + "00"


def test_every_freeze_release_and_destruction_comes_out_with_its_transaction_and_position():
    chain = Chain([(S + 10, "0ecb93c0", addr(1), True, False),     # freeze
                   (S + 20, None, addr(2), True, False),           # some other contract: not a USDT event
                   (S + 30, "0ecb93c0", addr(3), False, False),    # proposed, never carried out
                   (S + 40, "e4997dc5", addr(1), True, True),      # submission failed: not numbered
                   (S + 50, "e4997dc5", addr(1), True, False),     # release
                   (S + 9_000, "f3bdc228", addr(4), True, False),  # destruction
                   ], destroy_amounts={addr(4): 465_514_638_375})
    result, _ = run(chain)
    got = [(e.action, e.address, e.block_number, e.log_index, e.amount_raw) for e in result.events]
    assert got == [("freeze", addr(1), S + 11, 3, None), ("unfreeze", addr(1), S + 51, 3, None),
                   ("destroy", addr(4), S + 9_001, 3, 465_514_638_375)]
    assert {e.key for e in result.events} == {(g["transactionHash"], 3) for g in chain.usdt_logs}


def test_a_proposal_count_that_does_not_match_stops_the_route():
    chain = Chain([(S + 10, "0ecb93c0", addr(1), True, False)])
    chain.count += 1                                       # a proposal made some way the list does not show
    with pytest.raises(bf.OwnerRouteMismatch):
        run(chain)


def test_a_destruction_the_owner_did_not_make_stops_the_route():
    stray = {"address": USDT.contract, "topics": [DESTROYED.topic], "data": "0x" + word(addr(8)) + word(5),
             "blockNumber": hex(S + 9_002), "logIndex": hex(9), "transactionHash": "0x" + "77" * 32}
    chain = Chain([(S + 9_000, "f3bdc228", addr(4), True, False)], extra_destroy=stray)
    with pytest.raises(bf.OwnerRouteMismatch):
        run(chain)


def test_destruction_amounts_are_read_only_around_the_destructions():
    chain = Chain([(S + 10, "f3bdc228", addr(1), True, False), (S + 2_000, "f3bdc228", addr(2), True, False),
                   (S + 40_000, "f3bdc228", addr(3), True, False)])
    run(chain)
    windows = [r[2:] for r in chain.requests if r[0] == USDT.contract]
    assert windows == [(S + 11, S + 11 + bf.DESTROY_WINDOW - 1), (S + 40_001, S + 40_001 + bf.DESTROY_WINDOW - 1)]


def test_a_second_run_reads_only_what_the_first_did_not_finish():
    chain = Chain([(S + n * 100, "0ecb93c0", addr(n), True, False) for n in range(1, 2_500)])
    first, store = run(chain, hi=S + 300_000)
    again, _ = run(chain, store, hi=S + 300_000)
    assert [e.key for e in again.events] == [e.key for e in first.events] and len(first.events) == 2_499
    assert again.calls == 1                                # only the proposal count is asked again
    pages = [r for r in chain.requests if r[0] == "txlist"]
    assert len(pages) == 3                                 # 2,499 proposals in pages of 1,000, read once


def test_windows_around_blocks():
    assert bf.windows_around([10, 3_000, 4_009, 4_010, 9_000], 4_000) == [(10, 4_009), (4_010, 8_009),
                                                                          (9_000, 12_999)]


# -- USDC by windows ----------------------------------------------------------------------------

def test_usdc_events_come_from_windows():
    blk, unblk = USDC.events
    logs = [{"address": USDC.contract, "topics": [blk.topic, "0x" + word(addr(5))], "data": "0x",
             "blockNumber": hex(USDC.start_block + 7), "logIndex": "0x", "transactionHash": "0x" + "ab" * 32}]

    def get(contract, topic, lo, hi):
        return [g for g in logs if g["topics"][0] == topic and lo <= int(g["blockNumber"], 16) <= hi], "s" * 64
    result = bf.by_windows(bf.MemoryParts(), get, USDC, USDC.start_block + 100_000)
    assert [(e.action, e.address, e.log_index) for e in result.events] == [("freeze", addr(5), 0)]


# -- the allowance ------------------------------------------------------------------------------

RESET = dt.datetime(2026, 11, 22, tzinfo=dt.timezone.utc)


def test_the_backfill_may_go_past_thirty_percent():
    b.check_allowance("backfill", 20, {"worker": 6_000, "scripts": 30_000, "backfill": 30_000}, 34_000, 100_000, RESET)


def test_the_backfill_leaves_the_reserve_for_the_worker(monkeypatch):
    monkeypatch.delenv("BLOCKSCOUT_WORKER_RESERVE", raising=False)
    with pytest.raises(b.BudgetExhausted) as err:
        b.check_allowance("backfill", 20, {"backfill": 75_000}, 25_010, 100_000, RESET)
    assert "25,000 credits are kept for the production worker" in str(err.value)
    monkeypatch.setenv("BLOCKSCOUT_WORKER_RESERVE", "10000")
    b.check_allowance("backfill", 20, {"backfill": 75_000}, 25_010, 100_000, RESET)


def test_scripts_keep_their_thirty_percent_whatever_the_backfill_does():
    with pytest.raises(b.BudgetExhausted):
        b.check_allowance("scripts", 20, {"scripts": 30_000}, 70_000, 100_000, RESET)


def test_the_ledger_accepts_the_backfill():
    class Conn:
        pass
    assert b.PostgresBudget(Conn(), "backfill").consumer == "backfill"
