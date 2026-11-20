"""Lookups: who asked about which address, the jobs that answered, and the private
transaction match. Everything here touches the private tables (lookup, lookup_job,
lookup_report) and none of it reaches a report or the event log beyond the
id-only audit entry on lookup.

A lookup starts one address_lookup job per chain: a 0x address is looked up on
every EVM chain Phase 1 checks (registry.PHASE1_EVM_CHECKED). Jobs are shared:
two people asking about the same address share the queued job. When the victim
gave matching inputs, one lookup_match job (payload: the lookup id only) runs
after the address jobs finish.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal

from taw import jobs, matching, store
from taw.adapters import registry
from taw.report import inputs as report_inputs

MATCH_WAIT_SECONDS = 30


class LookupRefused(ValueError):
    """The request can't become a lookup; the message says why, in plain words."""


def family_of(address: str) -> str:
    a = (address or "").strip()
    if a.lower().startswith("0x"):
        return "evm"
    if a.startswith("T"):
        return "tron"
    return "btc"


def chains_for(family: str) -> tuple[str, ...]:
    return {"evm": registry.PHASE1_EVM_CHECKED, "tron": ("tron",), "btc": ("btc",)}[family]


@dataclass(frozen=True)
class LookupRequest:
    address: str
    chain: str | None = None            # a hint for matching; a 0x address is still looked up on every chain
    asset: str | None = None
    amount: str | None = None
    sent_on: dt.date | None = None
    sending_address: str | None = None
    tx_hash: str | None = None
    platform_name: str | None = None
    platform_url: str | None = None

    def wants_match(self) -> bool:
        return bool(self.tx_hash or self.amount)


def _tx_shape(family: str, tx: str) -> str:
    h = tx.strip().lower()
    if family == "evm":
        h = h if h.startswith("0x") else "0x" + h
        ok = len(h) == 66 and all(c in "0123456789abcdef" for c in h[2:])
    else:
        h = h.removeprefix("0x")
        ok = len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    if not ok:
        raise LookupRefused("the transaction hash is not the right shape for this kind of address")
    return h


def create(conn, actor: str, req: LookupRequest, *, user_id: str | None = None, session_hash: str | None = None,
           ip_hash: str | None = None) -> tuple[str, tuple[str, ...]]:
    """Record the lookup and queue its jobs. Returns (lookup id, chains). A lookup made
    before sign-in has no user yet, only the hashed session id; signing in claims it."""
    if not user_id and not session_hash:
        raise LookupRefused("a lookup needs a signed-in account or a session")
    family = family_of(req.address)
    chains = chains_for(family)
    try:
        address = registry.normalize_address(chains[0], req.address)
        sender = registry.normalize_address(chains[0], req.sending_address) if req.sending_address else None
    except ValueError as exc:
        raise LookupRefused(str(exc)) from exc
    if req.chain and req.chain not in chains:
        raise LookupRefused(f"{req.chain!r} is not a chain this address is looked up on ({', '.join(chains)})")
    try:
        amount = matching.MatchInputs.parse_amount(req.amount)
    except ValueError as exc:
        raise LookupRefused(str(exc)) from exc
    tx = _tx_shape(family, req.tx_hash) if req.tx_hash else None
    with store.actor_transaction(conn, actor):
        lookup_id = conn.execute(
            "INSERT INTO lookup (family, submitted_address, clerk_user_id, session_hash, ip_hash, claimed_at,"
            " chain_hint, asset, amount, sent_on, sending_address, tx_hash, platform_name, platform_url)"
            " VALUES (%s::chain_family, %s, %s, %s, %s, CASE WHEN %s::text IS NULL THEN NULL ELSE now() END,"
            " %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text",
            (family, address, user_id, session_hash, ip_hash, user_id, req.chain, (req.asset or "").strip().upper() or None, amount, req.sent_on,
             sender, tx, req.platform_name, req.platform_url)).fetchone()[0]
        for chain in chains:
            job_id, _ = jobs.enqueue_lookup(conn, chain, address)
            conn.execute("INSERT INTO lookup_job (lookup_id, chain, job_id) VALUES (%s, %s, %s)",
                         (lookup_id, chain, job_id))
        if req.wants_match():
            jobs.enqueue_match(conn, lookup_id)
    return lookup_id, chains


