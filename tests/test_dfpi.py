"""California DFPI's Crypto Scam Tracker: parsing the page, which addresses become
labels, host names, the platform check and its private section. No network."""
import datetime as dt

import pytest

from taw import platform
from taw.adapters.chains import evm_address
from taw.adapters.labels import dfpi
from taw.lookups import LookupRequest
from taw.report.render import render_text
from tests import report_fixtures as rf

CHECKSUMMED = "0x23De15ADbBA4F8A4acB9A1272FEB5e7A0eA0c4F6"
LOWER = "0x" + "ab" * 20
BAD = CHECKSUMMED[:-1] + ("7" if CHECKSUMMED[-1] != "7" else "8")
TRON = "TGuqbYqJ4m1J8dKWB3NVnsuXtgXCQkgdyf"
BECH32 = "bc1q8yja3gw33ngd8aunmfr4hj820adc9nlsv0syvz"


def page(rows, header=("Primary Subject", "Complaint Narrative", "Scam Type", "Website", "Screenshot")):
    head = "".join(f"<th>{h}</th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<html><body><p>About</p><table id='tablepress-20'><thead><tr>{head}</tr></thead><tbody>{body}</tbody>" \
           f"</table></body></html>"


ROWS = [
    ("grokai-network.net/ (Entity impersonating legitimate companies)",
     f"The victim sent crypto assets to the wallet address {CHECKSUMMED}. Also {LOWER} and {BAD}.",
     "<a href='#fra'>Fraudulent Trading Platform</a><br /> <br /> <a href='#imp'>Imposter Scam</a>",
     "grokai-network.net/", "<figure><img alt='A camera'></figure>"),
    ("hcxz.com <br /> gptss.com", f"Sent USDT to {TRON} and BTC to {BECH32}.", "Pig Butchering",
     "https://www.HCXZ.com/login<br />gptss.com", ""),
    ("Noel Crypto", "No address here.", "Other", "", ""),
]


def test_entries_websites_and_addresses():
    assert evm_address.is_valid(CHECKSUMMED) and not evm_address.is_valid(BAD)
    entries, skipped = dfpi.parse(page(ROWS).encode())
    assert [e.position for e in entries] == [1, 2, 3]
    assert entries[0].websites == ["grokai-network.net"] and entries[1].websites == ["hcxz.com", "gptss.com"]
    assert entries[0].scam_types == ["Fraudulent Trading Platform", "Imposter Scam"]     # kept as data
    assert entries[0].addresses == [("evm", CHECKSUMMED.lower())]
    assert entries[1].addresses == [("tron", TRON), ("btc", BECH32)]
    assert skipped == {"no_checksum": 1, "bad_checksum": 1}                 # one-case EVM; a typo'd checksum
    assert entries[1].subject == "hcxz.com gptss.com" and entries[2].addresses == []


def test_a_changed_table_stops_the_load():
    with pytest.raises(dfpi.TrackerFormatError):
        dfpi.parse(page(ROWS, header=("Name", "Story", "Type", "Site", "Picture")).encode())


@pytest.mark.parametrize("typed,host", [
    ("https://www.Example.com/path?x=1", "example.com"), ("example.com.", "example.com"),
    ("http://user@sub.example.co.uk:8443/a", "sub.example.co.uk"), ("bücher.de", "xn--bcher-kva.de"),
    ("192.168.1.10", None), ("localhost", None), ("not a site", None), ("", None), ("javascript:alert(1)", None),
])
def test_host_names(typed, host):
    assert dfpi.host_of(typed) == host


def test_labels_are_dfpis_record_dated_by_the_day_read():
    entries, _ = dfpi.parse(page(ROWS).encode())
    claims = dfpi.claims(entries, dt.date(2026, 11, 21), ["arbitrum", "eth", "optimism"], "s" * 64)
    evm = [c for c in claims if c.address == CHECKSUMMED.lower()]
    assert {c.chain for c in evm} == {"arbitrum", "eth", "optimism"}
    c = evm[0]
    assert c.category == "scam_report" and c.source_date == dt.date(2026, 11, 21) and c.claim_url == dfpi.URL
    assert c.label == ("named in a complaint reported to California's DFPI (tracker entry: \"grokai-network.net/ "
                       "(Entity impersonating legitimate companies)\")")
    assert "Fraudulent" not in c.label and "Imposter" not in c.label          # DFPI's categories stay in the data
    assert {(x.chain, x.address) for x in claims if x.chain in ("tron", "btc")} == {("tron", TRON), ("btc", BECH32)}


def test_an_entry_keeps_its_key_while_unchanged():
    a, _ = dfpi.parse(page(ROWS).encode())
    b, _ = dfpi.parse(page([ROWS[2], ROWS[0], ROWS[1]]).encode())
    assert {e.key for e in a} == {e.key for e in b}


def test_the_platform_field_reads_as_a_website_or_a_name():
    assert platform.entered(None, "https://www.HCXZ.com/x")["domain"] == "hcxz.com"
    assert platform.entered("HCXZ Technology", None)["name_words"] == "hcxz technology"
    assert platform.entered("ab!", None)["name_words"] is None               # too short to match anything
    assert platform.check(None, None, None) is None
    assert platform.check(None, "ab", None) == {"entered": platform.entered("ab", None), "usable": False}


def test_the_platform_wants_the_match_job():
    assert LookupRequest(address="x" * 30, platform_url="hcxz.com").wants_match()
    assert not LookupRequest(address="x" * 30).wants_match()


def platform_result(**kw):
    base = {"entered": {"domain": "hcxz.com", "domain_typed": "https://www.hcxz.com/login", "name": None,
                        "name_words": None}, "usable": True,
            "lists": [{"source": "dfpi-tracker", "loaded": True, "source_name": "California DFPI Crypto Scam Tracker",
                       "url": dfpi.URL, "read_on": "2026-11-21", "website_matches": 1,
                       "website_entries": [{"subject": "hcxz.com gptss.com", "website": "hcxz.com", "exact": True}],
                       "name_matches": 0, "name_entries": []}]}
    base.update(kw)
    return base


def test_the_private_section_states_what_matched_in_dfpis_words():
    _, data = (lambda inp: (inp, __import__("taw.report.build", fromlist=["build"]).build(inp)))(
        rf.case_input("sdny-1-26-cv-08010-usdt")[0])
    text = render_text(data, platform=platform_result())
    section = text[text.index("== The platform you entered (private) =="):text.index("== Chains ==")]
    assert "Only you see this section. It comes from what you typed" in section
    assert "You entered: https://www.hcxz.com/login\nWebsite checked: hcxz.com\n" in section
    assert "California DFPI Crypto Scam Tracker (read 2026-11-21):\n  - Website: 1 entry lists hcxz.com.\n" in section
    assert '      "hcxz.com gptss.com" (listed website: hcxz.com)' in section
    assert 'DFPI says they "are based on consumer complaints"' in section
    for word in ("scam ", "fraud", "suspicious", "risk", "dangerous", "legitimate"):
        assert word not in section.replace("Crypto Scam Tracker", "").lower()
    assert "== The platform you entered" not in render_text(data)          # nothing entered, no section


def test_no_match_says_only_that():
    _, data = (lambda inp: (inp, __import__("taw.report.build", fromlist=["build"]).build(inp)))(
        rf.case_input("sdny-1-26-cv-08010-usdt")[0])
    res = platform_result()
    res["lists"][0].update(website_matches=0, website_entries=[])
    text = render_text(data, platform=res)
    assert "  - Website: no entry lists hcxz.com or a domain it belongs to." in text
    assert "Not being listed means only that the tracker has no entry naming it." in text


def test_the_domain_is_text_in_html_never_a_link():
    from taw.report.build import build
    from taw.report.render import render_html
    data = build(rf.case_input("sdny-1-26-cv-08010-usdt")[0])
    html = str(render_html(data, platform=platform_result()))
    assert "Website checked: hcxz.com" in html and 'href="https://www.hcxz.com' not in html
    assert "hcxz.com</a>" not in html


def test_a_dfpi_label_carries_dfpis_caveat_in_the_shared_report():
    from taw.report.build import build
    inp = rf.case_input("sdny-1-26-cv-08010-usdt")[0]
    data = build(inp)
    data["labels"]["address"] = [{"source": "dfpi-tracker", "source_name": "California DFPI Crypto Scam Tracker",
                                  "category": "scam_report", "attribution": None, "claim_url": dfpi.URL,
                                  "label": "named in a complaint reported to California's DFPI (tracker entry: "
                                           "\"hcxz.com gptss.com\")", "source_date": "2026-11-21",
                                  "observed_at": "2026-11-21T07:00:00+00:00"}]
    text = render_text(data)
    assert ("  - named in a complaint reported to California's DFPI (tracker entry: \"hcxz.com gptss.com\")\n"
            "    source: California DFPI Crypto Scam Tracker; first read in the tracker 2026-11-21; read 2026-11-21; "
            f"{dfpi.URL}\n") in text
    assert 'it "has not verified the losses reported by complainants." The tracker gives no date for each' in text
