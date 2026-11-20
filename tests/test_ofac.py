"""OFAC SDN list: parsing a real excerpt (six entries from the 2026-11-19 list),
mapping addresses to chains by format, and storing the file under the stable URL."""
import datetime as dt
import pathlib

import httpx
import pytest

from taw.adapters.base import LocalRawStore
from taw.adapters.labels import ofac

SAMPLE = pathlib.Path(__file__).parent / "fixtures" / "ofac" / "SDN_sample.xml"
EVM = ["arbitrum", "base", "bsc", "eth", "optimism", "polygon"]


def rows():
    return ofac.parse(SAMPLE.read_bytes())


def test_publication_date_and_digital_currency_ids():
    published, count, found = rows()
    assert published == dt.date(2026, 11, 19) and count == 19595
    assert len(found) == 53 and all(r["id_type"].startswith("Digital Currency Address - ") for r in found)


def test_chain_comes_from_the_format_not_the_tag():
    _, _, found = rows()
    by = {(r["id_type"].split(" - ")[1], r["address"]): r for r in found}
    tron_tagged_xbt = next(a for (t, a) in by if t == "XBT" and a.startswith("T"))
    assert ofac.chains_for(tron_tagged_xbt, EVM) == [("tron", tron_tagged_xbt)]
    usdt_in_btc_format = next(a for (t, a) in by if t == "USDT" and a.startswith("1"))
    assert ofac.chains_for(usdt_in_btc_format, EVM) == [("btc", usdt_in_btc_format)]
    ltc = next(a for (t, a) in by if t == "LTC")
    assert ofac.chains_for(ltc, EVM) == []
    eth = next(a for (t, a) in by if t == "ETH" and a != a.lower())
    assert ofac.chains_for(eth, EVM) == [(c, eth.lower()) for c in EVM]          # case ignored, every EVM chain


def test_claims_word_it_as_ofac_and_keep_the_tag():
    published, _, found = rows()
    claims, unmapped = ofac.claims(published, found, EVM, "a" * 64)
    assert [r["id_type"] for r in unmapped] == ["Digital Currency Address - LTC"]
    assert all(c.category == "sanctioned" and c.label.startswith("listed by OFAC on the SDN list: ") for c in claims)
    assert all(c.source_date == published and c.raw_sha256 == "a" * 64 for c in claims)
    same = [c for c in claims if c.chain == "eth" and c.address == "0x4f47bc4960" + c.address[12:]
            and c.address.startswith("0x4f47bc4960")]
    assert {c.claim_key.split(":")[2] for c in same} == {"ARB", "BSC", "ETH"}     # one address, three tags
    assert len({(c.chain, c.claim_key) for c in claims}) == len(claims)
    assert all(c.claim_url.startswith("https://sanctionssearch.ofac.treas.gov/Details.aspx?id=") for c in claims)


def test_a_file_with_a_dtd_is_refused():
    bad = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><sdnList></sdnList>'
    with pytest.raises(ofac.OfacFormatError, match="DTD"):
        ofac.parse(bad)


def test_download_is_stored_under_the_stable_url(tmp_path):
    signed = "https://bucket.example/SDN.XML?X-Amz-Security-Token=temporary"

    def handler(request):
        if str(request.url) == ofac.URL:
            return httpx.Response(302, headers={"location": signed})
        return httpx.Response(200, content=SAMPLE.read_bytes())
    raw = LocalRawStore(tmp_path)
    source = ofac.OfacSdn(raw, EVM, http=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True))
    pub = source.load()
    index = (tmp_path / "index.jsonl").read_text()
    assert ofac.URL in index and "X-Amz" not in index
    assert pub.source_date == dt.date(2026, 11, 19) and pub.unmapped == 1
    assert pub.detail["unmapped_by_tag"] == {"Digital Currency Address - LTC": 1}
