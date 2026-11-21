"""Steps that run after a lookup is saved: prices now, labels later.

Each step is wrapped so a provider problem becomes a note on the job's log line
rather than a failed lookup: the snapshot and transfers are already stored.
"""
from __future__ import annotations

from typing import Callable

from taw import blacklist, labels, pricing
from taw.adapters import registry
from taw.adapters.labels import freeze
from taw.adapters.labels.blacklist import indexed
from taw.adapters.prices.chainlink import ChainlinkReader
from taw.reconcile import StablecoinContext


def index_for(data_conn) -> Callable:
    """freeze.Index over the global blacklist index: None for a contract not indexed."""
    def look(contract: freeze.FreezeContract, address: str, block: int | None):
        if indexed(contract.chain, contract.contract) is None:
            return None
        return blacklist.freeze_since(data_conn, contract.chain, contract.contract, address, block)
    return look


def freeze_checks(chain: str, address: str, block: int, adapter, index=None) -> list[freeze.FreezeCheck]:
    if chain == "tron":
        return [freeze.check_tron(adapter, c, address, index) for c in freeze.contracts_for(chain)]
    return [freeze.check_evm(adapter, c, address, block, index) for c in freeze.contracts_for(chain)]


def stablecoin_context(data_conn, chain: str, address: str, block: int,
                       checks: list[freeze.FreezeCheck]) -> StablecoinContext:
    """Freeze status from the contract reads, destructions from the index (no calls)."""
    frozen = {c.contract.contract: c.frozen for c in checks}
    burns, covered = {}, {}
    for c in freeze.contracts_for(chain):
        if indexed(chain, c.contract) is None:
            continue
        cov = blacklist.coverage(data_conn, chain, c.contract)
        if cov is None:
            continue
        covered[c.contract] = cov.to_block
        found = blacklist.destructions(data_conn, chain, c.contract, address, block)
        if found:
            burns[c.contract] = tuple(e.as_json() for e in found)
    return StablecoinContext(frozen, burns, covered)


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


def freeze_step(data_conn, state_conn, actor: str, cache: dict | None = None) -> Callable:
    """cache: checks already read by prepare_step for this snapshot, reused here."""
    def step(snap, batch, raw_store) -> str:
        if not freeze.contracts_for(snap.chain):
            return f"freeze status: no stablecoin contract checked on {snap.chain}"
        key = (snap.chain, snap.address, snap.as_of_block)
        checks = (cache or {}).pop(key, None)
        if checks is None:
            adapter = registry.adapter_for(snap.chain, raw_store, state_conn=state_conn)
            checks = freeze_checks(snap.chain, snap.address, snap.as_of_block, adapter, index_for(data_conn))
        save_freeze_checks(data_conn, actor, snap.chain, snap.address, checks)
        return "freeze status: " + ", ".join(f"{c.contract.token} {c.result.replace('_', ' ')}" for c in checks)
    return step


def goplus_step(data_conn, state_conn, actor: str) -> Callable:
    def step(snap, batch, raw_store) -> str:
        if not registry.goplus_allowed(snap.chain):
            return f"GoPlus: not asked ({snap.chain} is never sent to GoPlus)"
        source = registry.goplus_for(raw_store, state_conn=state_conn)
        claims, check, sha = source.claims_for(snap.chain, snap.address)
        labels.record_check(data_conn, actor, "goplus", snap.chain, snap.address, subject="address_security",
                            block=None, result="flags" if claims else "no_flags", detail=check, raw_sha256=sha,
                            raw_source="goplus")
        labels.sync(data_conn, actor, "goplus", claims, raw_sha256=sha, source_date=None,
                    only_address=(snap.chain, snap.address))
        return f"GoPlus: {len(claims)} flag(s)" + (f" ({', '.join(check['flags'])})" if claims else "")
    return step


def label_step(data_conn) -> Callable:
    def step(snap, batch, raw_store) -> str:
        return labels.apply_to_lookup(data_conn, snap.chain, snap.address, batch.transfers).summary()
    return step


def price_step(data_conn, state_conn, actor: str) -> Callable:
    def step(snap, batch, raw_store) -> str:
        reader = ChainlinkReader(lambda chain: registry.adapter_for(chain, raw_store, state_conn=state_conn))
        return pricing.fill(data_conn, actor, batch.transfers, reader, snap=snap).summary()
    return step


def run_steps(steps: list[tuple[str, Callable]], snap, batch, raw_store) -> list[str]:
    notes = []
    for name, step in steps:
        try:
            notes.append(step(snap, batch, raw_store))
        except Exception as exc:                                      # noqa: BLE001
            notes.append(f"{name}: not done ({type(exc).__name__}: {exc})")
    return notes


def prepare_step(data_conn, state_conn, cache: dict) -> Callable:
    """Before the completeness checks: read freeze status (kept in `cache` for the
    freeze step) and the address's indexed destructions."""
    def prepare(snap, batch, raw_store):
        if not freeze.contracts_for(snap.chain):
            return None, None
        checks = None
        try:
            adapter = registry.adapter_for(snap.chain, raw_store, state_conn=state_conn)
            checks = freeze_checks(snap.chain, snap.address, snap.as_of_block, adapter, index_for(data_conn))
            cache[(snap.chain, snap.address, snap.as_of_block)] = checks
            note = None
        except Exception as exc:                                      # noqa: BLE001
            note = f"freeze status before the checks: not read ({type(exc).__name__}: {exc})"
        ctx = stablecoin_context(data_conn, snap.chain, snap.address, snap.as_of_block, checks or [])
        return ctx, note
    return prepare


class Enrichment:
    """The worker's steps around a lookup: prepare (before the checks) and the steps
    after the save. Calling it runs the steps after the save."""

    def __init__(self, *, data_conn, state_conn, actor: str):
        cache: dict = {}
        self.prepare = prepare_step(data_conn, state_conn, cache)
        self.steps = [("freeze status", freeze_step(data_conn, state_conn, actor, cache)),
                      ("GoPlus", goplus_step(data_conn, state_conn, actor)),
                      ("prices", price_step(data_conn, state_conn, actor)),
                      ("labels", label_step(data_conn))]

    def __call__(self, snap, batch, raw_store) -> list[str]:
        return run_steps(self.steps, snap, batch, raw_store)


def for_worker(*, data_conn, state_conn, actor: str) -> Enrichment:
    return Enrichment(data_conn=data_conn, state_conn=state_conn, actor=actor)
