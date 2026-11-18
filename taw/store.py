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
                  raw_ids: Mapping[str, int]) -> int:
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
        " transfers_truncated, source_id, raw_response_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s) RETURNING id",
        (snap.chain, address_id, snap.as_of_block, snap.as_of_time, snap.native_balance_raw,
         snap.native_decimals, json.dumps(tokens), snap.is_contract, snap.truncated,
         len(batch.transfers), batch.truncated, source_id(conn, snap.source_slug),
         raw_ids[snap.raw_sha256[0]]),
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


def fresh_snapshot(conn, chain: str, address: str, max_age: dt.timedelta = FRESH_FOR) -> FreshSnapshot | None:
    """The newest snapshot of this address fetched within max_age, if any."""
    row = conn.execute(
        "SELECT s.id, s.as_of_block, s.fetched_at FROM address_snapshot s"
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


def save_lookup(conn, actor: str, snap: AddressSnapshot, batch: TransferBatch) -> LookupSaved:
    """Write one lookup's observations in a single actor-tagged transaction."""
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
        snapshot_id = save_snapshot(conn, snap, ids[snap.address], batch, raw_ids)
    return LookupSaved(snapshot_id, ids[snap.address], len(batch.transfers), new, len(ids))
