"""California DFPI's Crypto Scam Tracker (docs/research/more-sources.md; DECISIONS.md,
2026-11-21 "DFPI Crypto Scam Tracker").

One public page holds one HTML table (TablePress), one row per entry: Primary
Subject, Complaint Narrative, Scam Type, Website, Screenshot. DFPI's Conditions of
Use put the page's information in the public domain; the screenshots are not
copied. The page has no date per entry and no publication date, so every claim is
dated by the day we read the page, and the label keeps the first day we read it.

What we take:
  addresses   wallet addresses written in an entry's subject or narrative become
              labels, only when the address carries a checksum and passes it: an
              EVM address in mixed case that passes EIP-55, a Tron base58check
              address, a Bitcoin base58check or bech32 address. An EVM address all
              in one case has no checksum to check and is counted, not used.
  websites    the Website column, cut to host names, for matching what a user typed
              (private section only: it comes from the user's own input)
  subjects    the Primary Subject text, for matching a platform name the user typed

The label is DFPI's record, never ours (rule 9): "named in a complaint reported to
California's DFPI". DFPI's own scam-type words are kept in the data, not in labels.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable

import httpx

from taw.adapters.base import RawStore
from taw.adapters.chains import btc_address, evm_address, tron_address
from taw.adapters.labels.base import LabelClaim, Publication

SOURCE = "dfpi-tracker"
URL = "https://dfpi.ca.gov/consumers/crypto/crypto-scam-tracker/"
COLUMNS = ("Primary Subject", "Complaint Narrative", "Scam Type", "Website")
SUBJECT_MAX = 160

_EVM = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{40}(?![0-9A-Za-z])")
_TRON = re.compile(r"(?<![0-9A-Za-z])T[1-9A-HJ-NP-Za-km-z]{33}(?![0-9A-Za-z])")
_BTC = re.compile(r"(?<![0-9A-Za-z])(?:bc1[02-9ac-hj-np-z]{11,71}|BC1[02-9AC-HJ-NP-Z]{11,71}|[13][1-9A-HJ-NP-Za-km-z]{25,34})"
                  r"(?![0-9A-Za-z])")
_HOST = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}$")


class TrackerFormatError(ValueError):
    pass


@dataclass
class Entry:
    position: int
    subject: str
    narrative: str
    scam_types: list[str]
    websites: list[str]                       # host names, lower case, no "www."
    addresses: list[tuple[str, str]] = field(default_factory=list)    # (family, stored form)

    @property
    def key(self) -> str:
        """Stable while the entry's subject and websites are unchanged."""
        return hashlib.sha256(f"{self.subject}\n{' '.join(self.websites)}".encode()).hexdigest()[:16]


