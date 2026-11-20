"""Reports, lookups, matching and the API against a real Postgres (migration 0017).

Skipped unless TEST_DATABASE_URL is set, like tests/test_store_pg.py, whose
throwaway-database fixtures these reuse. CI sets it; a skip fails the run there.
"""
import dataclasses
import datetime as dt
import json

import pytest

psycopg = pytest.importorskip("psycopg")

from taw import jobs, labels, lookups, store  # noqa: E402
from taw.adapters.base import bucket_key_for  # noqa: E402
from taw.adapters.labels.base import LabelClaim  # noqa: E402
from taw.report import store as report_store  # noqa: E402
from taw.report.build import build  # noqa: E402
from taw.worker import enrich  # noqa: E402
from tests import report_fixtures as rf  # noqa: E402
from tests.test_store_pg import ADMIN_URL, conns, db_url, template_db  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="TEST_DATABASE_URL not set")
ACTOR = "worker:test"
KNWAN = "ddc-1-25-cv-02967-usdt"
CASE3 = "wdpa-2-26-cv-00087-usdt"


def seed_raw(conn, slug: str, shas) -> None:
    """raw_response rows for fixture responses (their bodies are not in the repo)."""
    sid = store.source_id(conn, slug)
    have = {r[0].strip() for r in conn.execute("SELECT sha256 FROM raw_response WHERE source_id = %s", (sid,))}
    for sha in sorted(set(shas) - have):
        conn.execute("INSERT INTO raw_response (source_id, sha256, bucket_key, request_url, http_status, byte_size)"
                     " VALUES (%s, %s, %s, 'fixture', 200, 0)", (sid, sha, bucket_key_for(sha)))


def save_case(data, raw, case: str) -> int:
    """Store a case as the worker would: lookup, freeze checks, GoPlus, labels."""
    inp, snap, batch, recon = rf.case_input(case)
    chain = rf.read_chain(case)
    stored = rf.read_stored(case)
    seed_raw(raw, snap.source_slug, list(snap.raw_sha256) + [t.raw_sha256 for t in batch.transfers])
    saved = store.save_lookup(data, ACTOR, snap, batch, recon=recon)
    checks = rf.freeze_checks(snap.chain, snap.address, chain["freeze"], stored)
    if checks:
        raw_source = "trongrid" if snap.chain == "tron" else "blockscout"
        seed_raw(raw, raw_source, [c.raw_sha256 for c in checks])
        enrich.save_freeze_checks(data, ACTOR, snap.chain, snap.address, checks)
    if chain.get("goplus"):
        gp = chain["goplus"]
        seed_raw(raw, "goplus", [gp["raw_sha256"]])
        labels.record_check(data, ACTOR, "goplus", snap.chain, snap.address, subject="address_security", block=None,
                            result=gp["result"], detail=gp["detail"], raw_sha256=gp["raw_sha256"], raw_source="goplus")
    for address, ls in stored["counterparty_labels"].items():
        for l in ls:
            claim = LabelClaim(snap.chain, address, l["category"], l["label"], f"test:{address}",
                               dt.date.fromisoformat(l["source_date"]), l["claim_url"])
            labels.sync(data, ACTOR, l["source"], [claim], raw_sha256=None, source_date=claim.source_date,
                        only_address=(snap.chain, address))
    return saved.snapshot_id


def strip_times(d):
    """Drop the fields that depend on when rows were written (read dates, cut-offs)."""
    if isinstance(d, dict):
        return {k: strip_times(v) for k, v in d.items() if k not in ("observed_at", "read_at", "labels_as_of", "list_date",
                                                                       "fetched_at")}
    if isinstance(d, list):
        return [strip_times(x) for x in d]
    return d


