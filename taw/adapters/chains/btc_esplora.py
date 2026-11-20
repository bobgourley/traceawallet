"""Bitcoin adapter on the Esplora REST API: mempool.space (primary) and
Blockstream (cross-check), one client for both.

Endpoints (github.com/Blockstream/esplora/blob/master/API.md, read 2026-11-20):

  GET /blocks/tip/height                    the tip, read once at the start
  GET /block-height/:h, /block/:hash        the snapshot block's time
  GET /address/:a                           chain_stats and mempool_stats (totals as of now)
  GET /address/:a/txs/chain[/:last_txid]    confirmed transactions, newest first, 25 a page
  GET /address/:a/utxo                      unspent outputs as of now

Probed 2026-11-20: both answered the tip height with HTTP 200 and no rate-limit
headers (mempool.space caches 1 s, Blockstream 10 s). Neither publishes a limit;
mempool.space's docs warn of HTTP 429 and bans. Requests are paced at 2 a second
and 429/5xx answers are retried with growing waits, honouring Retry-After.

Height. Everything is as of the tip height H read at the start, never later.
Unconfirmed transactions are ignored (counted from mempool_stats). Confirmed
transactions above H, which arrive if a block is mined during the lookup, are
read, set aside and counted; the provider's "as of now" totals and unspent
outputs are corrected by exactly those transactions, so every comparison is at H.

Rows (migration 0003). One transfer row is one side of one transaction:
  utxo_in   an input: from = the address that spent it, to = NULL, event_index = input position
  utxo_out  an output: to = the address paid, from = NULL, event_index = output position
For each transaction touching the address, every input spent from it and every
output paying it is stored, plus the other inputs and outputs of that
transaction, so funders and first-hop outputs are known. Other-party rows are
capped at 200 per transaction (inputs first, then outputs, in position order);
capped transactions are recorded in the check other_party_rows_capped. Coinbase
inputs and outputs with no address (OP_RETURN) are skipped. Amounts in satoshis,
decimals 8.

Report wording (0003): "outputs of transactions that spent from this address",
never "sent to": one output is usually change back to the spender.

Truncation. Up to 1,000 transactions at or below H, newest first, truncated=True
past that. The earliest transaction is always fetched, so first activity and the
first funder are right: paging continues to the end of the history, up to
earliest_pages pages (25 each); past that the check earliest_transaction fails
and first activity is unknown. A truncated history is never reconciled.

Checks (taw/reconcile.py runs them with the rest):
  native_reconciliation  received minus spent from our rows = the provider's
                         address totals at H = the sum of its unspent outputs at H
  tx_count               transactions we stored = the provider's count at H
  other_party_rows_capped, earliest_transaction
GoPlus is never called for Bitcoin (docs/DECISIONS.md).
"""
from __future__ import annotations

import datetime as dt
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from taw.adapters.base import (FAILED, NOT_RUN, PASSED, AddressSnapshot, CheckResult, RawStore, TransferBatch,
                               TransferRecord)
from taw.adapters.chains.btc_address import normalize
from taw.adapters.chains.common import Pacer

CHAIN = "btc"
MEMPOOL = ("https://mempool.space/api", "mempool-space", "mempool.space")
BLOCKSTREAM = ("https://blockstream.info/api", "blockstream", "Blockstream")
DECIMALS = 8
PAGE = 25                       # Esplora's page size for /txs/chain
MAX_TXS = 1000
OTHER_CAP = 200
EARLIEST_PAGES = 400            # 10,000 transactions; beyond that first activity is not reached
_KIND_ORDER = {"utxo_in": 0, "utxo_out": 1}


class EsploraError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status
        self.provider = "Esplora"


class EsploraRateLimited(EsploraError):
    """Still told to slow down after every retry."""


@dataclass(frozen=True)
class _Response:
    data: Any
    sha: str


@dataclass(frozen=True)
class _Head:
    height: int
    time: dt.datetime
    shas: tuple[str, ...]


