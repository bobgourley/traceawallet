"""The report builder on the five ground-truth cases (tests/report_fixtures.py).

Golden files: tests/fixtures/reports/<case>/report.json (the data) and report.txt
(the rendered text). A change to either is a change to what victims read, so it
must be looked at: run with TAW_UPDATE_GOLDEN=1 to rewrite them, then read the
diff before committing. CI never writes them.
"""
import dataclasses
import datetime as dt
import json
import os
import re
from decimal import Decimal

import pytest

from taw.report import SCHEMA_VERSION, TEMPLATE_VERSION
from taw.report.build import build, lookalike_groups, odd_characters, visible
from taw.report.canonical import NotCanonical, canonical_bytes, report_hash
from taw.report.render import TEMPLATES, bucket, num, outside_fixed_sections, render_text
from tests import report_fixtures as rf

UPDATE = os.environ.get("TAW_UPDATE_GOLDEN") == "1"
CASES = sorted(rf.CASES)


def built(case):
    inp, *_ = rf.case_input(case)
    return inp, build(inp)


def digest(inp, data):
    return report_hash(SCHEMA_VERSION, TEMPLATE_VERSION, inp.chain, inp.address, inp.snapshot["as_of_block"], data)


def golden(path, text):
    if UPDATE:
        path.write_text(text)
    assert path.exists(), f"{path} is missing: run with TAW_UPDATE_GOLDEN=1 and read the result"
    assert text == path.read_text(), f"{path.name} changed: run with TAW_UPDATE_GOLDEN=1 and read the diff"


@pytest.mark.parametrize("case", CASES)
def test_golden_report(case):
    inp, data = built(case)
    folder = rf.FIXTURES / case
    golden(folder / "report.json", json.dumps({"sha256": digest(inp, data), "data": data}, indent=1,
                                              sort_keys=True, ensure_ascii=False) + "\n")
    golden(folder / "report.txt", render_text(data))


# -- the hash -------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES)
def test_building_twice_gives_the_same_hash(case):
    inp, first = built(case)
    again = build(inp)
    shuffled = dataclasses.replace(inp, transfers=tuple(reversed(inp.transfers)), labels=tuple(reversed(inp.labels)),
                                   label_checks=tuple(reversed(inp.label_checks)))
    assert digest(inp, first) == digest(inp, again) == digest(inp, build(shuffled))
    assert canonical_bytes(first) == canonical_bytes(again)


def test_the_label_cut_off_is_part_of_the_report():
    inp, data = built("sdny-1-26-cv-08010-usdt")
    later = dataclasses.replace(inp, labels_as_of=inp.labels_as_of + dt.timedelta(days=1))
    assert digest(inp, data) != digest(later, build(later))


def test_canonical_json_refuses_what_could_print_two_ways():
    for bad in ({"a": 1.5}, {"a": dt.date(2026, 1, 1)}, {"a": Decimal("1")}, {"a": 2 ** 60}, {1: "x"}):
        with pytest.raises(NotCanonical):
            canonical_bytes(bad)
    assert canonical_bytes({"b": "é", "a": [1, None, True]}) == '{"a":[1,null,true],"b":"é"}'.encode()


def test_amounts_and_times_are_strings():
    _, data = built("sdny-1-26-cv-08010-usdt")
    usdt = next(t for t in data["holdings"]["tokens"] if t["asset"] == "USDT")
    assert isinstance(usdt["amount_raw"], str) and isinstance(data["header"]["as_of_time"], str)
    row = data["activity"]["assets"][0]
    assert isinstance(row["in"]["total"], str) and isinstance(row["in"]["total_raw"], str)


# -- wording ----------------------------------------------------------------------------------------

ASSESSMENT = re.compile(r"\b(scam\w*|fraud\w*|safe|safety|clean|suspicious|suspect\w*|legit\w*|risky|risk|"
                        r"criminal\w*|illicit|trusted|trustworthy|dangerous|bad actor|launder\w*|stolen|thie\w+)\b", re.I)


