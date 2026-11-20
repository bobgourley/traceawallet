from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from taw import db
from taw.api import router as api_router
from taw.web import HERE as WEB_DIR, router as web_router, security_headers

app = FastAPI(title="TraceAWallet", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(api_router)
app.include_router(web_router)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


@app.middleware("http")
async def headers(request: Request, call_next):
    response = canonical_host(request) or await call_next(request)
    security_headers(request.url.path, response.headers)
    return response


def canonical_host(request: Request):
    """www.traceawallet.com goes permanently to traceawallet.com (APP_BASE_URL). Sign-in
    tokens and the Turnstile widget are tied to the one address, so the site has only one."""
    from taw.auth import setting
    base = urlsplit(setting("APP_BASE_URL"))
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if base.hostname and host == f"www.{base.hostname}":
        target = f"{base.scheme}://{base.netloc}{request.url.path}"
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=301)
    return None


@app.get("/healthz")
def healthz() -> dict:
    """Railway and you both hit this to see whether the service and database are up."""
    try:
        with db.connect() as conn:
            chains = conn.execute("SELECT count(*) FROM chain").fetchone()[0]
        return {"ok": True, "database": "up", "chains": chains}
    except Exception as exc:  # report, don't crash
        return {"ok": False, "database": f"down: {type(exc).__name__}"}
