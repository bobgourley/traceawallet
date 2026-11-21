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
    golden(folder / "report.txt", render_text(data, sha256=digest(inp, data)))


def test_golden_case3_with_the_victims_match():
    """The owner's view of the case 3 report: the matched transaction in its private
    section. The match is an overlay, so the report's data and hash don't change."""
    from taw.matching import match_fetched
    inp, data = built("wdpa-2-26-cv-00087-usdt")
    snap, batch, chain = rf.chain_parts("wdpa-2-26-cv-00087-usdt")
    from taw.lookups import balances_trusted
    match = match_fetched("eth", snap.address, rf.fetch_from(chain["transactions"][0]), batch.transfers,
                          balances_trusted(inp.snapshot, "eth"))
    text = render_text(data, sha256=digest(inp, data), match={**match, "chains": ["eth"]})
    golden(rf.FIXTURES / "wdpa-2-26-cv-00087-usdt" / "report_with_match.txt", text)
    assert "== Your transaction (private) ==" in text and "242,209.413589 USDC" in text
    assert "This does not show that they were your funds" in text
    assert digest(inp, data) == digest(inp, build(inp))


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
    text = render_text(data, prior_lookups="1 to 4")
    body = outside_fixed_sections(text)
    assert not ASSESSMENT.findall(body), ASSESSMENT.findall(body)
    assert "recovery scams" in text and "ic3.gov" in text          # the fixed sections are there
    assert "DRAFT" not in text and "== Reporting what happened ==" in text


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
    assert ("USDT: frozen by Tether since 2024-11-04 18:55. This address can no longer send USDT. A freeze does "
            "not return money to anyone; that takes legal process. Tether can later release the frozen USDT or "
            "destroy it.") in text
    assert ("frozen in block 66,699,054, transaction "
            "a3e73759098f09f7547b30286fd6f87570a1f9288487702375b65a41420f6642") in text
    assert "Tether destroyed 465,514.638375 USDT held at this address on 2025-04-01 01:34." in text
    assert ("Detail: block 70,939,306, transaction "
            "d5cbc559c41e460159b793649b8c5f40eba583f4c50d62252b2ba11399060145") in text
    assert "Destroyed: Tether destroyed 465,514.638375 USDT held at this address on 2025-04-01." in text
    assert "counting the issuer's destruction shown under" in text


def test_a_frozen_address_with_no_destruction_found_says_the_history_may_be_incomplete():
    _, data = built("ddc-1-25-cv-01907-usdt-gb")
    text = render_text(data)
    assert data["completeness"]["history_incomplete"] is True
    assert "History read: may be incomplete." in text and "The history may be incomplete." in text
    assert ("USDT: the address is frozen and holds none, but the transfers read leave 2,137,276.136 USDT unaccounted "
            "for. No destruction by the issuer was found (the index of the issuer's destruction events was not "
            "available).") in text
    assert "2137276136000" not in text and "stablecoin 0xdac17f958d2ee523a2206206994597c13d831ec7" not in text
    assert "Stablecoin balances match the transfers read: no\n" in text


def test_a_balance_that_does_not_match_is_worded_with_amounts_and_the_token_name():
    _, data = built("ddc-1-25-cv-01907-usdt-gb")
    usdt = next(t for t in data["completeness"]["stablecoins"] if t["name"] == "USDT")
    usdt.update(status="not_reconciled", balance="10", destroyed=True, destroyed_amount="5")
    data["completeness"]["incomplete"] = [{"kind": "stablecoin", **usdt}]
    text = render_text(data)
    assert ("USDT: the balance is 10 USDT, but the transfers read add up to 2,137,276.136 USDT, less 5 USDT destroyed "
            "by the issuer.") in text
    usdt.update(decimals=None)
    data["completeness"]["incomplete"] = [{"kind": "stablecoin", **usdt}]
    assert "an amount we could not convert (the token's decimals were not read)" in render_text(data)


def test_other_reasons_are_shown_as_the_check_gave_them():
    _, data = built("ddc-1-25-cv-01907-usdt-gb")
    data["completeness"]["incomplete"] = [{"kind": "text", "text": "the two lists differ by 3 transfers"}]
    assert "  - the two lists differ by 3 transfers\n" in render_text(data)


