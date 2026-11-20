"""What a report is built from, and the loader that reads it from the database.

A report covers one chain and one address and is built from one snapshot. Every
fact in it comes from that snapshot's source for the chain data (the source that
produced the snapshot), plus our own tables as they stood at `labels_as_of`:

  transfers      rows from the snapshot's source in transactions that touch the
                 address, up to the snapshot block, recorded by the time the
                 snapshot was taken
  labels         every source's current claim on the address at labels_as_of
  counterparties the local sources only (OFAC, the exchange files) on first-hop
                 addresses, as the worker applies them (docs/DECISIONS.md)
  label checks   the newest answer per source and subject at labels_as_of
                 (freeze status per contract, GoPlus)
  prices         the price rows for the buckets the transfers and the snapshot need

labels_as_of is part of the report: building again from the same snapshot with the
same cut-off gives the same data and the same hash.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from taw.adapters.base import TransferRecord
from taw.adapters.prices.assets import NATIVE, TOKENS
from taw.adapters.prices.chainlink import FEEDS, bucket_for

LOCAL_SOURCES = ("ofac-sdn", "exchange-por", "okx-por")
PriceKey = tuple[str, dt.datetime, str]          # (asset, bucket_start, bucket_size)


@dataclass(frozen=True)
class ReportInput:
    chain: str
    chain_name: str
    family: str                                 # evm | tron | btc
    explorer_url: str
    address: str                                # stored form
    snapshot: dict                              # see snapshot_dict()
    source: str                                 # slug of the snapshot's source
    transfers: tuple[TransferRecord, ...]
    labels: tuple[dict, ...] = ()               # on the address: source, category, label, claim_url, source_date, observed_at
    counterparty_labels: dict[str, tuple[dict, ...]] = field(default_factory=dict)
    label_checks: tuple[dict, ...] = ()         # source, subject, block, result, detail, observed_at
    prices: dict[PriceKey, dict] = field(default_factory=dict)   # -> {"status", "usd": Decimal | None}
    sources: dict[str, dict] = field(default_factory=dict)       # slug -> name, attribution, url, kind
    labels_as_of: dt.datetime | None = None
    snapshot_id: int | None = None
    list_dates: dict[str, dt.date] = field(default_factory=dict)  # newest load of each list source by labels_as_of


def snapshot_dict(*, as_of_block: int, as_of_time: dt.datetime, fetched_at: dt.datetime, native_balance_raw: int,
                  native_decimals: int, token_balances: list[dict], is_contract: bool, tokens_truncated: bool,
                  transfer_count: int, transfers_truncated: bool, token_reconciliation: list | None,
                  history_incomplete: bool | None, incomplete_reasons: list[str] | None, checks: list | None,
                  internal_from_block: int | None, internal_to_block: int | None) -> dict:
    """The address_snapshot row as the builder reads it. token_balances: contract,
    amount_raw (int), decimals, symbol."""
    return {"as_of_block": as_of_block, "as_of_time": as_of_time, "fetched_at": fetched_at,
            "native_balance_raw": int(native_balance_raw), "native_decimals": native_decimals,
            "token_balances": [{**b, "amount_raw": int(b["amount_raw"])} for b in token_balances],
            "is_contract": is_contract, "tokens_truncated": tokens_truncated, "transfer_count": transfer_count,
            "transfers_truncated": transfers_truncated, "token_reconciliation": token_reconciliation,
            "history_incomplete": history_incomplete, "incomplete_reasons": incomplete_reasons, "checks": checks,
            "internal_from_block": internal_from_block, "internal_to_block": internal_to_block}


def asset_of(chain: str, contract: str | None) -> str | None:
    """The priced asset a native coin or token is, or None (no feed covers it)."""
    if contract is None:
        return NATIVE.get(chain)
    key = contract if chain == "tron" else contract.lower()
    return TOKENS.get((chain, key))


def price_keys(inp_chain: str, transfers, as_of_time: dt.datetime, token_contracts, now: dt.datetime) -> set[PriceKey]:
    """The (asset, bucket) pairs a report needs: each transfer's, and the snapshot time's
    for every holding. `now` is when the snapshot was taken, so buckets don't move."""
    keys: set[PriceKey] = set()
    for t in transfers:
        a = asset_of(inp_chain, t.token_contract)
        if a in FEEDS:
            keys.add((a, *bucket_for(t.block_time, now)))
    for c in [None, *token_contracts]:
        a = asset_of(inp_chain, c)
        if a in FEEDS:
            keys.add((a, *bucket_for(as_of_time, now)))
    return keys


