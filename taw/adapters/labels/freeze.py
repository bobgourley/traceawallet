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

Added 2026-11-21 (docs/research/more-sources.md). Each function and event was read
from the verified ABI of the implementation behind the proxy (Etherscan), and each
status function was called at the latest block: false for an unused address and
true for an address taken from that token's own freeze events.

  eth       PYUSD  Paxos           isFrozen(address)         FreezeAddress / UnfreezeAddress, address indexed
  eth       USDP   Paxos           isFrozen(address)         FreezeAddress / UnfreezeAddress, address indexed
  eth       USDG   Paxos           isFrozen(address)         FreezeAddress / UnfreezeAddress, address indexed
  eth       FDUSD  First Digital   frozen(address)           Freeze / Unfreeze (caller, account): account is
                                                             the SECOND indexed topic
  eth       USD1   USD1 issuer     frozen(address)           Freeze / Unfreeze (caller, account), the same
  eth       RLUSD  RLUSD issuer    accountPaused(address)    AccountPaused / AccountUnpaused, address NOT
                                                             indexed: no date
  eth       EURC   Circle          isBlacklisted(address)    Blacklisted / UnBlacklisted, address indexed
  arbitrum  PYUSD  Paxos           isFrozen(address)         FreezeAddress / UnfreezeAddress, address indexed
                                                             (no freeze event yet on 2026-11-21)
  optimism  USDT0  Tether          isBlocked(address)        not read: the public OP endpoint could not
                                                             confirm the events, so no date is claimed

USD1 and RLUSD name no issuer in their contracts that we have confirmed, so the
wording names the token's issuer without a company. RLUSD's contract calls the
action a pause, and the report uses that word.

Status is read at the snapshot block on EVM chains (eth_call through Blockscout,
metered). TronGrid has no calls at a past block, so on Tron it is read at the
current head right after the snapshot; the snapshot block is recorded beside it.

Freeze dates. Where the event indexes the address (USDC on the three EVM chains,
USDT0 on Arbitrum), one eth_getLogs for the address's freeze and unfreeze events
up to the snapshot block, plus one block read for the time. Ethereum USDT's
events don't index the address and TronGrid can't filter events by address, so
for Ethereum USDT and Tron USDT and USDC the date comes from the global index of
blacklist events (taw/blacklist.py): free, plus one block read on Ethereum when
the log carried no time. The note says how far the index was complete.

A frozen address gets a label worded as the issuer's action (rule 9): "frozen by
Tether: the USDT contract on Ethereum reports isBlackListed = true".
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable

from taw.adapters.chains.tron_address import to_hex
from taw.adapters.labels.base import LabelClaim

_SEL = {"isBlackListed": "0xe47d6060", "isBlacklisted": "0xfe575a87", "isBlocked": "0xfbac3951",
        "isFrozen": "0xe5839836", "frozen": "0xd0516650", "accountPaused": "0xbc8c4b4f"}
