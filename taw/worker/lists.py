"""Daily list refreshes: OFAC's SDN list, the exchange wallet lists, and the index of
stablecoin blacklist events."""
from __future__ import annotations

import datetime as dt

from taw import labels, store
from taw.adapters.labels.exchanges import FILE_SOURCE, OKX_SOURCE, ExchangeFile, NotPublished, OkxPor
from taw.adapters.labels.ofac import OfacSdn


def evm_chains(conn) -> list[str]:
    return [c for (c,) in conn.execute("SELECT id FROM chain WHERE family = 'evm' ORDER BY id").fetchall()]


def expire_lookups(*, data_conn, raw_store, actor: str) -> str:
    """Daily: erase lookups older than the retention period (taw/lookups.py)."""
    from taw import lookups
    n = lookups.expire(data_conn, actor)
    return f"lookups older than {lookups.retention_days()} days erased: {n}"


def refresh_ofac(*, data_conn, raw_store, actor: str, source=None) -> str:
    source = source or OfacSdn(raw_store, evm_chains(data_conn))
    pub = source.load()
    result = labels.load_publication(data_conn, actor, pub)
    return (f"OFAC SDN list published {pub.source_date}: {result.summary()}; {pub.unmapped} address(es) in "
            f"formats we don't cover")


def refresh_exchanges(*, data_conn, raw_store, actor: str, today: dt.date | None = None, okx=None,
                      exchange_file=None) -> str:
    """Reload data/exchange_wallets.csv if it changed, and load OKX's file for this
    month if it is out and not loaded yet. Run daily; OKX publishes monthly."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    notes = []
    pub = (exchange_file or ExchangeFile()).load()
    last = data_conn.execute(
        "SELECT detail->>'file_sha256' FROM label_load WHERE source_id = %s ORDER BY id DESC LIMIT 1",
        (store.source_id(data_conn, FILE_SOURCE),)).fetchone()
    if last and last[0] == pub.detail["file_sha256"]:
        notes.append("exchange file unchanged")
    else:
        notes.append(f"exchange file: {labels.load_publication(data_conn, actor, pub).summary()}")
    month = today.replace(day=1)
    done = data_conn.execute("SELECT 1 FROM label_load WHERE source_id = %s AND source_date = %s",
                             (store.source_id(data_conn, OKX_SOURCE), month)).fetchone()
    if done:
        notes.append(f"OKX {month:%Y-%m}: already loaded")
    else:
        try:
            okx_pub = (okx or OkxPor(raw_store)).load(month)
        except NotPublished:
            notes.append(f"OKX {month:%Y-%m}: not published yet")
        else:
            result = labels.load_publication(data_conn, actor, okx_pub)
            notes.append(f"OKX {month:%Y-%m}: {result.summary()}; {okx_pub.unmapped} row(s) skipped (not an address)")
    return "; ".join(notes)


def refresh_blacklists(*, data_conn, raw_store, actor: str) -> str:
    """Bring the global index of stablecoin blacklist events up to the head, for every
    contract already backfilled (scripts/backfill_blacklist.py does the first scan).
    Ethereum through Blockscout (metered), Tron through TronGrid."""
    from taw import blacklist
    from taw.adapters import registry
    notes = []
    for chain in ("eth", "tron"):
        try:
            notes += blacklist.update(data_conn, actor,
                                      lambda c: registry.adapter_for(c, raw_store, state_conn=data_conn), (chain,))
        except Exception as exc:
            from taw.budget import BudgetExhausted
            if isinstance(exc, BudgetExhausted):
                raise                          # the job is deferred to the reset
            notes.append(f"{chain}: not done ({type(exc).__name__}: {exc})")
    return "; ".join(notes)
