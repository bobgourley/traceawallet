"""The address_lookup job: snapshot, transfers, save.

The payload is {"chain": ..., "address": ...} and nothing else. If the same
address was fetched less than store.FRESH_FOR ago, that fetch is reused and the
job finishes without calling the provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import httpx
import psycopg

from taw import store
from taw.reconcile import run_checks
from taw.adapters.base import RawIntegrityError, RawStore, SecretLeak
from taw.adapters.chains.btc_address import InvalidBitcoinAddress
from taw.adapters.chains.evm_address import InvalidEvmAddress
from taw.adapters.chains.tron_address import InvalidTronAddress
from taw.adapters.registry import MissingSetting, UnsupportedChain, stablecoins_for
from taw.jobs import PayloadError, read_lookup_payload

TRANSFER_LIMIT = 2000


@dataclass(frozen=True)
class Outcome:
    message: str
    reused: bool
    saved: store.LookupSaved | None = None
    snapshot_id: int | None = None       # the snapshot the lookup ended with, new or reused
    chain: str | None = None
    address: str | None = None


def _read(adapter, address: str, step: Callable):
    step("reading_balances")
    snap = adapter.fetch_snapshot(address)
    step("reading_transfers")
    return snap, adapter.fetch_transfers(snap.address, limit=TRANSFER_LIMIT)


def _timed_out(exc: BaseException) -> bool:
    from taw.adapters.chains.btc_esplora import EsploraTimeout
    return isinstance(exc, (EsploraTimeout, httpx.TimeoutException))


def run_lookup(payload, *, data_conn, raw_store: RawStore, actor: str,
               adapter_for: Callable, store_module=store, enrich: Callable | None = None,
               prepare: Callable | None = None, progress: Callable | None = None) -> Outcome:
    """enrich(snap, batch, raw_store) -> list of notes runs after the lookup is saved
    (prices, labels). It must not raise for a provider problem; the lookup stands.

    prepare(snap, batch, raw_store) -> (StablecoinContext | None, note) runs before
    the completeness checks: freeze status and indexed destructions, so a burn by
    the issuer counts as an accounted movement. A failure there leaves the checks
    without it."""
    chain, address = read_lookup_payload(payload)

    fresh = store_module.fresh_snapshot(data_conn, chain, address)
    if fresh is not None:
        store_module.record_reused_lookup(data_conn, actor, chain, address, fresh)
        return Outcome(f"reused snapshot {fresh.id} (block {fresh.as_of_block}, fetched "
                       f"{fresh.fetched_at:%Y-%m-%d %H:%M:%S} UTC)", reused=True, snapshot_id=fresh.id,
                       chain=chain, address=address)

    step = progress or (lambda name: None)
    adapter = adapter_for(chain, raw_store)
    fallback_note = None
    try:
        snap, batch = _read(adapter, address, step)
    except Exception as exc:
        alt = adapter.fallback() if _timed_out(exc) and hasattr(adapter, "fallback") else None
        if alt is None:
            raise
        fallback_note = (f"{getattr(exc, 'provider', 'the provider')} did not answer within "
                         f"{getattr(exc, 'waited', 0):.0f} s ({getattr(exc, 'path', '?')}); read from "
                         f"{alt.provider_name} instead")
        snap, batch = _read(alt, address, step)
    step("checking")
    context, prep_note = None, None
    if prepare is not None:
        try:
            context, prep_note = prepare(snap, batch, raw_store)
        except Exception as exc:                                      # noqa: BLE001
            prep_note = f"freeze and burn context: not read ({type(exc).__name__}: {exc})"
    recon = run_checks(snap, batch, stablecoins_for(chain), context)
    saved = store_module.save_lookup(data_conn, actor, snap, batch, recon=recon)
    note = ", truncated at the limit" if batch.truncated else ""
    warning = ("; HISTORY MAY BE INCOMPLETE: " + "; ".join(recon.incomplete_reasons)) if recon.history_incomplete else ""
    step("checking_lists")
    notes = [n for n in (fallback_note, prep_note) if n] + (enrich(snap, batch, raw_store) if enrich else [])
    extra = "".join(f"; {n}" for n in notes)
    return Outcome(
        f"snapshot {saved.snapshot_id} at block {snap.as_of_block}; {saved.transfers_seen} transfers "
        f"({saved.transfers_new} new{note}); {saved.addresses_seen} addresses{warning}{extra}",
        reused=False, saved=saved, snapshot_id=saved.snapshot_id, chain=chain, address=snap.address,
    )


def allowance_reset(exc: BaseException):
    """When a provider's daily allowance ran out: the time it resets (00:00 UTC, plus five
    minutes' margin). None for any other failure."""
    import datetime as dt
    from taw.adapters.chains import evm_etherscan as es
    from taw.budget import BudgetExhausted, next_reset
    if isinstance(exc, BudgetExhausted):
        return exc.resets_at + dt.timedelta(minutes=5)
    if isinstance(exc, es.EtherscanDailyLimit):
        return next_reset() + dt.timedelta(minutes=5)
    return None


