from fastapi import FastAPI, Request
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
    response = await call_next(request)
    security_headers(request.url.path, response.headers)
    return response


@app.get("/healthz")
def healthz() -> dict:
    """Railway and you both hit this to see whether the service and database are up."""
    try:
        with db.connect() as conn:
            chains = conn.execute("SELECT count(*) FROM chain").fetchone()[0]
        return {"ok": True, "database": "up", "chains": chains}
    except Exception as exc:  # report, don't crash
        return {"ok": False, "database": f"down: {type(exc).__name__}"}
