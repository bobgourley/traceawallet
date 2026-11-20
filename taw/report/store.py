"""Build a report from a snapshot, hash it, store it once.

The same snapshot built with the same label cut-off and the same template gives
the same sha256 (taw/report/canonical.py), and the unique index on report
(snapshot_id, schema_version, template_version, sha256) keeps one row per build.
The report row's audit entry (migration 0001, audit_report) logs its hash.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from taw import store
from taw.report import SCHEMA_VERSION, TEMPLATE_VERSION, inputs
from taw.report.build import build
from taw.report.canonical import report_hash


@dataclass(frozen=True)
class StoredReport:
    id: int
    public_id: str
    sha256: str
    reused: bool


_FIND = ("SELECT id, public_id::text FROM report WHERE snapshot_id = %s AND schema_version = %s"
         " AND template_version = %s AND sha256 = %s")


def build_and_store(conn, actor: str, snapshot_id: int, labels_as_of: dt.datetime | None = None) -> StoredReport:
    """conn: an autocommit connection. labels_as_of defaults to now."""
    labels_as_of = labels_as_of or dt.datetime.now(dt.timezone.utc)
    inp = inputs.load(conn, snapshot_id, labels_as_of)
    data = build(inp)
    snap = inp.snapshot
    sha = report_hash(SCHEMA_VERSION, TEMPLATE_VERSION, inp.chain, inp.address, snap["as_of_block"], data)
    found = conn.execute(_FIND, (snapshot_id, SCHEMA_VERSION, TEMPLATE_VERSION, sha)).fetchone()
    if found:
        return StoredReport(found[0], found[1], sha, True)
    with store.actor_transaction(conn, actor):
        address_id = conn.execute("SELECT address_id FROM address_snapshot WHERE id = %s", (snapshot_id,)).fetchone()[0]
        row = conn.execute(
            "INSERT INTO report (address_id, snapshot_id, as_of_block, as_of_time, schema_version, template_version,"
            " data, sha256) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)"
            " ON CONFLICT (snapshot_id, schema_version, template_version, sha256) WHERE snapshot_id IS NOT NULL"
            " DO NOTHING RETURNING id, public_id::text",
            (address_id, snapshot_id, snap["as_of_block"], snap["as_of_time"], SCHEMA_VERSION, TEMPLATE_VERSION,
             json.dumps(data, sort_keys=True), sha)).fetchone()
    if row is None:                          # built by someone else at the same moment
        row = conn.execute(_FIND, (snapshot_id, SCHEMA_VERSION, TEMPLATE_VERSION, sha)).fetchone()
        return StoredReport(row[0], row[1], sha, True)
    return StoredReport(row[0], row[1], sha, False)


def verify(conn, report_id: int) -> bool:
    """Recompute a stored report's sha256 from its row."""
    schema, template, chain, address, block, data, sha = conn.execute(
        "SELECT r.schema_version, r.template_version, a.chain, a.address, r.as_of_block, r.data, r.sha256"
        " FROM report r JOIN address a ON a.id = r.address_id WHERE r.id = %s", (report_id,)).fetchone()
    return report_hash(schema, template, chain, address, block, data) == sha.strip()


def latest_for_snapshot(conn, snapshot_id: int) -> tuple[int, str] | None:
    row = conn.execute("SELECT id, public_id::text FROM report WHERE snapshot_id = %s ORDER BY id DESC LIMIT 1",
                       (snapshot_id,)).fetchone()
    return (row[0], row[1]) if row else None
