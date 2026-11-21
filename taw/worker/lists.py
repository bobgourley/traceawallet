"""Daily list refreshes: OFAC's SDN list, the exchange wallet lists, California DFPI's
Crypto Scam Tracker, and the index of stablecoin blacklist events."""
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


def refresh_dfpi(*, data_conn, raw_store, actor: str, source=None) -> str:
    """Read DFPI's tracker page. When it changed since the last load: sync the labels
    for the addresses it names, and keep its websites and subjects as a new load."""
    from taw.adapters.labels import dfpi
    loaded = (source or dfpi.DfpiTracker(raw_store, evm_chains(data_conn))).load()
    pub = loaded.publication
    src = store.source_id(data_conn, dfpi.SOURCE)
    last = data_conn.execute("SELECT page_sha256 FROM site_list_load WHERE source_id = %s ORDER BY id DESC LIMIT 1",
                             (src,)).fetchone()
    worded = data_conn.execute("SELECT (detail->>'label_version')::int FROM label_load WHERE source_id = %s"
                               " ORDER BY id DESC LIMIT 1", (src,)).fetchone()
    if last and last[0].strip() == pub.raw_sha256 and worded and worded[0] == dfpi.LABEL_VERSION:
        return f"DFPI tracker unchanged ({len(loaded.entries)} entries)"
    result = labels.load_publication(data_conn, actor, pub)
    raw_id = store.raw_response_ids(data_conn, dfpi.SOURCE, [pub.raw_sha256])[pub.raw_sha256]
    with store.actor_transaction(data_conn, actor):
        load_id = data_conn.execute(
            "INSERT INTO site_list_load (source_id, read_on, raw_response_id, page_sha256, entries)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (src, pub.source_date, raw_id, pub.raw_sha256, len(loaded.entries))).fetchone()[0]
        with data_conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO site_list_entry (load_id, position, entry_key, subject, websites, scam_types)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                [(load_id, e.position, e.key, e.subject, e.websites, e.scam_types) for e in loaded.entries])
    d = pub.detail
    return (f"DFPI tracker read {pub.source_date}: {d['entries']} entries, {d['websites']} websites; address labels "
            f"{result.summary()}; {d['skipped_no_checksum']} address(es) without a checksum and "
            f"{d['skipped_bad_checksum']} failing it left out")


def backfill_needed(conn) -> bool:
    from taw import blacklist
    from taw.adapters.labels import blacklist as bl
    return any(blacklist.coverage(conn, c.chain, c.contract) is None for c in bl.for_chain("eth"))


def run_backfill(*, data_conn, raw_store, actor: str, scout=None, ether=None, seconds: float | None = None) -> str:
    """One slice of the Ethereum blacklist backfill (taw/blacklist_backfill.py). It works
    for SLICE_SECONDS, then raises jobs.Requeue so lookups queued meanwhile go first;
    everything answered is saved, so the next slice, or the next worker after a deploy,
    carries on. An exhausted allowance raises BudgetExhausted, which the worker turns
    into a wait for the reset. Finished when every contract is written."""
    import os
    import time
    from taw import blacklist_backfill as bf
    from taw import jobs
    from taw.adapters.chains.evm_blockscout import BlockscoutAdapter
    from taw.adapters.chains.evm_etherscan import EtherscanAdapter
    from taw.budget import PostgresBudget, PostgresRangeStore
    if not backfill_needed(data_conn):
        return "eth blacklist backfill: already complete"
    if scout is None:
        scout = BlockscoutAdapter("eth", raw_store, os.environ["BLOCKSCOUT_API_KEY"],
                                  budget=PostgresBudget(data_conn, "backfill"), range_store=PostgresRangeStore(data_conn))
    ether = ether or EtherscanAdapter("eth", raw_store, os.environ["ETHERSCAN_API_KEY"])
    deadline = time.monotonic() + (bf.SLICE_SECONDS if seconds is None else seconds)
    try:
        out = bf.backfill_eth(data_conn, actor, scout, ether, deadline=deadline, log=lambda m: print(m, flush=True))
    except bf.PauseSlice as pause:
        raise jobs.Requeue(jobs.later(5), f"slice done, progress saved; {pause}") from None
    if out.differ:
        raise RuntimeError(out.message())          # a real failure: reported, retried, then left failed
    return out.message()
