"""Reports: one chain and one address, built from one snapshot (docs/DECISIONS.md,
2026-11-20 "Reports, matching and the lookup API").

  inputs.py     what a report is built from, loaded from the database
  build.py      report.data: structured facts only, no prose
  canonical.py  the canonical JSON and the sha256 stored on the report row
  render.py     plain text from the templates in templates/
  store.py      build, hash and store a report row (once per build)
"""
SCHEMA_VERSION = 1
TEMPLATE_VERSION = "2026-11-20.1"
