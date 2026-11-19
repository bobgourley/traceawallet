"""Chain coverage rules (docs/DECISIONS.md, 2026-11-19) and which adapter serves which chain."""
import pytest

from taw.adapters import registry
from taw.adapters.base import LocalRawStore


def test_every_evm_chain_is_either_checked_or_has_a_reason():
    checked, skipped = set(registry.PHASE1_EVM_CHECKED), set(registry.EVM_NOT_CHECKED)
    assert checked | skipped == set(registry.EVM_CHAINS)
    assert not checked & skipped
    assert set(registry.EVM_NOT_CHECKED.values()) <= set(registry.NOT_CHECKED_REASONS)


def test_phase1_chains():
    assert registry.PHASE1_CHAINS == ("eth", "polygon", "arbitrum", "optimism", "base", "tron", "btc")
    assert set(registry.EVM_NOT_CHECKED) == {"bsc"}


@pytest.mark.parametrize("chain", ["eth", "polygon", "arbitrum", "optimism", "base"])
def test_worker_uses_blockscout_for_evm(chain, tmp_path, monkeypatch):
    from taw.adapters.chains.evm_blockscout import BlockscoutAdapter
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_test_not_real")
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)              # Etherscan is not needed
    adapter = registry.adapter_for(chain, LocalRawStore(tmp_path))
    assert type(adapter) is BlockscoutAdapter and adapter.chain == chain and adapter.source == "blockscout"


def test_bsc_is_not_checked(tmp_path):
    with pytest.raises(registry.ChainNotChecked, match="bsc is not checked in Phase 1"):
        registry.adapter_for("bsc", LocalRawStore(tmp_path))
