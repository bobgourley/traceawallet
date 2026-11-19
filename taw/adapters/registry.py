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


# Every EVM chain is served by the Etherscan adapter, one instance per chain.
# Chains our Etherscan plan does not cover fail at the first call, with a plain message.
EVM_CHAINS = ("eth", "bsc", "polygon", "arbitrum", "optimism", "base")
SUPPORTED = ("tron",) + EVM_CHAINS

# Phase 1 coverage (docs/DECISIONS.md, 2026-11-19). A report for a 0x address
# must say which EVM chains were checked, and for each one that was not, why.
# The report template words these reason codes; nothing here is report prose.
PHASE1_CHAINS = ("eth", "polygon", "arbitrum", "tron", "btc")
PHASE1_EVM_CHECKED = ("eth", "polygon", "arbitrum")
EVM_NOT_CHECKED = {                     # chain -> reason code
    "bsc": "data_plan_excludes_chain",
    "optimism": "data_plan_excludes_chain",
    "base": "data_plan_excludes_chain",
}
NOT_CHECKED_REASONS = {
    "data_plan_excludes_chain": "Our Etherscan plan does not include this chain.",
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
    if chain in EVM_CHAINS:
        from taw.adapters.chains.evm_etherscan import EtherscanAdapter

        key = os.environ.get("ETHERSCAN_API_KEY")
        if not key:
            raise MissingSetting("ETHERSCAN_API_KEY is not set on this service")
        return EtherscanAdapter(chain, raw_store, key)
    raise _unsupported(chain)
