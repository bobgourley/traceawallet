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
                as a second list: the two must agree over the blocks both cover. The
                REST list leaves out tokens Blockscout judges to be spam. A transfer in
                only one list is settled from its transaction receipt
                (eth_getTransactionReceipt): stored with the receipt's log index, or
                confirmed. token_lists_agree fails only if a gap cannot be settled.
                There is no reputation filtering. The first token transfers, when
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
RANGE_TTL = dt.timedelta(hours=24)   # how long a found lower edge is trusted before it is re-checked
REST_CREDITS, CALL_CREDITS = 50, 20  # estimated credits per call (Blockscout docs: 20 by default, 30-50 for
                                     # some REST endpoints); the provider's x-credits-remaining is the truth


class MemoryRangeStore:
    """The finished range per chain, in this process only (tests, local runs)."""

    def __init__(self):
        self.rows: dict[str, tuple[int, int, dt.datetime]] = {}

    def get(self, chain):
        return self.rows.get(chain)

    def put(self, chain, low, high, found_at):
        self.rows[chain] = (low, high, found_at)

    def drop(self, chain):
        self.rows.pop(chain, None)


_DEFAULT_RANGES = MemoryRangeStore()


def clear_range_cache() -> None:
    _DEFAULT_RANGES.rows.clear()

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

    def __init__(self, chain: str, raw_store, api_key: str | None, *, rpc_url: str = RPC_URL,
                 budget=None, range_store=None, now=lambda: dt.datetime.now(dt.timezone.utc), **kw):
        """budget: taw.budget.PostgresBudget (worker or scripts); None only in tests and fakes.
        range_store: where the finished internal range is kept (Postgres in the worker)."""
        if chain not in CHAINS:
            raise ValueError(f"Blockscout does not serve chain {chain!r}")
        super().__init__(chain, raw_store, api_key, **kw)
        self.rpc_url = rpc_url.format(chain_id=self.info.chain_id)
        self.rest_url = REST_URL.format(chain_id=self.info.chain_id)
        self.budget = budget
        self.ranges = range_store if range_store is not None else _DEFAULT_RANGES
        self._now = now
        self.credits_spent = 0
        self.credits_remaining: int | None = None

    # -- budget ---------------------------------------------------------------------

    @staticmethod
    def _credits(url: str) -> int:
        return REST_CREDITS if "/api/v2/" in url else CALL_CREDITS

    def _before_request(self, url: str) -> None:
        if self.budget is not None:
            self.budget.before_call(self._credits(url))       # raises BudgetExhausted

    def _after_response(self, url: str, resp) -> None:
        cost = self._credits(url)
        self.credits_spent += cost
        header = resp.headers.get("x-credits-remaining", "")
        remaining = int(header) if header.strip().lstrip("-").isdigit() else None
        if remaining is not None:
            self.credits_remaining = remaining
        if self.budget is not None:
            self.budget.after_call(cost, remaining)

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

    def _tx_by_hash(self, tx_hash: str) -> tuple[dict | None, str]:
        r = self._rpc("eth_getTransactionByHash", [tx_hash])
        return r.data.get("result") or None, r.sha

    def _tx_receipt(self, tx_hash: str) -> tuple[dict | None, str]:
        r = self._rpc("eth_getTransactionReceipt", [tx_hash])
        return r.data.get("result") or None, r.sha

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
        only_rest, only_list = a - b, b - a
        filled, unfilled = self._fill_gaps(address, rest, only_rest, only_list)
        details = {"rest_transfers": sum(a.values()), "token_list_rows": sum(b.values()),
                   "only_in_rest": sum(only_rest.values()), "only_in_token_list": sum(only_list.values()),
                   "filled_from_receipts": filled, "unfilled": unfilled,
                   "compared_above_block": cutoff if cutoff >= 0 else None}
        if unfilled:
            check = CheckResult("token_lists_agree", FAILED,
                                f"Blockscout's two token lists disagree on {filled + unfilled} transfer(s) and "
                                f"{unfilled} could not be confirmed from the transaction receipt", details)
        elif filled:
            check = CheckResult("token_lists_agree", PASSED,
                                f"{filled} transfer(s) were in only one of Blockscout's lists; each was confirmed "
                                f"from its transaction receipt and stored with its log index", details)
        else:
            check = CheckResult("token_lists_agree", PASSED, None, details)
        self._rest_more = rest_more
        return rest, (rest_more or list_more), [check]

    def _fill_gaps(self, address: str, rest: list[TransferRecord], only_rest: Counter,
                   only_list: Counter) -> tuple[int, int]:
        """Settle every transfer that only one list has, from the transaction's receipt.

        Only in the token list: store it, with the receipt's log index. (Blockscout's REST
        list leaves out tokens it judges to be spam; investigators need them anyway, and
        there is no reputation filtering here.) Only in the REST list: keep it if the
        receipt has that log. Returns (filled, unfilled); records are added to `rest`.
        """
        if not only_rest and not only_list:
            return 0, 0
        filled = unfilled = 0
        have = {(t.tx_hash, t.event_index) for t in rest}
        for tx in sorted({k[0] for k in only_rest} | {k[0] for k in only_list}):
            receipt = self._rpc("eth_getTransactionReceipt", [tx])
            result = receipt.data.get("result") or {}
            logs = []
            if str(result.get("status", "0x1")) in ("0x1", "1"):
                for lg in result.get("logs") or []:
                    topics = [t for t in lg.get("topics") or [] if t]
                    if len(topics) == 3 and topics[0] == es.TRANSFER_TOPIC:
                        logs.append((_int(lg["logIndex"]), (lg["address"].lower(), es._topic_address(topics[1]),
                                     es._topic_address(topics[2]), es.decode_uint((lg.get("data") or "0x")[2:] or "0")),
                                     _int(lg.get("blockNumber") or result.get("blockNumber"))))
            for key, n in only_rest.items():
                if key[0] != tx:
                    continue
                listed = {t.event_index for t in rest if t.tx_hash == tx and
                          (t.token_contract, t.from_address, t.to_address, t.amount_raw) == key[1:]}
                confirmed = sum(1 for idx, k, _ in logs if k == key[1:] and idx in listed)
                filled += min(n, confirmed)
                unfilled += max(0, n - confirmed)
            for key, n in only_list.items():
                if key[0] != tx:
                    continue
                free = [(idx, blk) for idx, k, blk in logs if k == key[1:] and (tx, idx) not in have]
                for idx, blk in free[:n]:
                    block, _ = self._block(blk)
                    contract = key[1]
                    rest.append(TransferRecord(
                        chain=self.chain, tx_hash=tx, kind="token", event_index=idx, from_address=key[2],
                        to_address=key[3], token_contract=contract, token_symbol=None, token_decimals=0,
                        amount_raw=key[4], block_number=blk,
                        block_time=dt.datetime.fromtimestamp(_int(block["timestamp"]), tz=dt.timezone.utc),
                        source_slug=self.source, raw_sha256=receipt.sha))
                    have.add((tx, idx))
                    filled += 1
                unfilled += max(0, n - len(free))
        return filled, unfilled

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
        """The finished range, found once a day and kept in the range store.

        Within 24 hours of the last search the lower edge is reused and only the top
        is extended to the current head (one probe, or a short search if the newest
        blocks are unfinished). After 24 hours the search starts from the last known
        edges: can the lower edge drop to block 0, and is [low, high] still finished?
        That is two probes. Only if it is not does a full search run. Nothing runs on
        a timer; this is called only by a lookup that reads internal transfers.
        """
        now = self._now()
        cached = self.ranges.get(self.chain)
        if cached is not None and cached[0] > head:
            return self._full_search(address, head)  # pinned below the stored range: don't touch the store
        if cached is not None:
            lo, hi, found_at = cached
            if now - found_at >= RANGE_TTL:
                if self._internal_done(address, 0, hi):
                    lo = 0
                elif not self._internal_done(address, lo, hi):
                    cached = None                  # the old range no longer holds: search afresh
                found_at = now
            if cached is not None:
                if hi < head:
                    hi = head if self._internal_done(address, hi, head) else self._extend_top(address, hi, head)
                hi = min(hi, head)
                self.ranges.put(self.chain, lo, hi, found_at)
                return lo, hi
        found = self._full_search(address, head)
        self.ranges.put(self.chain, found[0], found[1], now)
        return found

    def _extend_top(self, address: str, good: int, head: int) -> int:
        """Highest block U in [good, head] with [good, U] finished (good is known finished)."""
        lo_ok, hi_bad = good, head
        while hi_bad - lo_ok > self.tail_window:
            mid = (lo_ok + hi_bad) // 2
            if self._internal_done(address, good, mid):
                lo_ok = mid
            else:
                hi_bad = mid
        return lo_ok

    def _full_search(self, address, head):
        if self._internal_done(address, 0, head):
            return 0, head
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
            return 0, hi
        lo_bad, hi_ok = 0, hi
        step = max(self.range_step, hi // 4096)
        while hi_ok - lo_bad > step:
            mid = (lo_bad + hi_ok) // 2
            if self._internal_done(address, mid, hi):
                hi_ok = mid
            else:
                lo_bad = mid
        return hi_ok, hi

    def fetch_transfers(self, address, limit=2000):
        try:
            return super().fetch_transfers(address, limit)
        except BlockscoutIncomplete:
            self.ranges.drop(self.chain)           # the stored range was wrong: search again next time
            raise
