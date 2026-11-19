"""Small helpers shared by chain adapters."""
from __future__ import annotations

from typing import Callable


class Pacer:
    """Keeps requests under `per_second`."""

    def __init__(self, per_second: float, clock: Callable[[], float], sleep: Callable[[float], None]):
        self.interval = 1.0 / per_second
        self.clock, self.sleep = clock, sleep
        self.last = float("-inf")

    def wait(self) -> None:
        delay = self.last + self.interval - self.clock()
        if delay > 0:
            self.sleep(delay)
        self.last = self.clock()


def decode_uint(hex_word: str) -> int:
    """First 32-byte word of ABI output as an unsigned integer."""
    return int(hex_word[:64] or "0", 16)


def decode_string(hex_data: str) -> str | None:
    """ABI string, or bytes32 for older tokens. NUL characters are dropped
    (Postgres text cannot hold them)."""
    try:
        if len(hex_data) >= 128:
            length = int(hex_data[64:128], 16)
            raw = bytes.fromhex(hex_data[128:128 + 2 * length])
        else:
            raw = bytes.fromhex(hex_data[:64]).rstrip(b"\0")
        text = raw.decode("utf-8", errors="replace").replace("\x00", "").strip()
        return text or None
    except ValueError:
        return None
