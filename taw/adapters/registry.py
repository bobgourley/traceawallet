"""Which adapter serves which chain. The one place that knows."""
from __future__ import annotations

import os

from taw.adapters.base import ChainAdapter, RawStore
from taw.adapters.chains.tron_address import to_base58


class UnsupportedChain(ValueError):
    pass


class MissingSetting(RuntimeError):
    pass


SUPPORTED = ("tron",)


def normalize_address(chain: str, address: str) -> str:
    """Validate and return the stored form of an address. Raises ValueError."""
    if chain == "tron":
        return to_base58(address)
    raise UnsupportedChain(f"lookups for chain {chain!r} are not built yet (supported: {', '.join(SUPPORTED)})")


def adapter_for(chain: str, raw_store: RawStore) -> ChainAdapter:
    if chain == "tron":
        from taw.adapters.chains.tron_trongrid import TronGridAdapter

        key = os.environ.get("TRONGRID_API_KEY")
        if not key:
            raise MissingSetting("TRONGRID_API_KEY is not set on this service")
        return TronGridAdapter(raw_store, key)
    raise UnsupportedChain(f"lookups for chain {chain!r} are not built yet (supported: {', '.join(SUPPORTED)})")
