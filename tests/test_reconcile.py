"""Completeness checks (taw/reconcile.py): token, native and transaction-count checks,
and the adapters' own list checks."""
import dataclasses
import datetime as dt

import pytest

from taw.adapters.base import (FAILED, NOT_RUN, PASSED, AddressSnapshot, CheckResult, LocalRawStore, TokenBalance,
                               TransferBatch, TransferRecord)
from taw.adapters.chains.evm_blockscout import BlockscoutAdapter
from taw.adapters.chains.evm_etherscan import EtherscanAdapter
from taw.adapters.chains.tron_trongrid import TronGridAdapter
from taw.adapters.registry import stablecoins_for
from taw.reconcile import NOT_CHECKABLE, NOT_RECONCILED, RECONCILED, run_checks
from tests.evm_replay import FIXTURES as EVM_FIXTURES, FakeClock, Replay as EvmReplay
from tests.tron_replay import FIXTURES as TRON_FIXTURES, Replay as TronReplay

EVM_ADDR = "0xaab6417993d04a861f1f02c5c4bd2e9145d8f637"
ARB_ADDR = "0x9be85deda2de03605af25fe6f33652559519e377"
TRON_ADDR = "TL7me8SQkNmieVngj9sPQGG2PSMENASHug"
USDT_ETH = "0xdac17f958d2ee523a2206206994597c13d831ec7"
USDC_ETH = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def run(adapter, address):
    snap = adapter.fetch_snapshot(address)
    return snap, adapter.fetch_transfers(snap.address)


def statuses(result):
    return {c.name: c.status for c in result.checks}


def by_contract(result):
    return {t.contract: t for t in result.tokens}


# -- the real test addresses ------------------------------------------------------

def test_etherscan_ethereum_passes_every_check(tmp_path):
    clock = FakeClock()
    adapter = EtherscanAdapter("eth", LocalRawStore(tmp_path), "k" * 20,
                               http=EvmReplay(EVM_FIXTURES / "eth" / EVM_ADDR).client(),
                               clock=clock.clock, sleep=clock.sleep)
    snap, batch = run(adapter, EVM_ADDR)
    result = run_checks(snap, batch, stablecoins_for("eth"))
    assert statuses(result) == {"stablecoin_reconciliation": PASSED, "other_token_reconciliation": NOT_RUN,
                                "native_reconciliation": PASSED, "tx_count": PASSED,
                                "token_list_matches_logs": PASSED}
    native = next(c for c in result.checks if c.name == "native_reconciliation")
    assert native.details == {"balance_raw": "428974286563892", "in_raw": "1073882133973332000",
                              "out_raw": "1072978686800000000", "fees_raw": "474472886768108",
                              "withdrawals_raw": "0", "expected_raw": "428974286563892", "transactions_sent": 22}
    for t in result.tokens:
        assert (t.status, t.balance_raw, t.net_flow_raw) == (RECONCILED, 0, 0)
    assert result.history_incomplete is False


def test_etherscan_arbitrum_native_reconciles_with_fees(tmp_path):
    clock = FakeClock()
    adapter = EtherscanAdapter("arbitrum", LocalRawStore(tmp_path), "k" * 20,
                               http=EvmReplay(EVM_FIXTURES / "arbitrum" / ARB_ADDR).client(),
                               clock=clock.clock, sleep=clock.sleep)
    snap, batch = run(adapter, ARB_ADDR)
    result = run_checks(snap, batch, stablecoins_for("arbitrum"))
    native = next(c for c in result.checks if c.name == "native_reconciliation")
    assert native.status == PASSED and native.details["fees_raw"] == "20609417062912"
    assert native.details["expected_raw"] == native.details["balance_raw"] == "3692587213088"
    assert statuses(result)["tx_count"] == PASSED and result.history_incomplete is False


