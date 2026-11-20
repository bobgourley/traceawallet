"""Who is asking: Clerk session tokens, the tester list, and whether the site is ready.

Clerk (docs checked 2026-11-20, clerk.com/docs/backend-requests/manual-jwt). A
signed-in visitor's browser holds a short-lived session token (a JWT, RS256) in
the `__session` cookie. The web pages pass it to the API as a Bearer token; the
API verifies it here:

  - the signature, with Clerk's public key: CLERK_JWT_KEY (the PEM shown on the
    API keys page) when set, otherwise the JWKS from https://api.clerk.com/v1/jwks
    fetched with CLERK_SECRET_KEY and cached for an hour
  - the algorithm is RS256; exp and nbf hold (five seconds' leeway)
  - azp, when present, is one of our own origins (APP_BASE_URL)

The email comes from a custom claim, `primaryEmail`, which the Clerk Dashboard
adds to the session token ({{user.primary_email_address}}). Invite-only: only
emails listed in TESTER_EMAILS may use the site.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import httpx
import jwt

JWKS_URL = "https://api.clerk.com/v1/jwks"
JWKS_TTL = 3600
LEEWAY = 5


def setting(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def missing_settings() -> list[str]:
    """Settings the site needs before it can open. Empty when ready."""
    need = ["CLERK_PUBLISHABLE_KEY", "CLERK_SECRET_KEY", "TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY",
            "TESTER_EMAILS", "HASH_SALT", "APP_BASE_URL"]
    return [n for n in need if not setting(n)]


def site_ready() -> bool:
    return not missing_settings()


def tester_emails() -> set[str]:
    raw = setting("TESTER_EMAILS").replace(";", ",").replace("\n", ",")
    return {e.strip().lower() for part in raw.split(",") for e in part.split() if e.strip()}


def authorized_parties() -> set[str]:
    base = setting("APP_BASE_URL").rstrip("/")
    return {base} if base else set()


@dataclass(frozen=True)
class Identity:
    user_id: str
    email: str | None

    @property
    def invited(self) -> bool:
        return bool(self.email) and self.email.lower() in tester_emails()


class _Keys:
    def __init__(self):
        self._lock = threading.Lock()
        self._keys: dict[str, object] = {}
        self._fetched = 0.0

    def get(self, kid: str | None):
        pem = setting("CLERK_JWT_KEY")
        if pem:
            return _load_pem(pem)
        with self._lock:
            if kid not in self._keys or time.monotonic() - self._fetched > JWKS_TTL:
                self._refresh()
            return self._keys.get(kid)

    def _refresh(self) -> None:
        secret = setting("CLERK_SECRET_KEY")
        if not secret:
            return
        r = httpx.get(JWKS_URL, headers={"Authorization": f"Bearer {secret}"}, timeout=10)
        r.raise_for_status()
        self._keys = {k["kid"]: jwt.PyJWK.from_dict(k).key for k in r.json().get("keys", []) if k.get("kid")}
        self._fetched = time.monotonic()

    def clear(self) -> None:
        with self._lock:
            self._keys, self._fetched = {}, 0.0


def _load_pem(pem: str):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    return load_pem_public_key(pem.replace("\\n", "\n").encode())


KEYS = _Keys()


def verify(token: str | None) -> Identity | None:
    """The identity in a Clerk session token, or None if it is missing or not valid."""
    if not token or len(token) > 8192:
        return None
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256":
            return None
        key = KEYS.get(header.get("kid"))
        if key is None:
            return None
        claims = jwt.decode(token, key=key, algorithms=["RS256"], leeway=LEEWAY,
                            options={"require": ["exp", "sub"], "verify_aud": False})
    except (jwt.PyJWTError, httpx.HTTPError, ValueError):
        return None
    azp = claims.get("azp")
    if azp and azp.rstrip("/") not in authorized_parties():
        return None
    if claims.get("sts") == "pending":
        return None
    email = claims.get("primaryEmail")
    return Identity(str(claims["sub"]), email.strip() if isinstance(email, str) and email.strip() else None)


def frontend_api_host(publishable_key: str | None = None) -> str | None:
    """Clerk's Frontend API host, which is encoded in the publishable key
    (pk_test_ or pk_live_ + base64 of "host$")."""
    import base64
    pk = publishable_key if publishable_key is not None else setting("CLERK_PUBLISHABLE_KEY")
    if not pk or "_" not in pk:
        return None
    body = pk.split("_", 2)[-1]
    try:
        host = base64.b64decode(body + "=" * (-len(body) % 4)).decode().rstrip("$")
    except (ValueError, UnicodeDecodeError):
        return None
    return host if host and "/" not in host and " " not in host else None