class _Table(HTMLParser):
    """Rows of the first table: each cell as text, <br> kept as a line break."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth, self.rows, self.row, self.cell, self.in_cell = 0, [], None, None, False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
        elif self.depth == 1 and tag == "tr":
            self.row = []
        elif self.depth == 1 and tag in ("td", "th") and self.row is not None:
            self.cell, self.in_cell = [], True
        elif self.in_cell and tag in ("br", "p", "li"):
            self.cell.append("\n")

    def handle_endtag(self, tag):
        if tag == "table":
            self.depth -= 1
        elif self.depth == 1 and tag in ("td", "th") and self.in_cell:
            self.row.append(re.sub(r"[ \t\r\f\v]+", " ", "".join(self.cell)).strip())
            self.in_cell = False
        elif self.depth == 1 and tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)


def host_of(text: str) -> str | None:
    """A host name from what someone wrote as a website: scheme, path, port, "www."
    and trailing dots removed, lower case, international names in ASCII form. None
    when it is not a host name. Never fetched."""
    t = (text or "").strip().strip("<>\"'").strip()
    t = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", t)
    t = re.split(r"[/?#\s]", t, maxsplit=1)[0]
    t = t.rsplit("@", 1)[-1]                      # user:pass@host
    t = re.sub(r":\d+$", "", t).strip(".").lower()
    try:
        t = t.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if t.startswith("www."):
        t = t[4:]
    return t if _HOST.match(t) else None


def find_addresses(text: str) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """([(family, stored form)], {reason: count} for the ones not used)."""
    found, skipped = [], {"no_checksum": 0, "bad_checksum": 0}
    for m in _EVM.finditer(text):
        body = m.group()[2:]
        if body == body.lower() or body == body.upper():
            skipped["no_checksum"] += 1
        elif evm_address.is_valid(m.group()):
            found.append(("evm", m.group().lower()))
        else:
            skipped["bad_checksum"] += 1
    for m in _TRON.finditer(text):
        if tron_address.is_valid(m.group()):
            found.append(("tron", m.group()))
        else:
            skipped["bad_checksum"] += 1
    for m in _BTC.finditer(text):
        if m.group().startswith("T"):
            continue
        try:
            found.append(("btc", btc_address.normalize(m.group())))
        except ValueError:
            skipped["bad_checksum"] += 1
    seen, unique = set(), []
    for a in found:
        if a not in seen:
            seen.add(a)
            unique.append(a)
    return unique, skipped


def parse(body: bytes | str) -> tuple[list[Entry], dict]:
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    p = _Table()
    p.feed(text)
    if not p.rows or [c.strip() for c in p.rows[0][:4]] != list(COLUMNS):
        raise TrackerFormatError(f"the tracker table's columns changed: {p.rows[0][:5] if p.rows else 'no table'}")
    entries, skipped = [], {"no_checksum": 0, "bad_checksum": 0}
    for i, row in enumerate(p.rows[1:], start=1):
        row = (row + [""] * 5)[:5]
        subject, narrative, types, sites = (html.unescape(c) for c in row[:4])
        websites = []
        for part in re.split(r"[\n,;]+|\s+", sites):
            h = host_of(part)
            if h and h not in websites:
                websites.append(h)
        found, why = find_addresses(f"{subject}\n{narrative}")
        for k, v in why.items():
            skipped[k] += v
        entries.append(Entry(i, " ".join(subject.split()), narrative.strip(),
                             [t.strip() for t in types.split("\n") if t.strip()], websites, found))
    if not entries:
        raise TrackerFormatError("the tracker table has no rows")
    return entries, skipped


def short_subject(subject: str) -> str:
    s = " ".join(subject.split())
    return s if len(s) <= SUBJECT_MAX else s[:SUBJECT_MAX - 1].rstrip() + "…"


def label_text(entry: Entry) -> str:
    return f"named in a complaint reported to California's DFPI (tracker entry: \"{short_subject(entry.subject)}\")"


def claims(entries: list[Entry], read_on: dt.date, evm_chains: Iterable[str], sha: str | None) -> list[LabelClaim]:
    evm_chains = list(evm_chains)
    out = []
    for e in entries:
        for family, stored in e.addresses:
            chains = evm_chains if family == "evm" else [family]
            for chain in chains:
                out.append(LabelClaim(chain=chain, address=stored, category="scam_report", label=label_text(e),
                                      claim_key=f"dfpi:{e.key}:{stored}", source_date=read_on, claim_url=URL,
                                      raw_sha256=sha))
    return out


@dataclass
class TrackerLoad:
    publication: Publication
    entries: list[Entry]


class DfpiTracker:
    slug = SOURCE
    local = True

    def __init__(self, raw_store: RawStore, evm_chains: Iterable[str], *, http: httpx.Client | None = None,
                 today: dt.date | None = None):
        self.raw_store, self.evm_chains = raw_store, list(evm_chains)
        self.http = http or httpx.Client(timeout=60.0, follow_redirects=True,
                                         headers={"user-agent": "TraceAWallet list loader"})
        self.today = today

    def fetch(self) -> tuple[bytes, str]:
        resp = self.http.get(URL)
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(f"DFPI answered HTTP {resp.status_code} with {len(resp.content)} bytes")
        return resp.content, self.raw_store.save(SOURCE, URL, None, 200, resp.content)

    def load(self) -> TrackerLoad:
        body, sha = self.fetch()
        entries, skipped = parse(body)
        read_on = self.today or dt.datetime.now(dt.timezone.utc).date()
        found = claims(entries, read_on, self.evm_chains, sha)
        detail = {"entries": len(entries), "websites": sum(len(e.websites) for e in entries),
                  "addresses": len({a for e in entries for a in e.addresses}),
                  "skipped_no_checksum": skipped["no_checksum"], "skipped_bad_checksum": skipped["bad_checksum"],
                  "bytes": len(body), "page_sha256": sha}
        pub = Publication(SOURCE, read_on, sha, found, unmapped=skipped["no_checksum"] + skipped["bad_checksum"],
                          detail=detail)
        return TrackerLoad(pub, entries)
