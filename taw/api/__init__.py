"""The API (/api/v1). The website is its first client (design rule 1): the web pages
call these routes and never read the database themselves.

Who is asking (taw/auth.py):
  Authorization: Bearer <Clerk session token>   a signed-in account; only accounts
                                                 whose email is in TESTER_EMAILS may
                                                 use the API (invite-only)
  X-TAW-Session: <opaque id>                     a browser session before sign-in. It
                                                 may start a lookup (with Turnstile)
                                                 and nothing else; signing in claims it
The API reads no cookies, so a cross-site form cannot act through it.

Client IP: the web pages pass the visitor's IP in X-TAW-Client-IP, which is
trusted only from the loopback address (the pages run in the same process).
Otherwise X-Real-IP, which Railway's edge sets on every request it forwards.

Until every setting the site needs exists (auth.missing_settings) every route
answers 503, so a deploy without them opens nothing.

  POST   /api/v1/lookups                    start a lookup (Turnstile, limits)
  POST   /api/v1/lookups/claim              signed in: claim this session's lookups
  GET    /api/v1/lookups                    signed in: my lookups
  GET    /api/v1/lookups/{id}               owner: status, chains, reports, match
  DELETE /api/v1/lookups/{id}               owner: delete (lookup_redact)
  GET    /api/v1/lookups/{id}/report?chain= owner: one chain's report with the match
  GET    /api/v1/reports/{public_id}        signed in: a shared report (no private part)
  GET    /api/v1/me                         who the token says you are
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
import uuid

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from taw import config, guard, lookups
from taw.auth import Identity, missing_settings, verify
from taw.report.render import bucket, render_text

ACTOR = "api"
router = APIRouter(prefix="/api/v1")


def ready() -> None:
    if missing_settings():
        raise HTTPException(status_code=503, detail="The site is not open yet.")


def caller(authorization: str | None = Header(default=None)) -> Identity | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return verify(authorization[7:].strip())


def member(who: Identity | None = Depends(caller)) -> Identity:
    """A signed-in account on the tester list."""
    if who is None:
        raise HTTPException(status_code=401, detail="Please sign in.")
    if not who.invited:
        raise HTTPException(status_code=403, detail="This site is invite-only for now.")
    return who


def client_ip(request: Request) -> str | None:
    peer = request.client.host if request.client else None
    forwarded = request.headers.get("x-taw-client-ip")
    try:
        if forwarded and peer and ipaddress.ip_address(peer).is_loopback:
            return forwarded.strip()
    except ValueError:
        pass
    return (request.headers.get("x-real-ip") or "").strip() or peer


def conn():
    c = psycopg.connect(config.DATABASE_URL or os.environ["DATABASE_URL"], autocommit=True)
    try:
        yield c
    finally:
        c.close()


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(status_code=404) from None


class LookupIn(BaseModel):
    address: str = Field(min_length=20, max_length=100)
    chain: str | None = None
    asset: str | None = Field(default=None, max_length=20)
    amount: str | None = Field(default=None, max_length=40)
    sent_on: dt.date | None = None
    sending_address: str | None = Field(default=None, max_length=100)
    tx_hash: str | None = Field(default=None, max_length=80)
    platform_name: str | None = Field(default=None, max_length=200)
    platform_url: str | None = Field(default=None, max_length=500)
    turnstile_token: str | None = Field(default=None, max_length=2048)


@router.get("/me", dependencies=[Depends(ready)])
def me(who: Identity | None = Depends(caller)) -> dict:
    if who is None:
        return {"signed_in": False, "invited": False, "email": None}
    return {"signed_in": True, "invited": who.invited, "email": who.email}


@router.post("/lookups", status_code=202, dependencies=[Depends(ready)])
def create_lookup(body: LookupIn, request: Request, who: Identity | None = Depends(caller),
                  x_taw_session: str | None = Header(default=None), db=Depends(conn)) -> dict:
    if who is not None and not who.invited:
        raise HTTPException(status_code=403, detail="This site is invite-only for now.")
    session = (x_taw_session or "").strip() or None
    if who is None and not session:
        raise HTTPException(status_code=401, detail="Please sign in.")
    ip = client_ip(request)
    check = guard.verify_turnstile(body.turnstile_token, ip)
    if not check.ok:
        raise HTTPException(status_code=400, detail="We could not confirm you are a person. Please try again.")
    ip_hash = guard.salted("ip", ip)
    hit = guard.check_limits(db, user_id=who.user_id if who else None, ip_hash=ip_hash)
    if hit:
        raise HTTPException(status_code=429, detail=guard.limit_message(hit))
    fields = body.model_dump(exclude={"turnstile_token"})
    try:
        lookup_id, chains = lookups.create(db, ACTOR, lookups.LookupRequest(**fields),
                                           user_id=who.user_id if who else None,
                                           session_hash=guard.salted("session", session), ip_hash=ip_hash)
    except lookups.LookupRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": lookup_id, "chains": list(chains), "status": "queued",
            "waits_for_allowance": guard.allowance_low(db, chains)}


@router.post("/lookups/claim", dependencies=[Depends(ready)])
def claim_lookups(who: Identity = Depends(member), x_taw_session: str | None = Header(default=None),
                  db=Depends(conn)) -> dict:
    if not x_taw_session:
        return {"claimed": []}
    return {"claimed": lookups.claim(db, ACTOR, who.user_id, guard.salted("session", x_taw_session.strip()))}


@router.get("/lookups", dependencies=[Depends(ready)])
def my_lookups(who: Identity = Depends(member), db=Depends(conn)) -> dict:
    return {"lookups": lookups.list_mine(db, who.user_id)}


@router.get("/lookups/{lookup_id}", dependencies=[Depends(ready)])
def read_lookup(lookup_id: str, who: Identity = Depends(member), db=Depends(conn)) -> dict:
    found = lookups.get(db, ACTOR, _uuid(lookup_id), who.user_id)
    if found is None:                       # someone else's, deleted or missing: the same answer
        raise HTTPException(status_code=404)
    return found


@router.delete("/lookups/{lookup_id}", dependencies=[Depends(ready)])
def delete_lookup(lookup_id: str, who: Identity = Depends(member), db=Depends(conn)) -> dict:
    if not lookups.redact(db, ACTOR, _uuid(lookup_id), who.user_id):
        raise HTTPException(status_code=404)
    return {"deleted": True}


def _report_row(db, public_id: str):
    return db.execute(
        "SELECT r.public_id::text, a.chain, a.address, r.address_id, r.as_of_block, r.as_of_time, r.schema_version,"
        " r.template_version, r.sha256, r.data, r.created_at FROM report r JOIN address a ON a.id = r.address_id"
        " WHERE r.public_id = %s", (public_id,)).fetchone()


def _report_json(db, row) -> dict:
    pid, chain, address, address_id, block, as_of, schema, template, sha, data, created = row
    return {"public_id": pid, "chain": chain, "address": address, "as_of_block": block,
            "as_of_time": as_of.astimezone(dt.timezone.utc).isoformat(), "schema_version": schema,
            "template_version": template, "sha256": sha.strip(), "created_at": created.isoformat(), "data": data,
            "prior_lookups": bucket(lookups.prior_count(db, address_id))}


@router.get("/lookups/{lookup_id}/report", dependencies=[Depends(ready)])
def read_lookup_report(lookup_id: str, chain: str | None = None, format: str = "json",
                       who: Identity = Depends(member), db=Depends(conn)):
    """One chain's report for the lookup's owner, with their match result beside it.
    The match is an overlay: the stored report never holds it."""
    found = lookups.get(db, ACTOR, _uuid(lookup_id), who.user_id)
    if found is None:
        raise HTTPException(status_code=404)
    ready_chains = [c for c in found["chains"] if c["report"] and (chain is None or c["chain"] == chain)]
    if not ready_chains:
        raise HTTPException(status_code=404, detail="No report for that chain yet.")
    pick = ready_chains[0]
    report = _report_json(db, _report_row(db, pick["report"]["public_id"]))
    match = found["match"]
    if match and pick["chain"] not in (match.get("matched_chain"), *(match.get("chains") or [])):
        match = None
    if format == "text":
        return PlainTextResponse(render_text(report["data"], prior_lookups=report["prior_lookups"],
                                             public_id=report["public_id"], sha256=report["sha256"], match=match))
    return {"chain": pick["chain"], "report": report, "match": match}


@router.get("/reports/{public_id}", dependencies=[Depends(ready)])
def read_report(public_id: str, format: str = "json", who: Identity = Depends(member), db=Depends(conn)):
    row = _report_row(db, _uuid(public_id))
    if row is None:
        raise HTTPException(status_code=404)
    report = _report_json(db, row)
    if format == "text":
        return PlainTextResponse(render_text(report["data"], prior_lookups=report["prior_lookups"],
                                             public_id=report["public_id"], sha256=report["sha256"]))
    return report
