"""Steps that run after a lookup is saved: prices now, labels later.

Each step is wrapped so a provider problem becomes a note on the job's log line
rather than a failed lookup: the snapshot and transfers are already stored.
"""
from __future__ import annotations

from typing import Callable

from taw import labels, pricing
from taw.adapters import registry
from taw.adapters.labels import freeze
from taw.adapters.prices.chainlink import ChainlinkReader


def freeze_checks(chain: str, address: str, block: int, adapter) -> list[freeze.FreezeCheck]:
    if chain == "tron":
        return [freeze.check_tron(adapter, c, address) for c in freeze.contracts_for(chain)]
    return [freeze.check_evm(adapter, c, address, block) for c in freeze.contracts_for(chain)]


def save_freeze_checks(conn, actor: str, chain: str, address: str, checks: list[freeze.FreezeCheck]) -> None:
    raw_source = "trongrid" if chain == "tron" else "blockscout"
    for chk in checks:
        labels.record_check(conn, actor, chk.contract.source, chain, address, subject=chk.contract.contract,
                            block=chk.block, result=chk.result,
                            detail={**chk.detail(), "also_read": list(chk.extra_sha256)},
                            raw_sha256=chk.raw_sha256, raw_source=raw_source)
    for source in sorted({c.contract.source for c in checks}):
        mine = [c for c in checks if c.contract.source == source]
        if any(c.frozen is None for c in mine):
            continue                       # an unanswered call never withdraws a freeze label
        labels.sync(conn, actor, source, [c.claim() for c in mine if c.frozen], raw_sha256=mine[0].raw_sha256,
                    source_date=None, only_address=(chain, address), raw_source=raw_source)


def freeze_step(data_conn, state_conn, actor: str) -> Callable:
    def step(snap, batch, raw_store) -> str:
        if not freeze.contracts_for(snap.chain):
            return f"freeze status: no stablecoin contract checked on {snap.chain}"
        adapter = registry.adapter_for(snap.chain, raw_store, state_conn=state_conn)
        checks = freeze_checks(snap.chain, snap.address, snap.as_of_block, adapter)
        save_freeze_checks(data_conn, actor, snap.chain, snap.address, checks)
        return "freeze status: " + ", ".join(f"{c.contract.token} {c.result.replace('_', ' ')}" for c in checks)
    return step


def price_step(data_conn, state_conn, actor: str) -> Callable:
    def step(snap, batch, raw_store) -> str:
        reader = ChainlinkReader(lambda chain: registry.adapter_for(chain, raw_store, state_conn=state_conn))
        return pricing.fill(data_conn, actor, batch.transfers, reader).summary()
    return step


def run_steps(steps: list[tuple[str, Callable]], snap, batch, raw_store) -> list[str]:
    notes = []
    for name, step in steps:
        try:
            notes.append(step(snap, batch, raw_store))
        except Exception as exc:                                      # noqa: BLE001
            notes.append(f"{name}: not done ({type(exc).__name__}: {exc})")
    return notes


def for_worker(*, data_conn, state_conn, actor: str) -> Callable:
    steps = [("freeze status", freeze_step(data_conn, state_conn, actor)),
             ("prices", price_step(data_conn, state_conn, actor))]
    return lambda snap, batch, raw_store: run_steps(steps, snap, batch, raw_store)
