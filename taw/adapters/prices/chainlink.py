"""USD prices from Chainlink price feeds, read on-chain through Blockscout.

docs/research/price-sources.md has the research; DECISIONS.md (2026-11-20,
"Prices: Chainlink at bucket starts") has the rules:

- Six feeds, all 8 decimals: ETH, BTC, DAI, USDT and USDC on Ethereum, TRX on OP
  Mainnet. Addresses, names and decimals were read from the contracts on
  2026-11-20 (description() and decimals()).
- A stablecoin is priced from its own feed like any other asset, never assumed
  to be $1.
- Time is bucketed: hourly for the last 48 hours, daily before that. A bucket's
  price is latestRoundData() read at the last block at or before the bucket's
  start, so it never uses anything that happened after the bucket began.
- A read gives no price when the feed did not exist yet (empty or reverted
  call), when its round is older than the feed's heartbeat plus a quarter, or
  when the answer is not positive. That outcome is recorded with its reason.

Every call goes through a BlockscoutAdapter, so it is paced, saved to the raw
store (rule 3) and counted in the Blockscout budget ledger. Cost: one
block-at-time lookup per chain and bucket, plus one eth_call per asset.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

SOURCE = "chainlink"
SEL_LATEST_ROUND_DATA = "0xfeaf968c"     # latestRoundData()
HOURLY_FOR = dt.timedelta(hours=48)


@dataclass(frozen=True)
class Feed:
    asset: str
    chain: str          # where the feed lives (our chain id)
    address: str        # proxy, lower case
    heartbeat: int      # seconds, from Chainlink's directory on 2026-11-20
    decimals: int = 8

    @property
    def max_age(self) -> int:
        """Older than this at the read block and the round is stale."""
        return self.heartbeat + self.heartbeat // 4


FEEDS: dict[str, Feed] = {f.asset: f for f in (
    Feed("ETH", "eth", "0x5f4ec3df9cbd43714fe2740f5e3616155c5b8419", 3600),
    Feed("BTC", "eth", "0xf4030086522a5beea4988f8ca5b36dbc97bee88c", 3600),
    Feed("DAI", "eth", "0xaed0c38402a5d19df6e4c03f4e2dced6e29c1ee9", 3600),
    Feed("USDT", "eth", "0x3e7d1eab13ad0104d2750b8863b489d65364e32d", 86400),
    Feed("USDC", "eth", "0x8fffffd4afb6115b954bd326cbe7b4ba576818f6", 82800),
    Feed("TRX", "optimism", "0x0e09921cf7801a5ad47b892c8727593275625a9f", 86400),
)}


def bucket_for(when: dt.datetime, now: dt.datetime) -> tuple[dt.datetime, str]:
    """(bucket start, 'hour' or 'day'): hourly within the last 48 hours of `now`."""
    when = when.astimezone(dt.timezone.utc)
    if now - when < HOURLY_FOR:
        return when.replace(minute=0, second=0, microsecond=0), "hour"
    return when.replace(hour=0, minute=0, second=0, microsecond=0), "day"


@dataclass(frozen=True)
class PriceRead:
    asset: str
    bucket_start: dt.datetime
    bucket_size: str
    status: str                       # ok, stale, no_feed, invalid
    usd: Decimal | None
    feed: Feed
    block_number: int
    round_id: int | None
    updated_at: dt.datetime | None
    age_seconds: int | None
    raw_sha256: str                   # the latestRoundData response
    block_raw_sha256: str             # the block-at-time response
    served_by: str = "blockscout"     # the raw responses' source


def _word(hex_out: str, i: int, signed: bool = False) -> int:
    v = int(hex_out[i * 64:(i + 1) * 64], 16)
    if signed and v >= 1 << 255:
        v -= 1 << 256
    return v


def judge(feed: Feed, out: str | None, bucket_start: dt.datetime) -> tuple[str, Decimal | None, int | None,
                                                                         dt.datetime | None, int | None]:
    """(status, usd, round, updatedAt, age) from latestRoundData's output (hex, no 0x)."""
    if out is None or len(out) < 5 * 64:
        return "no_feed", None, None, None, None
    round_id, answer, updated = _word(out, 0), _word(out, 1, signed=True), _word(out, 3)
    if updated == 0:
        return "no_feed", None, round_id, None, None
    updated_at = dt.datetime.fromtimestamp(updated, dt.timezone.utc)
    age = max(0, int((bucket_start - updated_at).total_seconds()))
    if answer <= 0:
        return "invalid", None, round_id, updated_at, age
    if age > feed.max_age:
        return "stale", None, round_id, updated_at, age
    return "ok", Decimal(answer).scaleb(-feed.decimals), round_id, updated_at, age


class ChainlinkReader:
    """Reads feeds through Blockscout adapters: adapter_for(chain) -> BlockscoutAdapter."""

    def __init__(self, adapter_for: Callable[[str], object]):
        self._adapter_for = adapter_for
        self._adapters: dict[str, object] = {}
        self._blocks: dict[tuple[str, int], tuple[int, str]] = {}

    def _adapter(self, chain: str):
        if chain not in self._adapters:
            self._adapters[chain] = self._adapter_for(chain)
        return self._adapters[chain]

    def block_at(self, chain: str, when: dt.datetime) -> tuple[int, str]:
        """(last block at or before `when`, sha of the response). One call, then cached."""
        ts = int(when.timestamp())
        if (chain, ts) not in self._blocks:
            r = self._adapter(chain)._request({"module": "block", "action": "getblocknobytime",
                                               "timestamp": ts, "closest": "before"})
            result = r.data.get("result")
            number = result.get("blockNumber") if isinstance(result, dict) else result
            self._blocks[(chain, ts)] = (int(str(number)), r.sha)
        return self._blocks[(chain, ts)]

    def read(self, asset: str, bucket_start: dt.datetime, bucket_size: str) -> PriceRead:
        feed = FEEDS[asset]
        block, block_sha = self.block_at(feed.chain, bucket_start)
        out, sha = self._adapter(feed.chain)._eth_call(feed.address, SEL_LATEST_ROUND_DATA, hex(block))
        status, usd, round_id, updated_at, age = judge(feed, out, bucket_start)
        return PriceRead(asset, bucket_start, bucket_size, status, usd, feed, block, round_id, updated_at, age,
                         sha, block_sha)
