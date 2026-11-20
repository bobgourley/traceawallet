"""The website and the API behind real sign-in, against a throwaway Postgres.

Clerk is stood in for by an RSA key made here: tokens are signed with it and the
site verifies them with CLERK_JWT_KEY, exactly as it would Clerk's. Turnstile's
check is replaced by a function that passes the token "pass" and fails anything
else (its HTTP call has its own test below). The pages reach the API through a
TestClient, the way they reach it over loopback in production.
"""
import base64
import datetime as dt
import re
import time

import httpx
import pytest

psycopg = pytest.importorskip("psycopg")
jwt = pytest.importorskip("jwt")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from taw import auth, guard, lookups  # noqa: E402
from tests import report_fixtures as rf  # noqa: E402
from tests.test_report_pg import ACTOR, CASE3, KNWAN, finish_jobs, save_case  # noqa: E402
from tests.test_store_pg import ADMIN_URL, conns, db_url, template_db  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="TEST_DATABASE_URL not set")

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
BASE = "https://testserver"
TESTER, OTHER_TESTER, STRANGER = "tester@example.org", "second@example.org", "stranger@example.org"


def token(sub: str, email: str | None, *, azp: str = BASE, exp: int = 60, alg_key=KEY) -> str:
    now = int(time.time())
    claims = {"sub": sub, "iat": now, "nbf": now - 1, "exp": now + exp, "azp": azp}
    if email:
        claims["primaryEmail"] = email
    return jwt.encode(claims, alg_key, algorithm="RS256", headers={"kid": "test"})


@pytest.fixture
def site(monkeypatch):
    pk = "pk_test_" + base64.b64encode(b"example.clerk.accounts.dev$").decode().rstrip("=")
    for k, v in {"CLERK_PUBLISHABLE_KEY": pk, "CLERK_SECRET_KEY": "sk_test_unused", "CLERK_JWT_KEY": PEM,
                 "TURNSTILE_SITE_KEY": "1x00000000000000000000AA", "TURNSTILE_SECRET_KEY": "test-secret",
                 "TESTER_EMAILS": f"{TESTER}, {OTHER_TESTER}", "HASH_SALT": "test-salt",
                 "APP_BASE_URL": BASE}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(guard, "verify_turnstile",
                        lambda tok, ip, **kw: guard.TurnstileResult(tok == "pass", () if tok == "pass" else ("bad",)))


@pytest.fixture
def browser(site, db_url):
    from taw import api, web
    from taw.main import app

    def conn():
        c = psycopg.connect(db_url, autocommit=True)
        try:
            yield c
        finally:
            c.close()
    app.dependency_overrides[api.conn] = conn
    inner = TestClient(app, base_url=BASE)
    app.dependency_overrides[web.get_api] = lambda: web.Api(inner)
    yield TestClient(app, base_url=BASE, follow_redirects=False)
    app.dependency_overrides.clear()


@pytest.fixture
def before_sign_in(monkeypatch):
    """The start-before-sign-in path, which is off while the site is invite-only."""
    monkeypatch.setenv("LOOKUP_BEFORE_SIGN_IN", "1")


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([0-9a-f]{64})"', html).group(1)


def submit(client, address, *, turnstile="pass", ip="203.0.113.7", **extra):
    page = client.get("/", headers={"X-Real-IP": ip})
    data = {"csrf": csrf_of(page.text), "address": address, "cf-turnstile-response": turnstile, **extra}
    return client.post("/lookups", data=data, headers={"X-Real-IP": ip})


def sign_in(client, sub="user_tester", email=TESTER):
    client.cookies.set("__session", token(sub, email))


# -- the site stays closed until it is configured -------------------------------------------------