def test_a_report_from_the_database_matches_the_fixture_build(conns):
    data, raw = conns
    sid = save_case(data, raw, KNWAN)
    first = report_store.build_and_store(data, ACTOR, sid)
    stored = data.execute("SELECT data, snapshot_id FROM report WHERE id = %s", (first.id,)).fetchone()
    assert stored[1] == sid and report_store.verify(data, first.id)
    inp, *_ = rf.case_input(KNWAN)
    expected = build(inp)
    for section in ("holdings", "activity", "first_hop", "set_apart", "completeness", "links"):
        assert strip_times(stored[0][section]) == strip_times(expected[section]), section
    okx = [r for r in stored[0]["first_hop"]["destinations"] + stored[0]["first_hop"]["funders"] if r["labels"]]
    assert okx and okx[0]["labels"][0]["source"] == "okx-por"


def test_building_again_from_the_same_snapshot_reuses_the_row(conns):
    data, raw = conns
    sid = save_case(data, raw, KNWAN)
    cut = dt.datetime.now(dt.timezone.utc)
    a = report_store.build_and_store(data, ACTOR, sid, cut)
    b = report_store.build_and_store(data, ACTOR, sid, cut)
    assert (b.id, b.sha256, b.reused) == (a.id, a.sha256, True)
    assert data.execute("SELECT count(*) FROM report WHERE snapshot_id = %s", (sid,)).fetchone()[0] == 1
    logged = data.execute("SELECT payload FROM event_log WHERE entity = 'report' AND entity_id = %s",
                          (str(a.id),)).fetchone()[0]
    assert logged["sha256"] == a.sha256                     # the audit entry carries the hash, not the body


def test_labels_recorded_after_the_cut_off_are_not_in_the_report(conns):
    data, raw = conns
    sid = save_case(data, raw, KNWAN)
    cut = dt.datetime.now(dt.timezone.utc)
    before = report_store.build_and_store(data, ACTOR, sid, cut)
    addr = rf.CASES[KNWAN][1]
    labels.sync(data, ACTOR, "ofac-sdn", [LabelClaim("tron", addr, "sanctioned", "listed by OFAC: TEST", "t:1",
                                                     dt.date(2026, 11, 20))],
                raw_sha256=None, source_date=dt.date(2026, 11, 20), only_address=("tron", addr))
    again = report_store.build_and_store(data, ACTOR, sid, cut)
    assert again.sha256 == before.sha256
    later = report_store.build_and_store(data, ACTOR, sid)
    assert later.sha256 != before.sha256
    row = data.execute("SELECT data FROM report WHERE id = %s", (later.id,)).fetchone()[0]
    assert row["holdings"]["ofac"]["listed"] is True


def test_report_rows_stay_immutable(conns):
    data, raw = conns
    rep = report_store.build_and_store(data, ACTOR, save_case(data, raw, KNWAN))
    with pytest.raises(psycopg.errors.RaiseException):
        data.execute("UPDATE report SET sha256 = %s WHERE id = %s", ("0" * 64, rep.id))


# -- lookups ----------------------------------------------------------------------------------------

def finish_jobs(data, chain_reports: dict):
    """Mark every queued address job done, building the report where a snapshot exists."""
    for job_id, payload in data.execute("SELECT id, payload FROM job WHERE kind = %s AND status = 'queued'",
                                        (jobs.LOOKUP,)).fetchall():
        sid = chain_reports.get(payload["chain"])
        if sid:
            report_store.build_and_store(data, ACTOR, sid)
        jobs.mark_done(data, job_id)


def test_a_0x_lookup_starts_one_job_per_chain_checked(conns):
    data, _ = conns
    lid, chains = lookups.create(data, "api", "user-a", lookups.LookupRequest(address=rf.CASES[CASE3][1].upper()
                                                                             .replace("0X", "0x")))
    assert chains == ("eth", "arbitrum", "optimism")
    rows = data.execute("SELECT lj.chain, j.payload FROM lookup_job lj JOIN job j ON j.id = lj.job_id"
                        " WHERE lj.lookup_id = %s ORDER BY lj.chain", (lid,)).fetchall()
    assert [r[0] for r in rows] == ["arbitrum", "eth", "optimism"]
    assert all(set(r[1]) == {"chain", "address"} for r in rows)      # nothing about the person in a job
    assert data.execute("SELECT count(*) FROM job WHERE kind = %s", (jobs.MATCH,)).fetchone()[0] == 0