# ---------------------------------------------------------------------------
# Loading from the database
# ---------------------------------------------------------------------------

_SNAPSHOT = """
SELECT s.id, s.chain, a.address, a.id, c.family::text, c.name, c.explorer_url, src.slug, s.source_id,
       s.as_of_block, s.as_of_time, s.fetched_at, s.native_balance_raw, s.native_decimals, s.token_balances,
       s.is_contract, s.tokens_truncated, s.transfer_count, s.transfers_truncated, s.token_reconciliation,
       s.history_incomplete, s.incomplete_reasons, s.checks, s.internal_from_block, s.internal_to_block
FROM address_snapshot s
JOIN address a ON a.id = s.address_id AND a.chain = s.chain
JOIN chain c ON c.id = s.chain
JOIN source src ON src.id = s.source_id
WHERE s.id = %s
"""

_TRANSFERS = """
WITH mine AS (
    SELECT DISTINCT t.tx_hash FROM transfer t
    WHERE t.chain = %(chain)s AND t.source_id = %(source)s AND t.block_number <= %(block)s
      AND t.recorded_at <= %(fetched)s AND (t.from_address_id = %(aid)s OR t.to_address_id = %(aid)s)
)
SELECT t.tx_hash, t.kind::text, t.event_index, fa.address, ta.address, t.token_contract, t.token_symbol,
       t.token_decimals, t.amount_raw, t.block_number, t.block_time, r.sha256
FROM transfer t
JOIN mine m ON m.tx_hash = t.tx_hash
JOIN raw_response r ON r.id = t.raw_response_id
LEFT JOIN address fa ON fa.id = t.from_address_id
LEFT JOIN address ta ON ta.id = t.to_address_id
WHERE t.chain = %(chain)s AND t.source_id = %(source)s AND t.block_number <= %(block)s
  AND t.recorded_at <= %(fetched)s
ORDER BY t.block_number DESC, t.tx_hash, t.kind, t.event_index
"""

# Labels as they stood at the cut-off: rows recorded by then, and no row recorded by
# then superseding them. status asserted only.
_LABELS = """
SELECT a.address, s.slug, l.category, l.label, l.claim_url, l.source_date, l.observed_at
FROM label l
JOIN address a ON a.id = l.address_id
JOIN source s ON s.id = l.source_id
WHERE a.chain = %s AND a.address = ANY(%s::text[]) AND l.recorded_at <= %s AND l.status = 'asserted'
  AND NOT EXISTS (SELECT 1 FROM label n WHERE n.supersedes_id = l.id AND n.recorded_at <= %s)
  {sources}
ORDER BY a.address, s.slug, l.id
"""

_CHECKS = """
SELECT DISTINCT ON (k.source_id, k.subject) s.slug, k.subject, k.block_number, k.result, k.detail, k.observed_at
FROM label_check k
JOIN source s ON s.id = k.source_id
WHERE k.address_id = %s AND k.observed_at <= %s
ORDER BY k.source_id, k.subject, k.observed_at DESC, k.id DESC
"""


def _labels(conn, chain: str, addresses: list[str], cutoff: dt.datetime, sources: tuple[str, ...] | None):
    if not addresses:
        return {}
    sql = _LABELS.format(sources="AND s.slug = ANY(%s::text[])" if sources else "")
    params: list = [chain, addresses, cutoff, cutoff]
    if sources:
        params.append(list(sources))
    out: dict[str, list[dict]] = {}
    for addr, slug, cat, lab, url, sdate, obs in conn.execute(sql, params).fetchall():
        out.setdefault(addr, []).append({"source": slug, "category": cat, "label": lab, "claim_url": url,
                                         "source_date": sdate, "observed_at": obs})
    return out


