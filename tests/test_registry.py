"""Chain coverage rules (docs/DECISIONS.md, 2026-11-19)."""
from taw.adapters import registry


def test_every_evm_chain_is_either_checked_or_has_a_reason():
    checked, skipped = set(registry.PHASE1_EVM_CHECKED), set(registry.EVM_NOT_CHECKED)
    assert checked | skipped == set(registry.EVM_CHAINS)
    assert not checked & skipped
    assert set(registry.EVM_NOT_CHECKED.values()) <= set(registry.NOT_CHECKED_REASONS)


def test_phase1_chains():
    assert registry.PHASE1_CHAINS == ("eth", "polygon", "arbitrum", "tron", "btc")
    assert set(registry.EVM_NOT_CHECKED) == {"bsc", "optimism", "base"}
