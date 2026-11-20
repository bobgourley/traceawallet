"""Stablecoin blacklist events, read in bulk for the global index (taw/blacklist.py).

  chain  token  contract                                     events (address field)
  eth    USDT   0xdac17f958d2ee523a2206206994597c13d831ec7   AddedBlackList(_user), RemovedBlackList(_user),
                                                             DestroyedBlackFunds(_blackListedUser, _balance):
                                                             nothing indexed, all in the data
  eth    USDC   0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48   Blacklisted(_account), UnBlacklisted(_account):
                                                             address indexed (topic 1)
  tron   USDT   TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t           the same three events as Ethereum USDT
  tron   USDC   TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8           the same two events as Ethereum USDC

USDC has no event for burning a frozen balance. Arbitrum and OP Mainnet are not
in the index: their freeze events index the address, so freeze.check_evm reads
one address's events directly.

Ethereum: Blockscout eth_getLogs per event topic over a block range. A reply of
LOG_CAP logs or more may have been cut short, so the range is halved and each
half asked again. Because Blockscout's log search has returned partial results
before, scripts/backfill_blacklist.py compares the backfill with Etherscan's
getLogs over the same range, log by log (etherscan_logs below).

Tron: TronGrid /v1/contracts/{contract}/events, one event name at a time, oldest
first, inside a time window, following its fingerprint. A window that would
need more than TRON_MAX_PAGES pages is halved, so no single walk goes deep.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable

from taw.adapters.chains.tron_address import to_base58

LOG_CAP = 1000                 # Blockscout and Etherscan return at most 1,000 logs a request
TRON_MAX_PAGES = 25            # 5,000 events per window before it is split
TRON_PAGE = 200


@dataclass(frozen=True)
class Event:
    name: str
    action: str                # freeze | unfreeze | destroy
    topic: str                 # keccak of the signature (EVM)


USDT_EVENTS = (
    Event("AddedBlackList", "freeze", "0x42e160154868087d6bfdc0ca23d96a1c1cfa32f1b72ba9ba27b69b98a0d819dc"),
    Event("RemovedBlackList", "unfreeze", "0xd7e9ec6e6ecd65492dce6bf513cd6867560d49544421d0783ddf06e76c24470c"),
    Event("DestroyedBlackFunds", "destroy", "0x61e6e66b0d6339b2980aecc6ccc0039736791f0ccde9ed512e789a7fbdd698c6"),
)
USDC_EVENTS = (
    Event("Blacklisted", "freeze", "0xffa4e6181777692565cf28528fc88fd1516ea86b56da075235fa575af6a4b855"),
    Event("UnBlacklisted", "unfreeze", "0x117e3210bb9aa7d9baff172026820255c6f6c30ba8999d1c2fd88e2848137c4e"),
)


@dataclass(frozen=True)
class IndexedContract:
    chain: str
    token: str
    contract: str
    source: str                # label source slug: the contract itself
    events: tuple[Event, ...]
    start_block: int           # deployment block (EVM); Tron starts from start_time
    start_time: dt.datetime | None = None


CONTRACTS: tuple[IndexedContract, ...] = (
    IndexedContract("eth", "USDT", "0xdac17f958d2ee523a2206206994597c13d831ec7", "usdt-contract", USDT_EVENTS, 4634748),
    IndexedContract("eth", "USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "usdc-contract", USDC_EVENTS, 6082465),
    IndexedContract("tron", "USDT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "usdt-contract", USDT_EVENTS, 0,
                    dt.datetime(2019, 4, 1, tzinfo=dt.timezone.utc)),
    IndexedContract("tron", "USDC", "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8", "usdc-contract", USDC_EVENTS, 0,
                    dt.datetime(2021, 6, 1, tzinfo=dt.timezone.utc)),
)


def indexed(chain: str, contract: str) -> IndexedContract | None:
    for c in CONTRACTS:
        if c.chain == chain and c.contract == contract:
            return c
    return None


def for_chain(chain: str) -> list[IndexedContract]:
    return [c for c in CONTRACTS if c.chain == chain]


@dataclass(frozen=True)
class BlacklistEvent:
    chain: str
    token: str
    contract: str
    action: str
    event_name: str
    address: str
    amount_raw: int | None
    block_number: int
    block_time: dt.datetime | None
    tx_hash: str
    log_index: int
    raw_sha256: str

    @property
    def key(self) -> tuple[str, int]:
        return self.tx_hash, self.log_index


@dataclass
class ScanResult:
    events: list[BlacklistEvent]
    calls: int
    responses: list[str]       # sha256 of every response read


# -- Ethereum ----------------------------------------------------------------------------------

def _int(v) -> int:
    return int(v, 16) if isinstance(v, str) and v.startswith("0x") else int(v)


def _word_address(word: str) -> str:
    return "0x" + word[-40:].lower()


def evm_event(c: IndexedContract, ev: Event, log: dict, sha: str) -> BlacklistEvent:
    data = (log.get("data") or "0x")[2:]
    topics = [t for t in (log.get("topics") or []) if t]
    if len(topics) > 1:
        address = _word_address(topics[1])                       # USDC: indexed
        rest = data
    else:
        address = _word_address(data[:64])                        # Ethereum USDT: in the data
        rest = data[64:]
    amount = int(rest[:64], 16) if ev.action == "destroy" else None
    stamp = log.get("blockTimestamp") or log.get("timeStamp")
    when = dt.datetime.fromtimestamp(_int(stamp), dt.timezone.utc) if stamp not in (None, "", "0x") else None
    return BlacklistEvent(c.chain, c.token, c.contract, ev.action, ev.name, address, amount,
                          _int(log["blockNumber"]), when, log["transactionHash"].lower(), _int(log["logIndex"]), sha)


GetLogs = Callable[[str, str, int, int], tuple[list[dict], str]]


def evm_scan(get_logs: GetLogs, c: IndexedContract, lo: int, hi: int) -> ScanResult:
    """Every event of `c` in blocks lo..hi. get_logs(contract, topic, lo, hi) -> (logs, sha)."""
    out = ScanResult([], 0, [])

    def walk(ev: Event, a: int, b: int) -> None:
        logs, sha = get_logs(c.contract, ev.topic, a, b)
        out.calls += 1
        out.responses.append(sha)
        if len(logs) >= LOG_CAP:
            if a == b:
                raise RuntimeError(f"{c.token} {ev.name}: {len(logs)} logs in block {a}; cannot split further")
            mid = (a + b) // 2
            walk(ev, a, mid)
            walk(ev, mid + 1, b)
            return
        for log in logs:
            if (log.get("address") or c.contract).lower() != c.contract or log.get("removed"):
                continue
            out.events.append(evm_event(c, ev, log, sha))

    for ev in c.events:
        walk(ev, lo, hi)
    seen, unique = set(), []
    for e in sorted(out.events, key=lambda e: (e.block_number, e.log_index)):
        if e.key not in seen:
            seen.add(e.key)
            unique.append(e)
    out.events = unique
    return out


def blockscout_logs(adapter) -> GetLogs:
    """get_logs through a BlockscoutAdapter's JSON-RPC (metered by its budget)."""
    def get(contract: str, topic: str, lo: int, hi: int):
        r = adapter._rpc("eth_getLogs", [{"address": contract, "fromBlock": hex(lo), "toBlock": hex(hi),
                                          "topics": [topic]}])
        return list(r.data.get("result") or []), r.sha
    return get


