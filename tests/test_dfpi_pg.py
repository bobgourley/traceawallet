"""DFPI tracker against a throwaway database (migration 0021): the daily load, the
private platform check and its erasure. TEST_DATABASE_URL must be set (CI sets it)."""
import datetime as dt

import pytest

psycopg = pytest.importorskip("psycopg")

from taw import lookups, platform, store  # noqa: E402
from taw.adapters.labels import dfpi  # noqa: E402
from taw.worker import lists  # noqa: E402
from tests.test_dfpi import BECH32, CHECKSUMMED, ROWS, TRON, page  # noqa: E402
from tests.test_store_pg import ACTOR, ADMIN_URL, FakeS3, conns, count, db_url, template_db  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="TEST_DATABASE_URL not set")


class FakeTracker:
    """dfpi.DfpiTracker with the page given instead of fetched."""

    def __init__(self, raw, body: str, today=dt.date(2026, 11, 21)):
        self.inner = dfpi.DfpiTracker(store.RecordingRawStore(raw, ACTOR, s3_client=FakeS3(), bucket="test"),
                                      ["arbitrum", "eth", "optimism"], today=today)
        self.body = body.encode()
        self.inner.fetch = lambda: (self.body, self.inner.raw_store.save(dfpi.SOURCE, dfpi.URL, None, 200, self.body))

    def load(self):
        return self.inner.load()


def test_the_daily_load_keeps_labels_and_sites_and_skips_an_unchanged_page(conns):
    data, raw = conns
    note = lists.refresh_dfpi(data_conn=data, raw_store=None, actor=ACTOR, source=FakeTracker(raw, page(ROWS)))
    assert "3 entries" in note and "1 address(es) without a checksum and 1 failing it left out" in note
    assert count(data, "SELECT count(*) FROM site_list_entry") == 3
    found = data.execute("SELECT a.chain, l.category, l.source_date FROM label_current l JOIN address a"
                         " ON a.id = l.address_id JOIN source s ON s.id = l.source_id WHERE s.slug = 'dfpi-tracker'"
                         " ORDER BY 1").fetchall()
    assert [c for c, _, _ in found] == ["arbitrum", "btc", "eth", "optimism", "tron"]
    assert {(cat, d) for _, cat, d in found} == {("scam_report", dt.date(2026, 11, 21))}
    again = lists.refresh_dfpi(data_conn=data, raw_store=None, actor=ACTOR, source=FakeTracker(raw, page(ROWS)))
    assert again == "DFPI tracker unchanged (3 entries)"
    assert count(data, "SELECT count(*) FROM site_list_load") == 1
    with pytest.raises(psycopg.errors.RaiseException):
        data.execute("DELETE FROM site_list_entry")


def test_an_entry_dropped_from_the_page_withdraws_its_labels(conns):
    data, raw = conns
    lists.refresh_dfpi(data_conn=data, raw_store=None, actor=ACTOR, source=FakeTracker(raw, page(ROWS)))
    later = FakeTracker(raw, page([ROWS[0], ROWS[2]]), today=dt.date(2026, 11, 22))
    lists.refresh_dfpi(data_conn=data, raw_store=None, actor=ACTOR, source=later)
    assert count(data, "SELECT count(*) FROM label_current l JOIN address a ON a.id = l.address_id"
                       " WHERE a.address IN (%s, %s)", TRON, BECH32) == 0
    assert count(data, "SELECT count(*) FROM label_current l JOIN address a ON a.id = l.address_id"
                       " WHERE a.address = %s", CHECKSUMMED.lower()) == 3      # still listed, first date kept


def test_the_platform_check_matches_websites_subdomains_and_names(conns):
    data, raw = conns
    assert platform.check(data, None, "hcxz.com")["lists"][0] == {
        "source": "dfpi-tracker", "loaded": False, "source_name": "California DFPI Crypto Scam Tracker"}
    lists.refresh_dfpi(data_conn=data, raw_store=None, actor=ACTOR, source=FakeTracker(raw, page(ROWS)))
    exact = platform.check(data, None, "https://www.hcxz.com/login")["lists"][0]
    assert exact["website_matches"] == 1 and exact["website_entries"][0] == {
        "subject": "hcxz.com gptss.com", "website": "hcxz.com", "exact": True}
    sub = platform.check(data, None, "app.gptss.com")["lists"][0]
    assert sub["website_entries"][0]["website"] == "gptss.com" and not sub["website_entries"][0]["exact"]
    assert platform.check(data, None, "xhcxz.com")["lists"][0]["website_matches"] == 0     # not a suffix match
    named = platform.check(data, "Noel Crypto", None)["lists"][0]
    assert named["name_matches"] == 1 and named["name_entries"] == [{"subject": "Noel Crypto"}]
    assert platform.check(data, "Noel", None)["lists"][0]["name_matches"] == 1
    assert platform.check(data, "Noe", None) == {"entered": platform.entered("Noe", None), "usable": False}


def test_the_match_job_stores_the_platform_result_privately_and_erasure_removes_it(conns):
    data, raw = conns
    lists.refresh_dfpi(data_conn=data, raw_store=None, actor=ACTOR, source=FakeTracker(raw, page(ROWS)))
    lid, _ = lookups.create(data, "api", lookups.LookupRequest(address="TGuqbYqJ4m1J8dKWB3NVnsuXtgXCQkgdyf",
                                                               platform_url="hcxz.com"), user_id="victim")
    assert count(data, "SELECT count(*) FROM job WHERE kind = 'lookup_match'") == 1
    assert lookups.run_match(data, ACTOR, lid, adapter_for=None, raw_store=None) == "platform_checked"
    got = lookups.get(data, "api", lid, "victim")
    assert got["platform"]["lists"][0]["website_matches"] == 1 and got["match"] is None
    assert lookups.get(data, "api", lid, "someone-else") is None
    assert lookups.redact(data, "api", lid, "victim")
    assert data.execute("SELECT platform_result, platform_checked_at, platform_url FROM lookup WHERE id = %s",
                        (lid,)).fetchone() == (None, None, None)