def test_blockscout_ethereum_native_not_run_outside_its_internal_range(tmp_path):
    clock = FakeClock()
    adapter = BlockscoutAdapter("eth", LocalRawStore(tmp_path), "proapi_test_not_real", at_block=26_452_607,
                                http=EvmReplay(EVM_FIXTURES / "blockscout-rest-eth" / EVM_ADDR).client(),
                                clock=clock.clock, sleep=clock.sleep)
    from taw.adapters.chains.evm_blockscout import clear_range_cache
    clear_range_cache()
    snap, batch = run(adapter, EVM_ADDR)
    result = run_checks(snap, batch, stablecoins_for("eth"))
    assert statuses(result) == {"stablecoin_reconciliation": PASSED, "other_token_reconciliation": NOT_RUN,
                                "native_reconciliation": NOT_RUN, "tx_count": PASSED, "token_lists_agree": PASSED}
    native = next(c for c in result.checks if c.name == "native_reconciliation")
    assert native.reason == ("Internal transfers were read only for blocks 23,397,898 to 26,452,607; "
                             "outside that range nothing is checked.")
    assert result.history_incomplete is False


def test_tron_test_address(tmp_path):
    clock = FakeClock()
    adapter = TronGridAdapter(LocalRawStore(tmp_path), "k" * 20, http=TronReplay(TRON_FIXTURES / TRON_ADDR).client(),
                              clock=clock.clock, sleep=clock.sleep)
    snap, batch = run(adapter, TRON_ADDR)
    result = run_checks(snap, batch, stablecoins_for("tron"))
    (usdt,) = result.tokens
    assert (usdt.contract, usdt.status, usdt.balance_raw, usdt.net_flow_raw) == (USDT_TRON, RECONCILED, 0, 0)
    reasons = {c.name: (c.status, c.reason) for c in result.checks}
    assert reasons["stablecoin_reconciliation"] == (PASSED, None)
    assert reasons["native_reconciliation"] == (NOT_RUN, "TRX staking, unstaking and resource delegation are not "
                                                         "parsed yet; they move the balance.")
    assert reasons["tx_count"] == (NOT_RUN, "Tron accounts have no transaction nonce.")
    assert result.history_incomplete is False


class LogsPathBlockscout(BlockscoutAdapter):
    """Blockscout as it worked on 2026-11-19, before tokens moved to the REST list: token
    transfers from getLogs, internal transfers from block 23,397,763. Replays the response
    that returned 6 of 41 transfers. Test only."""
    _token_transfers = EtherscanAdapter._token_transfers
    _firsts_token = EtherscanAdapter._firsts_token

    def _internal_range(self, address, head):
        return 23_397_763, head

    def _nonce(self, address, tag):
        return None, None                       # not in that recording

    def _withdrawals(self, address, head):
        return 0, []


def test_blockscout_6_of_41_is_caught(tmp_path):
    """The wallet passes everything through, so in minus out still equals the balance (0)
    and the arithmetic alone cannot see the gap. The token-list check can."""
    clock = FakeClock()
    adapter = LogsPathBlockscout("eth", LocalRawStore(tmp_path), "proapi_test_not_real",
                                 http=EvmReplay(EVM_FIXTURES / "blockscout-eth" / EVM_ADDR).client(),
                                 at_block=26_451_986, clock=clock.clock, sleep=clock.sleep)
    snap, batch = run(adapter, EVM_ADDR)
    assert sum(1 for t in batch.transfers if t.kind == "token") == 6
    result = run_checks(snap, batch, stablecoins_for("eth"))
    assert by_contract(result)[USDC_ETH].status == RECONCILED            # the limitation, stated
    assert statuses(result)["token_list_matches_logs"] == FAILED
    assert result.history_incomplete is True
    assert result.incomplete_reasons == ("35 of 41 token-list rows have no matching Transfer log",)


# -- token checks, each status ------------------------------------------------------------

A, B = "0x" + "a" * 40, "0x" + "b" * 40
STABLE, OTHER, UNREAD = "0x" + "5" * 40, "0x" + "6" * 40, "0x" + "7" * 40
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def snap(balances=(), read=(STABLE, OTHER), block=100, native=0, nonce=None, chain="eth", contract=False):
    return AddressSnapshot(chain, A, block, T0, "ETH", 18, native,
                           tuple(TokenBalance(c, n, 6, None) for c, n in balances), contract, False, "etherscan",
                           ("0" * 64,), balances_read=None if read is None else tuple(read), nonce=nonce)