def test_unconfigured_site_shows_coming_soon_and_the_api_is_closed(db_url, monkeypatch):
    from taw.main import app
    for k in ("CLERK_PUBLISHABLE_KEY", "TESTER_EMAILS"):
        monkeypatch.delenv(k, raising=False)
    c = TestClient(app, base_url=BASE)
    assert "Coming soon" in c.get("/").text
    assert c.get("/api/v1/me").status_code == 503
    assert c.post("/api/v1/lookups", json={"address": rf.CASES[KNWAN][1]}).status_code == 503


# -- starting a lookup ------------------------------------------------------------------------

@pytest.mark.usefixtures("before_sign_in")
def test_a_lookup_starts_before_sign_in_and_signing_in_claims_it(browser, db_url):
    r = submit(browser, rf.CASES[KNWAN][1])
    assert r.status_code == 303 and r.headers["location"].startswith("/sign-in?next=/lookups/")
    lid = r.headers["location"].rsplit("/", 1)[1]
    with psycopg.connect(db_url) as c:
        user, session, ip = c.execute("SELECT clerk_user_id, session_hash, ip_hash FROM lookup WHERE id = %s",
                                      (lid,)).fetchone()
    assert user is None and session and ip and ip != "203.0.113.7"       # stored hashed, never in clear
    assert browser.get(f"/lookups/{lid}").status_code == 303           # results only after sign-in
    sign_in(browser)
    page = browser.get(f"/lookups/{lid}")
    assert page.status_code == 200 and "Reading public records" in page.text
    with psycopg.connect(db_url) as c:
        assert c.execute("SELECT clerk_user_id FROM lookup WHERE id = %s", (lid,)).fetchone()[0] == "user_tester"


@pytest.mark.usefixtures("before_sign_in")
def test_turnstile_failure_starts_nothing(browser, db_url):
    r = submit(browser, rf.CASES[KNWAN][1], turnstile="wrong")
    assert r.status_code == 400 and "We could not confirm you are a person" in r.text
    with psycopg.connect(db_url) as c:
        assert c.execute("SELECT count(*) FROM lookup").fetchone()[0] == 0


@pytest.mark.usefixtures("before_sign_in")
def test_forms_need_their_csrf_token(browser, db_url):
    browser.get("/")
    r = browser.post("/lookups", data={"csrf": "0" * 64, "address": rf.CASES[KNWAN][1],
                                       "cf-turnstile-response": "pass"})
    assert r.status_code == 400 and "expired" in r.text
    with psycopg.connect(db_url) as c:
        assert c.execute("SELECT count(*) FROM lookup").fetchone()[0] == 0


@pytest.mark.usefixtures("before_sign_in")
def test_limit_per_ip_before_sign_in(browser, monkeypatch):
    monkeypatch.setitem(guard.LIMITS, "ip_hour", 2)
    assert submit(browser, rf.CASES[KNWAN][1]).status_code == 303
    assert submit(browser, rf.CASES[KNWAN][1]).status_code == 303
    third = submit(browser, rf.CASES[KNWAN][1])
    assert third.status_code == 429 and "2 lookups in the past hour from this connection" in third.text
    assert submit(browser, rf.CASES[KNWAN][1], ip="198.51.100.9").status_code == 303   # another connection


def test_limit_per_account_after_sign_in(browser, monkeypatch):
    monkeypatch.setitem(guard.LIMITS, "account_hour", 1)
    sign_in(browser)
    assert submit(browser, rf.CASES[KNWAN][1]).status_code == 303
    r = submit(browser, rf.CASES[KNWAN][1], ip="198.51.100.10")
    assert r.status_code == 429 and "1 lookups in the past hour. Please wait an hour" in r.text


@pytest.mark.usefixtures("before_sign_in")
def test_site_wide_limit_on_anonymous_lookups(browser, monkeypatch):
    monkeypatch.setitem(guard.LIMITS, "anonymous_site_day", 1)
    assert submit(browser, rf.CASES[KNWAN][1], ip="198.51.100.1").status_code == 303
    r = submit(browser, rf.CASES[KNWAN][1], ip="198.51.100.2")
    assert r.status_code == 429 and "Please sign in first" in r.text


