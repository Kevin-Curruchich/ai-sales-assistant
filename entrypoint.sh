#!/usr/bin/env sh
set -e

# Decode Google credentials at runtime when provided as base64.
if [ -n "${GOOGLE_CREDENTIALS_BASE64:-}" ]; then
  echo "$GOOGLE_CREDENTIALS_BASE64" | base64 -d > /tmp/gcp-credentials.json
  export GOOGLE_APPLICATION_CREDENTIALS=/tmp/gcp-credentials.json
fi

# Bring the schema up to date before serving.  With set -e, a failed migration
# aborts the boot instead of serving against a stale schema.
#
# IMPORTANT: this must ship together with the POSTGRES_SCHEMA cutover away
# from db_dev. db_dev holds the only copy of production data and its tables
# were created without Alembic (no alembic_version row), so alembic/env.py
# deliberately refuses to migrate it unless REVENEW_ALLOW_DB_DEV=1. If this
# entrypoint reaches production while POSTGRES_SCHEMA is still db_dev, the
# boot will fail loudly here (and set -e aborts it) instead of running
# migrations against real data. That is expected: the container simply won't
# start until POSTGRES_SCHEMA points at the migrated schema.
alembic upgrade head

exec hypercorn app.main:app --bind "0.0.0.0:${PORT:-8000}"