def xfer(n, contract, frm, to, amount, block=50, kind="token"):
    return TransferRecord("eth", "0x" + f"{n:064x}", kind, n, frm, to, contract, None, 6 if contract else 18,
                          amount, block, T0, "etherscan", "0" * 64)


def batch(transfers, truncated=False, checks=(), sent=0, fees=0, withdrawals=0, internal=(0, 100), chain="eth"):
    return TransferBatch(chain, A, tuple(transfers), truncated, 2000, checks=tuple(checks), sent_tx_count=sent,
                         fees_paid_raw=fees, withdrawals_raw=withdrawals, internal_range=internal)


def check(result, name):
    return next(c for c in result.checks if c.name == name)


def test_stablecoin_that_does_not_reconcile_marks_history_incomplete():
    result = run_checks(snap([(STABLE, 70)]), batch([xfer(1, STABLE, B, A, 100), xfer(2, STABLE, A, B, 20)]),
                        [STABLE])
    (t,) = result.tokens
    assert (t.status, t.balance_raw, t.net_flow_raw) == (NOT_RECONCILED, 70, 80)
    assert check(result, "stablecoin_reconciliation").status == FAILED
    assert result.history_incomplete is True
    assert result.incomplete_reasons == (f"stablecoin {STABLE} does not reconcile: balance 70, "
                                         f"transfers in minus out 80",)


def test_other_token_that_does_not_reconcile_is_reported_but_not_incomplete():
    result = run_checks(snap([(OTHER, 5)]), batch([xfer(1, OTHER, B, A, 9)]), [STABLE])
    assert by_contract(result)[OTHER].status == NOT_RECONCILED
    assert check(result, "other_token_reconciliation").status == FAILED and result.history_incomplete is False


def test_truncated_history_is_not_checkable_and_not_flagged():
    result = run_checks(snap([(STABLE, 70)]), batch([xfer(1, STABLE, B, A, 100)], truncated=True), [STABLE])
    (t,) = result.tokens
    assert (t.status, t.reason, t.balance_raw, t.net_flow_raw) == (NOT_CHECKABLE, "history_truncated", 70, 100)
    assert check(result, "stablecoin_reconciliation").status == NOT_RUN and result.history_incomplete is False


def test_balance_not_read_is_not_checkable():
    result = run_checks(snap(), batch([xfer(1, UNREAD, B, A, 3)]), [STABLE])
    (t,) = result.tokens
    assert (t.contract, t.status, t.reason, t.balance_raw) == (UNREAD, NOT_CHECKABLE, "balance_not_read", None)


def test_stablecoin_held_with_no_transfers_seen_is_flagged():
    result = run_checks(snap([(STABLE, 10)]), batch([]), [STABLE])
    assert by_contract(result)[STABLE].status == NOT_RECONCILED and result.history_incomplete is True


def test_transfers_after_the_snapshot_block_are_left_out():
    result = run_checks(snap([(STABLE, 100)], block=100),
                        batch([xfer(1, STABLE, B, A, 100, block=90), xfer(2, STABLE, A, B, 100, block=101)]), [STABLE])
    assert by_contract(result)[STABLE].status == RECONCILED


def test_transfer_to_self_nets_to_zero():
    result = run_checks(snap([(STABLE, 4)]), batch([xfer(1, STABLE, B, A, 4), xfer(2, STABLE, A, A, 9)]), [STABLE])
    assert by_contract(result)[STABLE].net_flow_raw == 4 and result.history_incomplete is False


def test_untouched_zero_balances_are_not_listed():
    assert run_checks(snap(), batch([]), [STABLE]).tokens == ()


def test_full_holdings_means_unlisted_tokens_hold_zero():
    result = run_checks(snap(read=None), batch([xfer(1, UNREAD, B, A, 3)]), [])
    assert by_contract(result)[UNREAD].status == NOT_RECONCILED            # 0 held, 3 received


def test_numbers_are_stored_as_strings():
    (t,) = run_checks(snap([(STABLE, 2 ** 200)]), batch([]), [STABLE]).tokens
    assert t.as_json()["balance_raw"] == str(2 ** 200) and t.as_json()["net_flow_raw"] == "0"