def test_only_the_owner_can_read_a_lookup(conns):
    data, raw = conns
    sid = save_case(data, raw, KNWAN)
    lid, _ = lookups.create(data, "api", "user-a", lookups.LookupRequest(address=rf.CASES[KNWAN][1]))
    finish_jobs(data, {"tron": sid})
    mine = lookups.get(data, "api", lid, "user-a")
    assert mine["chains"][0]["job_status"] == "done" and mine["chains"][0]["report"]["public_id"]
    assert lookups.get(data, "api", lid, "user-b") is None
    linked = data.execute("SELECT count(*) FROM lookup_report WHERE lookup_id = %s", (lid,)).fetchone()[0]
    assert linked == 1


def test_lookup_count_counts_distinct_signed_in_accounts(conns):
    data, _ = conns
    addr = rf.CASES[KNWAN][1]
    for who in ("a", "b", "a"):
        lookups.create(data, "api", who, lookups.LookupRequest(address=addr))
    store.upsert_addresses(data, "tron", [addr])
    assert lookups.refresh_count(data, "tron", addr) == 2


def test_refused_requests(conns):
    data, _ = conns
    for req in (lookups.LookupRequest(address="0x" + "1" * 39), lookups.LookupRequest(address=rf.CASES[KNWAN][1],
                                                                                       chain="eth"),
                lookups.LookupRequest(address=rf.CASES[KNWAN][1], tx_hash="abc"),
                lookups.LookupRequest(address=rf.CASES[KNWAN][1], amount="-5")):
        with pytest.raises(lookups.LookupRefused):
            lookups.create(data, "api", "u", req)


class FakeAdapter:
    def __init__(self, fetch):
        self.fetch = fetch
        self.asked = []

    def fetch_transaction(self, tx):
        self.asked.append(tx)
        if tx == self.fetch.tx_hash:
            return self.fetch
        return dataclasses.replace(self.fetch, found=False, records=(), note="unknown")


def case3_lookup(data, raw, **inputs):
    sid = save_case(data, raw, CASE3)
    lid, _ = lookups.create(data, "api", "victim", lookups.LookupRequest(address=rf.CASES[CASE3][1], **inputs))
    finish_jobs(data, {"eth": sid})
    return lid


def test_match_by_hash_is_stored_on_the_lookup_and_nowhere_else(conns):
    data, raw = conns
    lid = case3_lookup(data, raw, tx_hash=rf.CASE3_VICTIM_TX, amount="242209.53", asset="USDC")
    fetch = rf.fetch_from(rf.read_chain(CASE3)["transactions"][0])
    seed_raw(raw, fetch.source_slug, [r.raw_sha256 for r in fetch.records])
    adapter = FakeAdapter(fetch)
    assert lookups.run_match(data, ACTOR, lid, adapter_for=lambda chain, rs: adapter, raw_store=None) == "matched"
    result, method, tid = data.execute("SELECT match_result, match_method, matched_transfer_id FROM lookup"
                                       " WHERE id = %s", (lid,)).fetchone()
    assert result["status"] == "matched" and method == "tx_hash" and tid is not None
    assert result["next_outgoing"][0]["amount"] == "230000"
    amount = data.execute("SELECT amount_raw FROM transfer WHERE id = %s", (tid,)).fetchone()[0]
    assert int(amount) == 242_209_413_589
    leaked = data.execute("SELECT count(*) FROM event_log WHERE payload::text LIKE %s OR payload::text LIKE %s",
                          ("%60b23beb%", "%242209%")).fetchone()[0]
    assert leaked == 0
    reports = data.execute("SELECT count(*) FROM report WHERE data::text LIKE %s", ("%242209.53%",)).fetchone()[0]
    assert reports == 0
    assert lookups.get(data, "api", lid, "victim")["match"]["status"] == "matched"


