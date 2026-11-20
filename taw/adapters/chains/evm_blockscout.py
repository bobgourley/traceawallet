"""EVM chain adapter backed by the Blockscout PRO API (https://api.blockscout.com).

The worker's adapter for eth, arbitrum and optimism (free tier). Blockscout
speaks Etherscan's API, so this is EtherscanAdapter (evm_etherscan.py) with the
differences below; paging, parsing, block pinning, truncation, first transfers,
token balances and the raw-store rules are all inherited. Read that module's
docstring first. Etherscan stays for tests and cross-checks only
(docs/DECISIONS.md, 2026-11-19); its terms do not allow our use.

Differences from Etherscan, from docs.blockscout.com/devs/migrate-from-etherscan
and observed responses (to be confirmed with a key: see docs/STATUS.md):

  URL and key   GET https://api.blockscout.com/v2/api?chain_id=N&apikey=... for the
                Etherscan-style modules. The key travels in the query string and,
                as with Etherscan, only the bare URL and the other parameters are
                stored.
  No proxy      module=proxy answers "Unknown module". Block number, block, eth_call
                and eth_getCode go as JSON-RPC POST to
                https://api.blockscout.com/{chain_id}/json-rpc with the key in an
                "authorization: Bearer" header. The RawStore gets the URL and the
                JSON body, never the header.
  Not finished  A list can answer status "2", e.g. "Some internal transactions
                within this block range have not yet been processed". That is not an
                answer: it raises BlockscoutIncomplete, the lookup saves nothing, and
                the job retries later. A partial list is never stored as complete.
  Internal      Rows carry `transactionHash` and `index` where Etherscan has `hash`
                and `traceId`. `index` is Blockscout's position of the internal
                transaction within its transaction, unique there, so it is used as
                event_index directly. Where only traceId is present, the Etherscan
                encoding is used; with neither, the position as returned.
  Tokens        Not from getLogs: on Ethereum its log search returned 6 of the test
                wallet's 41 transfers and still answered OK (docs/research/
                evm-sources.md, Correction). ERC-20 transfers come from the REST list
                GET /{chain_id}/api/v2/addresses/{a}/token-transfers?type=ERC-20,
                newest first, 50 a page, each with its log_index. Blockscout's
                Etherscan-style token list (tokentx) is still read, for decimals and
                as a second list: the two must agree over the blocks both cover, or
                the check token_lists_agree fails. The first token transfers, when
                history is truncated, come from tokentx (oldest first) and then
                /api/v2/transactions/{hash}/token-transfers, filtered to this address,
                for their log index.
  Internal      Read only over the range Blockscout has finished indexing. A query
                over a range with unfinished blocks answers status "2". The finished
                range [low, high] is found by binary search on txlistinternal status
                (about 12 calls for each end), cached per chain for an hour, and
                recorded on the batch (internal_range). Nothing outside it is ever
                treated as checked. On 2026-11-19 Ethereum was unfinished before about
                block 23,397,763, and Optimism in its newest day or so.
  Nonce         JSON-RPC eth_getTransactionCount at the snapshot block (archive state,
                unlike Etherscan's free proxy).
  Withdrawals   Ethereum only. GET /1/api/v2/addresses/{a} answers
                has_beacon_chain_withdrawals in well under a second; only when it is
                true is the withdrawals list read (it timed out for an address with
                none).
  Limits        Free tier 5 requests a second and 100,000 credits a day (about 5,000
                calls); paced at 4 a second.

BNB Smart Chain has no Blockscout instance and is not handled here.
"""
from __future__ import annotations

import datetime as dt
import time
from collections import Counter
from typing import Any

from taw.adapters.base import FAILED, NOT_RUN, PASSED, CheckResult, TransferRecord
from taw.adapters.chains import evm_etherscan as es
from taw.adapters.chains.evm_etherscan import EtherscanAdapter, EtherscanError, _int, trace_index

SOURCE = "blockscout"
API_URL = "https://api.blockscout.com/v2/api"
RPC_URL = "https://api.blockscout.com/{chain_id}/json-rpc"
REST_URL = "https://api.blockscout.com/{chain_id}/api/v2"
MAX_REST_PAGES = 400            # 50 transfers a page
TAIL_WINDOW = 1_000             # blocks per probe when looking for the newest finished block
TAIL_SPAN = 2_000_000           # how far back that search may go
RANGE_TTL = 3600.0              # seconds a finished range is reused
_RANGE_CACHE: dict[str, tuple[int, int, float]] = {}


