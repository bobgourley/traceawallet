"""Which adapter serves which chain. The one place that knows."""
from __future__ import annotations

import os

from taw.adapters.base import ChainAdapter, RawStore
from taw.adapters.chains import evm_address
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
SUPPORTED = ("tron",) + EVM_CHAINS

# Phase 1 coverage (docs/DECISIONS.md, 2026-11-19). A report for a 0x address
# must say which EVM chains were checked, and for each one that was not, why.
# The report template words these reason codes; nothing here is report prose.
PHASE1_CHAINS = ("eth", "polygon", "arbitrum", "optimism", "base", "tron", "btc")
PHASE1_EVM_CHECKED = ("eth", "polygon", "arbitrum", "optimism", "base")
EVM_NOT_CHECKED = {                     # chain -> reason code
    "bsc": "no_source_with_suitable_terms",
}
NOT_CHECKED_REASONS = {
    "no_source_with_suitable_terms": "No data source we may use for a public site covers this chain yet.",
}


def _unsupported(chain: str) -> UnsupportedChain:
    return UnsupportedChain(f"lookups for chain {chain!r} are not built yet (supported: {', '.join(SUPPORTED)})")


def normalize_address(chain: str, address: str) -> str:
    """Validate and return the stored form of an address. Raises ValueError."""
    if chain == "tron":
        return to_base58(address)
    if chain in EVM_CHAINS:
        return evm_address.normalize(address)
    raise _unsupported(chain)


def adapter_for(chain: str, raw_store: RawStore) -> ChainAdapter:
    if chain == "tron":
        from taw.adapters.chains.tron_trongrid import TronGridAdapter

        key = os.environ.get("TRONGRID_API_KEY")
        if not key:
            raise MissingSetting("TRONGRID_API_KEY is not set on this service")
        return TronGridAdapter(raw_store, key)
    if chain in EVM_NOT_CHECKED:
        raise ChainNotChecked(f"{chain} is not checked in Phase 1: "
                              f"{NOT_CHECKED_REASONS[EVM_NOT_CHECKED[chain]].rstrip('.').lower()}")
    if chain in PHASE1_EVM_CHECKED:
        from taw.adapters.chains.evm_blockscout import BlockscoutAdapter

        key = os.environ.get("BLOCKSCOUT_API_KEY")
        if not key:
            raise MissingSetting("BLOCKSCOUT_API_KEY is not set on this service")
        return BlockscoutAdapter(chain, raw_store, key)
    raise _unsupported(chain)
