"""The label-source interface (design rule 7).

A label is what one source says about one address on one chain, worded as that
source's claim (rule 9). Sources come in two shapes:

  BulkLabelSource     loads a whole published list (OFAC's SDN list, the
                      exchange wallet file). Its labels sit in our own tables,
                      so applying them to any address costs no API calls. These
                      are the "local" sources.
  AddressLabelSource  is asked about one address at a time (GoPlus, the
                      stablecoin contracts). Each question costs provider calls,
                      so only the looked-up address is asked.

Every claim carries its source and date, and the sha256 of the raw response it
came from (None only for the hand-built exchange file).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Protocol

CATEGORIES = ("exchange", "entity", "sanctioned", "seized", "frozen", "malicious_flag", "scam_report", "other")


@dataclass(frozen=True)
class LabelClaim:
    chain: str
    address: str                  # in our stored form for the chain
    category: str
    label: str                    # the source's claim, in the source's wording
    claim_key: str                # stable identity of this claim within its source
    source_date: dt.date | None
    claim_url: str | None = None
    raw_sha256: str | None = None
    # The label text carries source_date ("listed by OKX as its wallet on 2026-11-01").
    # A reload that says the same thing with a later date keeps the existing row, so
    # the text keeps the first date it was listed; label_load records each later list.
    dated_label: bool = False

    def __post_init__(self):
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown label category {self.category!r}")


@dataclass
class Publication:
    """One load of a bulk source."""
    source_slug: str
    source_date: dt.date
    raw_sha256: str | None
    claims: list[LabelClaim]
    unmapped: int = 0
    detail: dict = field(default_factory=dict)


class BulkLabelSource(Protocol):
    slug: str
    local: bool                   # True: answered from our tables, no API calls

    def load(self) -> Publication: ...


class AddressLabelSource(Protocol):
    slug: str
    local: bool                   # False

    def claims_for(self, chain: str, address: str) -> tuple[list[LabelClaim], dict]:
        """(claims now true for the address, what was checked). An empty claim list
        with a check record means "checked, nothing to say"."""
        ...