def load(conn, snapshot_id: int, labels_as_of: dt.datetime) -> ReportInput:
    """Everything a report on this snapshot needs, as our tables stood at labels_as_of."""
    from taw.labels import counterparties

    row = conn.execute(_SNAPSHOT, (snapshot_id,)).fetchone()
    if row is None:
        raise LookupError(f"no snapshot {snapshot_id}")
    (sid, chain, address, address_id, family, chain_name, explorer, source_slug, source_id, block, as_of_time,
     fetched_at, native_raw, native_dec, tokens, is_contract, tok_trunc, count, tx_trunc, recon, incomplete,
     reasons, checks, int_lo, int_hi) = row
    snap = snapshot_dict(as_of_block=block, as_of_time=as_of_time, fetched_at=fetched_at,
                         native_balance_raw=int(native_raw), native_decimals=native_dec, token_balances=tokens,
                         is_contract=is_contract, tokens_truncated=tok_trunc, transfer_count=count,
                         transfers_truncated=tx_trunc, token_reconciliation=recon, history_incomplete=incomplete,
                         incomplete_reasons=reasons, checks=checks, internal_from_block=int_lo,
                         internal_to_block=int_hi)
    transfers = tuple(
        TransferRecord(chain, h, kind, idx, fa, ta, contract, sym, dec, int(amount), bn, bt, source_slug, sha.strip())
        for h, kind, idx, fa, ta, contract, sym, dec, amount, bn, bt, sha in conn.execute(
            _TRANSFERS, {"chain": chain, "source": source_id, "block": block, "fetched": fetched_at,
                         "aid": address_id}).fetchall())
    own = _labels(conn, chain, [address], labels_as_of, None).get(address, [])
    parties = sorted(counterparties(chain, address, transfers))
    theirs = _labels(conn, chain, parties, labels_as_of, LOCAL_SOURCES)
    checks_rows = tuple({"source": s, "subject": subj, "block": b, "result": res, "detail": det, "observed_at": obs}
                        for s, subj, b, res, det, obs in conn.execute(_CHECKS, (address_id, labels_as_of)).fetchall())
    keys = price_keys(chain, transfers, as_of_time, [t["contract"] for t in tokens], fetched_at)
    prices: dict[PriceKey, dict] = {}
    if keys:
        ks = sorted(keys)
        for asset, start, size, status, usd in conn.execute(
                "SELECT DISTINCT ON (asset, bucket_start, bucket_size) asset, bucket_start, bucket_size,"
                " status::text, usd FROM price"
                " WHERE (asset, bucket_start, bucket_size) IN (SELECT * FROM unnest(%s::text[], %s::timestamptz[],"
                " %s::text[])) AND observed_at <= %s ORDER BY asset, bucket_start, bucket_size, id",
                ([k[0] for k in ks], [k[1] for k in ks], [k[2] for k in ks], labels_as_of)).fetchall():
            prices[(asset, start.astimezone(dt.timezone.utc), size)] = {
                "status": status, "usd": Decimal(usd) if usd is not None else None}
    sources = {slug: {"name": name, "attribution": attr, "url": url, "kind": kind}
               for slug, name, attr, url, kind in conn.execute(
                   "SELECT slug, name, attribution, url, kind::text FROM source").fetchall()}
    list_dates = {slug: d for slug, d in conn.execute(
        "SELECT s.slug, max(l.source_date) FROM label_load l JOIN source s ON s.id = l.source_id"
        " WHERE l.loaded_at <= %s GROUP BY s.slug", (labels_as_of,)).fetchall()}
    return ReportInput(chain=chain, chain_name=chain_name, family=family, explorer_url=explorer, address=address,
                       snapshot=snap, source=source_slug, transfers=transfers, labels=tuple(own),
                       counterparty_labels={a: tuple(v) for a, v in theirs.items()}, label_checks=checks_rows,
                       prices=prices, sources=sources, labels_as_of=labels_as_of, snapshot_id=sid,
                       list_dates=list_dates)
