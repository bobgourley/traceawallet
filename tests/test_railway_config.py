"""Railway settings live in each service's dashboard settings, not in the repo.

Railway stops reading railway.json / railway.toml on 2026-12-01 (Config as Code
is deprecated). The start command, restart policy and watch paths were moved to
the dashboard for traceawallet-web and traceawallet-worker on 2026-11-18; see
docs/DECISIONS.md. A config file in the repo would override those settings, so
none may come back by accident. What the dashboard must hold is in
docs/STATUS.md.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_no_railway_config_file():
    assert not (ROOT / "railway.json").exists()
    assert not (ROOT / "railway.toml").exists()


def test_start_script_picks_the_role():
    # The dashboard start command is `sh start.sh`; it must keep choosing web or worker.
    script = (ROOT / "start.sh").read_text()
    assert "SERVICE_ROLE" in script and "python -m taw.worker" in script
    assert "python scripts/migrate.py" in script and "uvicorn taw.main:app" in script