# -- reading -----------------------------------------------------------------------------------

def _jobs(conn, lookup_id: str) -> list[tuple]:
    return conn.execute(
        "SELECT lj.chain, j.id, j.status::text, j.last_error, j.finished_at FROM lookup_job lj"
        " JOIN job j ON j.id = lj.job_id WHERE lj.lookup_id = %s ORDER BY lj.chain", (lookup_id,)).fetchall()


def _iso(t):
    return t.astimezone(dt.timezone.utc).isoformat() if t else None


def _progress(conn, lookup_id: str) -> dict[str, dict]:
    """Per chain: what its job is doing, for the waiting page. No error text: a code."""
    rows = conn.execute(
        "SELECT lj.chain, j.status::text, j.step, j.step_at, j.locked_at, j.attempts, j.max_attempts, j.run_after,"
        " j.failure_kind, j.created_at, j.finished_at FROM lookup_job lj JOIN job j ON j.id = lj.job_id"
        " WHERE lj.lookup_id = %s", (lookup_id,)).fetchall()
    return {chain: {"step": step if status == "running" else None, "step_since": _iso(step_at) if status == "running" else None,
                    "started_at": _iso(locked) if status == "running" else None, "attempts": attempts,
                    "max_attempts": max_attempts,
                    "next_attempt_at": _iso(run_after) if status == "queued" else None,
                    "failure_kind": kind if status in ("queued", "failed") else None,
                    "queued_at": _iso(created), "finished_at": _iso(finished)}
            for chain, status, step, step_at, locked, attempts, max_attempts, run_after, kind, created, finished in rows}


def report_for(conn, actor: str, lookup_id: str, chain: str, address: str, finished_at) -> dict | None:
    """The report this lookup was shown for a chain: the one linked in lookup_report, or
    else the newest report for the address made by the time its job finished (linked
    now, so the lookup keeps pointing at it)."""
    sql = ("SELECT r.id, r.public_id::text, r.sha256, r.as_of_block, r.as_of_time, r.snapshot_id FROM report r"
           " JOIN address a ON a.id = r.address_id")
    row = conn.execute(sql + " JOIN lookup_report lr ON lr.report_id = r.id WHERE lr.lookup_id = %s AND a.chain = %s"
                             " ORDER BY r.id DESC LIMIT 1", (lookup_id, chain)).fetchone()
    if row is None and finished_at is not None:
        row = conn.execute(sql + " WHERE a.chain = %s AND a.address = %s AND r.created_at <= %s"
                                 " ORDER BY r.created_at DESC, r.id DESC LIMIT 1",
                           (chain, address, finished_at)).fetchone()
        if row is not None:
            with store.actor_transaction(conn, actor):
                conn.execute("INSERT INTO lookup_report (lookup_id, report_id) VALUES (%s, %s)"
                             " ON CONFLICT DO NOTHING", (lookup_id, row[0]))
    if row is None:
        return None
    return {"id": row[0], "public_id": row[1], "sha256": row[2].strip(), "as_of_block": row[3],
            "as_of_time": row[4], "snapshot_id": row[5]}


