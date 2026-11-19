"""EVM chain adapter backed by Etherscan API V2 (https://api.etherscan.io/v2/api).

One adapter instance serves one chain; `chainid` picks the chain on every call.
Endpoints, checked against docs.etherscan.io on 2026-11-18:

  proxy   eth_blockNumber, eth_getBlockByNumber   the block we read at, and its time
  proxy   eth_getCode (at that block)             contract or wallet
  proxy   eth_call (at that block)                native balance through Multicall3,
                                                  token balanceOf, decimals(), symbol()
  account txlist                                  native transfers
  account txlistinternal                          internal transfers (value moved by contracts)
  account tokentx                                 ERC-20 transfers, without a log index
  logs    getLogs (Transfer topic, our address)   the same ERC-20 transfers, with the log index

Free plan (probed 2026-11-18): Ethereum, Polygon and Arbitrum work. BNB Smart
Chain, OP Mainnet and Base answer "Free API access is not supported for this
chain"; that raises EtherscanChainNotCovered and the job fails at once. The
documented limit is 3 calls a second and 100,000 a day; requests are paced at
2.5 a second. "Token holdings" (addresstokenbalance) is an API Pro endpoint.

Errors arrive as HTTP 200. Every body is checked: {"status": "0"} with a text
result is an error ("Max calls per sec rate limit reached (3/sec)", "Invalid
API Key", ...); {"status": "0"} with an empty list is just "nothing found".
Rate-limit replies are retried with growing waits.

The API key travels in the query string because Etherscan accepts it nowhere
else. It is added only to the outgoing request; what goes to the RawStore is the
bare endpoint URL and the other parameters. clean_request() in base.py would
also drop any field named like a key and refuses to store a known secret.

Block height. Every read is pinned to one block, `head`: the latest block minus
a few confirmations per chain (CHAINS below). Lists use endblock=head, logs
toBlock=head, and eth_call / eth_getCode read state at head. The native balance
comes from Multicall3's getEthBalance at head, because Etherscan's own balance
endpoint only answers "latest" (balance history is a paid endpoint). So the
snapshot's balances, contract flag and the transfer list agree on as_of_block.

event_index:
  native    0. One transaction moves native value once.
  token     The log index, from the logs endpoint. tokentx has no log index, so
            ERC-20 transfers are read from getLogs filtered on the Transfer topic
            with our address as topic1 (sender), then as topic2 (recipient). A log
            index is unique within its block, so it is unique for the transaction,
            and two identical transfers in one transaction are two logs with two
            indexes. tokentx is used only for decimals and symbols, and to choose
            the block range when history is long. Logs with four topics are ERC-721
            NFT transfers (the token id is indexed) and are ignored. Tokens whose
            Transfer event does not index from and to cannot be found this way;
            tokentx rows with no matching log are counted in unmatched_token_rows
            and not stored.
  internal  The trace position. Etherscan gives traceId as a path in the call
            tree ("0", "0_1", "0_1_1"). It is unique within the transaction, so
            it is encoded into one integer that keeps trace order: each step plus
            one, as base-32 digits over six places ("0_1_1" -> digits 1,2,2 ->
            1*32^5 + 2*32^4 + 2*32^3). That fits the int column for paths up to six
            deep with steps below 31. When traceId is missing or does not fit, the
            position within the transaction as returned is used instead (0, 1, ...);
            those numbers are all below 32^5, so the two schemes never collide.
            Unlike the trace path, a position depends on which rows the API
            returned for this address, so it is only a fallback.

Skipped: failed transactions and failed internal calls (isError = 1, or
txreceipt_status = 0) moved no value; zero-value native and internal rows are
not transfers; token transfers whose decimals cannot be read at all
(skipped_no_decimals).

Paging. Lists are read 1,000 rows a page. Etherscan refuses page x offset above
10,000, so after ten pages the block edge moves to the last block seen and paging
starts again; rows from that block arrive twice and are dropped by key. Lists are
read newest first until each has more than `limit` kept rows or runs out, so the
merged newest `limit` is exact, as in the Tron adapter. Logs are read oldest
first (getLogs has no sort) from the oldest block that tokentx needed.

Truncation. Over `limit`, the batch holds the newest `limit` transfers and
truncated=True. The address's very first native, internal and token transfers,
and the first incoming one of each, are then fetched separately and appended at
the end, so first activity and first funder are right. The batch then has a gap
between the newest `limit` and those first transfers.

Token balances. The full holdings endpoint is paid, so balanceOf is called at
head for the freezable stablecoins listed per chain, plus the 20 tokens with the
most transfers among the address's newest 1,000 token transfers. Only non-zero
balances are kept. The snapshot is truncated=True when that page was full or held
more tokens than were checked: the address may hold tokens we did not ask about.

Identity: tokens by contract address only, never symbol. Addresses and hashes
are lower case.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import httpx

from taw.adapters.base import AddressSnapshot, RawStore, TokenBalance, TransferBatch, TransferRecord
from taw.adapters.chains.common import Pacer, decode_string, decode_uint
from taw.adapters.chains.evm_address import normalize

SOURCE = "etherscan"
API_URL = "https://api.etherscan.io/v2/api"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"   # same address on every chain below
SEL_GET_ETH_BALANCE = "0x4d2301cc"                         # getEthBalance(address)
SEL_BALANCE_OF = "0x70a08231"                              # balanceOf(address)
SEL_DECIMALS = "0x313ce567"
SEL_SYMBOL = "0x95d89b41"
PAGE_SIZE = 1000
WINDOW = 10_000            # Etherscan's cap on page x offset
MAX_LIST_PAGES = 40        # per list, before calling it truncated
MAX_LOG_PAGES = 60         # logs are read oldest first, so they must finish
TOP_TOKENS = 20
TOKEN_PAGE = 1000          # token transfers counted to pick the top 20
FIRST_ROWS = 100           # rows read oldest first to find the first transfers


@dataclass(frozen=True)
class EvmChain:
    id: str                    # our chain id (chain table)
    chain_id: int              # EIP-155 id, Etherscan's chainid
    name: str
    native_symbol: str
    confirmations: int         # blocks behind latest that we read at
    stablecoins: tuple[tuple[str, str], ...] = ()   # (contract, display name)
    native_decimals: int = 18


# Freezable stablecoins: each contract's verified ABI on Etherscan was read on
# 2026-11-18 (Optimism and Base on 2026-11-19) and has a blacklist, block or
# freeze function (or event). DAI, USDe and RLUSD (Ethereum), USDT (OP Mainnet)
# and USDbC (Base) were checked and showed none, so they are not listed. BNB Smart
# Chain is not checked in Phase 1; its list is left empty.
CHAINS: dict[str, EvmChain] = {c.id: c for c in (
    EvmChain("eth", 1, "Ethereum", "ETH", 12, (
        ("0xdac17f958d2ee523a2206206994597c13d831ec7", "USDT (Tether)"),
        ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "USDC (Circle)"),
        ("0x6c3ea9036406852006290770bedfcaba0e23a0e8", "PYUSD (Paxos)"),
        ("0x8e870d67f660d95d5be530380d0ec0bd388289e1", "USDP (Paxos)"),
        ("0x0000000000085d4780b73119b644ae5ecd22b376", "TUSD"),
        ("0x1abaea1f7c830bd89acc67ec4af516284b1bc33c", "EURC (Circle)"),
        ("0xc5f0f7b66764f6ec8c8dff7ba683102295e16409", "FDUSD"),
        ("0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d", "USD1"),
        ("0xe343167631d89b6ffc58b88d6b7fb0228795491d", "USDG (Paxos)"),
    )),
    EvmChain("polygon", 137, "Polygon", "POL", 64, (
        ("0xc2132d05d31c914a87c6611c10748aeb04b58e8f", "USDT0 (Tether)"),
        ("0x3c499c542cef5e3811e1192ce70d8cc03d5c3359", "USDC (Circle)"),
        ("0x2791bca1f2de4661ed88a30c99a7a9449aa84174", "USDC.e (bridged)"),
    )),
    EvmChain("arbitrum", 42161, "Arbitrum One", "ETH", 20, (
        ("0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "USDT0 (Tether)"),
        ("0xaf88d065e77c8cc2239327c5edb3a432268e5831", "USDC (Circle)"),
        ("0xff970a61a04b1ca14834a43f5de4533ebddb5cc8", "USDC.e (bridged)"),
        ("0x46850ad61c2b7d64d08c9c754f45254596696984", "PYUSD (Paxos)"),
    )),
    EvmChain("bsc", 56, "BNB Smart Chain", "BNB", 15),
    EvmChain("optimism", 10, "OP Mainnet", "ETH", 10, (
        ("0x0b2c639c533813f4aa9d7837caf62653d097ff85", "USDC (Circle)"),
        ("0x7f5c764cbc14f9669b88837ca1490cca17c31607", "USDC.e (bridged)"),
    )),
    EvmChain("base", 8453, "Base", "ETH", 10, (
        ("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USDC (Circle)"),
        ("0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42", "EURC (Circle)"),
    )),
)}

_KIND_ORDER = {"native": 0, "internal": 1, "token": 2}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class EtherscanError(RuntimeError):
    """An error from an Etherscan-compatible provider. `provider` and `key_env` name
    which one, so failure messages point at the right service and setting."""
    provider = "Etherscan"
    key_env = "ETHERSCAN_API_KEY"

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class EtherscanRateLimited(EtherscanError):
    """Still told to slow down after every retry."""


class EtherscanChainNotCovered(EtherscanError):
    """Our plan does not include this chain. Retrying cannot help."""

    def __init__(self, message: str, chain_name: str):
        super().__init__(message)
        self.chain_name = chain_name


class EtherscanKeyRejected(EtherscanError):
    """The API key is missing or invalid."""


class EtherscanDailyLimit(EtherscanError):
    """The day's call allowance is used up."""