class EsploraClient:
    """GET requests with pacing, retries and raw storage. No key: both APIs are public."""

    def __init__(self, raw_store: RawStore, base_url: str, source: str, provider_name: str, *,
                 http: httpx.Client | None = None, per_second: float = 2.0, max_attempts: int = 6,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.raw_store, self.base_url, self.source, self.provider_name = raw_store, base_url.rstrip("/"), source, provider_name
        self.http = http or httpx.Client(timeout=60.0)
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._pacer = Pacer(per_second, clock, sleep)
        self._memo: dict[str, _Response] = {}
        self.request_count = 0

    def get(self, path: str, *, as_json: bool = True, memo: bool = True) -> _Response:
        url = self.base_url + path
        if memo and url in self._memo:
            return self._memo[url]
        for attempt in range(1, self.max_attempts + 1):
            self._pacer.wait()
            self.request_count += 1
            resp = self.http.get(url)
            sha = self.raw_store.save(self.source, url, None, resp.status_code, resp.content)
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt == self.max_attempts:
                    exc = (EsploraRateLimited if resp.status_code == 429 else EsploraError)(
                        f"{path}: still HTTP {resp.status_code} after {attempt} tries", resp.status_code)
                    exc.provider = self.provider_name
                    raise exc
                retry_after = resp.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else min(60.0, 2 ** attempt)
                self._sleep(wait + random.uniform(0, 0.5))
                continue
            if resp.status_code >= 400:
                exc = EsploraError(f"{path}: HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)
                exc.provider = self.provider_name
                raise exc
            text = resp.content.decode("utf-8", errors="replace")
            try:
                data = json.loads(text) if as_json else text.strip()
            except ValueError as err:
                exc = EsploraError(f"{path}: response is not JSON")
                exc.provider = self.provider_name
                raise exc from err
            result = _Response(data, sha)
            if memo:
                self._memo[url] = result
            return result
        raise AssertionError("unreachable")


def _block_time(tx: dict) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(tx["status"]["block_time"]), tz=dt.timezone.utc)


class BtcEsploraAdapter:
    chain = CHAIN

    def __init__(self, raw_store: RawStore, *, provider: tuple[str, str, str] = MEMPOOL, at_height: int | None = None,
                 max_txs: int = MAX_TXS, other_cap: int = OTHER_CAP, earliest_pages: int = EARLIEST_PAGES, **client_kw):
        base_url, source, name = provider
        self.client = EsploraClient(raw_store, base_url, source, name, **client_kw)
        self.source, self.provider_name = source, name
        self._at_height = at_height
        self.max_txs, self.other_cap, self.earliest_pages = max_txs, other_cap, earliest_pages
        self._head: _Head | None = None
        self._history: dict[str, dict] = {}

    @property
    def request_count(self) -> int:
        return self.client.request_count

    # -- the snapshot height ------------------------------------------------------

    def head(self) -> _Head:
        if self._head is None:
            shas = []
            if self._at_height is None:
                r = self.client.get("/blocks/tip/height", as_json=False, memo=False)
                height, shas = int(r.data), [r.sha]
            else:
                height = self._at_height
            h = self.client.get(f"/block-height/{height}", as_json=False)
            block = self.client.get(f"/block/{h.data}")
            if int(block.data["height"]) != height:
                raise EsploraError(f"block {height} was not returned")
            self._head = _Head(height, dt.datetime.fromtimestamp(int(block.data["timestamp"]), tz=dt.timezone.utc),
                               tuple(shas + [h.sha, block.sha]))
        return self._head

    # -- history, read once per address --------------------------------------------------

    def _read_history(self, addr: str) -> dict:
        """Confirmed transactions newest first: those at or below H (up to max_txs + 1),
        those above H (set aside), and whether more history exists."""
        if addr in self._history:
            return self._history[addr]
        H = self.head().height
        kept: list[tuple[dict, str]] = []
        above: list[dict] = []
        last, more = None, False
        while True:
            r = self.client.get(f"/address/{addr}/txs/chain" + (f"/{last}" if last else ""))
            page = r.data or []
            for tx in page:
                if not (tx.get("status") or {}).get("confirmed"):
                    continue
                if int(tx["status"]["block_height"]) > H:
                    above.append(tx)
                else:
                    kept.append((tx, r.sha))
            if len(page) < PAGE:
                break
            last = page[-1]["txid"]
            if len(kept) > self.max_txs:
                more = True
                break
        hist = {"kept": kept, "above": above, "more": more, "last": last}
        self._history[addr] = hist
        return hist

    def _earliest(self, addr: str, hist: dict) -> tuple[tuple[dict, str] | None, int]:
        """Page on from where the history stopped to the oldest transaction."""
        last, pages, final = hist["last"], 0, None
        while pages < self.earliest_pages:
            r = self.client.get(f"/address/{addr}/txs/chain/{last}")
            page = [tx for tx in (r.data or []) if (tx.get("status") or {}).get("confirmed")]
            pages += 1
            if page:
                final = (page[-1], r.sha)
            if len(r.data or []) < PAGE:
                return final, pages
            last = r.data[-1]["txid"]
        return None, pages

    # -- ChainAdapter ------------------------------------------------------------------------

    def fetch_snapshot(self, address: str) -> AddressSnapshot:
        addr = normalize(address)
        head = self.head()
        info = self.client.get(f"/address/{addr}", memo=False)
        self._info = info.data
        hist = self._read_history(addr)
        stats = info.data.get("chain_stats") or {}
        funded, spent = int(stats.get("funded_txo_sum", 0)), int(stats.get("spent_txo_sum", 0))
        # undo what the transactions above H added or spent
        for tx in hist["above"]:
            funded -= sum(int(o["value"]) for o in tx.get("vout") or [] if o.get("scriptpubkey_address") == addr)
            spent -= sum(int(i["prevout"]["value"]) for i in tx.get("vin") or []
                         if i.get("prevout") and i["prevout"].get("scriptpubkey_address") == addr)
        self._provider_balance = funded - spent
        self._provider_tx_count = int(stats.get("tx_count", 0)) - len(hist["above"])
        self._unconfirmed = int((info.data.get("mempool_stats") or {}).get("tx_count", 0))
        return AddressSnapshot(
            chain=CHAIN, address=addr, as_of_block=head.height, as_of_time=head.time,
            native_symbol="BTC", native_decimals=DECIMALS, native_balance_raw=self._provider_balance,
            token_balances=(), is_contract=False, truncated=False, source_slug=self.source,
            raw_sha256=(info.sha, *head.shas), balances_read=None,
        )

    def fetch_transfers(self, address: str, limit: int = MAX_TXS) -> TransferBatch:
        addr = normalize(address)
        if self.max_txs != limit:
            self.max_txs = limit
            self._history.pop(addr, None)
        if not hasattr(self, "_provider_balance"):
            self.fetch_snapshot(addr)
        hist = self._read_history(addr)
        kept = hist["kept"][:limit]
        truncated = len(hist["kept"]) > limit or hist["more"]

        checks: list[CheckResult] = []
        earliest_check = None
        if truncated:
            earliest, pages = self._earliest(addr, hist)
            if earliest is not None:
                if earliest[0]["txid"] not in {tx["txid"] for tx, _ in kept}:
                    kept = kept + [earliest]
                earliest_check = CheckResult("earliest_transaction", PASSED, None,
                                             {"txid": earliest[0]["txid"], "pages_read": pages})
            else:
                earliest_check = CheckResult("earliest_transaction", FAILED,
                                             f"the history is longer than {self.earliest_pages * PAGE:,} transactions; "
                                             "the earliest was not reached, so first activity is unknown",
                                             {"pages_read": pages})

        records, capped = [], []
        for tx, sha in kept:
            rows, was_capped = self._rows(tx, sha, addr)
            records += rows
            if was_capped:
                capped.append(tx["txid"])
        records.sort(key=lambda r: (r.block_number, r.tx_hash, _KIND_ORDER[r.kind], r.event_index), reverse=True)

        checks.append(self._balance_check(addr, records, truncated))
        checks.append(self._count_check(kept, truncated, hist))
        checks.append(CheckResult(
            "other_party_rows_capped", FAILED if capped else PASSED,
            (f"{len(capped)} transaction(s) had more than {self.other_cap} inputs and outputs of other addresses; "
             f"only the first {self.other_cap} of those are stored") if capped else None,
            {"capped_transactions": capped, "cap_per_transaction": self.other_cap}, marks_incomplete=False))
        if earliest_check:
            checks.append(earliest_check)
        return TransferBatch(CHAIN, addr, tuple(records), truncated, limit, checks=tuple(checks))

    # -- rows ------------------------------------------------------------------------------

    def _rows(self, tx: dict, sha: str, addr: str) -> tuple[list[TransferRecord], bool]:
        txid = tx["txid"].lower()
        height, when = int(tx["status"]["block_height"]), _block_time(tx)
        own, others = [], []
        for i, vin in enumerate(tx.get("vin") or []):
            prev = vin.get("prevout")
            if vin.get("is_coinbase") or not prev or not prev.get("scriptpubkey_address"):
                continue                               # coinbase or an input with no address
            rec = TransferRecord(CHAIN, txid, "utxo_in", i, prev["scriptpubkey_address"], None, None, "BTC",
                                 DECIMALS, int(prev["value"]), height, when, self.source, sha)
            (own if prev["scriptpubkey_address"] == addr else others).append(rec)
        for j, out in enumerate(tx.get("vout") or []):
            to = out.get("scriptpubkey_address")
            if not to:
                continue                               # OP_RETURN and other outputs with no address
            rec = TransferRecord(CHAIN, txid, "utxo_out", j, None, to, None, "BTC", DECIMALS, int(out["value"]),
                                 height, when, self.source, sha)
            (own if to == addr else others).append(rec)
        return own + others[:self.other_cap], len(others) > self.other_cap

    # -- checks ----------------------------------------------------------------------------

    def _balance_check(self, addr: str, records: list[TransferRecord], truncated: bool) -> CheckResult:
        name = "native_reconciliation"
        if truncated:
            return CheckResult(name, NOT_RUN, "The history is truncated, so received minus spent is not the "
                                              "whole history.")
        received = sum(r.amount_raw for r in records if r.kind == "utxo_out" and r.to_address == addr)
        spent = sum(r.amount_raw for r in records if r.kind == "utxo_in" and r.from_address == addr)
        ours = received - spent
        utxo_sum, utxo_note = self._utxo_sum_at_height(addr)
        details = {"received_sat": str(received), "spent_sat": str(spent), "ours_sat": str(ours),
                   "provider_totals_sat": str(self._provider_balance),
                   "unspent_outputs_sat": None if utxo_sum is None else str(utxo_sum)}
        if utxo_note:
            details["unspent_outputs_note"] = utxo_note
        agree = ours == self._provider_balance and (utxo_sum is None or ours == utxo_sum)
        if agree:
            return CheckResult(name, PASSED, utxo_note, details)
        return CheckResult(name, FAILED, f"received minus spent is {ours} sat but the provider's totals give "
                                         f"{self._provider_balance} and its unspent outputs {utxo_sum}", details)

    def _utxo_sum_at_height(self, addr: str) -> tuple[int | None, str | None]:
        """Sum of unspent outputs at H: confirmed at or below H, plus those spent above H."""
        H = self.head().height
        try:
            r = self.client.get(f"/address/{addr}/utxo")
        except EsploraError as exc:
            if exc.status == 400:                      # electrs refuses addresses with too many unspent outputs
                return None, f"the provider would not list the unspent outputs ({str(exc)[:120]})"
            raise
        total = sum(int(u["value"]) for u in r.data or []
                    if (u.get("status") or {}).get("confirmed") and int(u["status"]["block_height"]) <= H)
        hist = self._history.get(addr) or {"above": []}
        above_ids = {tx["txid"] for tx in hist["above"]}
        for tx in hist["above"]:                       # outputs spent after H were still unspent at H
            for vin in tx.get("vin") or []:
                prev = vin.get("prevout") or {}
                if prev.get("scriptpubkey_address") == addr and vin.get("txid") not in above_ids:
                    total += int(prev["value"])
        return total, None

    def _count_check(self, kept: list, truncated: bool, hist: dict) -> CheckResult:
        details = {"transactions_stored": len(kept), "provider_count": self._provider_tx_count,
                   "unconfirmed_ignored": self._unconfirmed,
                   "above_snapshot_height_ignored": len(hist["above"])}
        if truncated:
            return CheckResult("tx_count", NOT_RUN, "The history is truncated at "
                                                    f"{self.max_txs:,} transactions.", details)
        if len(kept) == self._provider_tx_count:
            return CheckResult("tx_count", PASSED, None, details)
        return CheckResult("tx_count", FAILED, f"{len(kept)} transactions were stored but the provider counts "
                                               f"{self._provider_tx_count}", details)