# -- invite-only and ownership ------------------------------------------------------------------

def test_invite_only(browser):
    sign_in(browser, "user_stranger", STRANGER)
    r = browser.get("/")
    assert r.status_code == 403 and "invite-only" in r.text
    stranger = {"Authorization": f"Bearer {token('user_stranger', STRANGER)}"}
    made = browser.post("/api/v1/lookups", json={"address": rf.CASES[KNWAN][1], "turnstile_token": "pass"},
                        headers=stranger)
    assert made.status_code == 403
    assert browser.get("/account").status_code == 403
    api = browser.get("/api/v1/lookups", headers={"Authorization": f"Bearer {token('user_stranger', STRANGER)}"})
    assert api.status_code == 403
    no_email = browser.get("/api/v1/me", headers={"Authorization": f"Bearer {token('user_x', None)}"}).json()
    assert no_email == {"signed_in": True, "invited": False, "email": None}


def test_only_the_owner_sees_a_lookup(browser):
    sign_in(browser)
    lid = submit(browser, rf.CASES[KNWAN][1]).headers["location"].rsplit("/", 1)[1]
    sign_in(browser, "user_second", OTHER_TESTER)
    assert browser.get(f"/lookups/{lid}").status_code == 404
    bearer = {"Authorization": f"Bearer {token('user_second', OTHER_TESTER)}"}
    assert browser.get(f"/api/v1/lookups/{lid}", headers=bearer).status_code == 404
    assert browser.delete(f"/api/v1/lookups/{lid}", headers=bearer).status_code == 404


def test_tokens_that_must_not_pass():
    import os
    os.environ.update({"CLERK_JWT_KEY": PEM, "APP_BASE_URL": BASE})
    try:
        assert auth.verify(token("u", TESTER)).user_id == "u"
        assert auth.verify(token("u", TESTER, exp=-60)) is None                     # expired
        assert auth.verify(token("u", TESTER, azp="https://evil.example")) is None  # another site's token
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        assert auth.verify(token("u", TESTER, alg_key=other)) is None             # not signed by our key
        assert auth.verify(jwt.encode({"sub": "u", "exp": int(time.time()) + 60}, "secret", algorithm="HS256")) is None
        assert auth.verify("not-a-token") is None and auth.verify(None) is None
    finally:
        os.environ.pop("CLERK_JWT_KEY")
        os.environ.pop("APP_BASE_URL")


def test_the_test_header_is_gone(browser):
    r = browser.get("/api/v1/lookups", headers={"X-TAW-User": "anyone"})
    assert r.status_code == 401


# -- reports: the shared page never shows the private section ------------------------------------

def test_owner_sees_their_transaction_and_the_shared_report_does_not(browser, db_url):
    sign_in(browser)
    r = submit(browser, rf.CASES[CASE3][1], amount="242209.53", sent_on="2025-05-10", asset="USDC")
    lid = r.headers["location"].rsplit("/", 1)[1]
    with psycopg.connect(db_url, autocommit=True) as data, psycopg.connect(db_url, autocommit=True) as raw:
        sid = save_case(data, raw, CASE3)
        finish_jobs(data, {"eth": sid})
        assert lookups.run_match(data, ACTOR, lid, adapter_for=None, raw_store=None) == "matched"
    owner = browser.get(f"/lookups/{lid}")
    assert owner.status_code == 200 and "Your transaction (private)" in owner.text
    assert "242,209.413589 USDC" in owner.text
    pid = re.search(r'href="/reports/([0-9a-f-]{36})"', owner.text).group(1)
    shared = browser.get(f"/reports/{pid}")
    assert shared.status_code == 200
    assert "Your transaction" not in shared.text and "just before your transfer" not in shared.text
    assert "0x60b23beb" not in shared.text.split("Where funds came from")[0]
    api_report = browser.get(f"/api/v1/reports/{pid}",
                             headers={"Authorization": f"Bearer {token('user_tester', TESTER)}"}).json()
    assert "match" not in api_report
    assert "Arbitrum One" in owner.text and "This site does not cover BNB Smart Chain, Polygon or Base yet" in owner.text


