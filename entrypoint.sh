#!/usr/bin/env bash
set -euo pipefail

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
PORT="${CALIBRE_WEB_CLI_PORT:-8084}"

# Create group/user if not already present (idempotent across restarts).
if ! getent group "$PGID" >/dev/null; then
  groupadd -g "$PGID" appgrp
fi
if ! getent passwd "$PUID" >/dev/null; then
  useradd -u "$PUID" -g "$PGID" -M -N -s /bin/false appuser
fi

# Ensure /data is writable by the app user. `|| true` because the bind mount may
# be owned by a different UID on the host and chown can fail without root caps.
mkdir -p /data/snapshots
chown -R "$PUID:$PGID" /data || true

# CRITICAL: --workers 1 is non-negotiable. The in-memory job queue lives in
# this process; spawning multiple worker processes would silently break the
# single-writer guarantee for metadata.db (see plan §Architecture).
exec gosu "$PUID:$PGID" uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers 1 \
  --no-access-log
