"""Bitcoin address checks, against the published BIP-173 and BIP-350 test vectors."""
import pytest

from taw.adapters.chains.btc_address import InvalidBitcoinAddress, is_valid, kind, normalize

VALID = [
    ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "p2pkh"),                                  # the genesis address
    ("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", "p2sh"),                                   # BIP-13 example
    ("BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4", "p2wpkh"),                         # BIP-173
    ("bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3", "p2wsh"),      # BIP-173
    ("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0", "p2tr"),       # BIP-350
    ("BC1SW50QGDZ25J", "witness_v16"),                                                # BIP-350
]


@pytest.mark.parametrize("address, expected_kind", VALID)
def test_valid_addresses(address, expected_kind):
    assert is_valid(address) and kind(address) == expected_kind


def test_stored_forms():
    assert normalize("BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4") == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    assert normalize(" 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa ") == "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


@pytest.mark.parametrize("bad", [
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb",                                     # base58 checksum fails
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7Divf0a",                                     # 0 is not base58
    "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5",                             # bech32 checksum fails
    "bc1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4",                             # mixed case
    "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqh2y7hd",         # BIP-350: v1 with bech32 checksum
    "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kemeawh",                             # BIP-350: v0 with bech32m checksum
    "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",                             # testnet
    "mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn",                                     # testnet P2PKH
    "0xdac17f958d2ee523a2206206994597c13d831ec7",                             # an EVM address
    "",
])
def test_bad_addresses_are_rejected(bad):
    assert not is_valid(bad)
    with pytest.raises(InvalidBitcoinAddress):
        normalize(bad)