def test_a_0x_report_names_the_chains_checked_and_not():
    text = render_text(built("wdpa-2-26-cv-00087-usdt")[1])
    assert "Checked: Ethereum, Arbitrum One, OP Mainnet." in text
    assert ("This site does not cover BNB Smart Chain, Polygon or Base yet. If you sent on one of these, this report "
            "cannot see it.") in text
    assert "paid Blockscout plan" not in text                    # the reason stays in the data
    _, data = built("wdpa-2-26-cv-00087-usdt")
    assert {x["reason"] for x in data["header"]["evm_coverage"]["not_checked"]} == {
        "no_source_with_suitable_terms", "needs_paid_blockscout_plan"}


def test_bitcoin_wording():
    _, data = built("edny-1-25-cv-05745-btc")
    text = render_text(data)
    assert "Outputs of transactions that spent from this address" in text
    assert "Returned to this address in its own spending transactions" in text
    assert "== Where funds went next ==" in text and "== Where funds came from ==" in text
    returned = data["first_hop"]["btc_returned"]
    assert all(r["address"] != data["header"]["address"] for r in data["first_hop"]["destinations"])
    assert returned["count"] >= 0


def test_what_is_not_read_is_said():
    tron = render_text(built("sdny-1-26-cv-08010-usdt")[1])
    assert "TRC-10 tokens and their balances are not read." in tron
    assert "TRX moved by smart contracts (internal transactions) is not read." in tron
    eth = render_text(built("wdpa-2-26-cv-00087-usdt")[1])
    assert "Internal transfers (value moved by contract code) were read for every block" in eth
    assert "NFT transfers are not read." in eth
    assert "the source listed the whole history" in eth


def test_no_price_is_never_zero():
    _, data = built("sdny-1-26-cv-08010-usdt")
    assert data["holdings"]["native"]["usd"] is None
    assert data["holdings"]["freezable"]["share_status"] == "missing_prices"
    text = render_text(data)
    assert text.count("no price data was available") == 1 and "no price available" not in text


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
    assert rows["TWzkbyenzzFmtwGkkfU3uGn2ytCpKBqchq"]["lookalike_group"] == rows[
        "TWzSiPtNaievW4n7ibFNkVK2ZSPkKBqchq"]["lookalike_group"] == "A"
    group = next(g for g in data["lookalikes"] if g["group"] == "A")
    text = render_text(data)
    assert text.count("Group A:") == 1 and "[lookalike group A]" in text
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
    assert [bucket(n) for n in (None, 0, 1, 4, 5, 19, 20, 500)] == [None, "none", "1 to 4", "1 to 4", "5 to 19",
                                                                   "5 to 19", "20 or more", "20 or more"]


def test_prior_lookups_are_not_in_the_data():
    _, data = built("sdny-1-26-cv-08010-usdt")
    assert "prior" not in json.dumps(data)
    assert "Other accounts on TraceAWallet that have looked up this address: 1 to 4." in \
        render_text(data, prior_lookups="1 to 4")
    assert "No one else has looked up this address here yet." in render_text(data, prior_lookups="none")


# -- the revisions of 2026-11-20 (evening) ------------------------------------------------------

def test_sources_say_what_each_was_used_for_and_print_no_claim_wording():
    text = render_text(built("sdny-1-26-cv-08010-usdt")[1])
    sources = text.split("== Sources ==")[1].split("<<<")[0]
    assert "  - TronGrid: balances and transfers" in sources
    assert "  - Tether USDT contract state: freeze status, read from the token contract" in sources
    assert "frozen by" not in sources and "logo" not in text and "results unmodified" not in text


def test_token_names_carry_the_warning_and_never_form_a_link():
    from taw.report.render import defang
    assert defang("Swap your Voucher on bio.link/shibax") == "Swap your Voucher on bio[.]link/shibax"
    assert defang("https://x.io") == "https[://]x[.]io"
    text = render_text(built("wdpa-2-26-cv-00087-usdt")[1])
    warning = ("Token names are chosen by their creators and are often advertisements. Do not contact anyone named "
               "in a token or message sent to this address.")
    assert warning in text
    assert not re.search(r"\b[a-z0-9-]+\.(link|com|io|xyz|org|net)\b", text.split("== Tokens we don't recognise ==")[1]
                         .split("==")[0], re.I)