@pytest.mark.parametrize("case", CASES)
def test_no_assessment_words_outside_the_fixed_sections(case):
    _, data = built(case)
    text = render_text(data, prior_lookups="2 to 5")
    body = outside_fixed_sections(text)
    assert not ASSESSMENT.findall(body), ASSESSMENT.findall(body)
    assert "recovery scams" in text and "ic3.gov" in text          # the fixed sections are there
    assert text.count("(DRAFT, not yet approved)") == 2


def test_the_template_itself_has_no_assessment_words():
    source = (TEMPLATES / "report.txt.j2").read_text()
    source = re.sub(r"\{\{ FIXED_START.*?\{\{ FIXED_END \}\}", "", source, flags=re.S)
    assert not ASSESSMENT.findall(source), ASSESSMENT.findall(source)


def test_goplus_silence_is_not_called_clean():
    text = render_text(built("wdpa-2-26-cv-00087-usdt")[1])
    assert "GoPlus has no flags for this address." in text and "Powered by GoPlus" in text
    btc = render_text(built("edny-1-25-cv-05745-btc")[1])
    assert "GoPlus: not asked. GoPlus answers every Bitcoin address with no flags" in btc
    assert "GoPlus Security" not in btc


def test_freeze_date_transaction_and_destruction():
    text = render_text(built("ddc-1-25-cv-02967-usdt")[1])
    assert ("Frozen since 2024-11-04 18:55 UTC (block 66,699,054, transaction "
            "a3e73759098f09f7547b30286fd6f87570a1f9288487702375b65a41420f6642)") in text
    assert ("Tether destroyed 465,514.638375 USDT held at this address on 2025-04-01 01:34 UTC (block 70,939,306, "
            "transaction d5cbc559c41e460159b793649b8c5f40eba583f4c50d62252b2ba11399060145)") in text
    assert "counting the issuer's destruction listed above" in text


def test_a_frozen_address_with_no_destruction_found_says_the_history_may_be_incomplete():
    _, data = built("ddc-1-25-cv-01907-usdt-gb")
    text = render_text(data)
    assert data["completeness"]["history_incomplete"] is True
    assert "THE HISTORY MAY BE INCOMPLETE" in text
    assert "the index of destruction events was not available" in text


def test_a_0x_report_names_the_chains_checked_and_not():
    text = render_text(built("wdpa-2-26-cv-00087-usdt")[1])
    assert "Checked: Ethereum, Arbitrum One, OP Mainnet." in text
    assert "  - BNB Smart Chain: no data source we may use for a public site covers this chain yet." in text
    assert "  - Polygon: it needs a paid Blockscout plan." in text
    assert "  - Base: it needs a paid Blockscout plan." in text


def test_bitcoin_wording():
    _, data = built("edny-1-25-cv-05745-btc")
    text = render_text(data)
    assert "Outputs of transactions that spent from this address" in text
    assert "Returned to this address in its own spending transactions" in text
    assert "== First-hop outputs ==" in text and "== First-hop destinations ==" not in text
    returned = data["first_hop"]["btc_returned"]
    assert all(r["address"] != data["header"]["address"] for r in data["first_hop"]["destinations"])
    assert returned["count"] >= 0


def test_what_is_not_read_is_said():
    tron = render_text(built("sdny-1-26-cv-08010-usdt")[1])
    assert "TRC-10 tokens and their balances are not read." in tron
    assert "TRX moved by smart contracts (internal transactions) is not read." in tron
    eth = render_text(built("wdpa-2-26-cv-00087-usdt")[1])
    assert "Internal transfers (value moved by contract code):" in eth and "NFT transfers are not read." in eth
    assert "Transfers read: " in eth


def test_no_price_is_never_zero():
    _, data = built("sdny-1-26-cv-08010-usdt")
    assert data["holdings"]["native"]["usd"] is None
    assert data["holdings"]["freezable"]["share_status"] == "missing_prices"
    assert "no price available" in render_text(data)


