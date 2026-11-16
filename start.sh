#!/bin/sh
# One repo, two Railway services. SERVICE_ROLE picks which one this is.
set -e
if [ "$SERVICE_ROLE" = "worker" ]; then
    exec python -m taw.worker
else
    python scripts/migrate.py
    exec uvicorn taw.main:app --host 0.0.0.0 --port "${PORT:-8000}"
fi