def test_summary_from_the_data():
    _, data = built("sdny-1-26-cv-08010-usdt")
    sm = data["summary"]
    assert sm["frozen"] == [{"token": "USDT", "issuer": "Tether", "since": "2025-07-26T10:52:15+00:00"}]
    assert sm["largest_destination"]["address"] == "TWzSiPtNaievW4n7ibFNkVK2ZSPkKBqchq"
    assert sm["largest_destination"]["share_percent"] == "84.8"
    assert (sm["history"], sm["list_matches"], sm["checked"]) == ("complete", 0, 193)
    text = render_text(data)
    assert text.index("== Summary ==") < text.index("== Chains ==")
    assert "Frozen: Tether has frozen this address's USDT since 2025-07-26." in text


def test_list_matching_counts_and_its_limits():
    _, data = built("ddc-1-25-cv-02967-usdt")
    assert len(data["lists"]["matched"]) == 1
    assert data["lists"]["matched"][0]["address"] == "TM1zzNDZD2DPASbKcgdVoTYhfmYgtfwx9R"
    text = render_text(data)
    assert "Matched: 1." in text and "listed by OKX as its wallet" in text
    assert "The exchange lists cover only the exchanges named. An address that matches nothing may still belong" in text
    assert "1 first-hop address matched" in text


def test_destinations_are_grouped_by_address_and_one_transfer_has_one_date():
    _, data = built("sdny-1-26-cv-08010-usdt")
    top = data["first_hop"]["destinations"][0]
    assert [a["asset"] for a in top["assets"]] == ["USDT", "TRX"]
    text = render_text(data)
    assert "19,999,900 USDT in 1 transfer on 2025-07-04" in text
    assert "in 44 transfers from 2025-03-29 to 2025-06-26" in text


def test_small_transfers_show_five_rows_and_keep_the_rest_in_the_data():
    _, data = built("sdny-1-26-cv-08010-usdt")
    assert len(data["set_apart"]["zero"]["rows"]) + len(data["set_apart"]["tiny"]["rows"]) == 287
    section = render_text(data).split("== Zero and very small transfers ==")[1].split("\n== ")[0]
    assert section.count("transaction ") == 5 and "The full list of 287 is in the report's data." in section


def test_completeness_leads_with_a_verdict_and_skips_what_does_not_apply():
    text = render_text(built("sdny-1-26-cv-08010-usdt")[1])
    part = text.split("== Is the history complete? ==")[1].split("== Prior lookups ==")[0]
    assert part.strip().startswith("Complete as far as we can check")
    assert "nonce" not in part
    assert part.index("What we checked:") < part.index("What we did not read or could not check:") < part.index("Detail:")


def test_the_fingerprint_and_id_close_the_report():
    inp, data = built("sdny-1-26-cv-08010-usdt")
    text = render_text(data, public_id="00000000-0000-0000-0000-000000000001", sha256=digest(inp, data))
    tail = text.split("== Report fingerprint ==")[1]
    assert "00000000-0000-0000-0000-000000000001" in tail and digest(inp, data) in tail


def test_fixed_sections_say_what_was_asked():
    text = render_text(built("sdny-1-26-cv-08010-usdt")[1])
    assert "someone claiming to be a lawyer" in text and '"unfreezing"' in text
    assert "pay a fee or a tax before you can withdraw" in text and "Do not send more money" in text
    checklist = text.split("<<< fixed section: IC3 checklist >>>")[1]
    assert checklist.index("tell the exchange or wallet provider you sent from") < checklist.index("ic3.gov")
    assert "reportfraud.ftc.gov" in checklist and "Outside the US" in checklist and "Report promptly" in checklist



# -- third wording pass ------------------------------------------------------------------------

def test_summary_gives_received_sent_and_what_remains_per_asset():
    text = render_text(built("wdpa-2-26-cv-00087-usdt")[1])
    summary = text.split("== Summary ==")[1].split("==")[0]
    assert "USDC: received 387,152.909159, sent 387,152.909159. No USDC remains at this address." in summary
    sdny = render_text(built("sdny-1-26-cv-08010-usdt")[1])
    assert "USDT: received 602,509,669.627689, sent 589,752,845.406849. 12,756,824.22084 USDT remains." in sdny


