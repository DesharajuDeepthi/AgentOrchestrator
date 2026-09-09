#!/usr/bin/env bash
# decom.sh — dismantle the stack COMPLETELY: containers, network, and data
# volumes (Postgres data + workspace). The next ./start.sh begins from a
# clean slate (schema recreated, seed data reloaded). Images are kept so the
# restart is fast; use ./build.sh to rebuild them after code changes.
set -euo pipefail
cd "$(dirname "$0")"

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not running." >&2
  exit 1
fi

if [ "${1:-}" != "-y" ] && [ "${1:-}" != "--yes" ]; then
  printf 'This DELETES all data (registries, runs, workspace files). Continue? [y/N] '
  read -r answer
  case "$answer" in [yY]|[yY][eE][sS]) ;; *) echo "aborted."; exit 0 ;; esac
fi

docker compose down -v --remove-orphans
echo "▸ stack dismantled — containers, network, and data volumes removed."
echo "  Next ./start.sh starts from a clean slate (fresh schema + seeds)."
