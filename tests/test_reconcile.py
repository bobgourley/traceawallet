"""Reconciliation: does the transfer history explain each token balance? (taw/reconcile.py)"""
import dataclasses
import datetime as dt

import pytest

from taw.adapters.base import AddressSnapshot, LocalRawStore, TokenBalance, TransferBatch, TransferRecord
from taw.adapters.chains.evm_blockscout import BlockscoutAdapter
from taw.adapters.chains.evm_etherscan import EtherscanAdapter, _int
from taw.adapters.chains.tron_trongrid import TronGridAdapter
from taw.adapters.registry import stablecoins_for
from taw.reconcile import NOT_CHECKABLE, NOT_RECONCILED, RECONCILED, reconcile
from tests.evm_replay import FIXTURES as EVM_FIXTURES, FakeClock, Replay as EvmReplay
from tests.tron_replay import FIXTURES as TRON_FIXTURES, Replay as TronReplay

EVM_ADDR = "0xaab6417993d04a861f1f02c5c4bd2e9145d8f637"
TRON_ADDR = "TL7me8SQkNmieVngj9sPQGG2PSMENASHug"
USDT_ETH = "0xdac17f958d2ee523a2206206994597c13d831ec7"
USDC_ETH = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def run(adapter, address):
    snap = adapter.fetch_snapshot(address)
    return snap, adapter.fetch_transfers(snap.address)


def by_contract(recon):
    return {c.contract: c for c in recon.checks}


# -- the real test addresses ------------------------------------------------------

def test_etherscan_test_address_reconciles(tmp_path):
    clock = FakeClock()
    adapter = EtherscanAdapter("eth", LocalRawStore(tmp_path), "k" * 20,
                               http=EvmReplay(EVM_FIXTURES / "eth" / EVM_ADDR).client(),
                               clock=clock.clock, sleep=clock.sleep)
    snap, batch = run(adapter, EVM_ADDR)
    recon = reconcile(snap, batch, stablecoins_for("eth"))
    checks = by_contract(recon)
    assert set(checks) == {USDC_ETH, USDT_ETH}
    for c in checks.values():
        assert (c.status, c.balance_raw, c.net_flow_raw, c.stablecoin) == (RECONCILED, 0, 0, True)
    assert recon.history_incomplete is False and recon.incomplete_reasons == ()


def test_tron_test_address_reconciles(tmp_path):
    clock = FakeClock()
    adapter = TronGridAdapter(LocalRawStore(tmp_path), "k" * 20, http=TronReplay(TRON_FIXTURES / TRON_ADDR).client(),
                              clock=clock.clock, sleep=clock.sleep)
    snap, batch = run(adapter, TRON_ADDR)
    recon = reconcile(snap, batch, stablecoins_for("tron"))
    (usdt,) = recon.checks
    assert (usdt.contract, usdt.status, usdt.balance_raw, usdt.net_flow_raw) == (USDT_TRON, RECONCILED, 0, 0)
    assert recon.history_incomplete is False


class InternalFromKnownGoodBlock(BlockscoutAdapter):
    """Reads internal transfers from block 23,397,763, as the 2026-11-19 diagnostic run
    did (Blockscout's index is unfinished before it). Test only."""

    def _account(self, action, addr, head, want, keep, key):
        if action != "txlistinternal":
            return super()._account(action, addr, head, want, keep, key)
        return self._scan({"module": "account", "action": action, "address": addr}, lo=23_397_763, hi=head,
                          descending=True, want=want, keep=keep, key=key,
                          block_of=lambda row: _int(row["blockNumber"]), max_pages=40)


def test_blockscout_6_of_41_is_caught(tmp_path):
    """Blockscout's log search returned 6 of the wallet's 41 token transfers, all answering OK.
    The wallet passes everything through, so in minus out still equals the balance (0)
    and the arithmetic alone cannot see the gap. The token-list check does, and the
    snapshot is marked incomplete."""
    clock = FakeClock()
    adapter = InternalFromKnownGoodBlock("eth", LocalRawStore(tmp_path), "proapi_test_not_real",
                                         http=EvmReplay(EVM_FIXTURES / "blockscout-eth" / EVM_ADDR).client(),
                                         at_block=26_451_986, clock=clock.clock, sleep=clock.sleep)
    snap, batch = run(adapter, EVM_ADDR)
    assert sum(1 for t in batch.transfers if t.kind == "token") == 6
    recon = reconcile(snap, batch, stablecoins_for("eth"))
    assert by_contract(recon)[USDC_ETH].status == RECONCILED            # the limitation, stated
    assert recon.history_incomplete is True
    assert recon.incomplete_reasons == ("35 of 41 token-list rows have no matching Transfer log",)


# -- each status ------------------------------------------------------------------

