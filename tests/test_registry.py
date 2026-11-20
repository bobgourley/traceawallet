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
    assert registry.PHASE1_CHAINS == ("eth", "arbitrum", "optimism", "tron", "btc")
    assert registry.EVM_NOT_CHECKED == {"bsc": "no_source_with_suitable_terms",
                                        "polygon": "needs_paid_blockscout_plan",
                                        "base": "needs_paid_blockscout_plan"}


@pytest.mark.parametrize("chain", ["eth", "arbitrum", "optimism"])
def test_worker_uses_blockscout_for_evm(chain, tmp_path, monkeypatch):
    from taw.adapters.chains.evm_blockscout import BlockscoutAdapter
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_test_not_real")
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)              # Etherscan is not needed
    adapter = registry.adapter_for(chain, LocalRawStore(tmp_path))
    assert type(adapter) is BlockscoutAdapter and adapter.chain == chain and adapter.source == "blockscout"


@pytest.mark.parametrize("chain", ["bsc", "polygon", "base"])
def test_not_checked_chains_never_reach_a_provider(chain, tmp_path, monkeypatch):
    """Not routed to Etherscan or anywhere else, even with every key set."""
    monkeypatch.setenv("BLOCKSCOUT_API_KEY", "proapi_test_not_real")
    monkeypatch.setenv("ETHERSCAN_API_KEY", "etherscan_test_not_real")
    with pytest.raises(registry.ChainNotChecked, match=f"{chain} is not checked in Phase 1"):
        registry.adapter_for(chain, LocalRawStore(tmp_path))


def test_goplus_is_never_used_for_bitcoin():
    assert registry.goplus_allowed("btc") is False
    assert "btc" not in registry.GOPLUS_CHAINS
    assert registry.goplus_allowed("eth") is True


def test_btc_needs_no_key(tmp_path, monkeypatch):
    from taw.adapters.chains.btc_esplora import BtcEsploraAdapter
    for name in ("BLOCKSCOUT_API_KEY", "ETHERSCAN_API_KEY", "TRONGRID_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    adapter = registry.adapter_for("btc", LocalRawStore(tmp_path))
    assert type(adapter) is BtcEsploraAdapter and adapter.source == "mempool-space"
    assert registry.normalize_address("btc", "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4") == \
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