def test_addresses_link_to_a_lookup_and_token_names_never_do(browser, db_url):
    sign_in(browser)
    lid = submit(browser, rf.CASES[CASE3][1]).headers["location"].rsplit("/", 1)[1]
    with psycopg.connect(db_url, autocommit=True) as data, psycopg.connect(db_url, autocommit=True) as raw:
        finish_jobs(data, {"eth": save_case(data, raw, CASE3)})
    html = browser.get(f"/lookups/{lid}").text
    assert 'href="/?address=0x5842b2e75d3566bd0d3f9f520e71c1f3b30b99a1"' in html
    first_funder_tx = "0xfdbf75a1030c3231039a9184e06c4b6acde6ee23493035f575ad46c3669f6d97"
    assert f'data-copy="{first_funder_tx}"' in html                             # hashes get a copy button
    assert 'href="/?address=0x5842b2e75d3566bd0d3f9f520e71c1f3b30b99a1"' in html
    assert not re.search(r'<a[^>]*>[^<]*(USDС|bio\.link)', html)
    assert "&lt;U+2063&gt;" in html                                              # escaped, shown as text


def test_account_lists_and_deletes(browser, db_url):
    sign_in(browser)
    lid = submit(browser, rf.CASES[KNWAN][1], amount="10", tx_hash="ab" * 32).headers["location"].rsplit("/", 1)[1]
    page = browser.get("/account")
    assert lid in page.text
    r = browser.post(f"/account/lookups/{lid}/delete", data={"csrf": csrf_of(page.text)})
    assert r.status_code == 303 and r.headers["location"] == "/account?deleted=1"
    with psycopg.connect(db_url) as c:
        row = c.execute("SELECT clerk_user_id, amount, tx_hash, redacted_at FROM lookup WHERE id = %s", (lid,)).fetchone()
    assert row[:3] == (None, None, None) and row[3] is not None
    assert lid not in browser.get("/account").text


# -- headers -----------------------------------------------------------------------------------

def test_security_headers_and_noindex(browser):
    sign_in(browser)
    home = browser.get("/")
    csp = home.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "'unsafe-eval'" not in csp
    assert "https://example.clerk.accounts.dev" in csp and "https://challenges.cloudflare.com" in csp
    assert "x-robots-tag" not in home.headers
    cookie = home.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    for path in ("/sign-in", "/account", "/lookups/00000000-0000-0000-0000-000000000000",
                 "/reports/00000000-0000-0000-0000-000000000000"):
        assert browser.get(path).headers.get("x-robots-tag") == "noindex, nofollow", path
    assert "<script>" not in home.text                                       # no inline scripts