A, B = "0x" + "a" * 40, "0x" + "b" * 40
STABLE, OTHER, UNREAD = "0x" + "5" * 40, "0x" + "6" * 40, "0x" + "7" * 40
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def snap(balances=(), read=(STABLE, OTHER), block=100):
    return AddressSnapshot("eth", A, block, T0, "ETH", 18, 0, tuple(TokenBalance(c, n, 6, None) for c, n in balances),
                           False, False, "etherscan", ("0" * 64,), balances_read=tuple(read))


def xfer(n, contract, frm, to, amount, block=50):
    return TransferRecord("eth", "0x" + f"{n:064x}", "token", n, frm, to, contract, None, 6, amount, block, T0,
                          "etherscan", "0" * 64)


def batch(transfers, truncated=False, reasons=()):
    return TransferBatch("eth", A, tuple(transfers), truncated, 2000, incomplete_reasons=tuple(reasons))


def test_stablecoin_that_does_not_reconcile_marks_history_incomplete():
    recon = reconcile(snap([(STABLE, 70)]), batch([xfer(1, STABLE, B, A, 100), xfer(2, STABLE, A, B, 20)]), [STABLE])
    (c,) = recon.checks
    assert (c.status, c.balance_raw, c.net_flow_raw) == (NOT_RECONCILED, 70, 80)
    assert recon.history_incomplete is True
    assert recon.incomplete_reasons == (f"stablecoin {STABLE} does not reconcile: balance 70, transfers in minus out 80",)


def test_other_token_that_does_not_reconcile_is_reported_but_not_incomplete():
    recon = reconcile(snap([(OTHER, 5)]), batch([xfer(1, OTHER, B, A, 9)]), [STABLE])
    assert by_contract(recon)[OTHER].status == NOT_RECONCILED and recon.history_incomplete is False


def test_truncated_history_is_not_checkable_and_not_flagged():
    recon = reconcile(snap([(STABLE, 70)]), batch([xfer(1, STABLE, B, A, 100)], truncated=True), [STABLE])
    (c,) = recon.checks
    assert (c.status, c.reason, c.balance_raw, c.net_flow_raw) == (NOT_CHECKABLE, "history_truncated", 70, 100)
    assert recon.history_incomplete is False


def test_balance_not_read_is_not_checkable():
    recon = reconcile(snap(), batch([xfer(1, UNREAD, B, A, 3)]), [STABLE])
    (c,) = recon.checks
    assert (c.contract, c.status, c.reason, c.balance_raw) == (UNREAD, NOT_CHECKABLE, "balance_not_read", None)


def test_stablecoin_held_with_no_transfers_seen_is_flagged():
    recon = reconcile(snap([(STABLE, 10)]), batch([]), [STABLE])
    assert by_contract(recon)[STABLE].status == NOT_RECONCILED and recon.history_incomplete is True


def test_transfers_after_the_snapshot_block_are_left_out():
    recon = reconcile(snap([(STABLE, 100)], block=100),
                      batch([xfer(1, STABLE, B, A, 100, block=90), xfer(2, STABLE, A, B, 100, block=101)]), [STABLE])
    assert by_contract(recon)[STABLE].status == RECONCILED


def test_transfer_to_self_nets_to_zero():
    recon = reconcile(snap([(STABLE, 4)]), batch([xfer(1, STABLE, B, A, 4), xfer(2, STABLE, A, A, 9)]), [STABLE])
    assert by_contract(recon)[STABLE].net_flow_raw == 4 and recon.history_incomplete is False


def test_untouched_zero_balances_are_not_listed():
    assert reconcile(snap(), batch([]), [STABLE]).checks == ()


def test_full_holdings_means_unlisted_tokens_hold_zero():
    s = dataclasses.replace(snap(), balances_read=None)                  # as TronGrid gives it
    recon = reconcile(s, batch([xfer(1, UNREAD, B, A, 3)]), [])
    assert by_contract(recon)[UNREAD].status == NOT_RECONCILED            # 0 held, 3 received


def test_adapter_reasons_mark_history_incomplete():
    recon = reconcile(snap(), batch([], reasons=["2 of 9 token-list rows have no matching Transfer log"]), [STABLE])
    assert recon.history_incomplete and recon.incomplete_reasons == ("2 of 9 token-list rows have no matching Transfer log",)


def test_numbers_are_stored_as_strings():
    (c,) = reconcile(snap([(STABLE, 2 ** 200)]), batch([]), [STABLE]).checks
    assert c.as_json()["balance_raw"] == str(2 ** 200) and c.as_json()["net_flow_raw"] == "0"


def test_different_addresses_are_refused():
    with pytest.raises(ValueError):
        reconcile(snap(), dataclasses.replace(batch([]), address=B), [])
