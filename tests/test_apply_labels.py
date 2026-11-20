"""Who counts as a funder or a first-hop destination of the looked-up address."""
import datetime as dt

from taw.adapters.base import TransferRecord
from taw.labels import LOCAL_SOURCES, counterparties

WHEN = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
ME = "0x" + "aa" * 20


def rec(chain, kind, frm, to, tx="0x01", idx=0):
    return TransferRecord(chain=chain, tx_hash=tx, kind=kind, event_index=idx, from_address=frm, to_address=to,
                          token_contract=None, token_symbol=None, token_decimals=8, amount_raw=1, block_number=1,
                          block_time=WHEN, source_slug="x", raw_sha256="0" * 64)


def test_account_chain_funders_and_destinations_ignore_case():
    b, c = "0x" + "bb" * 20, "0x" + "cc" * 20
    found = counterparties("eth", ME.upper().replace("0X", "0x"), [
        rec("eth", "native", b, ME), rec("eth", "token", ME, c), rec("eth", "native", c, ME), rec("eth", "native", ME, ME)])
    assert found == {b: {"funder"}, c: {"funder", "destination"}}


def test_tron_is_case_sensitive():
    me, other = "TC66d59uus8AWhYDvAgWM55T8NRa7KNWAN", "TH4qXPSp3S5g3HnDr1pgnEq4XAoJh7McvZ"
    assert counterparties("tron", me, [rec("tron", "token", me, other), rec("tron", "token", other, me.lower())]) == \
        {other: {"destination"}}


def test_bitcoin_uses_the_other_side_of_each_transaction():
    me, f1, f2, out, change_partner = "1Me", "1Funder", "1Cofunder", "1Out", "1Paidtogether"
    rows = [
        # tx A pays me: its inputs are funders; its other outputs are not (they're the payer's change)
        rec("btc", "utxo_in", f1, None, "A", 0), rec("btc", "utxo_in", f2, None, "A", 1),
        rec("btc", "utxo_out", None, me, "A", 0), rec("btc", "utxo_out", None, change_partner, "A", 1),
        # tx B spends from me: its outputs are destinations, my change is not
        rec("btc", "utxo_in", me, None, "B", 0), rec("btc", "utxo_out", None, out, "B", 0),
        rec("btc", "utxo_out", None, me, "B", 1),
    ]
    assert counterparties("btc", me, rows) == {f1: {"funder"}, f2: {"funder"}, out: {"destination"}}


def test_only_ofac_and_the_exchange_lists_are_local():
    assert LOCAL_SOURCES == ("ofac-sdn", "exchange-por", "okx-por")
