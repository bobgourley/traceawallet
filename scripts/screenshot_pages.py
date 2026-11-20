"""Screenshots of every page, for the owner's review before a merge.

    TEST_DATABASE_URL=postgresql://... python scripts/screenshot_pages.py OUT_DIR

Builds a throwaway database with every migration, stores two ground-truth cases
(W.D. Pa. with the victim's matched transaction, and S.D.N.Y.), serves the app on
127.0.0.1, and photographs each page with headless Chrome at phone width (390 px)
and the main report at desktop width (Playwright driving the installed Chrome,
full-page captures; `pip install playwright`, a local tool, not a dependency). Sign-in is stood in for inside this process
only: the API's caller dependency returns a fixed tester. Clerk's scripts point at
a placeholder host and do not load, so the sign-in box is empty in its picture.
"""
from __future__ import annotations

import os
import pathlib
import sys
import threading
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 8765
TESTER = "tester@example.org"


def main(out: pathlib.Path) -> None:
    import psycopg
    admin = os.environ["TEST_DATABASE_URL"]
    name = f"taw_shots_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f'CREATE DATABASE "{name}"')
    url = psycopg.conninfo.make_conninfo(admin, dbname=name)
    with psycopg.connect(url, autocommit=True) as c:
        for path in sorted((ROOT / "migrations").glob("*.sql")):
            c.execute(path.read_text())
    import base64
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
    from tests.test_report_pg import ACTOR, CASE3, finish_jobs, save_case

    who = {"id": Identity("user_shots", TESTER)}
    app.dependency_overrides[api.caller] = lambda: who["id"]

    with psycopg.connect(url, autocommit=True) as data, psycopg.connect(url, autocommit=True) as raw:
        sids = {"eth": save_case(data, raw, CASE3)}
        sdny_sid = save_case(data, raw, "sdny-1-26-cv-08010-usdt")
        case3, _ = lookups.create(data, "api", lookups.LookupRequest(
            address=rf.CASES[CASE3][1], amount="242209.53", sent_on=__import__("datetime").date(2025, 5, 10),
            asset="USDC"), user_id="user_shots")
        finish_jobs(data, sids)
        lookups.run_match(data, ACTOR, case3, adapter_for=None, raw_store=None)
        sdny, _ = lookups.create(data, "api", lookups.LookupRequest(address=rf.CASES["sdny-1-26-cv-08010-usdt"][1]),
                                 user_id="user_shots")
        finish_jobs(data, {"tron": sdny_sid})
        pending, _ = lookups.create(data, "api", lookups.LookupRequest(address="0x" + "7" * 40), user_id="user_shots")
        lookups.refresh_count(data, "tron", rf.CASES["sdny-1-26-cv-08010-usdt"][1])
        pid = data.execute("SELECT r.public_id::text FROM report r JOIN address a ON a.id = r.address_id"
                           " WHERE a.chain = 'tron'").fetchone()[0]

    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)

    out.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    shots = [
        ("01-home-form", "/", 390), ("01b-home-form-details-open", "/#details", 390),
        ("02-sign-in", "/sign-in?next=/account", 390),
        ("03-lookup-waiting", f"/lookups/{pending}", 390),
        ("04-lookup-case3-with-your-transaction", f"/lookups/{case3}", 390),
        ("05-shared-report-sdny", f"/reports/{pid}", 390),
        ("06-shared-report-sdny-desktop", f"/reports/{pid}", 1280),
        ("07-my-lookups", "/account", 390),
        ("08-about", "/about", 390), ("08b-goplus-credit-in-report", f"/lookups/{case3}#claims", 390), ("09-privacy", "/privacy", 390), ("10-terms", "/terms", 390),
    ]
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        signed_in = who["id"]
        who["id"] = None
        shoot(browser, base + "/", out / "00-signed-out-welcome.png", 390)
        who["id"] = signed_in
        for label, path, width in shots:
            shoot(browser, base + path, out / f"{label}.png", width)
        who["id"] = Identity("user_stranger", "stranger@example.org")
        shoot(browser, base + "/account", out / "11-invite-only.png", 390)
        browser.close()
    server.should_exit = True
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def shoot(browser, url: str, path: pathlib.Path, width: int) -> None:
    page = browser.new_page(viewport={"width": width, "height": 800}, device_scale_factor=2 if width < 600 else 1)
    page.goto(url.replace("#details", "").replace("#claims", ""), wait_until="load")
    page.wait_for_timeout(2500)                     # let the Turnstile widget draw
    if url.endswith("#details"):
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
    if url.endswith("#claims"):
        page.locator("summary", has_text="Claims about this address").first.screenshot(path=str(path))
        section = page.locator("details", has=page.locator("summary", has_text="Claims about this address")).first
        section.screenshot(path=str(path))
        page.close()
        print(path.name)
        return
    wide = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    page.screenshot(path=str(path), full_page=True)
    page.close()
    print(path.name, "OVERFLOWS SIDEWAYS" if wide else "")


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1]).expanduser())
