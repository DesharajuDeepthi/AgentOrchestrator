#!/usr/bin/env bash
# start.sh — start the full stack (db + backend + frontend), idempotently.
#   • errors out if Docker is not running
#   • creates containers if they don't exist, restarts them if they do —
#     data lives in named volumes, so a stopped stack resumes where it left off
#   • pulls/builds any missing image (postgres is pulled; app images are built)
#   • database schema is created/migrated automatically at backend startup
#     (alembic upgrade head), including on the very first run
set -euo pipefail
cd "$(dirname "$0")"

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not running. Start Docker (Desktop or dockerd) and retry." >&2
  exit 1
fi

[ -f .env ] || { echo "! no .env found — run ./quick-setup.sh first (continuing with defaults)"; }

existing="$(docker compose ps -aq 2>/dev/null | wc -l | tr -d ' ')"
if [ "$existing" -gt 0 ]; then
  echo "▸ found existing containers — starting them (data preserved in volumes)"
else
  echo "▸ no existing containers — creating the stack (first run: images are"
  echo "  pulled/built as needed and the DB schema is created automatically)"
fi

# `up -d` covers every case: pulls postgres if absent, builds app images if
# absent, creates missing containers, starts stopped ones, no-ops on running.
docker compose up -d

# read ports (defaults match docker-compose.yml)
BACKEND_PORT="$(grep -E '^BACKEND_PORT=' .env 2>/dev/null | cut -d= -f2- || true)"
FRONTEND_PORT="$(grep -E '^FRONTEND_PORT=' .env 2>/dev/null | cut -d= -f2- || true)"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

printf '▸ waiting for the backend to come up (migrations + seed run at startup)'
for _ in $(seq 1 60); do
  if curl -sf "http://localhost:${BACKEND_PORT}/health" >/dev/null 2>&1; then
    echo
    echo "▸ stack is up:"
    echo "    frontend  http://localhost:${FRONTEND_PORT}"
    echo "    api       http://localhost:${BACKEND_PORT}/api/v1"
    echo "    health    http://localhost:${BACKEND_PORT}/health"
    exit 0
  fi
  printf '.'
  sleep 2
done
echo
echo "ERROR: backend did not become healthy in time — check: docker compose logs backend" >&2
exit 1
