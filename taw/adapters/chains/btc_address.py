"""Bitcoin mainnet address forms, with their checksums verified.

  P2PKH    1...         base58check, version byte 0x00
  P2SH     3...         base58check, version byte 0x05
  P2WPKH   bc1q... (42) bech32 (BIP-173), witness v0, 20-byte program
  P2WSH    bc1q... (62) bech32 (BIP-173), witness v0, 32-byte program
  P2TR     bc1p...      bech32m (BIP-350), witness v1, 32-byte program
  (future) bc1...       bech32m, witness v2 to v16, 2 to 40 bytes

Stored form: base58 addresses as typed (case matters); bech32 addresses lower
case, as the address_normalize trigger expects. Mixed-case bech32 is refused
(BIP-173). Testnet and other networks are refused.
"""
from __future__ import annotations

import hashlib

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_INDEX = {c: i for i, c in enumerate(_BECH32)}
_BECH32_CONST, _BECH32M_CONST = 1, 0x2BC830A3


class InvalidBitcoinAddress(ValueError):
    pass


def _base58check(text: str) -> bytes:
    n = 0
    for c in text:
        if c not in _B58_INDEX:
            raise InvalidBitcoinAddress(f"not a base58 character: {c!r}")
        n = n * 58 + _B58_INDEX[c]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\0" * (len(text) - len(text.lstrip("1"))) + raw
    if len(raw) != 25:
        raise InvalidBitcoinAddress("a base58 address decodes to 25 bytes")
    payload, check = raw[:21], raw[21:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != check:
        raise InvalidBitcoinAddress("checksum mismatch: check the address for a typo")
    return payload


def _polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data: list[int], frombits: int, tobits: int) -> list[int] | None:
    acc = bits = 0
    out = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return out


def _segwit(text: str) -> tuple[int, bytes]:
    if text != text.lower() and text != text.upper():
        raise InvalidBitcoinAddress("bech32 addresses may not mix upper and lower case")
    low = text.lower()
    pos = low.rfind("1")
    hrp, data_part = low[:pos], low[pos + 1:]
    if hrp != "bc":
        raise InvalidBitcoinAddress("not a Bitcoin mainnet address (expected bc1...)")
    if len(low) > 90 or len(data_part) < 7 or any(c not in _BECH32_INDEX for c in data_part):
        raise InvalidBitcoinAddress("malformed bech32 address")
    data = [_BECH32_INDEX[c] for c in data_part]
    const = _polymod(_hrp_expand(hrp) + data)
    version = data[0]
    if const not in (_BECH32_CONST, _BECH32M_CONST):
        raise InvalidBitcoinAddress("checksum mismatch: check the address for a typo")
    if (version == 0) != (const == _BECH32_CONST):
        raise InvalidBitcoinAddress("wrong checksum type for this witness version (BIP-350)")
    program = _convertbits(data[1:-6], 5, 8)
    if program is None or not 2 <= len(program) <= 40 or version > 16:
        raise InvalidBitcoinAddress("malformed witness program")
    if version == 0 and len(program) not in (20, 32):
        raise InvalidBitcoinAddress("a version 0 witness program is 20 or 32 bytes")
    if version == 1 and len(program) != 32:
        raise InvalidBitcoinAddress("a taproot (version 1) witness program is 32 bytes")
    return version, bytes(program)


def normalize(address: str) -> str:
    """The stored form. Raises InvalidBitcoinAddress."""
    a = (address or "").strip()
    if a[:3].lower() == "bc1":
        _segwit(a)
        return a.lower()
    if a[:1] in ("1", "3"):
        payload = _base58check(a)
        if payload[0] not in (0x00, 0x05):
            raise InvalidBitcoinAddress("not a Bitcoin mainnet address")
        if (payload[0] == 0x00) != a.startswith("1"):
            raise InvalidBitcoinAddress("malformed Bitcoin address")
        return a
    raise InvalidBitcoinAddress(f"not a Bitcoin mainnet address: {address!r}")


def kind(address: str) -> str:
    a = normalize(address)
    if a.startswith("1"):
        return "p2pkh"
    if a.startswith("3"):
        return "p2sh"
    version, program = _segwit(a)
    if version == 0:
        return "p2wpkh" if len(program) == 20 else "p2wsh"
    return "p2tr" if version == 1 else f"witness_v{version}"


def is_valid(address: str) -> bool:
    try:
        normalize(address)
        return True
    except InvalidBitcoinAddress:
        return False
