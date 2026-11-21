"""Domain age for the website a user entered (private section only; docs/DECISIONS.md,
2026-11-21 "Domain age"). Two dated facts, each with its source:

  registration   the "registration" event in the domain registry's RDAP record. The
                 registry's RDAP service is found in IANA's bootstrap file
                 (https://data.iana.org/rdap/dns.json), by the domain's last label.
  certificate    the earliest validity start among the certificates crt.sh lists
                 for the domain, from the public certificate transparency logs.

The user's website itself is never contacted: the only hosts asked are IANA, the
registry's RDAP service named in IANA's file, and crt.sh. The host name was
already parsed and validated (taw.adapters.labels.dfpi.host_of). Redirects are not
followed. Every reply is saved to the bucket like any raw response (rule 3).

Which name the registry knows: a user may type a subdomain (app.example.com). The
registry is asked for the full host first, then each shorter name, and stops at
the first record. A two-label name such as "co.uk" is never asked about when the
host had more labels, because those are registry suffixes, not registrations.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Callable

import httpx

BOOTSTRAP = "https://data.iana.org/rdap/dns.json"
CRTSH = "https://crt.sh/"
RDAP_SOURCE, CRTSH_SOURCE = "rdap", "crtsh"
SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "mil", "ltd", "plc", "nom"}
MAX_BYTES = 20_000_000


def _date(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def candidates(host: str) -> list[str]:
    """Names to ask the registry about, longest first."""
    labels = host.split(".")
    out = []
    for i in range(0, len(labels) - 1):
        name = labels[i:]
        if len(name) == 2 and len(labels) > 2 and name[0] in SECOND_LEVEL:
            break
        out.append(".".join(name))
    return out


def gap_days(fact: str | None, paid_on: dt.date | str | None) -> int | None:
    """Days from the dated fact to the payment date (negative: the payment came first)."""
    if not fact or not paid_on:
        return None
    paid = paid_on if isinstance(paid_on, dt.date) else dt.date.fromisoformat(str(paid_on))
    return (paid - dt.date.fromisoformat(fact)).days


class DomainAge:
    def __init__(self, raw_store, *, http: httpx.Client | None = None,
                 today: Callable[[], dt.date] = lambda: dt.datetime.now(dt.timezone.utc).date()):
        self.raw_store = raw_store
        self.http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False,
                                         headers={"user-agent": "TraceAWallet domain check", "accept":
                                                  "application/rdap+json, application/json"})
        self.today = today
        self._bootstrap: dict[str, str] | None = None

    def _get(self, source: str, url: str, params: dict | None = None) -> tuple[int, bytes, str | None]:
        resp = self.http.get(url, params=params)
        body = resp.content[:MAX_BYTES]
        sha = self.raw_store.save(source, url, params, resp.status_code, body) if body else None
        return resp.status_code, body, sha

    def bootstrap(self) -> dict[str, str]:
        if self._bootstrap is None:
            status, body, _ = self._get(RDAP_SOURCE, BOOTSTRAP)
            if status != 200:
                raise RuntimeError(f"IANA's RDAP bootstrap answered HTTP {status}")
            data = json.loads(body)
            self._bootstrap = {tld.lower(): urls[0] for tlds, urls in data["services"] for tld in tlds if urls}
        return self._bootstrap

    def registration(self, host: str) -> dict:
        read_on = self.today().isoformat()
        tld = host.rsplit(".", 1)[-1]
        try:
            base = self.bootstrap().get(tld)
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as exc:
            return {"status": "not_read", "reason": type(exc).__name__, "read_on": read_on}
        if not base:
            return {"status": "no_service", "tld": tld, "read_on": read_on}
        server = httpx.URL(base).host
        asked = candidates(host)
        for name in asked:
            try:
                status, body, sha = self._get(RDAP_SOURCE, f"{base.rstrip('/')}/domain/{name}")
            except httpx.HTTPError as exc:
                return {"status": "not_read", "reason": type(exc).__name__, "server": server, "read_on": read_on}
            if status == 404:
                continue
            if status != 200:
                return {"status": "not_read", "reason": f"HTTP {status}", "server": server, "read_on": read_on}
            try:
                data = json.loads(body)
            except ValueError:
                return {"status": "not_read", "reason": "not JSON", "server": server, "read_on": read_on}
            events = {e.get("eventAction"): e.get("eventDate") for e in data.get("events") or []}
            registered = _date(events.get("registration"))
            if registered is None:
                return {"status": "no_date", "domain": name, "server": server, "read_on": read_on, "raw_sha256": sha}
            return {"status": "found", "domain": name, "registered": registered, "server": server,
                    "read_on": read_on, "raw_sha256": sha}
        return {"status": "no_record", "domain": asked[-1] if asked else host, "server": server, "read_on": read_on}

    def first_certificate(self, domain: str) -> dict:
        read_on = self.today().isoformat()
        last = None
        for _ in range(2):
            try:
                status, body, sha = self._get(CRTSH_SOURCE, CRTSH, {"q": domain, "output": "json",
                                                                    "deduplicate": "Y"})
            except httpx.HTTPError as exc:
                last = type(exc).__name__
                continue
            if status != 200:
                last = f"HTTP {status}"
                continue
            try:
                rows = json.loads(body)
            except ValueError:
                last = "not JSON"
                continue
            dated = [(r.get("not_before"), r.get("id")) for r in rows if r.get("not_before")]
            if not dated:
                return {"status": "none", "domain": domain, "read_on": read_on, "raw_sha256": sha}
            first, cert_id = min(dated)
            return {"status": "found", "domain": domain, "first_not_before": _date(first + "Z" if "T" in first and
                                                                                   not first.endswith("Z") else first),
                    "certificate_id": cert_id, "certificates": len(dated), "read_on": read_on, "raw_sha256": sha}
        return {"status": "not_read", "reason": last, "domain": domain, "read_on": read_on}

    def check(self, host: str, paid_on: dt.date | None = None) -> dict:
        reg = self.registration(host)
        cert = self.first_certificate(reg.get("domain") if reg.get("status") in ("found", "no_date") else host)
        out = {"host": host, "registration": reg, "certificate": cert,
               "paid_on": paid_on.isoformat() if paid_on else None}
        out["registration_gap_days"] = gap_days(reg.get("registered"), paid_on)
        out["certificate_gap_days"] = gap_days(cert.get("first_not_before"), paid_on)
        return out
