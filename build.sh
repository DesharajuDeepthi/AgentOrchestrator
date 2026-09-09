#!/usr/bin/env bash
# build.sh — build the backend and frontend docker images.
set -euo pipefail
cd "$(dirname "$0")"

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not running. Start Docker (Desktop or dockerd) and retry." >&2
  exit 1
fi

echo "▸ building backend and frontend images…"
docker compose build backend frontend
echo "▸ build complete. Run ./start.sh to start the stack."
