"""Writes chain observations to Postgres. Shared by every chain.

Two connections, both opened with autocommit=True, so every transaction here
is an explicit BEGIN ... COMMIT:

  raw connection   RecordingRawStore commits each provider response as soon as
                   it is saved. A job that fails halfway still leaves the record
                   of what was fetched, and a retry can reuse it.
  data connection  save_lookup() writes addresses, transfers and the snapshot
                   in one transaction: all of it or none of it.

Every transaction sets taw.actor first, so audit triggers name the worker in
event_log. Re-running a lookup inserts nothing new and raises nothing:
addresses use ON CONFLICT DO NOTHING on (chain, address) and transfers on the
table's unique key (chain, tx_hash, kind, event_index, source_id).
"""
from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence

from taw.adapters.base import AddressSnapshot, BucketRawStore, TransferBatch, TransferRecord
from taw.reconcile import Completeness

FRESH_FOR = dt.timedelta(minutes=10)


@contextmanager
def actor_transaction(conn, actor: str) -> Iterator[Any]:
    """BEGIN, name the actor for audit triggers, yield, COMMIT (ROLLBACK on error)."""
    if not conn.autocommit:
        raise RuntimeError("store connections must be opened with autocommit=True")
    with conn.transaction():
        conn.execute("SELECT set_config('taw.actor', %s, true)", (actor,))
        yield conn


class RecordingRawStore:
    """BucketRawStore that commits each saved response in its own actor-tagged
    transaction. Use a dedicated autocommit connection."""

    def __init__(self, conn, actor: str, s3_client=None, bucket: str | None = None):
        if not conn.autocommit:
            raise RuntimeError("RecordingRawStore needs an autocommit connection")
        self.conn, self.actor = conn, actor
        self.inner = BucketRawStore(conn, s3_client=s3_client, bucket=bucket)

    def save(self, source_slug: str, request_url: str, params: Mapping[str, Any] | None,
             status: int, body_bytes: bytes) -> str:
        with actor_transaction(self.conn, self.actor):
            return self.inner.save(source_slug, request_url, params, status, body_bytes)

    def find(self, source_slug: str, request_url: str, params: Mapping[str, Any] | None) -> str | None:
        return self.inner.find(source_slug, request_url, params)

    def body(self, sha: str) -> bytes:
        return self.inner.body(sha)


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

def chain_family(conn, chain: str) -> str:
    row = conn.execute("SELECT family::text FROM chain WHERE id = %s", (chain,)).fetchone()
    if row is None:
        raise ValueError(f"unknown chain {chain!r}")
    return row[0]


def normalize(family: str, address: str) -> str:
    """The form the address_normalize trigger stores, so lookups find the row."""
    a = address.strip()
    if family == "evm" or (family == "btc" and a.lower().startswith("bc1")):
        return a.lower()
    return a


def upsert_addresses(conn, chain: str, addresses: Iterable[str]) -> dict[str, int]:
    """Insert any addresses not yet known. Returns {address as given: address.id}."""
    family = chain_family(conn, chain)
    norm = {a: normalize(family, a) for a in set(addresses) if a}
    if not norm:
        return {}
    wanted = sorted(set(norm.values()))
    conn.execute(
        "INSERT INTO address (chain, address) SELECT %s, a FROM unnest(%s::text[]) AS a"
        " ON CONFLICT (chain, address) DO NOTHING",
        (chain, wanted),
    )
    found = dict(conn.execute(
        "SELECT address, id FROM address WHERE chain = %s AND address = ANY(%s::text[])", (chain, wanted)
    ).fetchall())
    missing = [n for n in wanted if n not in found]
    if missing:
        raise RuntimeError(f"addresses not stored: {missing[:3]}")
    return {a: found[n] for a, n in norm.items()}


# ---------------------------------------------------------------------------
# Sources and raw responses
# ---------------------------------------------------------------------------

def source_id(conn, slug: str) -> int:
    row = conn.execute("SELECT id FROM source WHERE slug = %s", (slug,)).fetchone()
    if row is None:
        raise ValueError(f"no source row with slug {slug!r}")
    return row[0]


def raw_response_ids(conn, source_slug: str, shas: Iterable[str]) -> dict[str, int]:
    """{sha256: id of the newest raw_response row for that body from that source}."""
    wanted = sorted(set(shas))
    if not wanted:
        return {}
    rows = conn.execute(
        "SELECT DISTINCT ON (r.sha256) r.sha256, r.id FROM raw_response r"
        " WHERE r.source_id = %s AND r.sha256 = ANY(%s::text[]) ORDER BY r.sha256, r.id DESC",
        (source_id(conn, source_slug), wanted),
    ).fetchall()
    found = {sha.strip(): rid for sha, rid in rows}
    missing = [s for s in wanted if s not in found]
    if missing:
        raise RuntimeError(f"{len(missing)} response(s) were never recorded, e.g. {missing[0]}")
    return found