def clear_range_cache() -> None:
    _RANGE_CACHE.clear()
# Free tier, probed 2026-11-19: eth, arbitrum and optimism answer. Polygon and
# Base answer HTTP 402 "Featured chain ... requires one of the following plans:
# Builder, Business, Pro" (paid since 2026-10-01); BNB Smart Chain answers 404
# "Network not supported". The settings for polygon and base stay so that a
# paid plan only needs adding them back here and in the registry.
CHAINS = ("eth", "arbitrum", "optimism")
PAID_PLAN_CHAINS = ("polygon", "base")
_INCOMPLETE = "not finished: "


class BlockscoutIncomplete(EtherscanError):
    """Blockscout has not finished indexing what was asked. Retry later; store nothing."""
    incomplete = True


class BlockscoutAdapter(EtherscanAdapter):
    source = SOURCE
    provider_name = "Blockscout"
    key_env = "BLOCKSCOUT_API_KEY"
    default_base_url = API_URL
    chain_param = "chain_id"
    default_per_second = 4.0
    logs_window = 1000
    tail_window = TAIL_WINDOW          # range search settings; tests use small chains
    tail_span = TAIL_SPAN
    range_step = 1_000                 # the lower edge is found to within max(range_step, high / 4096)

    def __init__(self, chain: str, raw_store, api_key: str | None, *, rpc_url: str = RPC_URL, **kw):
        if chain not in CHAINS:
            raise ValueError(f"Blockscout does not serve chain {chain!r}")
        super().__init__(chain, raw_store, api_key, **kw)
        self.rpc_url = rpc_url.format(chain_id=self.info.chain_id)
        self.rest_url = REST_URL.format(chain_id=self.info.chain_id)
        self._monotonic = kw.get("clock") or time.monotonic

    # -- responses ----------------------------------------------------------------

    def _body_error(self, data: Any) -> str | None:
        if isinstance(data, dict) and str(data.get("status")) == "2":
            return _INCOMPLETE + str(data.get("message") or "Blockscout has not finished indexing this range")
        return super()._body_error(data)

    def _classify(self, text: str) -> type[EtherscanError]:
        t = text.lower()
        if text.startswith(_INCOMPLETE):
            return BlockscoutIncomplete
        if "requires one of the following plans" in t or "network not supported" in t:
            return es.EtherscanChainNotCovered
        if "too many requests" in t:
            return es.EtherscanRateLimited
        if "api key" in t and ("invalid" in t or "proceed with" in t or "missing" in t or "unauthor" in t):
            return es.EtherscanKeyRejected
        if "daily" in t and ("limit" in t or "credit" in t):
            return es.EtherscanDailyLimit
        if "not enough credits" in t or "out of credits" in t:
            return es.EtherscanDailyLimit
        return super()._classify(text)

    def _normalize_row(self, action: str, row: dict) -> dict:
        if action == "txlistinternal" and "hash" not in row and "transactionHash" in row:
            row = {**row, "hash": row["transactionHash"]}
        return row

    def _internal_index(self, row: dict) -> int | None:
        index = row.get("index")
        if index not in (None, "") and str(index).strip().isdigit():
            return int(index)
        return trace_index(row.get("traceId"))

    # -- JSON-RPC instead of module=proxy ------------------------------------------

    def _rpc(self, method: str, params: list, *, rpc_error_ok: bool = False):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        headers = {"authorization": f"Bearer {self._key}"} if self._key else None
        return self._send(f"json-rpc/{method}", "POST", self.rpc_url, stored={"body": body}, body=body,
                          headers=headers, rpc_error_ok=rpc_error_ok, memo=method != "eth_blockNumber")

    def _block_number(self) -> tuple[int, str]:
        r = self._rpc("eth_blockNumber", [])
        return _int(r.data["result"]), r.sha

    def _block(self, number: int) -> tuple[dict, str]:
        r = self._rpc("eth_getBlockByNumber", [hex(number), False])
        return (r.data.get("result") or {}), r.sha

    def _get_code(self, address: str, tag: str) -> tuple[str, str]:
        r = self._rpc("eth_getCode", [address, tag])
        return (r.data.get("result") or "0x"), r.sha

    def _eth_call(self, to: str, data: str, tag: str) -> tuple[str | None, str]:
        r = self._rpc("eth_call", [{"to": to, "data": data}, tag], rpc_error_ok=True)
        out = r.data.get("result")
        if not isinstance(out, str) or len(out) < 66:
            return None, r.sha
        return out[2:], r.sha

    # -- REST --------------------------------------------------------------------

    def _rest(self, path: str, params: dict | None = None):
        url = self.rest_url + path
        query = dict(params or {})
        send = {**query, "apikey": self._key} if self._key else query
        return self._send(f"rest{path.split('?')[0]}", "GET", url, stored={"query": query}, query=send)

    def _transfer_from_item(self, item: dict, sha: str) -> TransferRecord | None:
        if item.get("token_type") != "ERC-20":
            return None
        total = item.get("total") or {}
        token = item.get("token") or {}
        contract = (token.get("address_hash") or token.get("address") or "").lower()
        decimals = total.get("decimals")
        if decimals not in (None, "") and str(decimals).isdigit() and 0 <= int(decimals) <= 77:
            self._token_meta.setdefault(contract, (int(decimals), token.get("symbol")))
        if item.get("timestamp"):
            when = dt.datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
        else:                                    # the per-transaction list leaves it empty
            block, _ = self._block(int(item["block_number"]))
            when = dt.datetime.fromtimestamp(_int(block["timestamp"]), tz=dt.timezone.utc)
        return TransferRecord(
            chain=self.chain, tx_hash=item["transaction_hash"].lower(), kind="token",
            event_index=int(item["log_index"]),
            from_address=((item.get("from") or {}).get("hash") or "").lower(),
            to_address=((item.get("to") or {}).get("hash") or "").lower(),
            token_contract=contract, token_symbol=None, token_decimals=0,     # filled in by _with_decimals
            amount_raw=int(total.get("value") or 0), block_number=int(item["block_number"]),
            block_time=when, source_slug=self.source, raw_sha256=sha)

    def _token_transfers(self, address, head, limit):
        # 1. The REST list, newest first, pinned to head by block number.
        rest: list[TransferRecord] = []
        params: dict = {"type": "ERC-20"}
        rest_more = False
        for _ in range(MAX_REST_PAGES):
            r = self._rest(f"/addresses/{address}/token-transfers", params)
            for item in r.data.get("items") or []:
                rec = self._transfer_from_item(item, r.sha)
                if rec is not None and rec.block_number <= head:
                    rest.append(rec)
            nxt = r.data.get("next_page_params")
            if not nxt:
                break
            if len(rest) > limit:
                rest_more = True
                break
            params = {"type": "ERC-20", **nxt}
        else:
            rest_more = True

        # 2. Blockscout's Etherscan-style token list: decimals, and the second list.
        rows, list_more = self._account(
            "tokentx", address, head, limit, keep=lambda row: True,
            key=lambda row: (row["hash"].lower(), row.get("contractAddress", "").lower(), row.get("from"),
                             row.get("to"), row.get("value"), row.get("blockNumber"), row.get("logIndex")))
        self._remember_meta(row for row, _ in rows)

        # Compare over the blocks both lists cover in full.
        cutoff = -1
        if rest_more:
            cutoff = max(cutoff, min((t.block_number for t in rest), default=-1))
        if list_more:
            cutoff = max(cutoff, min((_int(row["blockNumber"]) for row, _ in rows), default=-1))
        a = Counter((t.tx_hash, t.token_contract, t.from_address, t.to_address, t.amount_raw)
                    for t in rest if t.block_number > cutoff)
        b = Counter((row["hash"].lower(), (row.get("contractAddress") or "").lower(), (row.get("from") or "").lower(),
                     (row.get("to") or "").lower(), _int(row.get("value") or 0))
                    for row, _ in rows if _int(row["blockNumber"]) > cutoff)
        only_rest, only_list = sum((a - b).values()), sum((b - a).values())
        details = {"rest_transfers": sum(a.values()), "token_list_rows": sum(b.values()),
                   "only_in_rest": only_rest, "only_in_token_list": only_list,
                   "compared_above_block": cutoff if cutoff >= 0 else None}
        if only_rest or only_list:
            check = CheckResult("token_lists_agree", FAILED,
                                f"Blockscout's two token lists disagree: {sum(a.values())} transfers in the REST list, "
                                f"{sum(b.values())} rows in the token list", details)
        else:
            check = CheckResult("token_lists_agree", PASSED, None, details)
        self._rest_more = rest_more
        return rest, (rest_more or list_more), [check]

    def _firsts_token(self, addr, head, have, more):
        if not more:
            return self._first_and_first_in(have, addr)
        rows = self._oldest_rows("tokentx", addr, 0, head, lambda row: True)
        self._remember_meta(row for row, _ in rows)
        if not rows:
            return []
        first_in = next((row for row, _ in rows if (row.get("to") or "").lower() == addr), None)
        txs = list(dict.fromkeys([rows[0][0]["hash"].lower()] + ([first_in["hash"].lower()] if first_in else [])))
        records = []
        for tx in txs:
            r = self._rest(f"/transactions/{tx}/token-transfers", {"type": "ERC-20"})
            for item in r.data.get("items") or []:
                rec = self._transfer_from_item(item, r.sha)
                if rec is not None and addr in (rec.from_address, rec.to_address):    # a tx can pay many
                    records.append(rec)
        return self._first_and_first_in(records, addr)

    # -- nonce and withdrawals ------------------------------------------------------

    def _nonce(self, address, tag):
        r = self._rpc("eth_getTransactionCount", [address, tag], rpc_error_ok=True)
        out = r.data.get("result")
        return (_int(out) if isinstance(out, str) and out.startswith("0x") else None), r.sha

    def _withdrawals(self, address, head):
        if self.chain != "eth":
            return 0, []
        summary = self._rest(f"/addresses/{address}")
        shas = [summary.sha]
        flag = summary.data.get("has_beacon_chain_withdrawals")
        if flag is False:
            return 0, shas
        if flag is not True:
            return None, shas                    # the summary did not say: not read
        total, params = 0, {}
        for _ in range(MAX_REST_PAGES):
            r = self._rest(f"/addresses/{address}/withdrawals", params)
            shas.append(r.sha)
            for item in r.data.get("items") or []:
                if int(item["block_number"]) <= head:
                    total += int(item["amount"])             # wei
            nxt = r.data.get("next_page_params")
            if not nxt:
                return total, shas
            params = dict(nxt)
        return None, shas

    # -- the finished internal-transaction range --------------------------------------

    def _internal_done(self, address: str, lo: int, hi: int) -> bool:
        """True when Blockscout has finished indexing internal transactions in [lo, hi]."""
        r = self._send("account/txlistinternal (range probe)", "GET", self.base_url,
                       stored={"query": {self.chain_param: self.info.chain_id, "module": "account",
                                         "action": "txlistinternal", "address": address, "startblock": lo,
                                         "endblock": hi, "page": 1, "offset": 1}},
                       query={self.chain_param: self.info.chain_id, "module": "account", "action": "txlistinternal",
                              "address": address, "startblock": lo, "endblock": hi, "page": 1, "offset": 1,
                              **({"apikey": self._key} if self._key else {})},
                       accept=lambda data: isinstance(data, dict) and str(data.get("status")) == "2")
        return str(r.data.get("status")) != "2"

    def _internal_range(self, address, head):
        cached = _RANGE_CACHE.get(self.chain)
        if cached and self._monotonic() - cached[2] < RANGE_TTL:
            lo, hi = cached[0], min(cached[1], head)
            if lo <= hi:
                return lo, hi
        if self._internal_done(address, 0, head):
            found = (0, head)
        else:
            hi = head
            if not self._internal_done(address, max(0, head - self.tail_window), head):
                bottom = max(0, head - self.tail_span)
                if not self._internal_done(address, max(0, bottom - self.tail_window), bottom):
                    raise BlockscoutIncomplete(f"no finished internal-transaction range in the last "
                                               f"{self.tail_span:,} blocks")
                lo_ok, hi_bad = bottom, head
                while hi_bad - lo_ok > self.tail_window:
                    mid = (lo_ok + hi_bad) // 2
                    if self._internal_done(address, max(0, mid - self.tail_window), mid):
                        lo_ok = mid
                    else:
                        hi_bad = mid
                hi = lo_ok
            if self._internal_done(address, 0, hi):
                found = (0, hi)
            else:
                lo_bad, hi_ok = 0, hi
                step = max(self.range_step, hi // 4096)
                while hi_ok - lo_bad > step:
                    mid = (lo_bad + hi_ok) // 2
                    if self._internal_done(address, mid, hi):
                        hi_ok = mid
                    else:
                        lo_bad = mid
                found = (hi_ok, hi)
        _RANGE_CACHE[self.chain] = (found[0], found[1], self._monotonic())
        return found

