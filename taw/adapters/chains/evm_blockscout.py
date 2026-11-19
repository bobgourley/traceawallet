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
  getLogs       At most 1,000 logs per query, so the logs window is 1,000: after a
                full answer the block edge moves to the last block seen.
  Limits        Free tier 5 requests a second and 100,000 credits a day (about 5,000
                calls); paced at 4 a second.

BNB Smart Chain has no Blockscout instance and is not handled here.
"""
from __future__ import annotations

from typing import Any

from taw.adapters.chains import evm_etherscan as es
from taw.adapters.chains.evm_etherscan import EtherscanAdapter, EtherscanError, _int, trace_index

SOURCE = "blockscout"
API_URL = "https://api.blockscout.com/v2/api"
RPC_URL = "https://api.blockscout.com/{chain_id}/json-rpc"
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

    def __init__(self, chain: str, raw_store, api_key: str | None, *, rpc_url: str = RPC_URL, **kw):
        if chain not in CHAINS:
            raise ValueError(f"Blockscout does not serve chain {chain!r}")
        super().__init__(chain, raw_store, api_key, **kw)
        self.rpc_url = rpc_url.format(chain_id=self.info.chain_id)

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
