"""railway.json is the only place the start command and watch paths live (until
the move off Config as Code; see docs/DECISIONS.md). Guard both."""
import json
import pathlib

CONFIG = json.loads((pathlib.Path(__file__).resolve().parent.parent / "railway.json").read_text())


def test_start_command_runs_start_sh():
    # start.sh picks web or worker from SERVICE_ROLE and runs migrations for web.
    assert CONFIG["deploy"]["startCommand"] == "sh start.sh"


def test_docs_and_markdown_do_not_trigger_deploys():
    patterns = CONFIG["build"]["watchPatterns"]
    assert patterns[0] == "**"            # negations only work after an including rule
    assert {"!/docs/**", "!/*.md", "!/**/*.md"} <= set(patterns)
    assert not any(p.startswith("!") and ("taw" in p or "migrations" in p or "start.sh" in p) for p in patterns)
