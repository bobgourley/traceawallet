"""Shared pytest settings.

With TAW_FAIL_ON_SKIP set (as in CI), any skipped test fails the run, so the
database tests cannot silently skip when the database is missing.
"""
import os

import pytest


def pytest_sessionfinish(session, exitstatus):
    if not os.environ.get("TAW_FAIL_ON_SKIP"):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = len(reporter.stats.get("skipped", [])) if reporter else 0
    if skipped:
        if reporter:
            reporter.write_line(f"TAW_FAIL_ON_SKIP: {skipped} test(s) were skipped; every test must run here.",
                                red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