def get(conn, actor: str, lookup_id: str, user_id: str) -> dict | None:
    """The lookup, for its owner only. None for anyone else, and for a redacted lookup."""
    row = conn.execute(
        "SELECT id::text, family::text, submitted_address, created_at, match_result, match_checked_at"
        " FROM lookup WHERE id = %s AND clerk_user_id = %s AND redacted_at IS NULL",
        (lookup_id, user_id)).fetchone()
    if row is None:
        return None
    lid, family, address, created, match, checked = row
    chains = []
    progress = _progress(conn, lid)
    for chain, job_id, status, error, finished in _jobs(conn, lid):
        rep = report_for(conn, actor, lid, chain, address, finished) if status == "done" else None
        chains.append({"chain": chain, "job_status": status, **progress.get(chain, {}),
                       "report": None if rep is None else {
                           "public_id": rep["public_id"], "sha256": rep["sha256"], "as_of_block": rep["as_of_block"],
                           "as_of_time": rep["as_of_time"].astimezone(dt.timezone.utc).isoformat()}})
    return {"id": lid, "family": family, "address": address, "created_at": created.isoformat(), "chains": chains,
            "match": match, "match_checked_at": checked.isoformat() if checked else None}


class NothingToRetry(ValueError):
    pass


def retry(conn, actor: str, lookup_id: str, user_id: str) -> str:
    """Start the lookup again, as a new lookup with the same inputs, for its owner. Only
    when one of its jobs failed for good. Limits are checked by the caller."""
    row = conn.execute(
        "SELECT submitted_address, chain_hint, asset, amount, sent_on, sending_address, tx_hash, platform_name,"
        " platform_url FROM lookup WHERE id = %s AND clerk_user_id = %s AND redacted_at IS NULL",
        (lookup_id, user_id)).fetchone()
    if row is None:
        raise LookupError(lookup_id)
    if not any(status == "failed" for _, _, status, _, _ in _jobs(conn, lookup_id)):
        raise NothingToRetry("nothing failed in this lookup")
    address, chain, asset, amount, sent_on, sender, tx, pname, purl = row
    new_id, _ = create(conn, actor, LookupRequest(
        address=address, chain=chain, asset=asset, amount=None if amount is None else str(amount), sent_on=sent_on,
        sending_address=sender, tx_hash=tx, platform_name=pname, platform_url=purl), user_id=user_id)
    return new_id


CLAIM_WINDOW = dt.timedelta(days=1)


def claim(conn, actor: str, user_id: str, session_hash: str) -> list[str]:
    """Give the signed-in account every unclaimed lookup made in this browser session
    within the last day. Returns the lookup ids claimed."""
    with store.actor_transaction(conn, actor):
        ids = [r[0] for r in conn.execute(
            "UPDATE lookup SET clerk_user_id = %s, claimed_at = now() WHERE session_hash = %s"
            " AND clerk_user_id IS NULL AND redacted_at IS NULL AND created_at > now() - %s RETURNING id::text",
            (user_id, session_hash, CLAIM_WINDOW)).fetchall()]
        for lid in ids:
            for chain, address in conn.execute(
                    "SELECT lj.chain, l.submitted_address FROM lookup_job lj JOIN lookup l ON l.id = lj.lookup_id"
                    " WHERE lj.lookup_id = %s", (lid,)).fetchall():
                refresh_count(conn, chain, address)
    return ids


def list_mine(conn, user_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id::text, family::text, submitted_address, created_at FROM lookup"
        " WHERE clerk_user_id = %s AND redacted_at IS NULL ORDER BY created_at DESC LIMIT 200", (user_id,)).fetchall()
    return [{"id": i, "family": f, "address": a, "created_at": c.isoformat()} for i, f, a, c in rows]


def redact(conn, actor: str, lookup_id: str, user_id: str) -> bool:
    """Delete a lookup for its owner (lookup_redact keeps only the fact that it happened),
    then recount who has looked up the address."""
    with store.actor_transaction(conn, actor):
        row = conn.execute("SELECT submitted_address FROM lookup WHERE id = %s AND clerk_user_id = %s"
                           " AND redacted_at IS NULL FOR UPDATE", (lookup_id, user_id)).fetchone()
        if row is None:
            return False
        chains = [r[0] for r in conn.execute("SELECT chain FROM lookup_job WHERE lookup_id = %s", (lookup_id,))]
        conn.execute("SELECT lookup_redact(%s)", (lookup_id,))
        for chain in chains:
            refresh_count(conn, chain, row[0])
    return True


