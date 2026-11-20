"""Transaction matching (taw/matching.py), every result type.

The real example is case 3 (W.D. Pa. 2:26-cv-00087, paragraphs 16 and 22): A.T.'s
Coinbase transaction of 242,209.53 USDC to 0xFA0a... on or about 2025-05-10. On
chain it is one of fifteen transfers in a Coinbase batch transaction, and
242,209.413589 USDC arrived: 0.116411 less than the amount stated, the kind of
fee difference matching allows for. The next USDC out of 0xFA0a is the 230,000
USDC to 0x5842... that paragraph 22 describes.
"""
import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from taw.matching import (NOT_PROOF, MatchInputs, amount_fits, combine, date_fits, match_fetched,
                          match_transfers)
from tests import report_fixtures as rf

CASE = "wdpa-2-26-cv-00087-usdt"
SNAP, BATCH, CHAIN = rf.chain_parts(CASE)
FETCH = rf.fetch_from(CHAIN["transactions"][0])
ME = SNAP.address                                       # 0xfa0a50dc366e2f0a5381e4d4b5fede589ab633a9
COINBASE_SENDER = "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43"   # the transfer's sender, as read on chain
STATED = MatchInputs("USDC", Decimal("242209.53"), dt.date(2025, 5, 10), COINBASE_SENDER)


def run(inp, transfers=BATCH.transfers, truncated=False, oldest=None):
    return match_transfers("eth", ME, transfers, inp, truncated=truncated, oldest_read=oldest)


def test_the_fixture_holds_the_victim_transfer():
    assert FETCH.found and FETCH.tx_hash == rf.CASE3_VICTIM_TX
    mine = [r for r in FETCH.records if r.to_address == ME]
    assert len(mine) == 1 and mine[0].amount_raw == 242_209_413_589 and mine[0].token_symbol == "USDC"
    assert len(FETCH.records) == 15                       # a batch: fourteen other recipients


def test_matched_by_hash_even_though_it_is_read_directly():
    r = match_fetched("eth", ME, FETCH, BATCH.transfers)
    assert (r["status"], r["method"]) == ("matched", "tx_hash")
    assert r["transfer"]["amount"] == "242209.413589" and r["transfer"]["time"] == "2025-05-10T01:38:59Z"
    assert "internal transfers" in r["note"]


def test_matched_by_hash_when_the_transfers_read_do_not_reach_back_that_far():
    newer_only = [t for t in BATCH.transfers if t.block_time > FETCH.block_time]
    r = match_fetched("eth", ME, FETCH, newer_only)
    assert r["status"] == "matched" and r["transfer"]["tx_hash"] == rf.CASE3_VICTIM_TX


def test_the_hash_pays_another_address():
    r = match_fetched("eth", "0x5842b2e75d3566bd0d3f9f520e71c1f3b30b99a1", FETCH, [])
    assert r["status"] == "pays_another_address"
    assert ME in r["paid_addresses"] and len(r["paid_addresses"]) == 15


def test_an_unknown_hash_is_not_found_and_cannot_be_ruled_out():
    unknown = dataclasses.replace(FETCH, found=False, records=(), note="Etherscan does not know this transaction")
    r = match_fetched("eth", ME, unknown, BATCH.transfers)
    assert (r["status"], r["reason"], r["can_rule_out"]) == ("not_found", "tx_not_found", False)


def test_sender_amount_and_date_with_a_fee_difference():
    r = run(STATED)
    assert (r["status"], r["method"]) == ("matched", "sender_amount_date")
    assert r["transfer"]["tx_hash"] == rf.CASE3_VICTIM_TX and r["transfer"]["amount"] == "242209.413589"


def test_amount_and_date_without_the_sender():
    r = run(dataclasses.replace(STATED, sending_address=None))
    assert (r["status"], r["method"]) == ("matched", "amount_date")


def test_a_wrong_sender_falls_back_to_amount_and_date():
    r = run(dataclasses.replace(STATED, sending_address="0x" + "1" * 40))
    assert (r["status"], r["method"]) == ("matched", "amount_date")


