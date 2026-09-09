#!/usr/bin/env bash
# stop.sh — stop the stack without losing anything. Containers and volumes
# stay in place; ./start.sh resumes with the same data.
set -euo pipefail
cd "$(dirname "$0")"

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not running — nothing to stop." >&2
  exit 1
fi

docker compose stop
echo "▸ stack stopped. Data is preserved — ./start.sh picks up where you left off."
