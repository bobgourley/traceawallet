"""The website: server-rendered pages, a client of the API (design rule 1).

Nothing here reads the database. Every page calls /api/v1 over HTTP (loopback in
production, set by TAW_API_URL), passing:

  Authorization: Bearer <the __session cookie Clerk keeps in the browser>
  X-TAW-Session: <our own random session id, cookie taw_sid>
  X-Real-IP:     <the visitor's IP, as Railway's edge gave it to us>

Cookies: taw_sid is HttpOnly, Secure, SameSite=Lax, a random 32-byte id. Forms
carry a CSRF token, an HMAC of that id with HASH_SALT, checked on every POST.
Every response gets a strict content security policy (security_headers below);
lookup, report, account and sign-in pages are noindex.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import pathlib
import secrets
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from taw.auth import frontend_api_host, missing_settings, setting
from taw.report.render import render_html

HERE = pathlib.Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
router = APIRouter()
SESSION_COOKIE = "taw_sid"
SESSION_DAYS = 7
REFRESH_SECONDS = 10
NOINDEX = ("/lookups", "/reports", "/account", "/sign-in")


# -- the API client ---------------------------------------------------------------------------

@dataclass
class Reply:
    status: int
    body: dict

    @property
    def detail(self) -> str:
        d = self.body.get("detail") if isinstance(self.body, dict) else None
        return d if isinstance(d, str) else "Something went wrong. Please try again."


class Api:
    def __init__(self, http):
        self.http = http

    def call(self, method: str, path: str, request: Request, *, json: dict | None = None,
             params: dict | None = None) -> Reply:
        headers = {}
        token = request.cookies.get("__session")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            headers["X-TAW-Session"] = sid
        ip = request.headers.get("x-real-ip") or (request.client.host if request.client else None)
        if ip:
            headers["X-Real-IP"] = ip
        r = self.http.request(method, "/api/v1" + path, headers=headers, json=json, params=params)
        try:
            body = r.json()
        except ValueError:
            body = {}
        return Reply(r.status_code, body)


_HTTP: httpx.Client | None = None


def get_api() -> Api:
    global _HTTP
    if _HTTP is None:
        base = os.environ.get("TAW_API_URL") or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"
        _HTTP = httpx.Client(base_url=base, timeout=60)
    return Api(_HTTP)


# -- sessions, CSRF, page context -------------------------------------------------------------

def csrf_for(sid: str | None) -> str:
    if not sid:
        return ""
    return hmac.new(setting("HASH_SALT").encode(), f"csrf:{sid}".encode(), hashlib.sha256).hexdigest()


def csrf_ok(request: Request, token: str | None) -> bool:
    sid = request.cookies.get(SESSION_COOKIE)
    return bool(sid and token) and hmac.compare_digest(csrf_for(sid), token)


def ensure_session(request: Request) -> bool:
    """Pick this browser's session id before the page renders (the CSRF token needs it).
    Returns True when it is new and the response must set the cookie."""
    sid = request.cookies.get(SESSION_COOKIE)
    new = not sid or len(sid) < 32
    request.state.sid = secrets.token_urlsafe(32) if new else sid
    return new


def set_session(response: Response, request: Request) -> Response:
    response.set_cookie(SESSION_COOKIE, request.state.sid, max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=True, samesite="lax", path="/")
    return response


def safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    sid = getattr(request.state, "sid", None) or request.cookies.get(SESSION_COOKIE)
    base = {"request": request, "csrf": csrf_for(sid), "clerk_host": frontend_api_host(),
            "clerk_key": setting("CLERK_PUBLISHABLE_KEY"), "turnstile_key": setting("TURNSTILE_SITE_KEY"),
            "me": getattr(request.state, "me", {"signed_in": False, "invited": False, "email": None})}
    return templates.TemplateResponse(request, name, {**base, **ctx}, status_code=status)


def who(request: Request, api: Api) -> dict:
    r = api.call("GET", "/me", request)
    me = r.body if r.status == 200 else {"signed_in": False, "invited": False, "email": None}
    request.state.me = me
    return me


def gate(request: Request, api: Api) -> Response | None:
    """For pages that need a signed-in tester: a response to send instead, or None."""
    me = who(request, api)
    if not me.get("signed_in"):
        return RedirectResponse("/sign-in?next=" + quote(request.url.path, safe="/"), status_code=303)
    if not me.get("invited"):
        return page(request, "invite_only.html", status=403)
    return None


def not_ready(request: Request) -> Response | None:
    if missing_settings():
        return page(request, "coming_soon.html")         # 200, as before: a health check may watch /
    return None


# -- pages ------------------------------------------------------------------------------------

FIELDS = ("address", "chain", "asset", "amount", "sent_on", "sending_address", "tx_hash", "platform")


@router.get("/", response_class=HTMLResponse)
def home(request: Request, address: str = "", api: Api = Depends(get_api)):
    if (r := not_ready(request)) is not None:
        return r
    who(request, api)
    new = ensure_session(request)
    resp = page(request, "index.html", form={"address": address[:100]}, error=None)
    return set_session(resp, request) if new else resp


@router.post("/lookups", response_class=HTMLResponse)
def start_lookup(request: Request, api: Api = Depends(get_api), csrf: str = Form(""), address: str = Form(""),
                 chain: str = Form(""), asset: str = Form(""), amount: str = Form(""), sent_on: str = Form(""),
                 sending_address: str = Form(""), tx_hash: str = Form(""), platform: str = Form(""),
                 turnstile: str = Form("", alias="cf-turnstile-response")):
    if (r := not_ready(request)) is not None:
        return r
    form = {"address": address, "chain": chain, "asset": asset, "amount": amount, "sent_on": sent_on,
            "sending_address": sending_address, "tx_hash": tx_hash, "platform": platform}
    me = who(request, api)
    if not csrf_ok(request, csrf):
        return page(request, "index.html", status=400, form=form,
                    error="This form has expired. Please reload the page and try again.")
    if me.get("signed_in") and not me.get("invited"):
        return page(request, "invite_only.html", status=403)
    platform = platform.strip()
    body = {"address": address.strip(), "chain": chain or None, "asset": asset.strip() or None,
            "amount": amount.strip() or None, "sent_on": sent_on or None,
            "sending_address": sending_address.strip() or None, "tx_hash": tx_hash.strip() or None,
            "platform_name": platform if platform and "." not in platform else None,
            "platform_url": platform if "." in platform else None, "turnstile_token": turnstile or None}
    if not body["address"]:
        return page(request, "index.html", status=400, form=form, error="Please enter the address you sent money to.")
    r = api.call("POST", "/lookups", request, json=body)
    if r.status == 422 and isinstance(r.body.get("detail"), list):
        return page(request, "index.html", status=400, form=form,
                    error="Something in the form is not in the expected format. Please check the date and amount.")
    if r.status != 202:
        return page(request, "index.html", status=r.status if r.status >= 400 else 400, form=form, error=r.detail)
    target = f"/lookups/{r.body['id']}"
    if me.get("signed_in"):
        return RedirectResponse(target, status_code=303)
    return RedirectResponse("/sign-in?next=" + quote(target, safe="/"), status_code=303)


@router.get("/sign-in", response_class=HTMLResponse)
def sign_in(request: Request, next: str = "/", api: Api = Depends(get_api)):
    if (r := not_ready(request)) is not None:
        return r
    me = who(request, api)
    target = safe_next(next)
    if me.get("signed_in") and me.get("invited"):
        return RedirectResponse(target, status_code=303)
    if me.get("signed_in"):
        return page(request, "invite_only.html", status=403)
    return page(request, "sign_in.html", next=target)


def _chain_name(code: str) -> str:
    from taw.report.build import CHAIN_NAMES
    return CHAIN_NAMES.get(code, code)


@router.get("/lookups/{lookup_id}", response_class=HTMLResponse)
def lookup_page(request: Request, lookup_id: str, api: Api = Depends(get_api)):
    if (r := not_ready(request)) is not None:
        return r
    if (r := gate(request, api)) is not None:
        return r
    api.call("POST", "/lookups/claim", request)
    got = api.call("GET", f"/lookups/{quote(lookup_id, safe='')}", request)
    if got.status != 200:
        return page(request, "not_found.html", status=404)
    lk = got.body
    pending = [c for c in lk["chains"] if c["job_status"] in ("queued", "running")]
    sections, not_covered = [], []
    if not pending:
        for c in lk["chains"]:
            sec = {"chain": c["chain"], "name": _chain_name(c["chain"]), "status": c["job_status"],
                   "message": c.get("message"), "html": None, "quiet": False}
            if c["report"]:
                rep = api.call("GET", f"/lookups/{quote(lookup_id, safe='')}/report", request,
                               params={"chain": c["chain"]})
                if rep.status == 200:
                    data = rep.body["report"]["data"]
                    sec["quiet"] = not data["activity"]["first_activity"] and not data["summary"]["held"]
                    sec["html"] = render_html(data, prior_lookups=rep.body["report"]["prior_lookups"],
                                              public_id=rep.body["report"]["public_id"],
                                              sha256=rep.body["report"]["sha256"], match=rep.body["match"])
                    sec["public_id"] = rep.body["report"]["public_id"]
                    cov = data["header"].get("evm_coverage")
                    if cov and not not_covered:
                        not_covered = [x["name"] for x in cov["not_checked"]]
            sections.append(sec)
    return page(request, "lookup.html", lookup=lk, pending=pending, sections=sections, not_covered=not_covered,
                refresh=REFRESH_SECONDS if pending else None)


@router.get("/reports/{public_id}", response_class=HTMLResponse)
def report_page(request: Request, public_id: str, api: Api = Depends(get_api)):
    if (r := not_ready(request)) is not None:
        return r
    if (r := gate(request, api)) is not None:
        return r
    rep = api.call("GET", f"/reports/{quote(public_id, safe='')}", request)
    if rep.status != 200:
        return page(request, "not_found.html", status=404)
    b = rep.body
    html = render_html(b["data"], prior_lookups=b["prior_lookups"], public_id=b["public_id"], sha256=b["sha256"])
    return page(request, "report.html", report=b, html=html)


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, api: Api = Depends(get_api)):
    if (r := not_ready(request)) is not None:
        return r
    if (r := gate(request, api)) is not None:
        return r
    api.call("POST", "/lookups/claim", request)
    mine = api.call("GET", "/lookups", request)
    resp = page(request, "account.html", lookups=mine.body.get("lookups", []), deleted=request.query_params.get("deleted"))
    return resp


@router.post("/account/lookups/{lookup_id}/delete")
def delete(request: Request, lookup_id: str, csrf: str = Form(""), api: Api = Depends(get_api)):
    if (r := not_ready(request)) is not None:
        return r
    if not csrf_ok(request, csrf):
        return page(request, "error.html", status=400, message="This form has expired. Please reload the page.")
    if (r := gate(request, api)) is not None:
        return r
    r = api.call("DELETE", f"/lookups/{quote(lookup_id, safe='')}", request)
    if r.status != 200:
        return page(request, "not_found.html", status=404)
    return RedirectResponse("/account?deleted=1", status_code=303)


@router.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return page(request, "about.html")


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return page(request, "privacy.html")


@router.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return page(request, "terms.html")


# -- headers on every response ----------------------------------------------------------------

def content_security_policy() -> str:
    clerk = frontend_api_host()
    clerk_src = f" https://{clerk}" if clerk else ""
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self'{clerk_src} https://challenges.cloudflare.com https://*.protect.clerk.com",
        f"connect-src 'self'{clerk_src} https://*.protect.clerk.com:*",
        "img-src 'self' https://img.clerk.com",
        "worker-src 'self' blob:",
        "style-src 'self' 'unsafe-inline'",          # Clerk's components style themselves at runtime
        "frame-src https://challenges.cloudflare.com https://*.protect.clerk.com",
        "font-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    ])


def security_headers(path: str, headers) -> None:
    headers["Content-Security-Policy"] = content_security_policy()
    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "no-referrer"
    headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), interest-cohort=()"
    if path.startswith(NOINDEX) or path.startswith("/api/"):
        headers["X-Robots-Tag"] = "noindex, nofollow"
        headers["Cache-Control"] = "no-store"