def test_an_address_imitating_this_one_is_named():
    _, data = built("wdpa-2-26-cv-00087-usdt")
    assert data["summary"]["imitating_this_address"] == ["0xfa0ac3c12f1d55706fe78b8c1407edee2f5e33a9"]
    text = render_text(data)
    assert "Lookalikes of this address: 1 address in this report begins and ends with the same characters" in text
    assert "0xfa0a50dc366e2f0a5381e4d4b5fede589ab633a9  (this report's address)" in text
    assert "This address imitates the beginning and end of this report's address." in text


def test_imitation_tokens_name_the_real_one():
    from taw.report.build import imitates, skeleton
    assert skeleton("\u2063U\u2063S\u206fD\u200eС\u206a") == "USDC" and imitates("eth", "ՍSDС") == "USDC"
    assert imitates("eth", "Swap your Voucher on bio.link") is None and imitates("tron", "TG: jieuu") is None
    _, data = built("wdpa-2-26-cv-00087-usdt")
    assert data["activity"]["unrecognised_imitating"] == 13
    text = render_text(data)
    section = text.split("== Tokens we don't recognise ==")[1].split("\n== ")[0]
    assert section.count("This is not Circle's USDC. The real USDC on Ethereum is contract "
                         "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48.") == 5
    assert section.count("      contract ") == 5 and "...and 9 more tokens with" in section


def test_the_token_warning_is_only_in_the_tokens_section():
    text = render_text(built("wdpa-2-26-cv-00087-usdt")[1])
    small = text.split("== Zero and very small transfers ==")[1].split("\n== ")[0]
    assert "Token names are chosen" not in small
    assert text.count("Token names are chosen by their creators") == 1


def test_times_are_utc_once_and_no_one_else_has_looked():
    text = render_text(built("sdny-1-26-cv-08010-usdt")[1], prior_lookups="none")
    assert text.count("UTC") == 1 and "All times in this report are UTC." in text
    assert "No one else has looked up this address here yet." in text


def test_the_checklist_mentions_the_bank():
    text = render_text(built("sdny-1-26-cv-08010-usdt")[1])
    assert "and your bank, if the money started in a bank account" in text


def test_the_victims_balance_before_and_after():
    text = (rf.FIXTURES / "wdpa-2-26-cv-00087-usdt" / "report_with_match.txt").read_text()
    assert "USDC at this address just before your transfer arrived: 0.041921. Just after: 242,209.45551." in text


# -- the web page says what the text says --------------------------------------------------------

def _page_text(html: str) -> str:
    import html as h
    blocks = re.sub(r"</?(div|details|summary|h2|header|article)\b[^>]*>", " ", html)
    return re.sub(r"\s+", " ", h.unescape(re.sub(r"<[^>]+>", "", blocks)))


@pytest.mark.parametrize("case", CASES)
def test_the_html_report_has_every_line_of_the_text_report(case):
    from taw.report.render import render_html
    inp, data = built(case)
    kw = {"sha256": digest(inp, data), "public_id": "00000000-0000-0000-0000-000000000001", "prior_lookups": "none"}
    text = render_text(data, **kw)
    page = _page_text(str(render_html(data, **kw)))
    for line in text.splitlines():
        s = re.sub(r"\s+", " ", line.strip())
        if not s or s.startswith("<<<"):
            continue
        if s.startswith("== ") and s.endswith(" =="):
            s = s[3:-3]
        assert s in page, s


def test_the_html_report_escapes_token_names_and_links_only_addresses():
    from taw.report.render import render_html
    html = str(render_html(built("wdpa-2-26-cv-00087-usdt")[1]))
    assert "&lt;U+2063&gt;" in html and "<U+2063>" not in html
    anchors = re.findall(r"<a [^>]*>[^<]*</a>", html)
    assert anchors and all(re.search(r'href="(/\?address=|https://)', a) for a in anchors)
    assert not re.search(r'href="[^"]*(bio\.link|bio\[\.\]link)', html)
    assert "<script" not in html


def test_the_private_section_is_only_rendered_with_a_match():
    from taw.report.render import render_html
    _, data = built("wdpa-2-26-cv-00087-usdt")
    assert "Your transaction" not in str(render_html(data))
