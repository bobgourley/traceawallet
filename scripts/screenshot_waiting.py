"""Screenshots of the waiting page in each state, for the owner's review.

    TEST_DATABASE_URL=postgresql://... python scripts/screenshot_waiting.py OUT_DIR

Like scripts/screenshot_pages.py: a throwaway database, the app on 127.0.0.1, a
fixed tester in place of Clerk inside this process only, Playwright driving the
installed Chrome at 390 px. Each state is set directly on the job row.
"""
from __future__ import annotations

import base64
import os
import pathlib
import sys
import threading
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PORT = 8766
TESTER = "tester@example.org"

STATES = [
    ("01-waiting-to-start", "UPDATE job SET status = 'queued', attempts = 0"),
    ("02-running-reading-transfers", "UPDATE job SET status = 'running', attempts = 1, locked_at = now() - interval "
                                     "'95 seconds', step = 'reading_transfers', step_at = now() - interval '41 seconds'"),
    ("03-running-second-try-checking-lists", "UPDATE job SET status = 'running', attempts = 2, locked_at = now() - "
                                             "interval '3 minutes', step = 'checking_lists', step_at = now() - "
                                             "interval '4 seconds'"),
    ("04-waiting-to-retry", "UPDATE job SET status = 'queued', attempts = 1, failure_kind = 'provider_slow', "
                            "run_after = now() + interval '2 minutes', last_error = 'ReadTimeout'"),
    ("05-waiting-for-allowance", "UPDATE job SET status = 'queued', attempts = 0, failure_kind = 'allowance', "
                                 "run_after = date_trunc('day', now()) + interval '1 day 5 minutes'"),
    ("06-failed-try-again", "UPDATE job SET status = 'failed', attempts = 3, failure_kind = 'provider_slow', "
                            "finished_at = now(), last_error = 'ReadTimeout'"),
]


def main(out: pathlib.Path) -> None:
    import psycopg
    admin = os.environ["TEST_DATABASE_URL"]
    name = f"taw_wait_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f'CREATE DATABASE "{name}"')
    url = psycopg.conninfo.make_conninfo(admin, dbname=name)
    with psycopg.connect(url, autocommit=True) as c:
        for path in sorted((ROOT / "migrations").glob("*.sql")):
            c.execute(path.read_text())
    os.environ.update({
        "DATABASE_URL": url, "TAW_API_URL": f"http://127.0.0.1:{PORT}", "HASH_SALT": "shots",
        "CLERK_PUBLISHABLE_KEY": "pk_test_" + base64.b64encode(b"placeholder.invalid$").decode().rstrip("="),
        "CLERK_SECRET_KEY": "unused", "TURNSTILE_SITE_KEY": "1x00000000000000000000AA",
        "TURNSTILE_SECRET_KEY": "1x0000000000000000000000000000000AA", "TESTER_EMAILS": TESTER,
        "APP_BASE_URL": f"http://127.0.0.1:{PORT}"})

    from taw import api, lookups
    from taw.auth import Identity
    from taw.main import app
    from tests import report_fixtures as rf
    from tests.test_report_pg import finish_jobs, save_case

    app.dependency_overrides[api.caller] = lambda: Identity("user_shots", TESTER)
    with psycopg.connect(url, autocommit=True) as data, psycopg.connect(url, autocommit=True) as raw:
        tron, _ = lookups.create(data, "api", lookups.LookupRequest(address=rf.CASES["sdny-1-26-cv-08010-usdt"][1]),
                                 user_id="user_shots")
        tron_job = data.execute("SELECT job_id FROM lookup_job WHERE lookup_id = %s", (tron,)).fetchone()[0]
        mixed, _ = lookups.create(data, "api", lookups.LookupRequest(address=rf.CASES["wdpa-2-26-cv-00087-usdt"][1]),
                                  user_id="user_shots")
        eth_sid = save_case(data, raw, "wdpa-2-26-cv-00087-usdt")
        busy_sid = save_case(data, raw, "busy-btc-summary-only")
        busy, _ = lookups.create(data, "api", lookups.LookupRequest(address=rf.CASES["busy-btc-summary-only"][1]),
                                 user_id="user_shots")

    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    out.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw, psycopg.connect(url, autocommit=True) as data:
        browser = pw.chromium.launch(channel="chrome")
        for label, sql in STATES:
            data.execute(sql + " WHERE id = %s", (tron_job,))
            shoot(browser, f"{base}/lookups/{tron}", out / f"{label}.png")
        # an 0x address: Ethereum ready, Arbitrum One reading, OP Mainnet waiting to retry
        jobs = dict(data.execute("SELECT chain, job_id FROM lookup_job WHERE lookup_id = %s", (mixed,)).fetchall())
        from taw.report import store as report_store
        report_store.build_and_store(data, "shots", eth_sid)
        data.execute("UPDATE job SET status = 'done', finished_at = now() WHERE id = %s", (jobs["eth"],))
        data.execute(STATES[1][1] + " WHERE id = %s", (jobs["arbitrum"],))
        data.execute(STATES[3][1] + " WHERE id = %s", (jobs["optimism"],))
        shoot(browser, f"{base}/lookups/{mixed}", out / "07-three-chains-one-ready.png")
        finish_jobs(data, {"btc": busy_sid})
        shoot(browser, f"{base}/lookups/{busy}", out / "08-busy-bitcoin-summary-only.png")
        browser.close()
    server.should_exit = True
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def shoot(browser, url: str, path: pathlib.Path) -> None:
    page = browser.new_page(viewport={"width": 390, "height": 800}, device_scale_factor=2)
    page.goto(url, wait_until="load")
    page.wait_for_timeout(800)
    page.screenshot(path=str(path), full_page=True)
    page.close()
    print(path.name)


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1]).expanduser())