class EtherscanPaidEndpoint(EtherscanError):
    """An endpoint our plan does not include. A bug here, not a passing problem."""


def _classify(text: str) -> type[EtherscanError]:
    t = text.lower()
    if "not supported for this chain" in t or "upgrade your api plan" in t:
        return EtherscanChainNotCovered
    if "invalid api key" in t or "missing/invalid api key" in t or "missing api key" in t:
        return EtherscanKeyRejected
    if "daily" in t and "limit" in t:
        return EtherscanDailyLimit
    if "rate limit" in t or "max calls per sec" in t or "too many" in t:
        return EtherscanRateLimited
    if "api pro endpoint" in t:
        return EtherscanPaidEndpoint
    return EtherscanError


def _body_error(data: Any) -> str | None:
    """The error text in a 200 response, or None when the body is a real answer."""
    if not isinstance(data, dict):
        return "response is not a JSON object"
    if "error" in data and data.get("error"):           # JSON-RPC error from the proxy module
        err = data["error"]
        return (err.get("message") if isinstance(err, dict) else str(err)) or "proxy error"
    if str(data.get("status")) == "0":
        result = data.get("result")
        if isinstance(result, list):                     # "No transactions found" and the like
            return None
        return str(result or data.get("message") or "status 0")
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Response:
    data: Any
    sha: str


