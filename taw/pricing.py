"""Fill the price table for a lookup's transfers (Chainlink, read through Blockscout).

For each transfer whose asset has a feed (taw/adapters/prices/assets.py), the
(asset, bucket) it falls in is priced once and kept for every later report. So is
each asset the address holds, at the snapshot's time: the report values holdings
there, and before 2026-11-21 nothing asked for that bucket, so an address whose
last transfer was months ago showed "no price available" for what it holds.
Holdings come first, inside the cap. A
lookup reads at most PRICE_MAX_NEW_PER_LOOKUP new buckets, newest first, so one
long history can't use up the day's Blockscout allowance; the rest show "no
price yet" and are filled by later lookups. Running out of allowance stops
pricing without failing the lookup.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Iterable

from taw import store
from taw.adapters.base import TransferRecord
from taw.adapters.prices.assets import NATIVE, TOKENS, asset_of
from taw.adapters.prices.chainlink import FEEDS, SOURCE, ChainlinkReader, PriceRead, bucket_for


def max_new_per_lookup() -> int:
    return int(os.environ.get("PRICE_MAX_NEW_PER_LOOKUP", "40"))


Key = tuple[str, dt.datetime, str]       # (asset, bucket_start, bucket_size)


def needed(records: Iterable[TransferRecord], now: dt.datetime) -> list[Key]:
    """Distinct (asset, bucket) pairs the transfers need, newest first."""
    keys: set[Key] = set()
    for r in records:
        asset = asset_of(r)
        if asset in FEEDS:
            keys.add((asset, *bucket_for(r.block_time, now)))
    return sorted(keys, key=lambda k: (k[1], k[0]), reverse=True)


def held(snap, now: dt.datetime) -> list[Key]:
    """The (asset, bucket) pairs that value the snapshot's holdings."""
    assets = set()
    if snap.native_balance_raw > 0:
        assets.add(NATIVE.get(snap.chain))
    for b in snap.token_balances:
        contract = b.contract if snap.chain == "tron" else b.contract.lower()
        if b.amount_raw > 0:
            assets.add(TOKENS.get((snap.chain, contract)))
    start, size = bucket_for(snap.as_of_time, now)
    return [(a, start, size) for a in sorted(a for a in assets if a in FEEDS)]


def already_read(conn, keys: list[Key]) -> set[Key]:
    if not keys:
        return set()
    rows = conn.execute(
        "SELECT asset, bucket_start, bucket_size FROM price"
        " WHERE (asset, bucket_start, bucket_size) IN (SELECT * FROM unnest(%s::text[], %s::timestamptz[], %s::text[]))",
        ([k[0] for k in keys], [k[1] for k in keys], [k[2] for k in keys])).fetchall()
    return {(a, b.astimezone(dt.timezone.utc), s) for a, b, s in rows}


_INSERT = """
INSERT INTO price (asset, bucket_start, bucket_size, status, usd, feed_chain, feed_address, block_number,
                   round_id, feed_updated_at, age_seconds, max_age_seconds, source_id, raw_response_id,
                   block_raw_response_id)
VALUES (%s, %s, %s, %s::price_status, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (asset, bucket_start, bucket_size, feed_address) DO NOTHING
"""


def save(conn, actor: str, reads: list[PriceRead]) -> int:
    if not reads:
        return 0
    raw = store.raw_response_ids(conn, "blockscout", [s for r in reads for s in (r.raw_sha256, r.block_raw_sha256)])
    source = store.source_id(conn, SOURCE)
    with store.actor_transaction(conn, actor):
        for r in reads:
            conn.execute(_INSERT, (r.asset, r.bucket_start, r.bucket_size, r.status, r.usd, r.feed.chain,
                                   r.feed.address, r.block_number, r.round_id, r.updated_at, r.age_seconds,
                                   r.feed.max_age, source, raw[r.raw_sha256], raw[r.block_raw_sha256]))
    return len(reads)


@dataclass
class PriceFill:
    needed: int = 0
    reused: int = 0
    read: int = 0
    priced: int = 0
    left_for_later: int = 0
    stopped: str | None = None

    def summary(self) -> str:
        text = (f"prices: {self.needed} bucket(s) needed, {self.reused} already stored, {self.read} read "
                f"({self.priced} with a price)")
        if self.left_for_later:
            text += f", {self.left_for_later} left for a later lookup"
        if self.stopped:
            text += f"; stopped: {self.stopped}"
        return text


def fill(conn, actor: str, records: Iterable[TransferRecord], reader: ChainlinkReader, *,
         now: dt.datetime | None = None, cap: int | None = None, snap=None) -> PriceFill:
    from taw.adapters.chains.evm_etherscan import EtherscanDailyLimit
    from taw.budget import BudgetExhausted

    now = now or dt.datetime.now(dt.timezone.utc)
    cap = max_new_per_lookup() if cap is None else cap
    first = held(snap, now) if snap is not None else []
    keys = first + [k for k in needed(records, now) if k not in first]
    have = already_read(conn, keys)
    todo = [k for k in keys if k not in have]
    out = PriceFill(needed=len(keys), reused=len(keys) - len(todo), left_for_later=max(0, len(todo) - cap))
    reads: list[PriceRead] = []
    try:
        for asset, start, size in todo[:cap]:
            reads.append(reader.read(asset, start, size))
    except (BudgetExhausted, EtherscanDailyLimit) as exc:
        out.stopped = str(exc)
        out.left_for_later = len(todo) - len(reads)
    out.read = save(conn, actor, reads)
    out.priced = sum(1 for r in reads if r.status == "ok")
    return out
