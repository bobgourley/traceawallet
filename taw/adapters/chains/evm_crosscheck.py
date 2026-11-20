"""Compare what two EVM adapters saw for one address at one block.

Used to check Blockscout against Etherscan (scripts/cross_check_evm.py and
tests/test_evm_crosscheck.py). Both runs must be pinned to the same block
(`at_block`), or every difference is noise.

What must agree:
  - block height, native balance, contract flag, and each token balance
  - native and token transfers exactly, including tx hash, log index, from, to,
    token contract, decimals and amount
  - internal transfers by tx hash, from, to and amount. Not by event_index:
    Etherscan's is an encoded trace path and Blockscout's is its own index, so
    the numbers differ by design while the movement is the same. Only over the
    blocks both read in full (TransferBatch.internal_range): Blockscout reads
    internal transfers only where it has finished indexing.
  - first activity (oldest transfer) and first incoming transfer
  - whether the transfer list was truncated

Symbols are display only and are not compared.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from taw.adapters.base import AddressSnapshot, TransferBatch, TransferRecord


@dataclass(frozen=True)
class Mismatch:
    what: str
    a: object
    b: object

    def __str__(self) -> str:
        return f"{self.what}: {self.a!r} vs {self.b!r}"


def _transfer_key(t: TransferRecord) -> tuple:
    if t.kind == "internal":
        return ("internal", t.tx_hash, t.from_address, t.to_address, t.amount_raw)
    return (t.kind, t.tx_hash, t.event_index, t.from_address, t.to_address, t.token_contract,
            t.token_decimals, t.amount_raw)


def _first(batch: TransferBatch, incoming: bool) -> tuple | None:
    rows = sorted(batch.transfers, key=lambda t: (t.block_number, t.tx_hash))
    for t in rows:
        if not incoming or t.to_address == batch.address:
            return (t.block_number, t.tx_hash)
    return None


def compare(snap_a: AddressSnapshot, batch_a: TransferBatch,
            snap_b: AddressSnapshot, batch_b: TransferBatch) -> list[Mismatch]:
    out: list[Mismatch] = []

    def check(what, a, b):
        if a != b:
            out.append(Mismatch(what, a, b))

    check("address", snap_a.address, snap_b.address)
    check("as_of_block", snap_a.as_of_block, snap_b.as_of_block)
    check("native balance (raw)", snap_a.native_balance_raw, snap_b.native_balance_raw)
    check("is_contract", snap_a.is_contract, snap_b.is_contract)
    tokens_a = {b.contract: (b.amount_raw, b.decimals) for b in snap_a.token_balances}
    tokens_b = {b.contract: (b.amount_raw, b.decimals) for b in snap_b.token_balances}
    for contract in sorted(set(tokens_a) | set(tokens_b)):
        check(f"token balance {contract} (raw, decimals)", tokens_a.get(contract), tokens_b.get(contract))

    lo, hi = 0, snap_a.as_of_block
    for r in (batch_a.internal_range, batch_b.internal_range):
        if r is not None:
            lo, hi = max(lo, r[0]), min(hi, r[1])

    def comparable(t: TransferRecord) -> bool:
        return t.kind != "internal" or lo <= t.block_number <= hi

    keys_a = Counter(_transfer_key(t) for t in batch_a.transfers if comparable(t))
    keys_b = Counter(_transfer_key(t) for t in batch_b.transfers if comparable(t))
    for key in sorted((keys_a - keys_b).elements(), key=str):
        out.append(Mismatch("transfer only in first", key, None))
    for key in sorted((keys_b - keys_a).elements(), key=str):
        out.append(Mismatch("transfer only in second", None, key))

    check("first activity (block, tx)", _first(batch_a, False), _first(batch_b, False))
    check("first incoming (block, tx)", _first(batch_a, True), _first(batch_b, True))
    check("truncated", batch_a.truncated, batch_b.truncated)
    return out