def test_a_price_gives_dollar_figures():
    inp, _ = built("ddc-1-25-cv-02967-usdt")
    from taw.adapters.prices.chainlink import bucket_for
    prices = {}
    now = inp.snapshot["fetched_at"]
    for t in [*inp.transfers]:
        for asset in ("USDT", "TRX"):
            prices[(asset, *bucket_for(t.block_time, now))] = {"status": "ok", "usd": Decimal("1")}
    for asset in ("USDT", "TRX"):
        prices[(asset, *bucket_for(inp.snapshot["as_of_time"], now))] = {"status": "ok", "usd": Decimal("0.25")}
    data = build(dataclasses.replace(inp, prices=prices))
    assert data["holdings"]["native"]["usd"] == "0.07"           # 0.278724 TRX at $0.25
    assert data["holdings"]["freezable"]["share_status"] == "computed"
    usdt = next(a for a in data["activity"]["assets"] if a["asset"] == "USDT")
    assert usdt["in"]["usd"] == str(Decimal(usdt["in"]["total"]).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP"))
    assert "chainlink" in [s["slug"] for s in data["header"]["sources"]]


def test_tiny_zero_and_unrecognised_transfers_are_set_apart():
    _, data = built("sdny-1-26-cv-08010-usdt")
    shown = {r["address"] for r in data["first_hop"]["funders"] + data["first_hop"]["destinations"]}
    assert data["set_apart"]["zero"]["count"] == 16
    zero_parties = {r["counterparty"] for r in data["set_apart"]["zero"]["rows"]}
    assert "TWza6ZFbWGiosB9DvXrQkqREwchyKBqchq" in zero_parties and "TWza6ZFbWGiosB9DvXrQkqREwchyKBqchq" not in shown
    assert all(not t["recognised"] for t in data["activity"]["unrecognised_tokens"])
    assert "a token named USDT here need not be Tether's" in render_text(data)


def test_lookalike_addresses_are_pointed_out():
    _, data = built("sdny-1-26-cv-08010-usdt")
    rows = {r["address"]: r for r in data["first_hop"]["funders"] + data["first_hop"]["destinations"]}
    assert "TWzSiPtNaievW4n7ibFNkVK2ZSPkKBqchq" in rows["TWzkbyenzzFmtwGkkfU3uGn2ytCpKBqchq"]["lookalikes"]
    assert lookalike_groups(["0xAbcd1111", "0xabef2222"], "eth") == {}
    a, b, c = "0xab00" + "0" * 32 + "1111", "0xAB99" + "f" * 32 + "1111", "0xab12" + "3" * 32 + "4567"
    assert lookalike_groups([a, b, c], "eth") == {a: [b], b: [a]}


def test_token_names_with_hidden_or_lookalike_characters_are_shown_plainly():
    assert odd_characters("USDС") == ["U+0421"]                   # Cyrillic Es
    assert visible("⁣U⁣SD") == "<U+2063>U<U+2063>SD"
    _, data = built("wdpa-2-26-cv-00087-usdt")
    names = [t["symbol"] for t in data["holdings"]["tokens"] if not t["recognised"]]
    assert all("⁣" not in (n or "") for n in names)


def test_numbers_and_buckets():
    assert num("1234567.891") == "1,234,567.891" and num("0") == "0" and num("-1000") == "-1,000"
    assert [bucket(n) for n in (None, 0, 1, 2, 5, 6, 20, 21)] == [None, "none", "1", "2 to 5", "2 to 5", "6 to 20",
                                                                 "6 to 20", "more than 20"]


def test_prior_lookups_are_not_in_the_data():
    _, data = built("sdny-1-26-cv-08010-usdt")
    assert "prior" not in json.dumps(data)
    assert "looked up this address: 2 to 5" in render_text(data, prior_lookups="2 to 5")
