#!/usr/bin/env bash
# Runs a command with the same environment as `SP_STACK_LIVE=1 scripts/native-stack.sh start`
# (local stack endpoints + config/live-smoke.env + your untracked .env). Never prints values.
# Usage: scripts/live-env.sh services/backend/.venv/bin/python scripts/live-smoke.py preflight
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SP_STACK_LIVE=1
. "$ROOT/scripts/stack-env.sh"
cd "$ROOT"
exec "$@"
