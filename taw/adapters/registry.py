"""Which adapter serves which chain. The one place that knows."""
from __future__ import annotations

import os

from taw.adapters.base import ChainAdapter, RawStore
from taw.adapters.chains import btc_address, evm_address
from taw.adapters.chains.tron_address import to_base58


class UnsupportedChain(ValueError):
    pass


class MissingSetting(RuntimeError):
    pass


class ChainNotChecked(UnsupportedChain):
    """A known chain that Phase 1 deliberately does not check. Fails the job at once."""


# EVM chains are served by the Blockscout adapter, one instance per chain
# (docs/DECISIONS.md, 2026-11-19). The Etherscan adapter is kept for tests and
# cross-checks only: its terms do not allow a public site on our plan.
EVM_CHAINS = ("eth", "bsc", "polygon", "arbitrum", "optimism", "base")
SUPPORTED = ("tron", "btc") + EVM_CHAINS

# Phase 1 coverage (docs/DECISIONS.md, 2026-11-19). A report for a 0x address
# must say which EVM chains were checked, and for each one that was not, why.
# The report template words these reason codes; nothing here is report prose.
PHASE1_CHAINS = ("eth", "arbitrum", "optimism", "tron", "btc")
PHASE1_EVM_CHECKED = ("eth", "arbitrum", "optimism")
EVM_NOT_CHECKED = {                     # chain -> reason code
    "bsc": "no_source_with_suitable_terms",
    "polygon": "needs_paid_blockscout_plan",
    "base": "needs_paid_blockscout_plan",
}
NOT_CHECKED_REASONS = {
    "no_source_with_suitable_terms": "No data source we may use for a public site covers this chain yet.",
    "needs_paid_blockscout_plan": "It needs a paid Blockscout plan.",
}


def _unsupported(chain: str) -> UnsupportedChain:
    return UnsupportedChain(f"lookups for chain {chain!r} are not built yet (supported: {', '.join(SUPPORTED)})")


# GoPlus is never queried or shown for Bitcoin: it answers every Bitcoin address
# with all flags "0", which reads as clean (docs/DECISIONS.md, 2026-11-20).
GOPLUS_CHAINS = ("eth", "arbitrum", "optimism", "tron")


def goplus_allowed(chain: str) -> bool:
    return chain in GOPLUS_CHAINS and chain != "btc"


def normalize_address(chain: str, address: str) -> str:
    """Validate and return the stored form of an address. Raises ValueError."""
    if chain == "tron":
        return to_base58(address)
    if chain == "btc":
        return btc_address.normalize(address)
    if chain in EVM_CHAINS:
        return evm_address.normalize(address)
    raise _unsupported(chain)


def adapter_for(chain: str, raw_store: RawStore, *, state_conn=None) -> ChainAdapter:
    """state_conn: an autocommit connection for the Blockscout budget ledger and range
    store. The worker passes one; without it Blockscout calls are not metered."""
    if chain == "tron":
        from taw.adapters.chains.tron_trongrid import TronGridAdapter

        key = os.environ.get("TRONGRID_API_KEY")
        if not key:
            raise MissingSetting("TRONGRID_API_KEY is not set on this service")
        return TronGridAdapter(raw_store, key)
    if chain == "btc":
        from taw.adapters.chains.btc_esplora import BtcEsploraAdapter

        return BtcEsploraAdapter(raw_store)               # mempool.space; public API, no key
    if chain in EVM_NOT_CHECKED:
        reason = NOT_CHECKED_REASONS[EVM_NOT_CHECKED[chain]].rstrip(".")
        raise ChainNotChecked(f"{chain} is not checked in Phase 1: {reason[0].lower()}{reason[1:]}")
    if chain in PHASE1_EVM_CHECKED:
        from taw.adapters.chains.evm_blockscout import BlockscoutAdapter

        key = os.environ.get("BLOCKSCOUT_API_KEY")
        if not key:
            raise MissingSetting("BLOCKSCOUT_API_KEY is not set on this service")
        budget = ranges = None
        if state_conn is not None:
            from taw.budget import PostgresBudget, PostgresRangeStore
            budget, ranges = PostgresBudget(state_conn, "worker"), PostgresRangeStore(state_conn)
        return BlockscoutAdapter(chain, raw_store, key, budget=budget, range_store=ranges)
    raise _unsupported(chain)


def stablecoins_for(chain: str) -> tuple[str, ...]:
    """Contracts of the freezable stablecoins on a chain (identity by contract, never symbol)."""
    if chain == "tron":
        from taw.adapters.chains.tron_trongrid import STABLECOINS
        return tuple(c for c, _ in STABLECOINS)
    if chain in EVM_CHAINS:
        from taw.adapters.chains.evm_etherscan import CHAINS
        return tuple(c for c, _ in CHAINS[chain].stablecoins)
    return ()
