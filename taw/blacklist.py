"""The global index of stablecoin blacklist events (migration 0016).

  record_scan    store one scan's events with one event_log entry (row count and a
                 sha256 over the rows added), in one transaction
  verify_scans   recompute each scan's digest (scripts/verify_label_loads.py runs it too)
  coverage       how far the index is complete for a contract
  update         scan each indexed contract from where its index ends to the head;
                 the daily blacklist_refresh job, and the backfill when asked
  freeze_since   the freeze event behind an address's current freeze, if indexed
  destructions   DestroyedBlackFunds events for an address, up to a block

A contract never scanned has no coverage: a lookup then says the index was not
available, never that no event exists.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass

from taw import store
from taw.adapters.labels import blacklist as bl

EVM_CONFIRMATIONS = 64           # an Ethereum scan stops this many blocks below the head
DIGEST_METHOD = ("blacklist_event rows by id: json_build_array(id, chain, contract, token, action, event_name, "
                 "address, amount_raw, block_number, block_time UTC, tx_hash, log_index, raw_response_id), "
                 "one per line, sha256")
_DIGEST_ROWS = """
SELECT json_build_array(id, chain, contract, token, action::text, event_name, address, amount_raw::text,
                        block_number, to_char(block_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS'),
                        tx_hash, log_index, raw_response_id)::text
FROM blacklist_event WHERE scan_id = %s ORDER BY id
"""
_INSERT = """
INSERT INTO blacklist_event (scan_id, chain, contract, token, action, event_name, address, amount_raw, block_number,
                             block_time, tx_hash, log_index, source_id, raw_response_id)
SELECT %s, %s, %s, %s, a::blacklist_action, n, ad, am, b, t, h, i, %s, r
FROM unnest(%s::text[], %s::text[], %s::text[], %s::numeric[], %s::bigint[], %s::timestamptz[], %s::text[],
            %s::int[], %s::bigint[]) AS u(a, n, ad, am, b, t, h, i, r)
ON CONFLICT (chain, contract, tx_hash, log_index) DO NOTHING
"""


@dataclass(frozen=True)
class Coverage:
    from_block: int
    to_block: int
    to_time: dt.datetime | None
    scans: int


def coverage(conn, chain: str, contract: str) -> Coverage | None:
    row = conn.execute("SELECT min(from_block), max(to_block), max(to_time), count(*) FROM blacklist_scan"
                       " WHERE chain = %s AND contract = %s", (chain, contract)).fetchone()
    return Coverage(*row) if row and row[3] else None


def scan_digest(conn, scan_id: int) -> tuple[int, str]:
    h, n = hashlib.sha256(), 0
    for (line,) in conn.execute(_DIGEST_ROWS, (scan_id,)):
        h.update(line.encode() + b"\n")
        n += 1
    return n, h.hexdigest()


def record_scan(conn, actor: str, c: bl.IndexedContract, from_block: int, to_block: int,
                to_time: dt.datetime | None, result: bl.ScanResult, raw_source: str,
                detail: dict | None = None) -> tuple[int, int]:
    """(scan id, events added). Events already in the index are skipped."""
    raw_ids = store.raw_response_ids(conn, raw_source, {e.raw_sha256 for e in result.events})
    source = store.source_id(conn, c.source)
    with store.actor_transaction(conn, actor):
        scan_id = conn.execute(
            "INSERT INTO blacklist_scan (chain, contract, token, source_id, raw_source_id, from_block, to_block,"
            " to_time, calls, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)"
            " RETURNING id",
            (c.chain, c.contract, c.token, source, store.source_id(conn, raw_source), from_block, to_block, to_time,
             result.calls, json.dumps(detail or {}))).fetchone()[0]
        ev = result.events
        if ev:
            conn.execute(_INSERT, (scan_id, c.chain, c.contract, c.token, source,
                                   [e.action for e in ev], [e.event_name for e in ev], [e.address for e in ev],
                                   [e.amount_raw for e in ev], [e.block_number for e in ev],
                                   [e.block_time for e in ev], [e.tx_hash for e in ev], [e.log_index for e in ev],
                                   [raw_ids.get(e.raw_sha256) for e in ev]))
        n, digest = scan_digest(conn, scan_id)
        by_action: dict[str, int] = {}
        for (action, count) in conn.execute("SELECT action::text, count(*) FROM blacklist_event WHERE scan_id = %s"
                                            " GROUP BY 1 ORDER BY 1", (scan_id,)).fetchall():
            by_action[action] = count
        conn.execute(
            "INSERT INTO event_log (actor, action, entity, entity_id, payload) VALUES (%s, 'scan', 'blacklist_scan',"
            " %s, %s::jsonb)",
            (actor, str(scan_id), json.dumps({"chain": c.chain, "contract": c.contract, "from_block": from_block,
                                              "to_block": to_block, "rows": n, "by_action": by_action,
                                              "rows_sha256": digest, "method": DIGEST_METHOD})))
    return scan_id, n


def verify_scans(conn) -> list[tuple[int, bool, str]]:
    out = []
    for sid, payload in conn.execute("SELECT entity_id::bigint, payload FROM event_log WHERE entity = 'blacklist_scan'"
                                     " AND action = 'scan' ORDER BY 1").fetchall():
        n, digest = scan_digest(conn, sid)
        problems = []
        if n != payload["rows"]:
            problems.append(f"rows {n} now, {payload['rows']} logged")
        if digest != payload["rows_sha256"]:
            problems.append(f"rows sha256 {digest[:12]} now, {payload['rows_sha256'][:12]} logged")
        out.append((sid, not problems, "; ".join(problems) or f"{n} rows, sha256 {digest[:12]}"))
    return out


# -- scanning ---------------------------------------------------------------------------------

def update_evm(conn, actor: str, adapter, c: bl.IndexedContract, *, backfill: bool = False,
               to_block: int | None = None) -> str:
    cov = coverage(conn, c.chain, c.contract)
    if cov is None and not backfill:
        return f"{c.chain} {c.token}: not backfilled yet"
    if to_block is None:
        head, _ = adapter._block_number()
        to_block = head - EVM_CONFIRMATIONS
    lo = c.start_block if cov is None else cov.to_block + 1
    if lo > to_block:
        return f"{c.chain} {c.token}: up to date at block {cov.to_block:,}"
    result = bl.evm_scan(bl.blockscout_logs(adapter), c, lo, to_block)
    sid, n = record_scan(conn, actor, c, lo, to_block, None, result, "blockscout")
    return f"{c.chain} {c.token}: blocks {lo:,} to {to_block:,}, {n} new event(s), {result.calls} call(s) (scan {sid})"


def update_tron(conn, actor: str, adapter, c: bl.IndexedContract, *, backfill: bool = False) -> str:
    cov = coverage(conn, c.chain, c.contract)
    if cov is None and not backfill:
        return f"{c.chain} {c.token}: not backfilled yet"
    head, head_time, _ = adapter.latest_block()
    start = c.start_time if cov is None else cov.to_time
    lo = 0 if cov is None else cov.to_block + 1
    if head <= (cov.to_block if cov else -1):
        return f"{c.chain} {c.token}: up to date at block {cov.to_block:,}"
    before = adapter.request_count
    result = bl.tron_scan(adapter, c, start, head_time)
    sid, n = record_scan(conn, actor, c, lo, head, head_time, result, "trongrid",
                         detail={"from_time": start.isoformat(), "head_calls": 1,
                                 "requests": adapter.request_count - before})
    return (f"{c.chain} {c.token}: {start:%Y-%m-%d %H:%M} to {head_time:%Y-%m-%d %H:%M} UTC (block {head:,}), "
            f"{n} new event(s), {result.calls} call(s) (scan {sid})")


def update(conn, actor: str, adapter_for, chains: tuple[str, ...] = ("eth", "tron"), *,
           backfill: bool = False) -> list[str]:
    """adapter_for(chain) -> adapter. Each contract is scanned from where its index ends."""
    notes = []
    for chain in chains:
        contracts = bl.for_chain(chain)
        if not contracts:
            continue
        adapter = adapter_for(chain)
        for c in contracts:
            if chain == "tron":
                notes.append(update_tron(conn, actor, adapter, c, backfill=backfill))
            else:
                notes.append(update_evm(conn, actor, adapter, c, backfill=backfill))
    return notes


# -- reading ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class IndexedEvent:
    action: str
    block_number: int
    block_time: dt.datetime | None
    tx_hash: str
    log_index: int
    amount_raw: int | None

    def as_json(self) -> dict:
        return {"tx_hash": self.tx_hash, "log_index": self.log_index, "block": self.block_number,
                "time": self.block_time.isoformat() if self.block_time else None,
                "amount_raw": None if self.amount_raw is None else str(self.amount_raw)}


def events_for(conn, chain: str, contract: str, address: str, up_to_block: int | None = None) -> list[IndexedEvent]:
    rows = conn.execute(
        "SELECT action::text, block_number, block_time, tx_hash, log_index, amount_raw FROM blacklist_event"
        " WHERE chain = %s AND address = %s AND contract = %s AND (%s::bigint IS NULL OR block_number <= %s)"
        " ORDER BY block_number, log_index", (chain, address, contract, up_to_block, up_to_block)).fetchall()
    return [IndexedEvent(a, b, t, h, i, None if am is None else int(am)) for a, b, t, h, i, am in rows]


@dataclass(frozen=True)
class FreezeFound:
    event: IndexedEvent | None          # the freeze behind the current status; None if not in the index
    covered_to_block: int | None        # None: this contract is not indexed or never scanned


def freeze_since(conn, chain: str, contract: str, address: str, up_to_block: int | None) -> FreezeFound:
    cov = coverage(conn, chain, contract)
    if cov is None:
        return FreezeFound(None, None)
    last = None
    for e in events_for(conn, chain, contract, address, up_to_block):
        if e.action == "freeze":
            last = e
        elif e.action == "unfreeze":
            last = None
    return FreezeFound(last, cov.to_block)


def destructions(conn, chain: str, contract: str, address: str, up_to_block: int | None) -> list[IndexedEvent]:
    return [e for e in events_for(conn, chain, contract, address, up_to_block) if e.action == "destroy"]