def test_next_outgoing_transfers_of_the_same_asset():
    r = run(STATED)
    nxt = r["next_outgoing"]
    assert nxt[0]["to"] == "0x5842b2e75d3566bd0d3f9f520e71c1f3b30b99a1" and nxt[0]["amount"] == "230000"
    assert all(x["asset"] == "USDC" and x["amount"] != "0" for x in nxt)   # zero-value rows are left out
    assert r["next_outgoing_note"] == NOT_PROOF and "does not show that they were your funds" in NOT_PROOF


def test_several_candidates_are_returned_and_none_is_picked():
    t = next(x for x in BATCH.transfers if x.tx_hash == rf.CASE3_VICTIM_TX)
    twin = dataclasses.replace(t, tx_hash="0x" + "ab" * 32, block_time=t.block_time + dt.timedelta(hours=3),
                               block_number=t.block_number + 900)
    r = run(dataclasses.replace(STATED, sending_address=None), transfers=list(BATCH.transfers) + [twin])
    assert (r["status"], r["method"]) == ("candidates", "amount_date")
    assert {c["tx_hash"] for c in r["candidates"]} == {rf.CASE3_VICTIM_TX, "0x" + "ab" * 32}
    assert "transfer" not in r


def test_not_found_in_a_full_history_can_be_ruled_out():
    r = run(dataclasses.replace(STATED, amount=Decimal("1000000")))
    assert (r["status"], r["can_rule_out"], r["reason"]) == ("not_found", True, "not_in_transfers_read")


def test_not_found_in_a_truncated_history_cannot_be_ruled_out():
    oldest = dt.datetime(2025, 6, 1, tzinfo=dt.timezone.utc)
    newer = [t for t in BATCH.transfers if t.block_time >= oldest]
    r = run(STATED, transfers=newer, truncated=True, oldest=oldest)
    assert (r["status"], r["can_rule_out"], r["reason"]) == ("not_found", False, "history_truncated")
    covered = run(STATED, transfers=newer, truncated=True, oldest=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc))
    assert covered["can_rule_out"] is True                 # the date given lies inside what was read


def test_nothing_to_match_on():
    r = run(MatchInputs(asset="USDC"))
    assert (r["status"], r["reason"]) == ("not_attempted", "needs_amount_or_tx_hash")


def test_an_unrecognised_asset_name_is_not_used_and_says_so():
    r = run(dataclasses.replace(STATED, asset="USDCC"))
    assert r["status"] == "matched" and r["asset_note"] == "asset_not_recognised"


def test_another_asset_is_not_compared():
    r = run(dataclasses.replace(STATED, asset="USDT"))
    assert r["status"] == "not_found"


@pytest.mark.parametrize("received,sent,ok", [
    ("242209.413589", "242209.53", True),          # the case: 0.116 less
    ("990", "1000", True), ("989.4", "1000", False),       # up to 1% less, plus rounding of "1000"
    ("1000.004", "1000.00", True), ("1000.01", "1000.00", False),    # rounding of the last digit given, no more
])
def test_amount_tolerance(received, sent, ok):
    assert amount_fits(Decimal(received), Decimal(sent)) is ok


def test_date_window_is_a_day_either_side_in_utc():
    sent = dt.date(2025, 5, 10)
    assert date_fits(dt.datetime(2025, 5, 9, 0, 0, tzinfo=dt.timezone.utc), sent)
    assert date_fits(dt.datetime(2025, 5, 11, 23, 59, tzinfo=dt.timezone.utc), sent)
    assert not date_fits(dt.datetime(2025, 5, 12, 0, 0, tzinfo=dt.timezone.utc), sent)


def test_combining_chains():
    hit = match_fetched("eth", ME, FETCH, BATCH.transfers)
    miss = {"status": "not_found", "method": "tx_hash", "can_rule_out": False, "reason": "tx_not_found"}
    one = combine({"eth": hit, "arbitrum": miss, "optimism": miss})
    assert one["status"] == "matched" and one["matched_chain"] == "eth"
    two = combine({"eth": hit, "arbitrum": hit})
    assert two["status"] == "candidates"
    none = combine({"eth": miss, "arbitrum": miss})
    assert none["status"] == "not_found" and none["can_rule_out"] is False