# ---------------------------------------------------------------------------
# Transfers and snapshots
# ---------------------------------------------------------------------------

_INSERT_TRANSFERS = """
INSERT INTO transfer (chain, tx_hash, kind, event_index, from_address_id, to_address_id,
                      token_contract, token_symbol, token_decimals, amount_raw, block_number,
                      block_time, usd_value, price_source_id, source_id, raw_response_id)
SELECT t.chain, t.tx_hash, t.kind::transfer_kind, t.event_index, t.from_id, t.to_id,
       t.token_contract, t.token_symbol, t.token_decimals, t.amount_raw, t.block_number,
       t.block_time, NULL, NULL, t.source_id, t.raw_id
FROM unnest(%s::text[], %s::text[], %s::text[], %s::int[], %s::bigint[], %s::bigint[],
            %s::text[], %s::text[], %s::int[], %s::numeric[], %s::bigint[],
            %s::timestamptz[], %s::int[], %s::bigint[])
     AS t(chain, tx_hash, kind, event_index, from_id, to_id, token_contract, token_symbol,
          token_decimals, amount_raw, block_number, block_time, source_id, raw_id)
ON CONFLICT (chain, tx_hash, kind, event_index, source_id) DO NOTHING
RETURNING id
"""


def insert_transfers(conn, records: Sequence[TransferRecord], address_ids: Mapping[str, int],
                     raw_ids: Mapping[str, int]) -> int:
    """Insert transfers not already stored. Returns how many were new.

    Prices are not set here; usd_value stays NULL until the price step.
    """
    if not records:
        return 0
    source_ids = {slug: source_id(conn, slug) for slug in {r.source_slug for r in records}}
    cols: list[list] = [[] for _ in range(14)]
    for r in records:
        row = (r.chain, r.tx_hash, r.kind, r.event_index,
               address_ids[r.from_address] if r.from_address else None,
               address_ids[r.to_address] if r.to_address else None,
               r.token_contract, r.token_symbol, r.token_decimals, r.amount_raw, r.block_number,
               r.block_time, source_ids[r.source_slug], raw_ids[r.raw_sha256])
        for col, value in zip(cols, row):
            col.append(value)
    return len(conn.execute(_INSERT_TRANSFERS, cols).fetchall())


def save_snapshot(conn, snap: AddressSnapshot, address_id: int, batch: TransferBatch,
                  raw_ids: Mapping[str, int], recon: Completeness | None = None) -> int:
    """Insert the snapshot row and link every response it was built from.

    The adapter lists the balance response first in raw_sha256; that one is
    the snapshot row's own raw_response_id.
    """
    if not snap.raw_sha256:
        raise ValueError("a snapshot must rest on at least one stored response")
    tokens = [{"contract": b.contract, "amount_raw": str(b.amount_raw), "decimals": b.decimals,
               "symbol": b.symbol} for b in snap.token_balances]
    row = conn.execute(
        "INSERT INTO address_snapshot (chain, address_id, as_of_block, as_of_time, native_balance_raw,"
        " native_decimals, token_balances, is_contract, tokens_truncated, transfer_count,"
        " transfers_truncated, source_id, raw_response_id, token_reconciliation, history_incomplete,"
        " incomplete_reasons, checks, internal_from_block, internal_to_block)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s)"
        " RETURNING id",
        (snap.chain, address_id, snap.as_of_block, snap.as_of_time, snap.native_balance_raw,
         snap.native_decimals, json.dumps(tokens), snap.is_contract, snap.truncated,
         len(batch.transfers), batch.truncated, source_id(conn, snap.source_slug),
         raw_ids[snap.raw_sha256[0]],
         json.dumps([t.as_json() for t in recon.tokens]) if recon else None,
         recon.history_incomplete if recon else None,
         list(recon.incomplete_reasons) if recon else None,
         json.dumps([c.as_json() for c in recon.checks]) if recon else None,
         batch.internal_range[0] if batch.internal_range else None,
         batch.internal_range[1] if batch.internal_range else None),
    ).fetchone()
    conn.execute(
        "INSERT INTO address_snapshot_response (snapshot_id, raw_response_id)"
        " SELECT %s, r FROM unnest(%s::bigint[]) AS r",
        (row[0], sorted({raw_ids[s] for s in snap.raw_sha256})),
    )
    return row[0]