def test_different_addresses_are_refused():
    with pytest.raises(ValueError):
        run_checks(snap(), dataclasses.replace(batch([]), address=B), [])


# -- adapter checks ------------------------------------------------------------------------

def test_failed_adapter_check_marks_history_incomplete():
    lists = CheckResult("token_lists_agree", FAILED, "Blockscout's two token lists disagree")
    result = run_checks(snap(), batch([], checks=[lists]), [STABLE])
    assert result.history_incomplete and result.incomplete_reasons == ("Blockscout's two token lists disagree",)


def test_every_check_is_listed_once():
    names = [c.name for c in run_checks(snap(), batch([]), [STABLE]).checks]
    assert names == ["stablecoin_reconciliation", "other_token_reconciliation", "native_reconciliation", "tx_count"]


# -- native coin --------------------------------------------------------------------------------

def native_xfers():
    return [xfer(1, None, B, A, 1_000, kind="native"), xfer(2, None, B, A, 500, kind="internal"),
            xfer(3, None, A, B, 300, kind="native")]


def test_native_reconciles_with_fees_and_withdrawals():
    # 1,000 + 500 in, 300 out, 40 fees, 25 withdrawals: 1,185
    result = run_checks(snap(native=1_185), batch(native_xfers(), sent=2, fees=40, withdrawals=25), [])
    c = check(result, "native_reconciliation")
    assert c.status == PASSED and c.details["expected_raw"] == "1185"


def test_native_mismatch_marks_history_incomplete():
    result = run_checks(snap(native=1_200), batch(native_xfers(), sent=2, fees=40, withdrawals=25), [])
    c = check(result, "native_reconciliation")
    assert c.status == FAILED and c.reason == "ETH balance 1200 but in - out - fees + withdrawals = 1185"
    assert result.history_incomplete is True


@pytest.mark.parametrize("kw, reason", [
    ({"truncated": True}, "The history is truncated at 2,000 transfers."),
    ({"internal": (40, 100)}, "Internal transfers were read only for blocks 40 to 100; outside that range nothing "
                              "is checked."),
    ({"fees": None}, "The list of transactions sent was not read in full."),
    ({"withdrawals": None}, "Validator withdrawals were not read."),
])
def test_native_not_run_reasons(kw, reason):
    c = check(run_checks(snap(native=1), batch(native_xfers(), **kw), []), "native_reconciliation")
    assert (c.status, c.reason) == (NOT_RUN, reason)


def test_native_not_run_on_optimism():
    s = dataclasses.replace(snap(), chain="optimism")
    c = check(run_checks(s, batch([], chain="optimism"), []), "native_reconciliation")
    assert c.status == NOT_RUN and "L1 data fee" in c.reason


# -- transaction count -------------------------------------------------------------------------------

def test_nonce_matches_transactions_sent():
    c = check(run_checks(snap(nonce=3), batch([], sent=3), []), "tx_count")
    assert c.status == PASSED and c.details == {"nonce": 3, "transactions_sent": 3}


def test_nonce_mismatch_marks_history_incomplete():
    result = run_checks(snap(nonce=5), batch([], sent=3), [])
    c = check(result, "tx_count")
    assert c.status == FAILED and result.history_incomplete is True
    assert c.reason == "The address has sent 5 transactions (nonce) but the provider listed 3"


@pytest.mark.parametrize("s, b, reason", [
    (snap(nonce=None), batch([], sent=3), "The provider could not read the nonce at the snapshot block."),
    (snap(nonce=9, contract=True), batch([], sent=0), "The address is a contract: its nonce counts the contracts "
                                                      "it created, not transactions it sent."),
    (snap(nonce=3), batch([], sent=3, truncated=True), "The history is truncated, so not every transaction sent "
                                                       "was read."),
])
def test_nonce_not_run_reasons(s, b, reason):
    c = check(run_checks(s, b, []), "tx_count")
    assert (c.status, c.reason) == (NOT_RUN, reason)


def test_eip7702_account_skips_the_nonce_check():
    s = dataclasses.replace(snap(nonce=4), eip7702_delegate="0x" + "c" * 40)
    c = check(run_checks(s, batch([], sent=3), []), "tx_count")
    assert c.status == NOT_RUN and "EIP-7702" in c.reason
