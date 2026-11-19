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
from taw.adapters.base import RawIntegrityError, RawStore, SecretLeak
from taw.adapters.chains.evm_address import InvalidEvmAddress
from taw.adapters.chains.tron_address import InvalidTronAddress
from taw.adapters.registry import MissingSetting, UnsupportedChain
from taw.jobs import PayloadError, read_lookup_payload

TRANSFER_LIMIT = 2000


@dataclass(frozen=True)
class Outcome:
    message: str
    reused: bool
    saved: store.LookupSaved | None = None


def run_lookup(payload, *, data_conn, raw_store: RawStore, actor: str,
               adapter_for: Callable, store_module=store) -> Outcome:
    chain, address = read_lookup_payload(payload)

    fresh = store_module.fresh_snapshot(data_conn, chain, address)
    if fresh is not None:
        store_module.record_reused_lookup(data_conn, actor, chain, address, fresh)
        return Outcome(f"reused snapshot {fresh.id} (block {fresh.as_of_block}, fetched "
                       f"{fresh.fetched_at:%Y-%m-%d %H:%M:%S} UTC)", reused=True)

    adapter = adapter_for(chain, raw_store)
    snap = adapter.fetch_snapshot(address)
    batch = adapter.fetch_transfers(snap.address, limit=TRANSFER_LIMIT)
    saved = store_module.save_lookup(data_conn, actor, snap, batch)
    note = ", truncated at the limit" if batch.truncated else ""
    return Outcome(
        f"snapshot {saved.snapshot_id} at block {snap.as_of_block}; {saved.transfers_seen} transfers "
        f"({saved.transfers_new} new{note}); {saved.addresses_seen} addresses",
        reused=False, saved=saved,
    )


def describe_failure(exc: BaseException) -> tuple[bool, str]:
    """(permanent, plain-language message). Permanent failures are not retried."""
    # Imported here so the worker starts even if one chain's module is broken.
    from taw.adapters.chains import evm_etherscan as es
    from taw.adapters.chains.tron_trongrid import TronGridBlocked, TronGridError

    if isinstance(exc, TronGridBlocked):
        return True, ("TronGrid refused the request (HTTP 403), which means it has blocked our API key "
                      "for now. Not retried. Check TRONGRID_API_KEY in Railway and wait before trying again.")
    if isinstance(exc, es.EtherscanChainNotCovered):
        return True, (f"Etherscan's free plan does not cover {exc.chain_name}, so this lookup cannot run. Not retried. "
                      "It needs a paid Etherscan plan.")
    if isinstance(exc, es.EtherscanError):
        who = exc.provider
        if isinstance(exc, es.EtherscanKeyRejected):
            return True, f"{who} rejected our API key. Not retried. Check {exc.key_env} in Railway."
        if isinstance(exc, es.EtherscanDailyLimit):
            return True, (f"{who}'s daily call allowance for our key is used up. Not retried; "
                          "queue the lookup again tomorrow.")
        if isinstance(exc, es.EtherscanPaidEndpoint):
            return True, f"A request needs a paid {who} endpoint, which is a bug in our code: {exc}."
        if isinstance(exc, es.EtherscanRateLimited):
            return False, f"{who} kept saying we were calling too fast, even after backing off."
        if getattr(exc, "incomplete", False):
            return False, (f"{who} has not finished indexing this address yet ({exc}). Nothing was saved; "
                           "the lookup will be tried again.")
        return False, f"{who} returned an error: {exc}."
    if isinstance(exc, (PayloadError, InvalidTronAddress, InvalidEvmAddress, UnsupportedChain)):
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