@dataclass(frozen=True)
class Head:
    number: int
    time: dt.datetime
    shas: tuple[str, ...]


def _int(v: Any) -> int:
    """Decimal text, 0x-hex text or an int."""
    if isinstance(v, int):
        return v
    s = str(v).strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s or "0")


def _ts(v: Any) -> dt.datetime:
    return dt.datetime.fromtimestamp(_int(v), tz=dt.timezone.utc)


def _topic_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _pad(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def trace_index(trace_id: str | None) -> int | None:
    """Order-keeping integer for an Etherscan traceId ("0_1_1"), or None if it does not fit."""
    if not trace_id:
        return None
    try:
        steps = [int(p) for p in str(trace_id).split("_")]
    except ValueError:
        return None
    if not 1 <= len(steps) <= 6 or any(not 0 <= s <= 30 for s in steps):
        return None
    return sum((s + 1) * 32 ** (5 - i) for i, s in enumerate(steps))


def _failed(row: dict) -> bool:
    return str(row.get("isError", "0")) == "1" or str(row.get("txreceipt_status", "")) == "0"


def _row_to(row: dict) -> str:
    return (row.get("to") or row.get("contractAddress") or "").lower()


def _sort_key(r: TransferRecord):
    return (r.block_number, r.tx_hash, _KIND_ORDER[r.kind], r.event_index)


def _record_key(r: TransferRecord):
    return (r.tx_hash, r.kind, r.event_index)


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class EtherscanAdapter:
    # What differs between Etherscan-compatible providers. A subclass changes these
    # and the few hook methods below (see evm_blockscout.py).
    source = SOURCE
    provider_name = "Etherscan"
    key_env = "ETHERSCAN_API_KEY"
    default_base_url = API_URL
    chain_param = "chainid"
    default_per_second = 2.5
    logs_window: int | None = None          # rows one logs query may return; None = the list window

    def __init__(self, chain: str, raw_store: RawStore, api_key: str | None, *,
                 http: httpx.Client | None = None,
                 base_url: str | None = None,
                 per_second: float | None = None,
                 max_attempts: int = 6,
                 page_size: int = PAGE_SIZE,
                 window: int = WINDOW,
                 at_block: int | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        """at_block pins every read to that block instead of latest minus confirmations
        (used by the cross-check so two providers are compared at one height)."""
        if chain not in CHAINS:
            raise ValueError(f"no Etherscan settings for chain {chain!r}")
        self.info = CHAINS[chain]
        self.chain = chain
        self.raw_store = raw_store
        self.base_url = base_url or self.default_base_url
        self._at_block = at_block
        self.http = http or httpx.Client(timeout=30.0)
        self._key = api_key
        self.max_attempts = max_attempts
        self.page_size = page_size
        self.window = window
        self._sleep = sleep
        self._pacer = Pacer(per_second or self.default_per_second, clock, sleep)
        self._head: Head | None = None
        self._token_meta: dict[str, tuple[int | None, str | None]] = {}
        self._memo: dict[str, _Response] = {}   # every read is pinned to head, so a repeat gets the same answer
        self.request_count = 0
        self.unmatched_token_rows = 0      # tokentx rows with no Transfer log (see module docstring)
        self.skipped_no_decimals = 0

    # -- HTTP ------------------------------------------------------------------

    def _request(self, params: dict, *, rpc_error_ok: bool = False) -> _Response:
        """One GET to the Etherscan-style endpoint. rpc_error_ok: return a JSON-RPC
        error (a reverted eth_call) as an answer."""
        query = {self.chain_param: self.info.chain_id, **params}
        send = {**query, "apikey": self._key} if self._key else dict(query)
        return self._send(f"{params.get('module')}/{params.get('action')}", "GET", self.base_url,
                          stored={"query": query}, query=send, rpc_error_ok=rpc_error_ok,
                          memo=params.get("action") != "eth_blockNumber")

    def _send(self, what: str, method: str, url: str, *, stored: dict, query: dict | None = None,
              body: dict | None = None, headers: dict | None = None, rpc_error_ok: bool = False,
              memo: bool = True) -> _Response:
        """Send with pacing and retries, store every response, check the body.

        `stored` is what the RawStore records for the request: never the key, which
        travels only in `query` or `headers`.
        """
        memo_key = json.dumps([url, stored], sort_keys=True, default=str)
        if memo and memo_key in self._memo:
            return self._memo[memo_key]
        try:
            return self._send_loop(what, method, url, stored=stored, query=query, body=body, headers=headers,
                                   rpc_error_ok=rpc_error_ok, memo=memo, memo_key=memo_key)
        except EtherscanError as exc:
            exc.provider, exc.key_env = self.provider_name, self.key_env
            raise

    def _send_loop(self, what, method, url, *, stored, query, body, headers, rpc_error_ok, memo, memo_key):
        for attempt in range(1, self.max_attempts + 1):
            self._pacer.wait()
            self.request_count += 1
            resp = self.http.request(method, url, params=query, json=body, headers=headers)
            sha = self.raw_store.save(self.source, url, stored, resp.status_code, resp.content)

            if resp.status_code in (429, 502, 503, 504):
                if attempt == self.max_attempts:
                    raise EtherscanRateLimited(f"{what}: still HTTP {resp.status_code} after {attempt} tries",
                                               resp.status_code)
                retry_after = resp.headers.get("Retry-After", "")
                self._backoff(attempt, float(retry_after) if retry_after.isdigit() else None)
                continue
            if resp.status_code >= 400:
                kind = self._classify(resp.text[:300]) if resp.status_code in (401, 402, 403, 404) else EtherscanError
                if kind is EtherscanError:
                    raise EtherscanError(f"{what}: HTTP {resp.status_code}", resp.status_code)
                if kind is EtherscanChainNotCovered:
                    raise EtherscanChainNotCovered(f"{self.info.name}: HTTP {resp.status_code}: {resp.text[:200]}",
                                                   self.info.name)
                raise kind(f"{what}: HTTP {resp.status_code}: {resp.text[:200]}")
            try:
                data = json.loads(resp.content or b"{}")
            except ValueError as exc:
                raise EtherscanError(f"{what}: response is not JSON") from exc
            error = self._body_error(data)
            if error is None or (rpc_error_ok and isinstance(data, dict) and "jsonrpc" in data):
                result = _Response(data, sha)
                if memo:
                    self._memo[memo_key] = result
                return result
            kind = self._classify(error)
            if kind is EtherscanRateLimited:
                if attempt == self.max_attempts:
                    raise EtherscanRateLimited(f"{what}: {error} (after {attempt} tries)")
                self._backoff(attempt, None)
                continue
            if kind is EtherscanChainNotCovered:
                raise EtherscanChainNotCovered(f"{self.info.name}: {error}", self.info.name)
            raise kind(f"{what}: {error}")
        raise AssertionError("unreachable")

    # -- hooks a provider dialect may override -------------------------------------

    def _body_error(self, data: Any) -> str | None:
        return _body_error(data)

    def _classify(self, text: str) -> type[EtherscanError]:
        return _classify(text)

    def _normalize_row(self, action: str, row: dict) -> dict:
        """A list row in Etherscan's field names."""
        return row

    def _internal_index(self, row: dict) -> int | None:
        """event_index for an internal transfer, or None to fall back to position."""
        return trace_index(row.get("traceId"))

    def _block_number(self) -> tuple[int, str]:
        r = self._proxy("eth_blockNumber")
        return _int(r.data["result"]), r.sha

    def _block(self, number: int) -> tuple[dict, str]:
        r = self._proxy("eth_getBlockByNumber", tag=hex(number), boolean="false")
        return (r.data.get("result") or {}), r.sha

    def _get_code(self, address: str, tag: str) -> tuple[str, str]:
        r = self._proxy("eth_getCode", address=address, tag=tag)
        return (r.data.get("result") or "0x"), r.sha

    def _backoff(self, attempt: int, seconds: float | None) -> None:
        wait = seconds if seconds is not None else min(30.0, 2 ** (attempt - 1))
        self._sleep(wait + random.uniform(0, 0.5))

    def _proxy(self, action: str, **params) -> _Response:
        return self._request({"module": "proxy", "action": action, **params})

    def _eth_call(self, to: str, data: str, tag: str) -> tuple[str | None, str]:
        """(hex output without 0x, or None when the call returned nothing), sha."""
        r = self._request({"module": "proxy", "action": "eth_call", "to": to, "data": data, "tag": tag},
                          rpc_error_ok=True)
        out = r.data.get("result")
        if not isinstance(out, str) or len(out) < 66:
            return None, r.sha
        return out[2:], r.sha

    # -- small lookups -----------------------------------------------------------

    def head(self) -> Head:
        """The block every read is pinned to, fixed for the life of this adapter."""
        if self._head is None:
            if self._at_block is not None:
                number, shas = self._at_block, ()
            else:
                latest, latest_sha = self._block_number()
                number, shas = max(0, latest - self.info.confirmations), (latest_sha,)
            got, block_sha = self._block(number)
            if _int(got.get("number", -1)) != number:
                raise EtherscanError(f"block {number} was not returned")
            self._head = Head(number, _ts(got["timestamp"]), shas + (block_sha,))
        return self._head

    def token_meta(self, contract: str) -> tuple[tuple[int | None, str | None], list[str]]:
        """(decimals, symbol) read from the token contract, and the responses used."""
        if contract in self._token_meta:
            return self._token_meta[contract], []
        dec_hex, sha1 = self._eth_call(contract, SEL_DECIMALS, "latest")
        sym_hex, sha2 = self._eth_call(contract, SEL_SYMBOL, "latest")
        decimals = decode_uint(dec_hex) if dec_hex else None
        if decimals is not None and not 0 <= decimals <= 77:
            decimals = None
        meta = (decimals, decode_string(sym_hex) if sym_hex else None)
        self._token_meta[contract] = meta
        return meta, [sha1, sha2]

    def _remember_meta(self, rows: Iterable[dict]) -> None:
        """Decimals and symbol as tokentx gives them (Etherscan's reading of the contract)."""
        for row in rows:
            contract = (row.get("contractAddress") or "").lower()
            if not contract or contract in self._token_meta:
                continue
            dec = str(row.get("tokenDecimal", "")).strip()
            if not dec.isdigit() or not 0 <= int(dec) <= 77:
                continue                                 # unreadable here; ask the contract later
            symbol = (row.get("tokenSymbol") or "").replace("\x00", "") or None
            self._token_meta[contract] = (int(dec), symbol)

    # -- paging ----------------------------------------------------------------

    def _scan(self, params: dict, *, lo: int, hi: int, descending: bool, want: int | None,
              keep: Callable[[dict], bool], key: Callable[[dict], Any],
              block_of: Callable[[dict], int], max_pages: int,
              lo_name: str = "startblock", hi_name: str = "endblock",
              sort: bool = True, window: int | None = None) -> tuple[list[tuple[dict, str]], bool]:
        """Page through a list between blocks lo and hi.

        Returns ([(row, sha)], more). `more` is True when it stopped with rows
        still to read: past `want` kept rows, or at max_pages. Past the 10,000-row
        window the block edge moves to the last block seen and paging restarts.
        """
        out: list[tuple[dict, str]] = []
        seen: set = set()
        pages = 0
        window = window or self.window
        page_size = min(self.page_size, window)
        while True:
            rows: list = []
            for page in range(1, window // page_size + 1):
                q = {**params, lo_name: lo, hi_name: hi, "page": page, "offset": page_size}
                if sort:
                    q["sort"] = "desc" if descending else "asc"
                r = self._request(q)
                rows = [self._normalize_row(params.get("action", ""), row) for row in (r.data.get("result") or [])]
                pages += 1
                for row in rows:
                    k = key(row)
                    if k in seen:
                        continue
                    seen.add(k)
                    if keep(row):
                        out.append((row, r.sha))
                if len(rows) < page_size:
                    return out, False
                if want is not None and len(out) > want:
                    return out, True
                if pages >= max_pages:
                    return out, True
            edge = block_of(rows[-1])
            if descending:
                if edge == hi:
                    raise EtherscanError(f"more than {window:,} rows in block {edge}; cannot page past it")
                hi = edge
            else:
                if edge == lo:
                    raise EtherscanError(f"more than {window:,} rows in block {edge}; cannot page past it")
                lo = edge

    def _account(self, action: str, addr: str, head: int, want: int | None,
                 keep: Callable[[dict], bool], key: Callable[[dict], Any]):
        return self._scan({"module": "account", "action": action, "address": addr},
                          lo=0, hi=head, descending=True, want=want, keep=keep, key=key,
                          block_of=lambda row: _int(row["blockNumber"]), max_pages=MAX_LIST_PAGES)

    def _logs(self, addr: str, lo: int, hi: int) -> list[tuple[dict, str]]:
        """ERC-20 Transfer logs with addr as sender or recipient, oldest first, no duplicates."""
        out: list[tuple[dict, str]] = []
        seen: set = set()
        for topic, opr in (("topic1", "topic0_1_opr"), ("topic2", "topic0_2_opr")):
            rows, more = self._scan(
                {"module": "logs", "action": "getLogs", "topic0": TRANSFER_TOPIC, topic: _pad(addr), opr: "and"},
                lo=lo, hi=hi, descending=False, want=None,
                keep=lambda log: len(log.get("topics") or []) == 3,        # four topics: an NFT
                key=lambda log: (log["transactionHash"].lower(), _int(log["logIndex"])),
                block_of=lambda log: _int(log["blockNumber"]), max_pages=MAX_LOG_PAGES,
                lo_name="fromBlock", hi_name="toBlock", sort=False, window=self.logs_window)
            if more:
                raise EtherscanError(f"token transfer logs for {addr} did not fit in {MAX_LOG_PAGES} pages")
            for log, sha in rows:
                k = (log["transactionHash"].lower(), _int(log["logIndex"]))
                if k not in seen:                     # a transfer to oneself is in both lists
                    seen.add(k)
                    out.append((log, sha))
        return out

    # -- ChainAdapter --------------------------------------------------------------

    def fetch_snapshot(self, address: str) -> AddressSnapshot:
        addr = normalize(address)
        head = self.head()
        tag = hex(head.number)

        native_hex, native_sha = self._eth_call(MULTICALL3, SEL_GET_ETH_BALANCE + _pad(addr)[2:], tag)
        if native_hex is None:
            raise EtherscanError(f"could not read the native balance at block {head.number}")
        shas = [native_sha, *head.shas]

        code_hex, code_sha = self._get_code(addr, tag)
        shas.append(code_sha)
        is_contract = isinstance(code_hex, str) and code_hex not in ("0x", "0x0")

        page = self._request({"module": "account", "action": "tokentx", "address": addr, "startblock": 0,
                              "endblock": head.number, "page": 1, "offset": TOKEN_PAGE, "sort": "desc"})
        shas.append(page.sha)
        rows = [self._normalize_row("tokentx", row) for row in (page.data.get("result") or [])]
        self._remember_meta(rows)
        counts = Counter((row.get("contractAddress") or "").lower() for row in rows)
        counts.pop("", None)
        stable = [c for c, _ in self.info.stablecoins]
        top = [c for c, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if c not in stable][:TOP_TOKENS]
        checked = stable + top
        truncated = len(rows) >= TOKEN_PAGE or any(c not in checked for c in counts)

        balances = []
        for contract in checked:
            out, sha = self._eth_call(contract, SEL_BALANCE_OF + _pad(addr)[2:], tag)
            shas.append(sha)
            amount = decode_uint(out) if out else 0
            if amount <= 0:
                continue
            (decimals, symbol), used = self.token_meta(contract)
            shas.extend(used)
            balances.append(TokenBalance(contract, amount, decimals, symbol))

        return AddressSnapshot(
            chain=self.chain, address=addr, as_of_block=head.number, as_of_time=head.time,
            native_symbol=self.info.native_symbol, native_decimals=self.info.native_decimals,
            native_balance_raw=decode_uint(native_hex), token_balances=tuple(balances),
            is_contract=is_contract, truncated=truncated, source_slug=self.source, raw_sha256=tuple(shas),
        )

    def fetch_transfers(self, address: str, limit: int = 2000) -> TransferBatch:
        addr = normalize(address)
        head = self.head().number

        native_rows, native_more = self._account(
            "txlist", addr, head, limit,
            keep=lambda row: not _failed(row) and _int(row.get("value") or 0) > 0,
            key=lambda row: row["hash"].lower())
        native = [self._native_record(row, sha) for row, sha in native_rows]

        internal_rows, internal_more = self._account(
            "txlistinternal", addr, head, limit,
            keep=lambda row: not _failed(row) and _int(row.get("value") or 0) > 0,
            key=lambda row: (row["hash"].lower(), row.get("traceId") or "", row.get("from"), _row_to(row),
                             row.get("value"), row.get("type")))
        internal = self._internal_records(internal_rows)

        token_rows, token_more = self._account(
            "tokentx", addr, head, limit, keep=lambda row: True,
            key=lambda row: (row["hash"].lower(), row.get("contractAddress", "").lower(), row.get("from"),
                             row.get("to"), row.get("value"), row.get("blockNumber")))
        self._remember_meta(row for row, _ in token_rows)
        lo = min((_int(row["blockNumber"]) for row, _ in token_rows), default=0) if token_more else 0
        logs = self._logs(addr, lo, head)
        self._count_unmatched([row for row, _ in token_rows if _int(row["blockNumber"]) >= lo], logs)
        token = [self._log_record(log, sha) for log, sha in logs]

        merged = sorted(native + internal + token, key=_sort_key, reverse=True)
        truncated = len(merged) > limit or native_more or internal_more or token_more
        kept = merged[:limit]

        if truncated:
            have = {_record_key(r) for r in kept}
            firsts = []
            firsts += self._firsts_native(addr, head, native, native_more)
            firsts += self._firsts_internal(addr, head, internal, internal_more)
            firsts += self._firsts_token(addr, head, token, token_more)
            extra = {}
            for r in firsts:
                if _record_key(r) not in have:
                    extra[_record_key(r)] = r
            kept = kept + sorted(extra.values(), key=_sort_key, reverse=True)

        return TransferBatch(self.chain, addr, tuple(self._with_decimals(kept)), truncated, limit)

    # -- first transfers ---------------------------------------------------------

    @staticmethod
    def _first_and_first_in(records: list[TransferRecord], addr: str) -> list[TransferRecord]:
        if not records:
            return []
        oldest = sorted(records, key=_sort_key)
        first_in = next((r for r in oldest if r.to_address == addr), None)
        return [oldest[0]] + ([first_in] if first_in else [])

    def _oldest_rows(self, action: str, addr: str, head: int, keep: Callable[[dict], bool]):
        r = self._request({"module": "account", "action": action, "address": addr, "startblock": 0,
                           "endblock": head, "page": 1, "offset": FIRST_ROWS, "sort": "asc"})
        rows = [self._normalize_row(action, row) for row in (r.data.get("result") or [])]
        return [(row, r.sha) for row in rows if keep(row)]

    def _firsts_native(self, addr, head, have, more):
        if not more:
            return self._first_and_first_in(have, addr)
        rows = self._oldest_rows("txlist", addr, head,
                                 lambda row: not _failed(row) and _int(row.get("value") or 0) > 0)
        return self._first_and_first_in([self._native_record(row, sha) for row, sha in rows], addr)

    def _firsts_internal(self, addr, head, have, more):
        if not more:
            return self._first_and_first_in(have, addr)
        rows = self._oldest_rows("txlistinternal", addr, head,
                                 lambda row: not _failed(row) and _int(row.get("value") or 0) > 0)
        return self._first_and_first_in(self._internal_records(rows), addr)

    def _firsts_token(self, addr, head, have, more):
        if not more:
            return self._first_and_first_in(have, addr)
        rows = self._oldest_rows("tokentx", addr, head, lambda row: True)
        self._remember_meta(row for row, _ in rows)
        if not rows:
            return []
        first_in = next((row for row, _ in rows if (row.get("to") or "").lower() == addr), None)
        blocks = sorted({_int(rows[0][0]["blockNumber"])} | ({_int(first_in["blockNumber"])} if first_in else set()))
        records = []
        for b in blocks:
            records += [self._log_record(log, sha) for log, sha in self._logs(addr, b, b)]
        return self._first_and_first_in(records, addr)

    # -- parsing -------------------------------------------------------------------

    def _native_record(self, row: dict, sha: str) -> TransferRecord:
        return TransferRecord(
            chain=self.chain, tx_hash=row["hash"].lower(), kind="native", event_index=0,
            from_address=row["from"].lower(), to_address=_row_to(row),
            token_contract=None, token_symbol=self.info.native_symbol,
            token_decimals=self.info.native_decimals, amount_raw=_int(row["value"]),
            block_number=_int(row["blockNumber"]), block_time=_ts(row["timeStamp"]),
            source_slug=self.source, raw_sha256=sha,
        )

    def _internal_records(self, rows: list[tuple[dict, str]]) -> list[TransferRecord]:
        position: dict[str, int] = defaultdict(int)
        out = []
        for row, sha in rows:
            tx = row["hash"].lower()
            index = self._internal_index(row)
            if index is None:
                index = position[tx]
            position[tx] += 1
            out.append(TransferRecord(
                chain=self.chain, tx_hash=tx, kind="internal", event_index=index,
                from_address=row["from"].lower(), to_address=_row_to(row),
                token_contract=None, token_symbol=self.info.native_symbol,
                token_decimals=self.info.native_decimals, amount_raw=_int(row["value"]),
                block_number=_int(row["blockNumber"]), block_time=_ts(row["timeStamp"]),
                source_slug=self.source, raw_sha256=sha,
            ))
        return out

    def _log_record(self, log: dict, sha: str) -> TransferRecord:
        topics = log["topics"]
        data = (log.get("data") or "0x")[2:]
        contract = log["address"].lower()
        return TransferRecord(
            chain=self.chain, tx_hash=log["transactionHash"].lower(), kind="token",
            event_index=_int(log["logIndex"]),
            from_address=_topic_address(topics[1]), to_address=_topic_address(topics[2]),
            token_contract=contract, token_symbol=None, token_decimals=0,   # filled in by _with_decimals
            amount_raw=decode_uint(data) if data else 0,
            block_number=_int(log["blockNumber"]), block_time=_ts(log["timeStamp"]),
            source_slug=self.source, raw_sha256=sha,
        )

    def _count_unmatched(self, rows: list[dict], logs: list[tuple[dict, str]]) -> None:
        have = Counter((log["transactionHash"].lower(), log["address"].lower(), _topic_address(log["topics"][1]),
                        _topic_address(log["topics"][2]), decode_uint((log.get("data") or "0x")[2:] or "0"))
                       for log, _ in logs)
        for row in rows:
            k = (row["hash"].lower(), (row.get("contractAddress") or "").lower(), (row.get("from") or "").lower(),
                 (row.get("to") or "").lower(), _int(row.get("value") or 0))
            if have[k] > 0:
                have[k] -= 1
            else:
                self.unmatched_token_rows += 1

    def _with_decimals(self, records: list[TransferRecord]) -> list[TransferRecord]:
        out = []
        for r in records:
            if r.kind != "token":
                out.append(r)
                continue
            (decimals, symbol), _ = self.token_meta(r.token_contract)
            if decimals is None:
                self.skipped_no_decimals += 1
                continue
            out.append(dataclasses.replace(r, token_decimals=decimals, token_symbol=symbol))
        return out
