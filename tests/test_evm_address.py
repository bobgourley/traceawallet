import pytest

from taw.adapters.chains.evm_address import InvalidEvmAddress, is_valid, keccak256, normalize, to_checksum


def test_keccak256_vectors():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    # The ERC-20 Transfer event topic.
    assert keccak256(b"Transfer(address,address,uint256)").hex() == (
        "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
    assert keccak256(b"The quick brown fox jumps over the lazy dog").hex() == (
        "4d741b6f1eb29cb2a9b9911c82f56fa8d73b04959d3d9d222895df6c0b28aa15")


# Examples from the EIP-55 specification.
EIP55 = [
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
    "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
]


@pytest.mark.parametrize("addr", EIP55)
def test_eip55_examples(addr):
    assert normalize(addr) == addr.lower()
    assert to_checksum(addr.lower()) == addr


def test_case_without_checksum_is_accepted():
    a = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    assert normalize(a) == a
    assert normalize("0x" + a[2:].upper()) == a
    assert normalize("  " + a + "\n") == a


@pytest.mark.parametrize("bad", [
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD",    # last letter's case flipped: checksum fails
    "0xdac17f958d2ee523a2206206994597c13d831ec",     # 39 hex characters
    "dac17f958d2ee523a2206206994597c13d831ec7",      # no 0x
    "0xdac17f958d2ee523a2206206994597c13d831ecg",    # not hex
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",            # a Tron address
    "",
])
def test_bad_addresses_are_rejected(bad):
    assert not is_valid(bad)
    with pytest.raises(InvalidEvmAddress):
        normalize(bad)
