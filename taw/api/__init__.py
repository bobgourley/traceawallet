"""The API (/api/v1). The website will be its first client (design rule 1).

Off unless TAW_API_ENABLED=1. It stays off in production until sign-in (Clerk),
Turnstile and per-account rate limits exist: with it off every route answers 404,
as if it did not exist.

Who is asking. Until Clerk is wired in, the caller names their account in the
X-TAW-User header. That is a stand-in for tests and private trials only; it is
the reason the setting must stay off in production.

  POST /api/v1/lookups               start a lookup (one job per chain covered)
  GET  /api/v1/lookups/{id}          the lookup, its jobs, reports and match: owner only
  GET  /api/v1/reports/{public_id}   a report's data and hash (?format=text for plain text)
  GET  /api/v1/lookups/{id}/report?chain=   the report as text with the owner's matched
                                     transaction shown: owner only, never stored in the report
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from taw import config, lookups
from taw.report.render import bucket, render_text

ACTOR = "api"
router = APIRouter(prefix="/api/v1")


def enabled() -> bool:
    return os.environ.get("TAW_API_ENABLED", "") == "1"


def gate() -> None:
    if not enabled():
        raise HTTPException(status_code=404)


def user(x_taw_user: str | None = Header(default=None)) -> str:
    if not x_taw_user or not x_taw_user.strip():
        raise HTTPException(status_code=401, detail="sign in first")
    return x_taw_user.strip()


def conn():
    c = psycopg.connect(config.DATABASE_URL or os.environ["DATABASE_URL"], autocommit=True)
    try:
        yield c
    finally:
        c.close()


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


@router.post("/lookups", status_code=202, dependencies=[Depends(gate)])
def create_lookup(body: LookupIn, who: str = Depends(user), db=Depends(conn)) -> dict:
    try:
        lookup_id, chains = lookups.create(db, ACTOR, who, lookups.LookupRequest(**body.model_dump()))
    except lookups.LookupRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": lookup_id, "chains": list(chains), "status": "queued"}


@router.get("/lookups/{lookup_id}", dependencies=[Depends(gate)])
def read_lookup(lookup_id: str, who: str = Depends(user), db=Depends(conn)) -> dict:
    try:
        uuid.UUID(lookup_id)
    except ValueError:
        raise HTTPException(status_code=404) from None
    found = lookups.get(db, ACTOR, lookup_id, who)
    if found is None:                       # someone else's, redacted or missing: the same answer
        raise HTTPException(status_code=404)
    return found


@router.get("/reports/{public_id}", dependencies=[Depends(gate)])
def read_report(public_id: str, format: str = "json", who: str = Depends(user), db=Depends(conn)):
    try:
        uuid.UUID(public_id)
    except ValueError:
        raise HTTPException(status_code=404) from None
    row = db.execute(
        "SELECT r.public_id::text, a.chain, a.address, r.address_id, r.as_of_block, r.as_of_time, r.schema_version,"
        " r.template_version, r.sha256, r.data, r.created_at FROM report r JOIN address a ON a.id = r.address_id"
        " WHERE r.public_id = %s", (public_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404)
    pid, chain, address, address_id, block, as_of, schema, template, sha, data, created = row
    prior = bucket(lookups.prior_count(db, address_id))
    if format == "text":
        return PlainTextResponse(render_text(data, prior_lookups=prior, public_id=pid, sha256=sha.strip()))
    return {"public_id": pid, "chain": chain, "address": address, "as_of_block": block,
            "as_of_time": as_of.astimezone(dt.timezone.utc).isoformat(), "schema_version": schema,
            "template_version": template, "sha256": sha.strip(), "created_at": created.isoformat(), "data": data,
            "prior_lookups": prior}


@router.get("/lookups/{lookup_id}/report", dependencies=[Depends(gate)])
def read_lookup_report(lookup_id: str, chain: str | None = None, who: str = Depends(user), db=Depends(conn)):
    """The lookup's report for one chain as text, with the owner's match result shown in
    a private section. The match is an overlay: the stored report never holds it."""
    try:
        uuid.UUID(lookup_id)
    except ValueError:
        raise HTTPException(status_code=404) from None
    found = lookups.get(db, ACTOR, lookup_id, who)
    if found is None:
        raise HTTPException(status_code=404)
    ready = [c for c in found["chains"] if c["report"] and (chain is None or c["chain"] == chain)]
    if not ready:
        raise HTTPException(status_code=404, detail="no report for that chain yet")
    pick = ready[0]
    row = db.execute("SELECT r.data, r.sha256, r.address_id FROM report r WHERE r.public_id = %s",
                     (pick["report"]["public_id"],)).fetchone()
    match = found["match"]
    if match and pick["chain"] not in (match.get("matched_chain"), *(match.get("chains") or [])):
        match = None
    text = render_text(row[0], prior_lookups=bucket(lookups.prior_count(db, row[2])),
                       public_id=pick["report"]["public_id"], sha256=row[1].strip(), match=match)
    return PlainTextResponse(text)
