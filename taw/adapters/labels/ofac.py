"""OFAC's SDN list: every "Digital Currency Address" as a label.

The list is downloaded whole from OFAC's Sanctions List Service (SDN.XML, about
30 MB). The file goes to the bucket like any raw response, under the stable
OFAC URL (the service redirects to a short-lived signed link, which is not
stored). Every <id> whose idType starts "Digital Currency Address" becomes one
claim per chain its address fits:

  0x + 40 hex       every EVM chain we know (matched without regard to case)
  T... base58check  Tron
  Bitcoin formats   Bitcoin (1..., 3..., bc1...; checksums verified)
  anything else     not mapped (Litecoin, Monero, Bitcoin Cash's own format...)

The chain comes from the address's format, never from the currency tag alone:
the 2026-11-19 list has an "XBT" entry in Tron format and "USDT" entries in
Bitcoin format (USDT on Omni). The tag stays in the label text.

The label is OFAC's claim (rule 9): "listed by OFAC on the SDN list: <name>
(SDN entry <uid>; programs ...); listed as Digital Currency Address - XBT".
"""
from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET
from typing import Iterable

import httpx

from taw.adapters.base import RawStore
from taw.adapters.chains import btc_address, tron_address
from taw.adapters.labels.base import LabelClaim, Publication

SOURCE = "ofac-sdn"
URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
DETAIL_URL = "https://sanctionssearch.ofac.treas.gov/Details.aspx?id={uid}"
NS = "{https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML}"
ID_PREFIX = "Digital Currency Address"
_EVM = re.compile(r"0x[0-9a-fA-F]{40}")


class OfacFormatError(ValueError):
    pass


def chains_for(address: str, evm_chains: Iterable[str]) -> list[tuple[str, str]]:
    """[(chain, stored address)] by the address's format; [] when none fits."""
    a = address.strip()
    if _EVM.fullmatch(a):
        return [(c, a.lower()) for c in evm_chains]
    if a.startswith("T") and tron_address.is_valid(a):
        return [("tron", a)]
    try:
        return [("btc", btc_address.normalize(a))]
    except ValueError:
        return []


def parse(body: bytes) -> tuple[dt.date, int, list[dict]]:
    """(publication date, record count, [{uid, name, sdn_type, programs, id_type, address}])."""
    root_at = body.find(b"<sdnList")
    if root_at < 0:
        raise OfacFormatError("not an SDN list: no <sdnList> element")
    if b"<!DOCTYPE" in body[:root_at] or b"<!ENTITY" in body[:root_at]:
        raise OfacFormatError("refusing an SDN file with a DTD")      # no entity expansion, ever
    root = ET.fromstring(body)
    date_text = root.findtext(f"{NS}publshInformation/{NS}Publish_Date")
    count_text = root.findtext(f"{NS}publshInformation/{NS}Record_Count")
    if not date_text:
        raise OfacFormatError("no Publish_Date")
    published = dt.datetime.strptime(date_text.strip(), "%m/%d/%Y").date()
    rows = []
    for entry in root.iter(f"{NS}sdnEntry"):
        name = " ".join(p.strip() for p in (entry.findtext(f"{NS}firstName"), entry.findtext(f"{NS}lastName")) if p)
        programs = [p.text.strip() for p in entry.iter(f"{NS}program") if p.text]
        for ident in entry.iter(f"{NS}id"):
            id_type = (ident.findtext(f"{NS}idType") or "").strip()
            if not id_type.startswith(ID_PREFIX):
                continue
            rows.append({"uid": (entry.findtext(f"{NS}uid") or "").strip(), "name": name,
                         "sdn_type": (entry.findtext(f"{NS}sdnType") or "").strip(), "programs": programs,
                         "id_type": id_type, "address": (ident.findtext(f"{NS}idNumber") or "").strip()})
    return published, int(count_text or 0), rows


def label_text(row: dict) -> str:
    programs = ", ".join(row["programs"]) or "none given"
    return (f"listed by OFAC on the SDN list: {row['name']} (SDN entry {row['uid']}; programs {programs}); "
            f"listed as {row['id_type']}")


def claims(published: dt.date, rows: list[dict], evm_chains: Iterable[str],
           sha: str | None) -> tuple[list[LabelClaim], list[dict]]:
    """(claims, rows whose address fits no chain we cover)."""
    evm_chains = list(evm_chains)
    out, unmapped = [], []
    for row in rows:
        targets = chains_for(row["address"], evm_chains)
        if not targets:
            unmapped.append(row)
            continue
        for chain, stored in targets:
            out.append(LabelClaim(chain=chain, address=stored, category="sanctioned", label=label_text(row),
                                  claim_key=f"sdn:{row['uid']}:{row['id_type'].split(' - ')[-1]}:{stored}", source_date=published,
                                  claim_url=DETAIL_URL.format(uid=row["uid"]), raw_sha256=sha))
    return out, unmapped


class OfacSdn:
    slug = SOURCE
    local = True

    def __init__(self, raw_store: RawStore, evm_chains: Iterable[str], *, http: httpx.Client | None = None,
                 attempts: int = 3):
        self.raw_store, self.evm_chains = raw_store, list(evm_chains)
        self.http = http or httpx.Client(timeout=180.0, follow_redirects=True,
                                         headers={"user-agent": "TraceAWallet list loader"})
        self.attempts = attempts

    def fetch(self) -> tuple[bytes, str]:
        last: Exception | None = None
        for _ in range(self.attempts):
            try:
                resp = self.http.get(URL)
                if resp.status_code == 200 and resp.content:
                    # Stored under the stable URL: the signed redirect target carries temporary tokens.
                    return resp.content, self.raw_store.save(SOURCE, URL, None, 200, resp.content)
                last = RuntimeError(f"OFAC answered HTTP {resp.status_code} with {len(resp.content)} bytes")
            except httpx.HTTPError as exc:
                last = exc
        raise RuntimeError(f"could not download the SDN list: {last}")

    def load(self) -> Publication:
        body, sha = self.fetch()
        published, record_count, rows = parse(body)
        found, unmapped = claims(published, rows, self.evm_chains, sha)
        tags: dict[str, int] = {}
        for r in unmapped:
            tags[r["id_type"]] = tags.get(r["id_type"], 0) + 1
        return Publication(SOURCE, published, sha, found, unmapped=len(unmapped),
                           detail={"record_count": record_count, "digital_currency_ids": len(rows),
                                   "unmapped_by_tag": tags, "bytes": len(body)})
