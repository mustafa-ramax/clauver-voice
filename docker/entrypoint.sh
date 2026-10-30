#!/usr/bin/env bash
set -euo pipefail

cd /app

AGENT_MODE="${AGENT_MODE:-default}"

case "$AGENT_MODE" in
  default)
    TARGET="agent.py"
    ;;
  cloud)
    TARGET="agent-cloud.py"
    ;;
  local)
    TARGET="agent-local.py"
    ;;
  *)
    echo "[entrypoint] Invalid AGENT_MODE: $AGENT_MODE (expected: default|cloud|local)"
    exit 1
    ;;
esac

if [[ $# -gt 0 ]]; then
  echo "[entrypoint] Executing custom command: $*" >&2
  exec "$@"
else
  WORKER_CMD="${CLAUVER_WORKER_COMMAND:-start}"
  case "$WORKER_CMD" in
    start|dev)
      ;;
    *)
      echo "[entrypoint] Invalid CLAUVER_WORKER_COMMAND: $WORKER_CMD (expected: start|dev)" >&2
      exit 1
      ;;
  esac
  echo "[entrypoint] Starting Clauver agent ($AGENT_MODE) → python $TARGET $WORKER_CMD ..." >&2
  exec python "$TARGET" "$WORKER_CMD"
fi