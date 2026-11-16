from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from taw import db

app = FastAPI(title="TraceAWallet", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> dict:
    """Railway and you both hit this to see whether the service and database are up."""
    try:
        with db.connect() as conn:
            chains = conn.execute("SELECT count(*) FROM chain").fetchone()[0]
        return {"ok": True, "database": "up", "chains": chains}
    except Exception as exc:  # report, don't crash
        return {"ok": False, "database": f"down: {type(exc).__name__}"}


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return "<h1>TraceAWallet</h1><p>Coming soon.</p>"
