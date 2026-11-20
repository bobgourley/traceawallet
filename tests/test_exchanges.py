"""Exchange wallet lists: the CSV the exchanges' own pages produced, and OKX's file."""
import csv
import datetime as dt
import pathlib
import re

import httpx
import pytest

from taw.adapters.base import LocalRawStore
from taw.adapters.labels import exchanges

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "okx" / "por_sample.zip"
NOV = dt.date(2026, 11, 1)


def test_okx_rows_on_covered_networks_with_okx_type():
    claims, detail = exchanges.parse_okx(FIXTURE.read_bytes(), NOV, "a" * 64)
    assert detail["by_chain"] == {"eth": 3, "tron": 3, "btc": 3, "arbitrum": 1, "optimism": 1}
    assert detail["skipped_not_an_address"] == {"ETH / Native ETH Staking": 1}     # a 98-character validator key
    assert detail["rows_on_other_networks"] == 1                                    # APTOS
    kinds = {c.label.split("type in OKX's file: ")[1].rstrip(")") for c in claims}
    assert kinds == {"Non Staking", "Native Staking"}
    c = claims[0]
    assert c.label.startswith("listed by OKX as its wallet on 2026-11-01 (type in OKX's file: ")
    assert c.category == "exchange" and c.dated_label and c.source_date == NOV and c.raw_sha256 == "a" * 64
    assert all(x.address == x.address.lower() for x in claims if x.chain in ("eth", "arbitrum", "optimism"))


def test_okx_download_is_kept_and_a_missing_month_is_not_an_error(tmp_path):
    def handler(request):
        if "202611" in str(request.url):
            return httpx.Response(200, content=FIXTURE.read_bytes())
        return httpx.Response(404)
    raw = LocalRawStore(tmp_path)
    okx = exchanges.OkxPor(raw, http=httpx.Client(transport=httpx.MockTransport(handler)))
    pub = okx.load(NOV)
    assert pub.source_date == NOV and raw.body(pub.raw_sha256) == FIXTURE.read_bytes()      # the zip, unmodified
    assert exchanges.okx_url(NOV) in (tmp_path / "index.jsonl").read_text()
    with pytest.raises(exchanges.NotPublished):
        okx.load(dt.date(2026, 12, 1))


def test_month_only_dates_say_so():
    assert exchanges.wording("Bybit", "2022-12") == "listed by Bybit as its wallet on 2022-12 (month only; no day given)"
    assert exchanges.wording("KuCoin", "2022-11-11") == "listed by KuCoin as its wallet on 2022-11-11"


def test_the_exchange_file_is_valid():
    rows = list(csv.DictReader(exchanges.CSV_PATH.open()))
    assert {r["exchange"] for r in rows} == {"HTX", "Bybit", "KuCoin", "Bitfinex", "Crypto.com"}
    for r in rows:
        assert r["chain"] in ("eth", "arbitrum", "optimism", "tron", "btc"), r
        assert exchanges.stored(r["chain"], r["address"]) == r["address"], r                # checksum, stored form
        assert re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", r["published"]) and r["source_url"].startswith("https://")
        assert r["description"] and r["note"]
    assert len({(r["exchange"], r["chain"], r["address"]) for r in rows}) == len(rows)
    pub = exchanges.ExchangeFile().load()
    assert len(pub.claims) == len(rows) and all(c.label.startswith("listed by ") for c in pub.claims)
