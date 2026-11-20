"""Stablecoin freeze status, read from the token contracts themselves.

The function names were confirmed on 2026-11-20 from each contract's published
code: verified source on Etherscan and Blockscout for the EVM contracts (the
implementation behind each proxy), and the on-chain ABI through TronGrid for Tron.

  chain     token  contract        status function           freeze / unfreeze events
  eth       USDT   TetherToken     isBlackListed(address)    AddedBlackList / RemovedBlackList, address NOT indexed
  eth       USDC   FiatTokenV2_2   isBlacklisted(address)    Blacklisted / UnBlacklisted, address indexed
  arbitrum  USDT0  ArbitrumExtV2   isBlocked(address)        BlockPlaced / BlockReleased, address indexed
  arbitrum  USDC   FiatTokenV2_2   isBlacklisted(address)    Blacklisted / UnBlacklisted, address indexed
  optimism  USDC   FiatTokenV2_2   isBlacklisted(address)    Blacklisted / UnBlacklisted, address indexed
  tron      USDT   TetherToken     isBlackListed(address)    AddedBlackList / RemovedBlackList
  tron      USDC   FiatTokenProxy  isBlacklisted(address)    Blacklisted / UnBlacklisted

Status is read at the snapshot block on EVM chains (eth_call through Blockscout,
metered). TronGrid has no calls at a past block, so on Tron it is read at the
current head right after the snapshot; the snapshot block is recorded beside it.

The freeze date is read only where it is cheap: where the event indexes the
address, one eth_getLogs for the address's freeze and unfreeze events up to the
snapshot block, plus one block read for the time. That is USDC on the three EVM
chains and USDT0 on Arbitrum. Ethereum USDT's events don't index the address,
and TronGrid can't filter events by address (Tron USDT logs thousands of
freezes), so those dates are not read.

A frozen address gets a label worded as the issuer's action (rule 9): "frozen by
Tether: the USDT contract on Ethereum reports isBlackListed = true".
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from taw.adapters.chains.tron_address import to_hex
from taw.adapters.labels.base import LabelClaim

_SEL = {"isBlackListed": "0xe47d6060", "isBlacklisted": "0xfe575a87", "isBlocked": "0xfbac3951"}
_TOPICS = {
    "usdc": ("0xffa4e6181777692565cf28528fc88fd1516ea86b56da075235fa575af6a4b855",    # Blacklisted(address)
             "0x117e3210bb9aa7d9baff172026820255c6f6c30ba8999d1c2fd88e2848137c4e"),   # UnBlacklisted(address)
    "usdt0": ("0x406bbf2d8d145125adf1198d2cf8a67c66cc4bb0ab01c37dccd4f7c0aae1e7c7",   # BlockPlaced(address)
              "0x665918c9e02eb2fd85acca3969cb054fc84c138e60ec4af22ab6ef2fd4c93c27"),  # BlockReleased(address)
}
CHAIN_NAMES = {"eth": "Ethereum", "arbitrum": "Arbitrum One", "optimism": "OP Mainnet", "tron": "Tron"}


@dataclass(frozen=True)
class FreezeContract:
    chain: str
    token: str
    contract: str
    issuer: str
    source: str                         # source slug of the label
    function: str
    events: tuple[str, str] | None      # (freeze, unfreeze) topics when the address is indexed

    @property
    def date_readable(self) -> bool:
        return self.events is not None


CONTRACTS: tuple[FreezeContract, ...] = (
    FreezeContract("eth", "USDT", "0xdac17f958d2ee523a2206206994597c13d831ec7", "Tether", "usdt-contract",
                   "isBlackListed", None),
    FreezeContract("eth", "USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "Circle", "usdc-contract",
                   "isBlacklisted", _TOPICS["usdc"]),
    FreezeContract("arbitrum", "USDT0", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "Tether", "usdt-contract",
                   "isBlocked", _TOPICS["usdt0"]),
    FreezeContract("arbitrum", "USDC", "0xaf88d065e77c8cc2239327c5edb3a432268e5831", "Circle", "usdc-contract",
                   "isBlacklisted", _TOPICS["usdc"]),
    FreezeContract("optimism", "USDC", "0x0b2c639c533813f4aa9d7837caf62653d097ff85", "Circle", "usdc-contract",
                   "isBlacklisted", _TOPICS["usdc"]),
    FreezeContract("tron", "USDT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "Tether", "usdt-contract",
                   "isBlackListed", None),
    FreezeContract("tron", "USDC", "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8", "Circle", "usdc-contract",
                   "isBlacklisted", None),
)


def contracts_for(chain: str) -> list[FreezeContract]:
    return [c for c in CONTRACTS if c.chain == chain]


@dataclass(frozen=True)
class FreezeCheck:
    contract: FreezeContract
    address: str
    frozen: bool | None                 # None: the call gave no answer
    block: int | None                   # the block the status is for (None on Tron: current head)
    raw_sha256: str
    frozen_since_block: int | None = None
    frozen_since: dt.datetime | None = None
    date_note: str = ""
    extra_sha256: tuple[str, ...] = ()

    @property
    def result(self) -> str:
        return {True: "frozen", False: "not_frozen", None: "no_answer"}[self.frozen]

    def claim(self) -> LabelClaim | None:
        if not self.frozen:
            return None
        c = self.contract
        text = (f"frozen by {c.issuer}: the {c.token} contract on {CHAIN_NAMES[c.chain]} ({c.contract}) reports "
                f"{c.function} = true for this address")
        return LabelClaim(chain=c.chain, address=self.address, category="frozen", label=text,
                          claim_key=f"freeze:{c.chain}:{c.contract}",
                          source_date=self.frozen_since.date() if self.frozen_since else None,
                          raw_sha256=self.raw_sha256)

    def detail(self) -> dict:
        return {"token": self.contract.token, "function": self.contract.function, "block": self.block,
                "frozen_since_block": self.frozen_since_block,
                "frozen_since": self.frozen_since.isoformat() if self.frozen_since else None,
                "date_note": self.date_note}


def _word_bool(out: str | None) -> bool | None:
    if not out or len(out) < 64:
        return None
    return int(out[:64], 16) != 0


def _pad(hex40: str) -> str:
    return hex40.lower().removeprefix("0x").rjust(64, "0")


def check_evm(adapter, contract: FreezeContract, address: str, block: int) -> FreezeCheck:
    """adapter: a BlockscoutAdapter for the chain (metered). address: 0x... lower case."""
    out, sha = adapter._eth_call(contract.contract, _SEL[contract.function] + _pad(address), hex(block))
    frozen = _word_bool(out)
    check = FreezeCheck(contract, address, frozen, block, sha)
    if not frozen:
        return check
    if not contract.date_readable:
        return FreezeCheck(contract, address, frozen, block, sha,
                           date_note="not read: this contract's freeze event does not index the address")
    freeze, unfreeze = contract.events
    r = adapter._rpc("eth_getLogs", [{"address": contract.contract, "fromBlock": "0x0", "toBlock": hex(block),
                                      "topics": [[freeze, unfreeze], "0x" + _pad(address)]}])
    logs = sorted(r.data.get("result") or [], key=lambda g: (int(g["blockNumber"], 16), int(g.get("logIndex", "0x0"), 16)))
    last = logs[-1] if logs else None
    if last is None or last["topics"][0].lower() != freeze:
        return FreezeCheck(contract, address, frozen, block, sha, extra_sha256=(r.sha,),
                           date_note="no freeze event found for this address up to the snapshot block")
    since_block = int(last["blockNumber"], 16)
    head, block_sha = adapter._block(since_block)
    since = dt.datetime.fromtimestamp(int(head["timestamp"], 16), dt.timezone.utc) if head.get("timestamp") else None
    return FreezeCheck(contract, address, frozen, block, sha, since_block, since,
                       date_note="from the contract's last freeze event for this address",
                       extra_sha256=(r.sha, block_sha))


def check_tron(adapter, contract: FreezeContract, address: str) -> FreezeCheck:
    """adapter: a TronGridAdapter. Read at the current head (TronGrid has no past-block calls)."""
    out, sha = adapter._constant_call(contract.contract, f"{contract.function}(address)", _pad(to_hex(address)[2:]))
    return FreezeCheck(contract, address, _word_bool(out), None, sha,
                       date_note="not read: TronGrid cannot filter freeze events by address")