@dataclass(frozen=True)
class FreshSnapshot:
    id: int
    as_of_block: int
    fetched_at: dt.datetime
    transfer_count: int
    history_incomplete: bool | None = None     # None: not checked (rows from before 0007)


def fresh_snapshot(conn, chain: str, address: str, max_age: dt.timedelta = FRESH_FOR) -> FreshSnapshot | None:
    """The newest snapshot of this address fetched within max_age, if any."""
    row = conn.execute(
        "SELECT s.id, s.as_of_block, s.fetched_at, s.transfer_count, s.history_incomplete FROM address_snapshot s"
        " JOIN address a ON a.id = s.address_id AND a.chain = s.chain"
        " WHERE a.chain = %s AND a.address = %s AND s.fetched_at > now() - %s"
        " ORDER BY s.fetched_at DESC LIMIT 1",
        (chain, normalize(chain_family(conn, chain), address), max_age),
    ).fetchone()
    return FreshSnapshot(*row) if row else None


@dataclass(frozen=True)
class LookupSaved:
    snapshot_id: int
    address_id: int
    transfers_seen: int
    transfers_new: int
    addresses_seen: int

    @property
    def transfers_reused(self) -> int:
        """Transfers this lookup found that were already stored."""
        return self.transfers_seen - self.transfers_new


LOOKUP_COMPLETED = "lookup_completed"


def log_lookup_completed(conn, actor: str, *, chain: str, address: str, snapshot_id: int,
                         as_of_block: int, transfers_saved: int, transfers_reused: int,
                         snapshot_reused: bool, history_incomplete: bool | None) -> None:
    """One event_log row per completed lookup. Call inside an actor_transaction.

    Holds only what the evidence tables already hold (address, snapshot, block,
    counts). Nothing about who asked: that stays in the private lookup table.
    """
    payload = {"chain": chain, "address": address, "snapshot_id": snapshot_id,
               "as_of_block": as_of_block, "transfers_saved": transfers_saved,
               "transfers_reused": transfers_reused, "snapshot_reused": snapshot_reused,
               "history_incomplete": history_incomplete}
    conn.execute(
        "INSERT INTO event_log (actor, action, entity, entity_id, payload)"
        " VALUES (%s, %s, 'address_snapshot', %s, %s::jsonb)",
        (actor, LOOKUP_COMPLETED, str(snapshot_id), json.dumps(payload, sort_keys=True)),
    )


def record_reused_lookup(conn, actor: str, chain: str, address: str, fresh: FreshSnapshot) -> None:
    """Log a lookup that finished by reusing a recent snapshot. Nothing else is written."""
    with actor_transaction(conn, actor):
        log_lookup_completed(conn, actor, chain=chain, address=address, snapshot_id=fresh.id,
                             as_of_block=fresh.as_of_block, transfers_saved=0,
                             transfers_reused=fresh.transfer_count, snapshot_reused=True,
                             history_incomplete=fresh.history_incomplete)


def save_lookup(conn, actor: str, snap: AddressSnapshot, batch: TransferBatch,
                recon: Completeness | None = None) -> LookupSaved:
    """Write one lookup's observations, its reconciliation and its lookup_completed
    event, in a single actor-tagged transaction: the log never records a lookup
    that did not land."""
    if batch.chain != snap.chain or batch.address != snap.address:
        raise ValueError("snapshot and transfers are for different addresses")
    with actor_transaction(conn, actor):
        addresses = {snap.address}
        for t in batch.transfers:
            addresses.update(a for a in (t.from_address, t.to_address) if a)
        ids = upsert_addresses(conn, snap.chain, addresses)

        raw_ids: dict[str, int] = {}
        for slug in {snap.source_slug} | {t.source_slug for t in batch.transfers}:
            shas = [s for s in snap.raw_sha256 if snap.source_slug == slug]
            shas += [t.raw_sha256 for t in batch.transfers if t.source_slug == slug]
            raw_ids.update(raw_response_ids(conn, slug, shas))

        new = insert_transfers(conn, batch.transfers, ids, raw_ids)
        snapshot_id = save_snapshot(conn, snap, ids[snap.address], batch, raw_ids, recon)
        saved = LookupSaved(snapshot_id, ids[snap.address], len(batch.transfers), new, len(ids))
        log_lookup_completed(conn, actor, chain=snap.chain, address=snap.address, snapshot_id=snapshot_id,
                             as_of_block=snap.as_of_block, transfers_saved=saved.transfers_new,
                             transfers_reused=saved.transfers_reused, snapshot_reused=False,
                             history_incomplete=recon.history_incomplete if recon else None)
    return saved
