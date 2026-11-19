"""The cross-check comparison (taw/adapters/chains/evm_crosscheck.py), on the real
Ethereum fixture. The live Blockscout-vs-Etherscan check is scripts/cross_check_evm.py."""
import dataclasses

import pytest

from taw.adapters.base import LocalRawStore, TransferBatch
from taw.adapters.chains.evm_crosscheck import compare
from taw.adapters.chains.evm_etherscan import EtherscanAdapter
from tests.evm_replay import FIXTURES, FakeClock, Replay

ADDR = "0xaab6417993d04a861f1f02c5c4bd2e9145d8f637"


@pytest.fixture
def seen(tmp_path):
    clock = FakeClock()
    adapter = EtherscanAdapter("eth", LocalRawStore(tmp_path), "k" * 20,
                               http=Replay(FIXTURES / "eth" / ADDR).client(), clock=clock.clock, sleep=clock.sleep)
    snap = adapter.fetch_snapshot(ADDR)
    return snap, adapter.fetch_transfers(ADDR)


def with_transfers(batch, transfers):
    return dataclasses.replace(batch, transfers=tuple(transfers))


def test_identical_runs_match(seen):
    snap, batch = seen
    assert compare(snap, batch, snap, batch) == []


def test_internal_index_may_differ_between_providers(seen):
    snap, batch = seen
    other = [dataclasses.replace(t, event_index=7) if t.kind == "internal" else t for t in batch.transfers]
    assert compare(snap, batch, snap, with_transfers(batch, other)) == []


def test_token_log_index_must_match(seen):
    snap, batch = seen
    i = next(i for i, t in enumerate(batch.transfers) if t.kind == "token")
    other = list(batch.transfers)
    other[i] = dataclasses.replace(other[i], event_index=other[i].event_index + 1)
    found = {m.what for m in compare(snap, batch, snap, with_transfers(batch, other))}
    assert found == {"transfer only in first", "transfer only in second"}


def test_balances_and_first_activity_must_match(seen):
    snap, batch = seen
    poorer = dataclasses.replace(snap, native_balance_raw=snap.native_balance_raw - 1)
    missing_first = with_transfers(batch, batch.transfers[:-1])          # drop the oldest transfer
    found = {m.what for m in compare(snap, batch, poorer, missing_first)}
    assert {"native balance (raw)", "first activity (block, tx)", "first incoming (block, tx)",
            "transfer only in first"} <= found


def test_different_blocks_are_reported(seen):
    snap, batch = seen
    later = dataclasses.replace(snap, as_of_block=snap.as_of_block + 1)
    assert [m.what for m in compare(snap, batch, later, batch)] == ["as_of_block"]