def failure_kind(exc: BaseException) -> str:
    """A code for why an attempt failed, which the waiting page words for people. The
    technical message stays in job.last_error."""
    from taw.adapters.chains import evm_etherscan as es
    from taw.adapters.chains.btc_esplora import EsploraError, EsploraRateLimited
    from taw.adapters.chains.tron_trongrid import TronGridBlocked, TronGridError
    from taw.budget import BudgetExhausted
    if _timed_out(exc):
        return "provider_slow"
    if isinstance(exc, (BudgetExhausted, es.EtherscanDailyLimit)):
        return "allowance"
    if isinstance(exc, (PayloadError, InvalidTronAddress, InvalidEvmAddress, InvalidBitcoinAddress, UnsupportedChain)):
        return "cannot_look_up"
    if isinstance(exc, (EsploraRateLimited, es.EtherscanRateLimited)) or (
            isinstance(exc, TronGridError) and getattr(exc, "status", None) in (429, 503)):
        return "provider_busy"
    if isinstance(exc, (TronGridBlocked, es.EtherscanKeyRejected, es.EtherscanPaidEndpoint, MissingSetting, SecretLeak,
                        RawIntegrityError, psycopg.Error)):
        return "service_problem"
    if getattr(exc, "incomplete", False):
        return "provider_indexing"
    if isinstance(exc, httpx.TransportError):
        return "provider_unreachable"
    if isinstance(exc, (EsploraError, es.EtherscanError, TronGridError)):
        return "provider_error"
    return "service_problem"


def describe_failure(exc: BaseException) -> tuple[bool, str]:
    """(permanent, plain-language message). Permanent failures are not retried."""
    # Imported here so the worker starts even if one chain's module is broken.
    from taw.adapters.chains import evm_etherscan as es
    from taw.adapters.chains.tron_trongrid import TronGridBlocked, TronGridError

    if isinstance(exc, TronGridBlocked):
        return True, ("TronGrid refused the request (HTTP 403), which means it has blocked our API key "
                      "for now. Not retried. Check TRONGRID_API_KEY in Railway and wait before trying again.")
    if isinstance(exc, es.EtherscanChainNotCovered):
        return True, (f"{exc.provider}'s plan does not cover {exc.chain_name}, so this lookup cannot run. "
                      f"Not retried. It needs a paid {exc.provider} plan.")
    if isinstance(exc, es.EtherscanError):
        who = exc.provider
        if isinstance(exc, es.EtherscanKeyRejected):
            return True, f"{who} rejected our API key. Not retried. Check {exc.key_env} in Railway."
        if isinstance(exc, es.EtherscanDailyLimit):
            return False, (f"{who}'s daily allowance used up, resets at 00:00 UTC. The lookup is retried "
                           "after the reset.")
        if isinstance(exc, es.EtherscanPaidEndpoint):
            return True, f"A request needs a paid {who} endpoint, which is a bug in our code: {exc}."
        if isinstance(exc, es.EtherscanRateLimited):
            return False, f"{who} kept saying we were calling too fast, even after backing off."
        if getattr(exc, "incomplete", False):
            return False, (f"{who} has not finished indexing this address yet ({exc}). Nothing was saved; "
                           "the lookup will be tried again.")
        return False, f"{who} returned an error: {exc}."
    from taw.adapters.chains.btc_esplora import EsploraError, EsploraRateLimited, EsploraTimeout
    if isinstance(exc, EsploraTimeout):
        return False, f"{exc.provider} did not answer within {exc.waited:.0f} s ({exc.path})."
    if isinstance(exc, EsploraRateLimited):
        return False, f"{exc.provider} kept saying we were calling too fast, even after backing off."
    if isinstance(exc, EsploraError):
        return False, f"{exc.provider} returned an error: {exc}."
    if isinstance(exc, (PayloadError, InvalidTronAddress, InvalidEvmAddress, InvalidBitcoinAddress, UnsupportedChain)):
        return True, f"This lookup cannot run: {exc}."
    if isinstance(exc, MissingSetting):
        return True, f"The worker is missing a setting: {exc}."
    if isinstance(exc, SecretLeak):
        return True, "Stopped because a request would have stored an API key. That request was not saved."
    if isinstance(exc, RawIntegrityError):
        return True, f"A saved provider response failed its hash check: {exc}. Needs a person to look."
    if isinstance(exc, TronGridError):
        if exc.status in (429, 503):
            what = "too many requests" if exc.status == 429 else "service unavailable"
            return False, f"TronGrid kept answering {exc.status} ({what}) even after backing off."
        return False, f"TronGrid returned an error: {exc}."
    if isinstance(exc, httpx.TransportError):
        return False, f"Could not reach the data provider ({type(exc).__name__})."
    if isinstance(exc, psycopg.Error):
        first = (str(exc).strip().splitlines() or [""])[0]
        return False, f"Database error ({type(exc).__name__}): {first}"
    return False, f"Unexpected error ({type(exc).__name__}): {exc}"
