"""EVM address forms.

An EVM address is 20 bytes, written 0x + 40 hex characters. We store it lower
case. Mixed case is an EIP-55 checksum: if someone types a mixed-case address,
the checksum must hold, which catches most typos. All-lower and all-upper
addresses carry no checksum and are accepted as typed.

EIP-55 needs Keccak-256, which is not hashlib's sha3_256 (that is the final
SHA-3 standard, with different padding). A small pure-Python Keccak-256 is
below so no extra package is needed; it is checked against published vectors
in tests/test_evm_address.py.
"""
from __future__ import annotations

import re

_SHAPE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class InvalidEvmAddress(ValueError):
    pass


# -- Keccak-256 (the original Keccak padding, as Ethereum uses) ------------------

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_MASK = (1 << 64) - 1


def _rol(v: int, n: int) -> int:
    return ((v << n) | (v >> (64 - n))) & _MASK if n else v


def _keccak_f(s: list[list[int]]) -> None:
    for rc in _RC:
        c = [s[x][0] ^ s[x][1] ^ s[x][2] ^ s[x][3] ^ s[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                s[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(s[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                s[x][y] = b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y])
        s[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    rate = 136
    msg = bytearray(data) + b"\x01"
    msg += b"\x00" * (-len(msg) % rate)
    msg[-1] |= 0x80
    s = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        block = msg[off:off + rate]
        for i in range(rate // 8):
            x, y = i % 5, i // 5
            s[x][y] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        _keccak_f(s)
    out = b"".join(s[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
    return out


# -- addresses ---------------------------------------------------------------------

def to_checksum(address: str) -> str:
    """The EIP-55 mixed-case form, for display."""
    low = normalize(address)[2:]
    digest = keccak256(low.encode("ascii")).hex()
    return "0x" + "".join(ch.upper() if int(digest[i], 16) >= 8 else ch for i, ch in enumerate(low))


def normalize(address: str) -> str:
    """The stored form (0x + 40 lower-case hex). Raises InvalidEvmAddress."""
    a = (address or "").strip()
    if not _SHAPE.match(a):
        raise InvalidEvmAddress(f"malformed EVM address {address!r} (expected 0x and 40 hex characters)")
    body = a[2:]
    low = "0x" + body.lower()
    if body != body.lower() and body != body.upper():
        digest = keccak256(body.lower().encode("ascii")).hex()
        expected = "".join(ch.upper() if int(digest[i], 16) >= 8 else ch for i, ch in enumerate(body.lower()))
        if body != expected:
            raise InvalidEvmAddress(f"checksum mismatch in EVM address {address!r}: check it for a typo")
    return low


def is_valid(address: str) -> bool:
    try:
        normalize(address)
        return True
    except InvalidEvmAddress:
        return False
