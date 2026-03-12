#!/usr/bin/env sh
set -e

# Decode Google credentials at runtime when provided as base64.
if [ -n "${GOOGLE_CREDENTIALS_BASE64:-}" ]; then
  echo "$GOOGLE_CREDENTIALS_BASE64" | base64 -d > /tmp/gcp-credentials.json
  export GOOGLE_APPLICATION_CREDENTIALS=/tmp/gcp-credentials.json
fi

exec hypercorn app.main:app --bind "0.0.0.0:${PORT:-8000}"