def retention_days() -> int:
    """How long a lookup is kept before it is erased like a Delete (LOOKUP_RETENTION_DAYS,
    default 365). The Privacy page states this period."""
    import os
    raw = (os.environ.get("LOOKUP_RETENTION_DAYS") or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 365


def expire(conn, actor: str, days: int | None = None) -> int:
    """Erase every lookup older than the retention period with lookup_redact, then
    recount who has looked up each address. Returns how many were erased."""
    days = days or retention_days()
    with store.actor_transaction(conn, actor):
        rows = conn.execute(
            "SELECT l.id::text, l.submitted_address, array_remove(array_agg(lj.chain), NULL) FROM lookup l"
            " LEFT JOIN lookup_job lj ON lj.lookup_id = l.id WHERE l.redacted_at IS NULL"
            " AND l.created_at < now() - make_interval(days => %s) GROUP BY l.id", (days,)).fetchall()
        for lid, _, _ in rows:
            conn.execute("SELECT lookup_redact(%s)", (lid,))
        for _, address, chains in rows:
            for chain in chains or []:
                refresh_count(conn, chain, address)
    return len(rows)


def refresh_count(conn, chain: str, address: str) -> int | None:
    """Recompute lookup_count for one address on one chain: distinct signed-in accounts
    whose lookups covered it. Run by the worker; the report path reads only the result."""
    row = conn.execute("SELECT id FROM address WHERE chain = %s AND address = %s", (chain, address)).fetchone()
    if row is None:
        return None
    n = conn.execute(
        "SELECT count(DISTINCT l.clerk_user_id) FROM lookup l JOIN lookup_job lj ON lj.lookup_id = l.id"
        " WHERE lj.chain = %s AND l.submitted_address = %s AND l.clerk_user_id IS NOT NULL"
        " AND l.redacted_at IS NULL", (chain, address)).fetchone()[0]
    conn.execute(
        "INSERT INTO lookup_count (address_id, account_count, updated_at) VALUES (%s, %s, now())"
        " ON CONFLICT (address_id) DO UPDATE SET account_count = EXCLUDED.account_count, updated_at = now()",
        (row[0], n))
    return n


def others_count(conn, address_id: int, viewer: str | None) -> int | None:
    """How many other signed-in accounts have looked up this address: lookup_count, less
    the viewer's own account when it is among them. The only lookup rows read are the
    viewer's own. None when no count has been written for the address."""
    total = prior_count(conn, address_id)
    if total is None or not viewer:
        return total
    mine = conn.execute(
        "SELECT 1 FROM lookup l JOIN lookup_job lj ON lj.lookup_id = l.id JOIN address a ON a.id = %s"
        " WHERE l.clerk_user_id = %s AND l.redacted_at IS NULL AND lj.chain = a.chain"
        " AND l.submitted_address = a.address LIMIT 1", (address_id, viewer)).fetchone()
    return max(0, total - (1 if mine else 0))


def prior_count(conn, address_id: int) -> int | None:
    row = conn.execute("SELECT account_count FROM lookup_count WHERE address_id = %s", (address_id,)).fetchone()
    return row[0] if row else None


# -- matching ---------------------------------------------------------------------------------

def _save_fetched(conn, actor: str, fetch) -> None:
    """Store the transfers of a transaction read by hash, like any other observation."""
    if not fetch.records:
        return
    with store.actor_transaction(conn, actor):
        ids = store.upsert_addresses(conn, fetch.chain, {a for r in fetch.records
                                                         for a in (r.from_address, r.to_address) if a})
        raw = store.raw_response_ids(conn, fetch.source_slug, [r.raw_sha256 for r in fetch.records])
        store.insert_transfers(conn, fetch.records, ids, raw)


def _transfer_id(conn, row: dict, source: str) -> int | None:
    r = conn.execute(
        "SELECT t.id FROM transfer t JOIN source s ON s.id = t.source_id WHERE t.chain = %s AND t.tx_hash = %s"
        " AND t.kind = %s::transfer_kind AND t.event_index = %s AND s.slug = %s",
        (row["chain"], row["tx_hash"], row["kind"], row["event_index"], source)).fetchone()
    return r[0] if r else None


def balances_trusted(snapshot: dict, chain: str):
    """contract -> whether a running balance of that asset can be stated: the whole
    history was read and the asset's balance reconciled (Bitcoin: the native check passed)."""
    if snapshot.get("transfers_truncated") or snapshot.get("history_incomplete") is not False:
        return lambda contract: False
    ok = {t["contract"] if chain == "tron" else t["contract"].lower()
          for t in snapshot.get("token_reconciliation") or [] if t["status"] == "reconciled"}
    native = any(ch["name"] == "native_reconciliation" and ch["status"] == "passed"
                 for ch in snapshot.get("checks") or [])

    def trusted(contract):
        if contract is None:
            return chain == "btc" and native
        return (contract if chain == "tron" else contract.lower()) in ok
    return trusted


def run_match(conn, actor: str, lookup_id: str, *, adapter_for, raw_store) -> str:
    """Match the lookup's inputs and store the result on the lookup row. Returns the
    result's status, or 'wait' while an address job is still queued or running."""
    row = conn.execute(
        "SELECT family::text, submitted_address, chain_hint, asset, amount, sent_on, sending_address, tx_hash,"
        " redacted_at FROM lookup WHERE id = %s", (lookup_id,)).fetchone()
    if row is None:
        return "no_lookup"
    family, address, hint, asset, amount, sent_on, sender, tx, redacted = row
    if redacted is not None:
        return "redacted"
    rows = _jobs(conn, lookup_id)
    if any(status in ("queued", "running") for _, _, status, _, _ in rows):
        return "wait"
    inp = matching.MatchInputs(asset, Decimal(amount) if amount is not None else None, sent_on, sender, tx)
    per_chain: dict[str, dict] = {}
    sources: dict[str, str] = {}
    now = dt.datetime.now(dt.timezone.utc)
    for chain, _, status, _, finished in rows:
        if hint and chain != hint:
            continue
        if status != "done":
            per_chain[chain] = {"status": "not_found", "can_rule_out": False, "reason": "lookup_failed"}
            continue
        rep = report_for(conn, actor, lookup_id, chain, address, finished)
        if rep is None or rep["snapshot_id"] is None:
            per_chain[chain] = {"status": "not_found", "can_rule_out": False, "reason": "no_report"}
            continue
        data = report_inputs.load(conn, rep["snapshot_id"], now)
        oldest = min((t.block_time for t in data.transfers), default=None)
        trusted = balances_trusted(data.snapshot, chain)
        if tx:
            fetch = adapter_for(chain, raw_store).fetch_transaction(tx)
            _save_fetched(conn, actor, fetch)
            per_chain[chain] = matching.match_fetched(chain, address, fetch, data.transfers, trusted)
            sources[chain] = fetch.source_slug
        else:
            per_chain[chain] = matching.match_transfers(chain, address, data.transfers, inp,
                                                        truncated=data.snapshot["transfers_truncated"],
                                                        oldest_read=oldest, trusted=trusted)
            sources[chain] = data.source
    result = matching.combine(per_chain) if per_chain else {"status": "not_found", "reason": "no_chain"}
    matched_id = method = None
    if result["status"] == "matched":
        chain = result["transfer"]["chain"]
        matched_id = _transfer_id(conn, result["transfer"], sources[chain])
        method = result["method"] if matched_id else None
    with store.actor_transaction(conn, actor):
        conn.execute("UPDATE lookup SET match_result = %s::jsonb, match_checked_at = now(), matched_transfer_id = %s,"
                     " match_method = %s WHERE id = %s",
                     (json.dumps(result, sort_keys=True, default=str), matched_id, method, lookup_id))
    return result["status"]