def test_match_without_a_hash_and_a_match_waits_for_the_address_jobs(conns):
    data, raw = conns
    sid = save_case(data, raw, CASE3)
    lid, _ = lookups.create(data, "api", "victim", lookups.LookupRequest(
        address=rf.CASES[CASE3][1], amount="242209.53", sent_on=dt.date(2025, 5, 10), asset="USDC"))
    assert lookups.run_match(data, ACTOR, lid, adapter_for=None, raw_store=None) == "wait"
    finish_jobs(data, {"eth": sid})
    status = lookups.run_match(data, ACTOR, lid, adapter_for=None, raw_store=None)
    result = data.execute("SELECT match_result FROM lookup WHERE id = %s", (lid,)).fetchone()[0]
    # arbitrum and optimism finished with no report, eth matched: one match overall
    assert status == "matched" and result["method"] == "amount_date" and result["matched_chain"] == "eth"


def test_redaction_clears_the_match(conns):
    data, raw = conns
    lid = case3_lookup(data, raw, amount="242209.53", sent_on=dt.date(2025, 5, 10))
    lookups.run_match(data, ACTOR, lid, adapter_for=None, raw_store=None)
    data.execute("SELECT lookup_redact(%s)", (lid,))
    row = data.execute("SELECT match_result, match_checked_at, matched_transfer_id, amount, redacted_at FROM lookup"
                       " WHERE id = %s", (lid,)).fetchone()
    assert row[:4] == (None, None, None, None) and row[4] is not None
    assert lookups.run_match(data, ACTOR, lid, adapter_for=None, raw_store=None) == "redacted"


def test_lookup_job_rows_are_append_only(conns):
    data, _ = conns
    lid, _ = lookups.create(data, "api", "u", lookups.LookupRequest(address=rf.CASES[KNWAN][1]))
    with pytest.raises(psycopg.errors.RaiseException):
        data.execute("DELETE FROM lookup_job WHERE lookup_id = %s", (lid,))


# -- the API ------------------------------------------------------------------------------------------

@pytest.fixture
def client(db_url, monkeypatch):
    from fastapi.testclient import TestClient

    from taw import api
    from taw.main import app

    def conn():
        c = psycopg.connect(db_url, autocommit=True)
        try:
            yield c
        finally:
            c.close()
    app.dependency_overrides[api.conn] = conn
    monkeypatch.setenv("TAW_API_ENABLED", "1")
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_the_api_is_off_unless_the_setting_is_on(client, monkeypatch):
    monkeypatch.setenv("TAW_API_ENABLED", "")
    for method, path in (("post", "/api/v1/lookups"), ("get", "/api/v1/lookups/x"), ("get", "/api/v1/reports/x")):
        assert getattr(client, method)(path, headers={"X-TAW-User": "a"}).status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_the_api_end_to_end(client, db_url):
    addr = rf.CASES[KNWAN][1]
    assert client.post("/api/v1/lookups", json={"address": addr}).status_code == 401
    r = client.post("/api/v1/lookups", json={"address": addr}, headers={"X-TAW-User": "owner"})
    assert r.status_code == 202 and r.json()["chains"] == ["tron"]
    lid = r.json()["id"]
    bad = client.post("/api/v1/lookups", json={"address": "T" + "1" * 33}, headers={"X-TAW-User": "owner"})
    assert bad.status_code == 422
    assert client.get(f"/api/v1/lookups/{lid}", headers={"X-TAW-User": "someone-else"}).status_code == 404
    with psycopg.connect(db_url, autocommit=True) as data, psycopg.connect(db_url, autocommit=True) as raw:
        sid = save_case(data, raw, KNWAN)
        finish_jobs(data, {"tron": sid})
        lookups.refresh_count(data, "tron", addr)
    got = client.get(f"/api/v1/lookups/{lid}", headers={"X-TAW-User": "owner"}).json()
    public_id = got["chains"][0]["report"]["public_id"]
    rep = client.get(f"/api/v1/reports/{public_id}", headers={"X-TAW-User": "anyone"}).json()
    assert rep["address"] == addr and rep["prior_lookups"] == "1" and len(rep["sha256"]) == 64
    assert "prior" not in json.dumps(rep["data"])
    text = client.get(f"/api/v1/reports/{public_id}?format=text", headers={"X-TAW-User": "anyone"}).text
    assert "Tether destroyed 465,514.638375 USDT" in text and "looked up this address: 1" in text