def test_turnstile_siteverify_call():
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        ok = b"response=good" in request.content
        return httpx.Response(200, json={"success": ok, "error-codes": [] if ok else ["invalid-input-response"]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    import os
    os.environ["TURNSTILE_SECRET_KEY"] = "s3cret"
    try:
        assert guard.verify_turnstile("good", "203.0.113.7", client=client).ok
        assert "remoteip=203.0.113.7" in seen["body"] and "secret=s3cret" in seen["body"]
        bad = guard.verify_turnstile("bad", None, client=client)
        assert not bad.ok and bad.codes == ("invalid-input-response",)
        assert not guard.verify_turnstile(None, None, client=client).ok
        assert not guard.verify_turnstile("x" * 2049, None, client=client).ok
    finally:
        os.environ.pop("TURNSTILE_SECRET_KEY")


def test_reports_as_text_through_the_api(browser, db_url):
    sign_in(browser)
    lid = submit(browser, rf.CASES[KNWAN][1]).headers["location"].rsplit("/", 1)[1]
    with psycopg.connect(db_url, autocommit=True) as data, psycopg.connect(db_url, autocommit=True) as raw:
        finish_jobs(data, {"tron": save_case(data, raw, KNWAN)})
        lookups.refresh_count(data, "tron", rf.CASES[KNWAN][1])
    bearer = {"Authorization": f"Bearer {token('user_tester', TESTER)}"}
    got = browser.get(f"/api/v1/lookups/{lid}", headers=bearer).json()
    pid = got["chains"][0]["report"]["public_id"]
    text = browser.get(f"/api/v1/reports/{pid}?format=text", headers=bearer).text
    assert "Tether destroyed 465,514.638375 USDT" in text and f"Report id: {pid}" in text
    assert "looked up this address here: 1" in text or "looked up this address: 1" in text
    assert browser.get(f"/api/v1/reports/{pid}").status_code == 401           # results only after sign-in



# -- sign-in before the form (the default while invite-only) --------------------------------------

def test_by_default_the_form_needs_sign_in(browser, db_url):
    home = browser.get("/")
    assert home.status_code == 200 and "Please sign in to start" in home.text and 'action="/lookups"' not in home.text
    r = browser.post("/lookups", data={"csrf": "x", "address": rf.CASES[KNWAN][1]})
    assert r.status_code in (303, 400)
    api = browser.post("/api/v1/lookups", json={"address": rf.CASES[KNWAN][1], "turnstile_token": "pass"},
                       headers={"X-TAW-Session": "s" * 43})
    assert api.status_code == 401
    with psycopg.connect(db_url) as c:
        assert c.execute("SELECT count(*) FROM lookup").fetchone()[0] == 0
    sign_in(browser)
    assert 'action="/lookups"' in browser.get("/").text
    sign_in(browser, "user_stranger", STRANGER)
    assert browser.get("/").status_code == 403


# -- the visitor's IP behind Cloudflare -----------------------------------------------------------

def test_cloudflare_ip_is_believed_only_from_cloudflare():
    via_cf = {"cf-connecting-ip": "198.51.100.23", "x-real-ip": "172.70.1.2"}           # 172.64.0.0/13
    assert guard.client_ip(via_cf, "10.0.0.1") == "198.51.100.23"
    direct = {"cf-connecting-ip": "1.2.3.4", "x-real-ip": "203.0.113.50"}               # not Cloudflare: forged
    assert guard.client_ip(direct, "10.0.0.1") == "203.0.113.50"
    assert guard.client_ip({"x-real-ip": "2001:db8::1"}, None) == "2001:db8::1"
    assert guard.client_ip({"cf-connecting-ip": "1.2.3.4"}, "10.0.0.1") == "10.0.0.1"
    v6 = {"cf-connecting-ip": "2001:db8::9", "x-real-ip": "2606:4700::1"}
    assert guard.client_ip(v6, None) == "2001:db8::9"


def test_two_networks_give_two_connection_hashes(browser):
    one = browser.get("/api/v1/connection", headers={"CF-Connecting-IP": "198.51.100.1", "X-Real-IP": "172.70.1.2"})
    two = browser.get("/api/v1/connection", headers={"CF-Connecting-IP": "198.51.100.2", "X-Real-IP": "172.70.1.2"})
    same = browser.get("/api/v1/connection", headers={"CF-Connecting-IP": "198.51.100.1", "X-Real-IP": "172.71.9.9"})
    assert one.json()["connection"] != two.json()["connection"] == two.json()["connection"]
    assert one.json()["connection"] == same.json()["connection"]
    assert "198.51" not in one.text


def test_goplus_logo_beside_its_result():
    from taw.report.render import render_html
    from tests.test_report import built
    html = str(render_html(built("wdpa-2-26-cv-00087-usdt")[1]))
    assert '<img src="/static/goplus-logo.svg" alt="GoPlus logo"' in html and "Powered by GoPlus" in html
    assert "goplus-logo" not in str(render_html(built("edny-1-25-cv-05745-btc")[1]))     # not asked for Bitcoin
