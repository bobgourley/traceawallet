"""Writes label claims to the append-only label table.

Each claim is keyed by (address, source, claim_key). Comparing what a source says
now with the newest row for each key (the "head"):

  no head                     insert the claim
  head asserted, same claim   nothing
  head asserted, changed      insert the claim, superseding the head
  head withdrawn              insert the claim, superseding it (relisted)
  head asserted, not claimed  insert a 'withdrawn' copy superseding it

So history is append-only (rule 5): a delisting is a new row that withdraws
the old one, and label_current shows what each source says today.

For a bulk source the comparison covers everything that source ever said. For
an address source it covers only the address that was asked about.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from dataclasses import dataclass

from taw import store
from taw.adapters.labels.base import LabelClaim, Publication


@dataclass(frozen=True)
class SyncResult:
    claims: int
    added: int
    changed: int
    withdrawn: int

    def summary(self) -> str:
        return f"{self.claims} claim(s): {self.added} added, {self.changed} changed, {self.withdrawn} withdrawn"


_HEADS = """
SELECT l.id, l.address_id, l.claim_key, l.category, l.label, l.claim_url, l.status::text
FROM label l
WHERE l.source_id = %s AND l.claim_key IS NOT NULL {scope}
  AND NOT EXISTS (SELECT 1 FROM label n WHERE n.supersedes_id = l.id)
"""

_INSERT = """
INSERT INTO label (address_id, source_id, category, label, claim_url, source_date, observed_at, status,
                   supersedes_id, raw_response_id, claim_key)
SELECT * FROM unnest(%s::bigint[], %s::int[], %s::text[], %s::text[], %s::text[], %s::date[], %s::timestamptz[],
                     %s::label_status[], %s::bigint[], %s::bigint[], %s::text[])
"""


def _address_ids(conn, claims: list[LabelClaim], extra: dict[str, set[str]] | None = None) -> dict[tuple[str, str], int]:
    by_chain: dict[str, set[str]] = defaultdict(set)
    for c in claims:
        by_chain[c.chain].add(c.address)
    for chain, addrs in (extra or {}).items():
        by_chain[chain] |= addrs
    ids: dict[tuple[str, str], int] = {}
    for chain, addrs in by_chain.items():
        for given, i in store.upsert_addresses(conn, chain, addrs).items():
            ids[(chain, given)] = i
    return ids


def sync(conn, actor: str, source_slug: str, claims: list[LabelClaim], *, raw_sha256: str | None,
         source_date: dt.date | None, observed_at: dt.datetime | None = None,
         only_address: tuple[str, str] | None = None, raw_source: str | None = None) -> SyncResult:
    """Make the label table agree with `claims`. only_address=(chain, address) limits
    withdrawals to that address (address sources); None covers the whole source.
    raw_source: the source the raw response was saved under, when it isn't the
    label's (a contract read served by Blockscout or TronGrid)."""
    observed_at = observed_at or dt.datetime.now(dt.timezone.utc)
    source = store.source_id(conn, source_slug)
    raw_id = None
    if raw_sha256:
        raw_id = store.raw_response_ids(conn, raw_source or source_slug, [raw_sha256])[raw_sha256]
    rows: list[tuple] = []
    with store.actor_transaction(conn, actor):
        extra = {only_address[0]: {only_address[1]}} if only_address else None
        ids = _address_ids(conn, claims, extra)
        scope, params = "", [source]
        if only_address:
            scope = "AND l.address_id = %s"
            params.append(ids[only_address])
        heads = {(a, k): (i, cat, lab, url, status)
                 for i, a, k, cat, lab, url, status in conn.execute(_HEADS.format(scope=scope), params).fetchall()}
        seen = set()
        added = changed = withdrawn = 0
        for c in claims:
            key = (ids[(c.chain, c.address)], c.claim_key)
            if key in seen:
                continue
            seen.add(key)
            head = heads.get(key)
            if head and head[4] == "asserted" and head[1:4] == (c.category, c.label, c.claim_url):
                continue
            rows.append((key[0], source, c.category, c.label, c.claim_url, c.source_date, observed_at, "asserted",
                         head[0] if head else None, raw_id, c.claim_key))
            if head is None:
                added += 1
            else:
                changed += 1
        for key, (i, cat, lab, url, status) in heads.items():
            if status == "asserted" and key not in seen:
                rows.append((key[0], source, cat, lab, url, source_date, observed_at, "withdrawn", i, raw_id, key[1]))
                withdrawn += 1
        if rows:
            conn.execute(_INSERT, [list(col) for col in zip(*rows)])
    return SyncResult(len(seen), added, changed, withdrawn)


def load_publication(conn, actor: str, pub: Publication) -> SyncResult:
    """Sync a bulk source's whole publication and record the load."""
    result = sync(conn, actor, pub.source_slug, pub.claims, raw_sha256=pub.raw_sha256, source_date=pub.source_date)
    raw_id = store.raw_response_ids(conn, pub.source_slug, [pub.raw_sha256])[pub.raw_sha256] if pub.raw_sha256 else None
    with store.actor_transaction(conn, actor):
        conn.execute(
            "INSERT INTO label_load (source_id, source_date, raw_response_id, claims, added, changed, withdrawn,"
            " unmapped, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            (store.source_id(conn, pub.source_slug), pub.source_date, raw_id, result.claims, result.added,
             result.changed, result.withdrawn, pub.unmapped, json.dumps(pub.detail)))
    return result


