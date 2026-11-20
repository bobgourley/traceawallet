"""Tron chain adapter backed by TronGrid (https://api.trongrid.io).

Endpoints, checked against developers.tron.network on 2026-11-17:

  GET  /v1/accounts/{addr}                       TRX balance (sun) and TRC-20 balances
  POST /wallet/getcontract                       {} when no contract is deployed at the address
  POST /wallet/triggerconstantcontract           decimals() and symbol() of a TRC-20 token
  POST /walletsolidity/getblock {"detail":false} latest confirmed block header
  GET  /v1/accounts/{addr}/transactions          TRX transfers (TransferContract entries)
  GET  /v1/accounts/{addr}/transactions/trc20    TRC-20 transfers, without block number or log index
  GET  /v1/transactions/{txid}/events            block number and log index for those transfers

Only confirmed data is requested (only_confirmed=true, walletsolidity), so the
numbers agree with a confirmed block height.

Every response, including 429s and errors, goes to the RawStore before it is
parsed. The API key travels only in the TRON-PRO-API-KEY header.

Not covered yet:
  - TRC-10 tokens (TransferAssetContract) and their balances.
  - TRX moved by smart contracts (internal transactions).
  - Failed transactions. A TRX transfer whose contractRet is not SUCCESS moved
    nothing and is skipped; TRC-20 transfers come from events, which exist only
    on success.

event_index: TRX transfers use 0 (a Tron transaction holds one contract call).
TRC-20 transfers use the log index from the events endpoint; identical transfers
in one transaction each claim their own event. If no event matches a listed
transfer, the record keeps the block number from the events response and gets
an index numbered after the transaction's highest real log index, so it can
never collide with a matched transfer under the table's unique key.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from taw.adapters.base import (AddressSnapshot, RawMissing, RawStore, ReusableRawStore,
                               TokenBalance, TransferBatch, TransferRecord)
from taw.adapters.chains.common import Pacer, decode_string, decode_uint
from taw.adapters.chains.tron_address import to_base58

SOURCE = "trongrid"
CHAIN = "tron"
TRX_DECIMALS = 6
PAGE_SIZE = 200                 # TronGrid maximum
MAX_TRX_PAGES = 60              # /transactions lists every kind of transaction; stop somewhere
MAX_TOKEN_LOOKUPS = 50          # decimals()/symbol() calls per snapshot (spam tokens are common)

# Freezable stablecoins on Tron. Each contract's ABI was read through TronGrid
# /wallet/getcontract on 2026-11-19: USDT (TetherToken) has addBlackList /
# isBlackListed, USDC (FiatTokenProxy) has blacklist / isBlacklisted.
STABLECOINS = (
    ("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "USDT (Tether)"),
    ("TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8", "USDC (Circle)"),
)


class TronGridError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TronGridBlocked(TronGridError):
    """403: TronGrid has temporarily blocked this key. Retrying makes it worse."""


@dataclass(frozen=True)
class _Response:
    data: Any
    sha: str


def _ms_to_dt(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)


class TronGridAdapter:
    chain = CHAIN

    def __init__(self, raw_store: RawStore, api_key: str | None, *,
                 http: httpx.Client | None = None,
                 base_url: str = "https://api.trongrid.io",
                 per_second: float = 9.0,
                 max_attempts: int = 6,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.raw_store = raw_store
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=30.0)
        self._headers = {"Accept": "application/json"}
        if api_key:
            self._headers["TRON-PRO-API-KEY"] = api_key
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._pacer = Pacer(per_second, clock, sleep)
        self._token_meta: dict[str, tuple[int | None, str | None]] = {}
        self.request_count = 0          # HTTP requests sent
        self.reused_count = 0           # saved responses used instead of a request

    # -- HTTP --------------------------------------------------------------

    def _reuse(self, url: str, stored: dict | None) -> _Response | None:
        """A saved copy of this exact request, when the store keeps them and the
        copy holds data. Only used for responses that cannot change."""
        if not isinstance(self.raw_store, ReusableRawStore):
            return None
        sha = self.raw_store.find(SOURCE, url, stored)
        if sha is None:
            return None
        try:
            body = self.raw_store.body(sha)
        except RawMissing:
            return None                     # fetch again; saving it restores the object
        data = json.loads(body)
        if not (isinstance(data, dict) and data.get("data")):
            return None                     # an empty answer may have been a lagging node; ask again
        self.reused_count += 1
        return _Response(data, sha)

    def _request(self, method: str, path: str, *, query: dict | None = None,
                 body: dict | None = None, reuse: bool = False) -> _Response:
        url = self.base_url + path
        stored = {k: v for k, v in (("query", query), ("body", body)) if v is not None} or None
        if reuse:
            saved = self._reuse(url, stored)
            if saved is not None:
                return saved
        for attempt in range(1, self.max_attempts + 1):
            self._pacer.wait()
            self.request_count += 1
            resp = self.http.request(method, url, params=query, json=body, headers=self._headers)
            sha = self.raw_store.save(SOURCE, url, stored, resp.status_code, resp.content)

            if resp.status_code in (429, 503):
                if attempt == self.max_attempts:
                    raise TronGridError(f"{path}: still {resp.status_code} after {attempt} tries",
                                        resp.status_code)
                retry_after = resp.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else min(30.0, 2 ** (attempt - 1))
                self._sleep(wait + random.uniform(0, 0.5))
                continue
            if resp.status_code == 403:
                raise TronGridBlocked(f"{path}: 403, TronGrid has blocked this key for now", 403)
            if resp.status_code >= 400:
                raise TronGridError(f"{path}: HTTP {resp.status_code}", resp.status_code)
            try:
                data = json.loads(resp.content or b"{}")
            except ValueError as exc:
                raise TronGridError(f"{path}: response is not JSON") from exc
            if isinstance(data, dict) and data.get("success") is False:
                raise TronGridError(f"{path}: {data.get('error') or 'success=false'}")
            return _Response(data, sha)
        raise AssertionError("unreachable")

    def _pages(self, path: str, query: dict, max_pages: int):
        """Yield (items, sha) per page; the final value says whether more pages remain."""
        q = dict(query)
        for _ in range(max_pages):
            r = self._request("GET", path, query=q)
            yield r.data.get("data") or [], r.sha
            fingerprint = (r.data.get("meta") or {}).get("fingerprint")
            if not fingerprint:
                return False
            q = {**query, "fingerprint": fingerprint}
        return True

    # -- small lookups -------------------------------------------------------

    def latest_block(self) -> tuple[int, dt.datetime, str]:
        r = self._request("POST", "/walletsolidity/getblock", body={"detail": False})
        raw = r.data["block_header"]["raw_data"]
        return int(raw["number"]), _ms_to_dt(int(raw["timestamp"])), r.sha

    def _constant_call(self, contract: str, selector: str, parameter: str = "") -> tuple[str | None, str]:
        """A read-only call at the current head. parameter: ABI-encoded arguments, hex without 0x."""
        r = self._request("POST", "/wallet/triggerconstantcontract", body={
            "owner_address": contract, "contract_address": contract,
            "function_selector": selector, "parameter": parameter, "visible": True,
        })
        ok = (r.data.get("result") or {}).get("result")
        results = r.data.get("constant_result") or []
        return (results[0] if ok and results and results[0] else None), r.sha

    def token_meta(self, contract: str) -> tuple[tuple[int | None, str | None], list[str]]:
        """(decimals, symbol) for a TRC-20 contract, and the responses used."""
        if contract in self._token_meta:
            return self._token_meta[contract], []
        dec_hex, sha1 = self._constant_call(contract, "decimals()")
        sym_hex, sha2 = self._constant_call(contract, "symbol()")
        decimals = decode_uint(dec_hex) if dec_hex else None
        if decimals is not None and not 0 <= decimals <= 77:
            decimals = None
        meta = (decimals, decode_string(sym_hex) if sym_hex else None)
        self._token_meta[contract] = meta
        return meta, [sha1, sha2]

    # -- ChainAdapter ----------------------------------------------------------

    def fetch_snapshot(self, address: str) -> AddressSnapshot:
        """Balances, contract status and the confirmed block height they were read at.

        The block height is read last, so balances reflect that block or an
        earlier one. truncated is True when more than MAX_TOKEN_LOOKUPS tokens
        are held: every balance is still listed, but the ones past the limit
        have no decimals or symbol.
        """
        addr = to_base58(address)
        shas: list[str] = []

        acct = self._request("GET", f"/v1/accounts/{addr}", query={"only_confirmed": "true"})
        shas.append(acct.sha)
        rows = acct.data.get("data") or []
        account = rows[0] if rows else {}
        trx_raw = int(account.get("balance") or 0)

        held: list[tuple[str, int]] = []
        for entry in account.get("trc20") or []:
            for contract, amount in entry.items():
                held.append((to_base58(contract), int(amount)))

        contract = self._request("POST", "/wallet/getcontract", body={"value": addr, "visible": True})
        shas.append(contract.sha)
        is_contract = bool(contract.data.get("contract_address") or contract.data.get("bytecode"))

        balances = []
        for i, (token, amount) in enumerate(held):
            decimals = symbol = None
            if i < MAX_TOKEN_LOOKUPS:
                (decimals, symbol), used = self.token_meta(token)
                shas.extend(used)
            balances.append(TokenBalance(token, amount, decimals, symbol))

        block, block_time, sha = self.latest_block()
        shas.append(sha)

        return AddressSnapshot(
            chain=CHAIN, address=addr, as_of_block=block, as_of_time=block_time,
            native_symbol="TRX", native_decimals=TRX_DECIMALS, native_balance_raw=trx_raw,
            token_balances=tuple(balances), is_contract=is_contract,
            truncated=len(held) > MAX_TOKEN_LOOKUPS, source_slug=SOURCE, raw_sha256=tuple(shas),
        )

    def fetch_transfers(self, address: str, limit: int = 2000) -> TransferBatch:
        """The newest `limit` TRX and TRC-20 transfers into or out of the address.

        Both lists are read newest first until each has more than `limit`
        transfers or runs out, so the merged newest `limit` is exact.
        """
        addr = to_base58(address)
        query = {"only_confirmed": "true", "limit": PAGE_SIZE, "order_by": "block_timestamp,desc"}

        # TRX: (timestamp_ms, tx_hash, record)
        native: list[tuple[int, str, TransferRecord]] = []
        pages = self._pages(f"/v1/accounts/{addr}/transactions", query, MAX_TRX_PAGES)
        trx_more = False
        while True:
            try:
                items, sha = next(pages)
            except StopIteration as stop:
                trx_more = bool(stop.value)
                break
            for tx in items:
                rec = self._native_record(tx, sha)
                if rec is not None:
                    native.append((int(tx["block_timestamp"]), rec.tx_hash, rec))
            if len(native) > limit:
                trx_more = True
                break

        # TRC-20: (timestamp_ms, tx_hash, item, page sha)
        listed: list[tuple[int, str, dict, str]] = []
        pages = self._pages(f"/v1/accounts/{addr}/transactions/trc20", query, 10 ** 6)
        token_more = False
        while True:
            try:
                items, sha = next(pages)
            except StopIteration as stop:
                token_more = bool(stop.value)
                break
            for item in items:
                if item.get("type") == "Transfer":
                    listed.append((int(item["block_timestamp"]), item["transaction_id"].lower(), item, sha))
            if len(listed) > limit:
                token_more = True
                break

        merged = sorted([(ts, h, "trx", r) for ts, h, r in native] +
                        [(ts, h, "trc20", (item, sha)) for ts, h, item, sha in listed],
                        key=lambda m: (m[0], m[1]), reverse=True)
        truncated = len(merged) > limit or trx_more or token_more
        kept = merged[:limit]

        token_items = [m[3] for m in kept if m[2] == "trc20"]
        token_records = iter(self._token_records(token_items))
        out = [m[3] if m[2] == "trx" else next(token_records) for m in kept]
        return TransferBatch(CHAIN, addr, tuple(out), truncated, limit)

    # -- parsing ---------------------------------------------------------------

    def _native_record(self, tx: dict, sha: str) -> TransferRecord | None:
        contracts = (tx.get("raw_data") or {}).get("contract") or []
        if len(contracts) != 1 or contracts[0].get("type") != "TransferContract":
            return None
        ret = tx.get("ret") or [{}]
        if ret[0].get("contractRet") != "SUCCESS":
            return None
        value = contracts[0]["parameter"]["value"]
        return TransferRecord(
            chain=CHAIN, tx_hash=tx["txID"].lower(), kind="native", event_index=0,
            from_address=to_base58(value["owner_address"]), to_address=to_base58(value["to_address"]),
            token_contract=None, token_symbol="TRX", token_decimals=TRX_DECIMALS,
            amount_raw=int(value.get("amount") or 0), block_number=int(tx["blockNumber"]),
            block_time=_ms_to_dt(int(tx["block_timestamp"])), source_slug=SOURCE, raw_sha256=sha,
        )

    def _token_records(self, items: list[tuple[dict, str]]) -> list[TransferRecord]:
        """Add block number and log index from the events endpoint, one call per transaction."""
        events_by_tx: dict[str, tuple[list[dict], str]] = {}
        fallback_next: dict[str, int] = {}     # next unused index for unmatched transfers, per tx
        out = []
        for item, page_sha in items:
            tx = item["transaction_id"].lower()
            if tx not in events_by_tx:
                # A confirmed transaction's events never change: reuse a saved copy.
                r = self._request("GET", f"/v1/transactions/{tx}/events",
                                  query={"only_confirmed": "true"}, reuse=True)
                events_by_tx[tx] = (list(r.data.get("data") or []), r.sha)
            events, events_sha = events_by_tx[tx]

            info = item.get("token_info") or {}
            token = to_base58(info.get("address") or item["contract_address"])
            frm, to, value = to_base58(item["from"]), to_base58(item["to"]), int(item["value"])

            match = None
            for ev in events:
                if (ev.get("event_name") == "Transfer" and not ev.get("_used")
                        and to_base58(ev["contract_address"]) == token):
                    # Positional keys "0", "1", "2" are always present; named keys
                    # vary by token (from/to/value or _from/_to/_value).
                    res = ev.get("result") or {}
                    if ("0" in res and "1" in res and "2" in res
                            and to_base58(res["0"]) == frm and to_base58(res["1"]) == to
                            and int(res["2"]) == value):
                        match = ev
                        break
            if match is not None:
                match["_used"] = True
                block, index, sha = int(match["block_number"]), int(match["event_index"]), events_sha
            elif events:
                # Numbered after the transaction's highest real log index, so a
                # fallback index can never equal a matched one.
                block, sha = int(events[0]["block_number"]), page_sha
                if tx not in fallback_next:
                    fallback_next[tx] = 1 + max(int(ev.get("event_index") or 0) for ev in events)
                index = fallback_next[tx]
                fallback_next[tx] += 1
            else:
                raise TronGridError(f"no confirmed events for transaction {tx}")

            decimals = info.get("decimals")
            symbol = info.get("symbol")
            if decimals is None or not 0 <= int(decimals) <= 77:
                (decimals, looked_up_symbol), _ = self.token_meta(token)
                symbol = symbol or looked_up_symbol
                if decimals is None:
                    raise TronGridError(f"token {token} has no readable decimals")
            else:
                self._token_meta.setdefault(token, (int(decimals), symbol))

            out.append(TransferRecord(
                chain=CHAIN, tx_hash=tx, kind="token", event_index=index,
                from_address=frm, to_address=to, token_contract=token, token_symbol=symbol,
                token_decimals=int(decimals), amount_raw=value, block_number=block,
                block_time=_ms_to_dt(int(item["block_timestamp"])), source_slug=SOURCE, raw_sha256=sha,
            ))
        return out
