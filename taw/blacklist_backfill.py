"""Backfill of the Ethereum blacklist index, resumable across days (migration 0019).

Blockscout cannot search the USDT or USDC contract's events in wide block ranges:
it answered windows of about 4,000 to 15,000 blocks and timed out on wider ones
(2026-11-21). A plain scan of USDT's 19.5 million blocks would take about 15,000
requests. So:

USDT, through its owner. Every AddedBlackList, RemovedBlackList and
DestroyedBlackFunds on Ethereum USDT came from Tether's owner contract
(0xc6cd...a828, a 2017 Gnosis MultiSigWallet). Each proposal is a submitTransaction
call whose input holds the exact call to USDT; proposals are numbered in the order
they succeed; each one carried out emits Execution(id) on the multisig. Reading the
multisig's transactions (pages of 1,000) and its Execution events gives every
freeze, release and destruction with its address and transaction. The USDT event
is the log just before Execution in that transaction. Checked 2026-11-21 against
Etherscan's USDT events: 3,243 / 347 / 1,309, every one matched by transaction,
log position and address. Destruction amounts are only in USDT's own event, so
USDT's DestroyedBlackFunds logs are read in windows around the destructions (218
windows of at most 4,000 blocks), and must be exactly the destructions the owner
route found.

USDC, by windows. Each event's logs, window by window (walk_windows).

Every answered reply is saved as a part (blacklist_backfill_part) before the next
request, so a run stopped by the allowance, a timeout or a restart starts again
where it stopped. Nothing is written to blacklist_event here: the script compares
the result with Etherscan's and records the scan only when they agree.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Callable

from taw.adapters.labels import blacklist as bl

OWNER_MULTISIG = "0xc6cde7c39eb2f0f0095f41570af89efc2c1ea828"     # USDT owner() on Ethereum
EXECUTION_TOPIC = "0x33e13ecb54c3076d8e8bb8c2881800a4d972b792045ffae98fdf46df365fed75"   # Execution(uint256)
SUBMIT_SELECTOR = "0xc6427474"            # submitTransaction(address,uint256,bytes)
TRANSACTION_COUNT = "0xb77bf600"          # transactionCount()
USDT_CALLS = {"0ecb93c0": bl.USDT_EVENTS[0],   # addBlackList(address)
              "e4997dc5": bl.USDT_EVENTS[1],   # removeBlackList(address)
              "f3bdc228": bl.USDT_EVENTS[2]}   # destroyBlackFunds(address)
TX_PAGE = 1000
DESTROY_WINDOW = 4_000
LOG_FIELDS = ("address", "topics", "data", "blockNumber", "logIndex", "transactionHash", "timeStamp",
              "blockTimestamp", "removed")
TX_FIELDS = ("hash", "blockNumber", "transactionIndex", "to", "input", "isError")


class OwnerRouteMismatch(RuntimeError):
    """The owner route does not account for everything: do not use it."""


# -- progress ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Part:
    from_block: int
    to_block: int
    rows: list
    sha256: str


class Parts:
    """Saved replies, in Postgres (blacklist_backfill_part)."""

    def __init__(self, conn, chain: str = "eth"):
        self.conn, self.chain = conn, chain

    def load(self, contract: str, part: str) -> list[Part]:
        rows = self.conn.execute(
            "SELECT from_block, to_block, rows, sha256 FROM blacklist_backfill_part WHERE chain = %s AND contract = %s"
            " AND part = %s ORDER BY from_block", (self.chain, contract, part)).fetchall()
        return [Part(int(a), int(b), r, s.strip()) for a, b, r, s in rows]

    def save(self, contract: str, part: str, a: int, b: int, rows: list, sha: str) -> None:
        self.conn.execute(
            "INSERT INTO blacklist_backfill_part (chain, contract, part, from_block, to_block, rows, sha256)"
            " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s) ON CONFLICT DO NOTHING",
            (self.chain, contract, part, a, b, json.dumps(rows), sha))


class MemoryParts:
    """The same, in memory (tests and dry runs)."""

    def __init__(self):
        self.saved: dict[tuple[str, str], dict[int, Part]] = {}

    def load(self, contract: str, part: str) -> list[Part]:
        return [p for _, p in sorted(self.saved.get((contract, part), {}).items())]

    def save(self, contract: str, part: str, a: int, b: int, rows: list, sha: str) -> None:
        self.saved.setdefault((contract, part), {}).setdefault(a, Part(a, b, rows, sha))


def _cut(rows: list, fields) -> list:
    return [{k: r[k] for k in fields if k in r} for r in rows]


def _hexint(v) -> int:
    return 0 if v in (None, "") else bl._int(v)


def _when(stamp) -> dt.datetime | None:
    return None if stamp in (None, "", "0x") else dt.datetime.fromtimestamp(_hexint(stamp), dt.timezone.utc)


def _next_block(parts: list[Part], start: int) -> int:
    return max([start] + [p.to_block + 1 for p in parts])


def read_logs(store, get_logs, contract: str, part: str, topic: str, lo: int, hi: int, **kw) -> tuple[list, int]:
    """Every log of `topic` from `contract` in lo..hi, resuming after the saved parts.
    (logs with their reply's sha256, requests made this run)."""
    def save(a, b, logs, sha):
        store.save(contract, part, a, b, _cut(logs, LOG_FIELDS), sha)
    start = _next_block([p for p in store.load(contract, part) if lo <= p.from_block <= hi], lo)
    calls = bl.walk_windows(get_logs, contract, topic, start, hi, save, **kw) if start <= hi else 0
    out = [(log, p.sha256) for p in store.load(contract, part) if lo <= p.from_block and p.to_block <= hi
           for log in p.rows]
    return out, calls


def read_owner_transactions(store, txlist, hi: int) -> tuple[list, int]:
    """The multisig's transactions up to block hi, a page at a time. txlist(startblock,
    endblock) -> (rows oldest first, at most TX_PAGE, sha). A page that ends inside a
    block is followed by one starting at that block; rows are kept once by hash."""
    parts, calls = store.load(OWNER_MULTISIG, "owner_transactions"), 0
    start = max([0] + [p.to_block for p in parts])
    done = any(p.to_block >= hi and len(p.rows) < TX_PAGE for p in parts)
    while not done:
        rows, sha = txlist(start, hi)
        calls += 1
        last = int(rows[-1]["blockNumber"]) if rows else hi
        full = len(rows) >= TX_PAGE
        if full and last == start:
            raise RuntimeError(f"more than {TX_PAGE} transactions in block {start}")
        store.save(OWNER_MULTISIG, "owner_transactions", start, last if full else hi, _cut(rows, TX_FIELDS), sha)
        parts = store.load(OWNER_MULTISIG, "owner_transactions")
        start, done = (last, False) if full else (hi, True)
    seen, out = set(), []
    for p in store.load(OWNER_MULTISIG, "owner_transactions"):
        for r in p.rows:
            if r["hash"] not in seen and int(r["blockNumber"]) <= hi:
                seen.add(r["hash"])
                out.append((r, p.sha256))
    return out, calls


# -- the owner route --------------------------------------------------------------------------

def submitted_call(tx_input: str) -> tuple[str, str]:
    """(destination, call data) from a submitTransaction input."""
    body = tx_input[10:]
    destination = "0x" + body[24:64].lower()
    offset = int(body[128:192], 16) * 2
    length = int(body[offset:offset + 64], 16) * 2
    return destination, body[offset + 64:offset + 64 + length].lower()


def first_address(call_data: str) -> str:
    """The first argument as an address. Short call data reads as zeros on the right,
    as the EVM reads it (one 2017 destroyBlackFunds call was sent short)."""
    return bl._word_address(call_data[8:72].ljust(64, "0"))


def owner_events(transactions: list, executions: list, count: int, usdt: bl.IndexedContract) -> list[bl.BlacklistEvent]:
    """USDT blacklist events from the multisig's proposals and executions.
    transactions: (tx row, sha); executions: (Execution log, sha); count: the
    multisig's transactionCount() at the last block read."""
    proposals = sorted((r for r, _ in transactions
                        if (r.get("input") or "").lower().startswith(SUBMIT_SELECTOR)
                        and (r.get("to") or "").lower() == OWNER_MULTISIG and str(r.get("isError")) == "0"),
                       key=lambda r: (int(r["blockNumber"]), int(r["transactionIndex"])))
    if len(proposals) != count:
        raise OwnerRouteMismatch(f"{len(proposals)} successful proposals read, the multisig counts {count}")
    executed = {_hexint(log["topics"][1]): (log, sha) for log, sha in executions}
    out = []
    for number, proposal in enumerate(proposals):
        if number not in executed:
            continue
        destination, data = submitted_call(proposal["input"])
        event = USDT_CALLS.get(data[:8])
        if destination != usdt.contract or event is None:
            continue
        log, sha = executed[number]
        position = _hexint(log.get("logIndex")) - 1
        when = _when(log.get("blockTimestamp") or log.get("timeStamp"))
        out.append(bl.BlacklistEvent(usdt.chain, usdt.token, usdt.contract, event.action, event.name,
                                     first_address(data), None, _hexint(log["blockNumber"]), when,
                                     log["transactionHash"].lower(), position, sha))
    return out


def windows_around(blocks: list[int], width: int = DESTROY_WINDOW) -> list[tuple[int, int]]:
    """Fewest windows of at most `width` blocks, oldest first, covering every block."""
    out: list[tuple[int, int]] = []
    for b in sorted(set(blocks)):
        if not out or b > out[-1][1]:
            out.append((b, b + width - 1))
    return out


@dataclass
class Result:
    events: list[bl.BlacklistEvent]
    calls: int
    responses: list[str]


def usdt_through_owner(store, scout, usdt: bl.IndexedContract, hi: int,
                       txlist: Callable | None = None, get_logs: Callable | None = None,
                       count: Callable | None = None) -> Result:
    """The owner route for Ethereum USDT up to block hi. scout: a BlockscoutAdapter
    (metered); txlist, get_logs and count can be given instead (tests)."""
    get_logs = get_logs or bl.blockscout_logs(scout)

    def _txlist(lo, top):
        r = scout._request({"module": "account", "action": "txlist", "address": OWNER_MULTISIG, "startblock": lo,
                            "endblock": top, "page": 1, "offset": TX_PAGE, "sort": "asc"})
        rows = r.data.get("result")
        return (rows if isinstance(rows, list) else []), r.sha

    def _count(top):
        out, _ = scout._eth_call(OWNER_MULTISIG, TRANSACTION_COUNT, hex(top))
        return int(out or "0x0", 16)

    transactions, calls = read_owner_transactions(store, txlist or _txlist, hi)
    executions, n = read_logs(store, get_logs, OWNER_MULTISIG, "owner_executions", EXECUTION_TOPIC,
                              usdt.start_block, hi, span=200_000)
    calls += n
    proposals = (count or _count)(hi)
    calls += 1
    events = owner_events(transactions, executions, proposals, usdt)
    destroy = bl.USDT_EVENTS[2]
    owned = {(e.tx_hash, e.log_index, e.address) for e in events if e.action == "destroy"}
    logs = []
    for lo, top in windows_around([e.block_number for e in events if e.action == "destroy"]):
        got, n = read_logs(store, get_logs, usdt.contract, destroy.name, destroy.topic, lo, top, span=top - lo + 1)
        logs += got
        calls += n
    burned = {}
    for log, sha in logs:
        if (log.get("address") or usdt.contract).lower() != usdt.contract or log.get("removed"):
            continue
        e = bl.evm_event(usdt, destroy, log, sha)
        burned[e.key] = e
    if {(e.tx_hash, e.log_index, e.address) for e in burned.values()} != owned:
        raise OwnerRouteMismatch(f"USDT's own destruction events ({len(burned)}) are not the {len(owned)} the owner "
                                 "route found")
    by_key = {e.key: e for e in events if e.action != "destroy"}
    by_key.update(burned)
    ordered = sorted(by_key.values(), key=lambda e: (e.block_number, e.log_index))
    shas = sorted({sha for _, sha in transactions} | {sha for _, sha in executions} | {sha for _, sha in logs})
    return Result(ordered, calls, shas)


def by_windows(store, get_logs, c: bl.IndexedContract, hi: int) -> Result:
    """Every event of c up to block hi, window by window (USDC)."""
    events, calls, shas = {}, 0, set()
    for ev in c.events:
        logs, n = read_logs(store, get_logs, c.contract, ev.name, ev.topic, c.start_block, hi)
        calls += n
        for log, sha in logs:
            shas.add(sha)
            if (log.get("address") or c.contract).lower() != c.contract or log.get("removed"):
                continue
            e = bl.evm_event(c, ev, log, sha)
            events.setdefault(e.key, e)
    return Result(sorted(events.values(), key=lambda e: (e.block_number, e.log_index)), calls, sorted(shas))