def current(conn, chain: str, addresses: list[str], *, sources: list[str] | None = None) -> dict[str, list[dict]]:
    """What each source says today about each address: {address: [label, ...]}."""
    if not addresses:
        return {}
    family = store.chain_family(conn, chain)
    norm = {store.normalize(family, a): a for a in addresses}
    sql = ("SELECT a.address, s.slug, l.category, l.label, l.claim_url, l.source_date, l.observed_at"
           " FROM label_current l JOIN address a ON a.id = l.address_id JOIN source s ON s.id = l.source_id"
           " WHERE a.chain = %s AND a.address = ANY(%s::text[])")
    params: list = [chain, list(norm)]
    if sources is not None:
        sql += " AND s.slug = ANY(%s::text[])"
        params.append(sources)
    out: dict[str, list[dict]] = {a: [] for a in addresses}
    for addr, slug, cat, lab, url, sdate, obs in conn.execute(sql + " ORDER BY s.slug, l.id", params).fetchall():
        out[norm[addr]].append({"source": slug, "category": cat, "label": lab, "claim_url": url,
                                "source_date": sdate, "observed_at": obs})
    return out


def record_check(conn, actor: str, source_slug: str, chain: str, address: str, *, subject: str,
                 block: int | None, result: str, detail: dict, raw_sha256: str, raw_source: str) -> None:
    """Record that a source was asked about an address, and its answer (label_check)."""
    raw_id = store.raw_response_ids(conn, raw_source, [raw_sha256])[raw_sha256]
    with store.actor_transaction(conn, actor):
        address_id = store.upsert_addresses(conn, chain, [address])[address]
        conn.execute(
            "INSERT INTO label_check (source_id, address_id, subject, block_number, result, detail, raw_response_id)"
            " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)",
            (store.source_id(conn, source_slug), address_id, subject, block, result, json.dumps(detail, default=str),
             raw_id))


# ---------------------------------------------------------------------------
# Applying labels to a lookup (design: DECISIONS.md 2026-11-20 "Labels: applying them")
# ---------------------------------------------------------------------------

# Answered from our own tables: applying them costs no API calls.
LOCAL_SOURCES = ("ofac-sdn", "exchange-por")


def counterparties(chain: str, address: str, records) -> dict[str, set[str]]:
    """{address: {'funder', 'destination'}} one hop from `address`, from its transfers.

    Account chains: funders sent to it, destinations received from it. Bitcoin rows
    are one side of a transaction each, so a funder is another input of a
    transaction that paid the address, and a destination is an output of a
    transaction that spent from it ("outputs of transactions that spent from this
    address"); the address's own change is left out.
    """
    me = address.lower() if chain != "tron" else address
    same = (lambda a: a is not None and a.lower() == me) if chain != "tron" else (lambda a: a == me)
    out: dict[str, set[str]] = defaultdict(set)
    if chain == "btc":
        by_tx: dict[str, list] = defaultdict(list)
        for r in records:
            by_tx[r.tx_hash].append(r)
        for rows in by_tx.values():
            paid = any(r.kind == "utxo_out" and same(r.to_address) for r in rows)
            spent = any(r.kind == "utxo_in" and same(r.from_address) for r in rows)
            for r in rows:
                if paid and r.kind == "utxo_in" and r.from_address and not same(r.from_address):
                    out[r.from_address].add("funder")
                if spent and r.kind == "utxo_out" and r.to_address and not same(r.to_address):
                    out[r.to_address].add("destination")
        return dict(out)
    for r in records:
        if same(r.to_address) and r.from_address and not same(r.from_address):
            out[r.from_address].add("funder")
        if same(r.from_address) and r.to_address and not same(r.to_address):
            out[r.to_address].add("destination")
    return dict(out)


@dataclass(frozen=True)
class Applied:
    address: list[dict]                         # every current label, from every source
    counterparties: dict[str, dict]             # address -> {"roles": [...], "labels": [...]} (local sources only)
    counterparties_checked: int

    def summary(self) -> str:
        hit = len(self.counterparties)
        return (f"labels: {len(self.address)} on the address; {hit} of {self.counterparties_checked} first-hop "
                f"address(es) carry an OFAC or exchange label")


def apply_to_lookup(conn, chain: str, address: str, records) -> Applied:
    """The looked-up address gets every source's labels. Its funders and first-hop
    destinations get only the local sources (OFAC, the exchange file)."""
    own = current(conn, chain, [address])[address]
    parties = counterparties(chain, address, records)
    found = current(conn, chain, sorted(parties), sources=list(LOCAL_SOURCES)) if parties else {}
    hits = {a: {"roles": sorted(parties[a]), "labels": labs} for a, labs in found.items() if labs}
    return Applied(own, hits, len(parties))