def etherscan_logs(adapter) -> GetLogs:
    """get_logs through an EtherscanAdapter's module=logs (for the comparison)."""
    def get(contract: str, topic: str, lo: int, hi: int):
        r = adapter._request({"module": "logs", "action": "getLogs", "address": contract, "topic0": topic,
                              "fromBlock": lo, "toBlock": hi, "page": 1, "offset": LOG_CAP})
        rows = r.data.get("result")
        return (rows if isinstance(rows, list) else []), r.sha
    return get


# -- Tron --------------------------------------------------------------------------------------

def _ms(t: dt.datetime) -> int:
    return int(t.timestamp() * 1000)


def tron_event(c: IndexedContract, ev: Event, item: dict, sha: str) -> BlacklistEvent:
    result = item.get("result") or {}
    types = item.get("result_type") or {}
    addr_field = next(k for k, v in types.items() if v == "address")
    amount = None
    if ev.action == "destroy":
        amount_field = next(k for k, v in types.items() if v.startswith("uint"))
        amount = int(result[amount_field])
    return BlacklistEvent(c.chain, c.token, c.contract, ev.action, ev.name, to_base58(result[addr_field]), amount,
                          int(item["block_number"]),
                          dt.datetime.fromtimestamp(int(item["block_timestamp"]) / 1000, dt.timezone.utc),
                          item["transaction_id"].lower(), int(item["event_index"]), sha)


def tron_scan(adapter, c: IndexedContract, start: dt.datetime, end: dt.datetime) -> ScanResult:
    """Every event of `c` with start <= block time <= end, through a TronGridAdapter."""
    out = ScanResult([], 0, [])
    path = f"/v1/contracts/{c.contract}/events"

    def walk(ev: Event, a: dt.datetime, b: dt.datetime) -> None:
        query = {"event_name": ev.name, "only_confirmed": "true", "order_by": "block_timestamp,asc",
                 "limit": TRON_PAGE, "min_block_timestamp": _ms(a), "max_block_timestamp": _ms(b)}
        found: list[BlacklistEvent] = []
        pages = adapter._pages(path, query, TRON_MAX_PAGES)
        more = True
        try:
            while True:
                items, sha = next(pages)
                out.calls += 1
                out.responses.append(sha)
                for item in items:
                    if item.get("event_name") == ev.name and item.get("contract_address") == c.contract:
                        found.append(tron_event(c, ev, item, sha))
        except StopIteration as stop:
            more = bool(stop.value)
        if more:
            if b - a < dt.timedelta(minutes=1):
                raise RuntimeError(f"{c.token} {ev.name}: more than {TRON_MAX_PAGES * TRON_PAGE} events "
                                   f"between {a} and {b}")
            mid = a + (b - a) / 2
            mid = mid.replace(microsecond=0)
            walk(ev, a, mid)
            walk(ev, mid + dt.timedelta(milliseconds=1), b)
            return
        out.events.extend(found)

    for ev in c.events:
        walk(ev, start, end)
    seen, unique = set(), []
    for e in sorted(out.events, key=lambda e: (e.block_number, e.log_index)):
        if e.key not in seen:
            seen.add(e.key)
            unique.append(e)
    out.events = unique
    return out
