import pytest

from taw.adapters.chains.tron_address import InvalidTronAddress, is_valid, to_base58, to_hex

USDT_B58 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_HEX = "41a614f803b6fd780986a42c78ec9c7f77e6ded13c"


def test_hex_forms_convert_to_base58():
    assert to_base58(USDT_HEX) == USDT_B58
    assert to_base58(USDT_HEX.upper()) == USDT_B58
    assert to_base58("0x" + USDT_HEX[2:]) == USDT_B58          # event form, 0x41 implied
    assert to_base58("0x" + USDT_HEX) == USDT_B58              # 0x + full 21 bytes


def test_base58_round_trip():
    assert to_base58(USDT_B58) == USDT_B58
    assert to_hex(USDT_B58) == USDT_HEX
    assert to_base58("  " + USDT_B58 + " ") == USDT_B58


@pytest.mark.parametrize("bad", [
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u",    # last character changed: checksum fails
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6",     # too short
    "0xdac17f958d2ee523a2206206994597c13d831ec",  # 39 hex characters
    "42a614f803b6fd780986a42c78ec9c7f77e6ded13c",  # wrong prefix byte
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj0t",    # 0 is not base58
    "",
])
def test_bad_addresses_are_rejected(bad):
    assert not is_valid(bad)
    with pytest.raises(InvalidTronAddress):
        to_base58(bad)