_TOPICS = {
    "usdc": ("0xffa4e6181777692565cf28528fc88fd1516ea86b56da075235fa575af6a4b855",    # Blacklisted(address)
             "0x117e3210bb9aa7d9baff172026820255c6f6c30ba8999d1c2fd88e2848137c4e"),   # UnBlacklisted(address)
    "usdt0": ("0x406bbf2d8d145125adf1198d2cf8a67c66cc4bb0ab01c37dccd4f7c0aae1e7c7",   # BlockPlaced(address)
              "0x665918c9e02eb2fd85acca3969cb054fc84c138e60ec4af22ab6ef2fd4c93c27"),  # BlockReleased(address)
    "paxos": ("0x1aa660498c83ea285bc55e4cfc00afcaa7120798db87b74f3c0d7c6e001bc392",   # FreezeAddress(address)
              "0x150465b020dfc06a59269da94ed66db9b65a516cf4fdd5f583b0f12752339bbe"),  # UnfreezeAddress(address)
    "freeze2": ("0x51d18786e9cb144f87d46e7b796309ea84c7c687d91e09c97f051eacf59bc528",  # Freeze(address,address)
                "0x4f3ab9ff0cc4f039268532098e01239544b0420171876e36889d01c62c784c79"),  # Unfreeze(address,address)
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
    topic_position: int = 1             # which indexed topic holds the address (FDUSD, USD1: 2)
    action: str = "frozen"              # the contract's own word: "paused" for RLUSD
    always_shown: bool = True           # False: in reports only when frozen, unanswered, held or moved
    removal: str | None = None          # what else the contract lets the issuer do with a frozen balance

    @property
    def date_readable(self) -> bool:
        return self.events is not None


CONTRACTS: tuple[FreezeContract, ...] = (
    FreezeContract("eth", "USDT", "0xdac17f958d2ee523a2206206994597c13d831ec7", "Tether", "usdt-contract",
                   "isBlackListed", None, removal="destroy it"),
    FreezeContract("eth", "USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "Circle", "usdc-contract",
                   "isBlacklisted", _TOPICS["usdc"]),
    FreezeContract("arbitrum", "USDT0", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", "Tether", "usdt-contract",
                   "isBlocked", _TOPICS["usdt0"], removal="destroy it"),
    FreezeContract("arbitrum", "USDC", "0xaf88d065e77c8cc2239327c5edb3a432268e5831", "Circle", "usdc-contract",
                   "isBlacklisted", _TOPICS["usdc"]),
    FreezeContract("optimism", "USDC", "0x0b2c639c533813f4aa9d7837caf62653d097ff85", "Circle", "usdc-contract",
                   "isBlacklisted", _TOPICS["usdc"]),
    FreezeContract("eth", "PYUSD", "0x6c3ea9036406852006290770bedfcaba0e23a0e8", "Paxos", "paxos-contracts",
                   "isFrozen", _TOPICS["paxos"], always_shown=False,
                   removal="wipe it from the address"),
    FreezeContract("eth", "USDP", "0x8e870d67f660d95d5be530380d0ec0bd388289e1", "Paxos", "paxos-contracts",
                   "isFrozen", _TOPICS["paxos"], always_shown=False,
                   removal="wipe it from the address"),
    FreezeContract("eth", "USDG", "0xe343167631d89b6ffc58b88d6b7fb0228795491d", "Paxos", "paxos-contracts",
                   "isFrozen", _TOPICS["paxos"], always_shown=False,
                   removal="wipe it from the address"),
    FreezeContract("eth", "FDUSD", "0xc5f0f7b66764f6ec8c8dff7ba683102295e16409", "First Digital", "fdusd-contract",
                   "frozen", _TOPICS["freeze2"], topic_position=2, always_shown=False),
    FreezeContract("eth", "USD1", "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d", "the USD1 issuer", "usd1-contract",
                   "frozen", _TOPICS["freeze2"], topic_position=2, always_shown=False,
                   removal="move it to another address"),
    FreezeContract("eth", "RLUSD", "0x8292bb45bf1ee4d140127049757c2e0ff06317ed", "the RLUSD issuer", "rlusd-contract",
                   "accountPaused", None, action="paused", always_shown=False),
    FreezeContract("eth", "EURC", "0x1abaea1f7c830bd89acc67ec4af516284b1bc33c", "Circle", "usdc-contract",
                   "isBlacklisted", _TOPICS["usdc"], always_shown=False),
    FreezeContract("arbitrum", "PYUSD", "0x46850ad61c2b7d64d08c9c754f45254596696984", "Paxos", "paxos-contracts",
                   "isFrozen", _TOPICS["paxos"], always_shown=False,
                   removal="wipe it from the address"),
    FreezeContract("optimism", "USDT0", "0x01bff41798a0bcf287b996046ca68b395dbc1071", "Tether", "usdt-contract",
                   "isBlocked", None, always_shown=False, removal="destroy it"),
    FreezeContract("tron", "USDT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "Tether", "usdt-contract",
                   "isBlackListed", None, removal="destroy it"),
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
    frozen_since_tx: str | None = None  # the freeze event's transaction, when the date was found

    @property
    def result(self) -> str:
        return {True: "frozen", False: "not_frozen", None: "no_answer"}[self.frozen]

    def claim(self) -> LabelClaim | None:
        if not self.frozen:
            return None
        c = self.contract
        text = (f"{c.action} by {c.issuer}: the {c.token} contract on {CHAIN_NAMES[c.chain]} ({c.contract}) reports "
                f"{c.function} = true for this address")
        return LabelClaim(chain=c.chain, address=self.address, category="frozen", label=text,
                          claim_key=f"freeze:{c.chain}:{c.contract}",
                          source_date=self.frozen_since.date() if self.frozen_since else None,
                          raw_sha256=self.raw_sha256)

    def detail(self) -> dict:
        return {"token": self.contract.token, "function": self.contract.function, "block": self.block,
                "frozen_since_block": self.frozen_since_block,
                "frozen_since": self.frozen_since.isoformat() if self.frozen_since else None,
                "frozen_since_tx": self.frozen_since_tx, "date_note": self.date_note}


def _word_bool(out: str | None) -> bool | None:
    if not out or len(out) < 64:
        return None
    return int(out[:64], 16) != 0


def _pad(hex40: str) -> str:
    return hex40.lower().removeprefix("0x").rjust(64, "0")


def check_evm(adapter, contract: FreezeContract, address: str, block: int, index: Index | None = None) -> FreezeCheck:
    """adapter: a BlockscoutAdapter for the chain (metered). address: 0x... lower case.
    index(contract, address, block) -> taw.blacklist.FreezeFound, for contracts whose
    events don't index the address."""
    out, sha = adapter._eth_call(contract.contract, _SEL[contract.function] + _pad(address), hex(block))
    frozen = _word_bool(out)
    check = FreezeCheck(contract, address, frozen, block, sha)
    if not frozen:
        return check
    if not contract.date_readable:
        return _from_index(contract, address, frozen, block, sha, index, adapter)
    freeze, unfreeze = contract.events
    r = adapter._rpc("eth_getLogs", [{"address": contract.contract, "fromBlock": "0x0", "toBlock": hex(block),
                                      "topics": [[freeze, unfreeze]] + [None] * (contract.topic_position - 1)
                                      + ["0x" + _pad(address)]}])
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
                       extra_sha256=(r.sha, block_sha), frozen_since_tx=last.get("transactionHash"))


def check_tron(adapter, contract: FreezeContract, address: str, index: Index | None = None) -> FreezeCheck:
    """adapter: a TronGridAdapter. Read at the current head (TronGrid has no past-block calls)."""
    out, sha = adapter._constant_call(contract.contract, f"{contract.function}(address)", _pad(to_hex(address)[2:]))
    frozen = _word_bool(out)
    if not frozen:
        return FreezeCheck(contract, address, frozen, None, sha)
    return _from_index(contract, address, frozen, None, sha, index, None)


Index = Callable[[FreezeContract, str, "int | None"], object]


def _from_index(contract, address, frozen, block, sha, index, adapter) -> FreezeCheck:
    """The freeze date from the global index of blacklist events (taw/blacklist.py)."""
    found = index(contract, address, block) if index else None
    if found is None or found.covered_to_block is None:
        return FreezeCheck(contract, address, frozen, block, sha,
                           date_note="not read: the index of this contract's freeze events is not available")
    upto = f"the index of the contract's freeze events, complete to block {found.covered_to_block:,}"
    e = found.event
    if e is None:
        return FreezeCheck(contract, address, frozen, block, sha,
                           date_note=f"no freeze event for this address in {upto}")
    when, extra = e.block_time, ()
    if when is None and adapter is not None:
        head, block_sha = adapter._block(e.block_number)
        when = dt.datetime.fromtimestamp(int(head["timestamp"], 16), dt.timezone.utc) if head.get("timestamp") else None
        extra = (block_sha,)
    return FreezeCheck(contract, address, frozen, block, sha, e.block_number, when,
                       date_note=f"from the last freeze event for this address (transaction {e.tx_hash}) in {upto}",
                       extra_sha256=extra, frozen_since_tx=e.tx_hash)
