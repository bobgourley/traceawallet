"""Protection for starting lookups: Turnstile, salted hashes, per-IP and per-account
limits, and the Blockscout ledger.

Turnstile (docs checked 2026-11-20, developers.cloudflare.com/turnstile): the form's
widget gives a token, valid for 300 seconds and good for one check, at most 2048
characters. The server posts it with TURNSTILE_SECRET_KEY and the visitor's IP to
https://challenges.cloudflare.com/turnstile/v0/siteverify and gets success true or
false with error codes.

Limits, counted from the lookup table itself (no extra state):

  before sign-in, per IP        5 an hour, 20 a day
  signed in, per account        10 an hour, 40 a day
  before sign-in, whole site    200 a day   (keeps strangers from spending the allowance)

IPs and session ids are stored only as HMAC-SHA256 with HASH_SALT, never in clear.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import uuid
from dataclasses import dataclass

import httpx

from taw.auth import setting

SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
LIMITS = {
    "ip_hour": 5, "ip_day": 20,
    "account_hour": 10, "account_day": 40,
    "anonymous_site_day": 200,
}
EVM_CHAINS = ("eth", "arbitrum", "optimism")
LOW_ALLOWANCE = 0.05            # below 5% of the day's Blockscout allowance, say the lookup waits


def salted(kind: str, value: str | None) -> str | None:
    """HMAC-SHA256 of a session id or an IP, keyed by HASH_SALT, labelled by kind."""
    if not value:
        return None
    key = setting("HASH_SALT").encode()
    return hmac.new(key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class TurnstileResult:
    ok: bool
    codes: tuple[str, ...] = ()


def verify_turnstile(token: str | None, remote_ip: str | None, *, client: httpx.Client | None = None) -> TurnstileResult:
    if not token or len(token) > 2048:
        return TurnstileResult(False, ("missing-input-response",))
    data = {"secret": setting("TURNSTILE_SECRET_KEY"), "response": token, "idempotency_key": str(uuid.uuid4())}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        http = client or httpx.Client(timeout=10)
        r = http.post(SITEVERIFY, data=data)
        body = r.json()
    except (httpx.HTTPError, ValueError):
        return TurnstileResult(False, ("siteverify-unreachable",))
    return TurnstileResult(bool(body.get("success")), tuple(body.get("error-codes") or ()))


@dataclass(frozen=True)
class LimitHit:
    which: str
    limit: int


def check_limits(conn, *, user_id: str | None, ip_hash: str | None, now: dt.datetime | None = None) -> LimitHit | None:
    """The first limit this request would exceed, or None."""
    now = now or dt.datetime.now(dt.timezone.utc)
    hour, day = now - dt.timedelta(hours=1), now - dt.timedelta(days=1)

    def count(sql: str, *params) -> int:
        return conn.execute(sql, params).fetchone()[0]

    if user_id:
        if count("SELECT count(*) FROM lookup WHERE clerk_user_id = %s AND created_at > %s", user_id, hour) \
                >= LIMITS["account_hour"]:
            return LimitHit("account_hour", LIMITS["account_hour"])
        if count("SELECT count(*) FROM lookup WHERE clerk_user_id = %s AND created_at > %s", user_id, day) \
                >= LIMITS["account_day"]:
            return LimitHit("account_day", LIMITS["account_day"])
        return None
    if ip_hash:
        if count("SELECT count(*) FROM lookup WHERE ip_hash = %s AND created_at > %s", ip_hash, hour) \
                >= LIMITS["ip_hour"]:
            return LimitHit("ip_hour", LIMITS["ip_hour"])
        if count("SELECT count(*) FROM lookup WHERE ip_hash = %s AND created_at > %s", ip_hash, day) \
                >= LIMITS["ip_day"]:
            return LimitHit("ip_day", LIMITS["ip_day"])
    if count("SELECT count(*) FROM lookup WHERE clerk_user_id IS NULL AND claimed_at IS NULL AND created_at > %s",
             day) >= LIMITS["anonymous_site_day"]:
        return LimitHit("anonymous_site_day", LIMITS["anonymous_site_day"])
    return None


LIMIT_WORDS = {
    "ip_hour": "You have started {n} lookups in the past hour from this connection. Please wait an hour, or sign in "
               "first.",
    "ip_day": "You have started {n} lookups today from this connection. Please try again tomorrow, or sign in first.",
    "account_hour": "You have started {n} lookups in the past hour. Please wait an hour and try again.",
    "account_day": "You have started {n} lookups today. Please try again tomorrow.",
    "anonymous_site_day": "The site has taken as many lookups as it can today from people who are not signed in. "
                          "Please sign in first, or try again tomorrow.",
}


def limit_message(hit: LimitHit) -> str:
    return LIMIT_WORDS[hit.which].format(n=hit.limit)


def allowance_low(conn, chains) -> bool:
    """Whether today's Blockscout allowance is nearly used. The lookup still queues; its
    jobs wait for the reset at 00:00 UTC (the worker defers them)."""
    if not any(c in EVM_CHAINS for c in chains):
        return False
    from taw.budget import PostgresBudget
    b = PostgresBudget(conn, "worker")
    used = sum(b.used().values())
    left = b.remaining()
    left = b.allowance - used if left is None else min(left, b.allowance - used)
    return left < b.allowance * LOW_ALLOWANCE
