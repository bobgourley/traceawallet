"""Tron address forms.

A Tron address is 21 bytes: 0x41 followed by 20 bytes. It appears as
  base58check  T...      (34 characters; how we store it)
  hex          41 + 40 hex characters (TronGrid account responses)
  0x + 40 hex            (event results; the 0x41 prefix is implied)
"""
from __future__ import annotations

import hashlib
import re

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}
_BASE58_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")   # same shape check as the database


class InvalidTronAddress(ValueError):
    pass


def _checksum(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def _b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _ALPHABET[rem] + out
    pad = len(raw) - len(raw.lstrip(b"\0"))
    return "1" * pad + out


def _b58decode(text: str) -> bytes:
    n = 0
    for c in text:
        if c not in _INDEX:
            raise InvalidTronAddress(f"not a base58 character: {c!r}")
        n = n * 58 + _INDEX[c]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = len(text) - len(text.lstrip("1"))
    return b"\0" * pad + raw


def _from_payload(payload: bytes) -> str:
    if len(payload) != 21 or payload[0] != 0x41:
        raise InvalidTronAddress("a Tron address is 0x41 followed by 20 bytes")
    return _b58encode(payload + _checksum(payload))


def to_base58(address: str) -> str:
    """Return the T... form of any Tron address form. Raises InvalidTronAddress."""
    a = (address or "").strip()
    if a.startswith("T"):
        if not _BASE58_RE.match(a):
            raise InvalidTronAddress(f"malformed Tron address {a!r}")
        raw = _b58decode(a)
        if len(raw) != 25 or _checksum(raw[:21]) != raw[21:]:
            raise InvalidTronAddress(f"checksum mismatch in Tron address {a!r}")
        if raw[0] != 0x41:
            raise InvalidTronAddress(f"not a Tron mainnet address {a!r}")
        return a
    h = a.lower()
    if h.startswith("0x"):
        h = h[2:]
        if len(h) == 40:
            h = "41" + h
    if not re.fullmatch(r"41[0-9a-f]{40}", h):
        raise InvalidTronAddress(f"malformed Tron address {address!r}")
    return _from_payload(bytes.fromhex(h))


def to_hex(address: str) -> str:
    """Return the 41... hex form (42 characters, lower case)."""
    return _b58decode(to_base58(address))[:21].hex()


def is_valid(address: str) -> bool:
    try:
        to_base58(address)
        return True
    except InvalidTronAddress:
        return False
