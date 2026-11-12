# Sourced by native-stack.sh and live-env.sh: the local stack's configuration.
# With SP_STACK_LIVE=1 it adds config/live-smoke.env (public) and the untracked .env (only your three provider keys).
export SP_ENV=local SP_PROVIDER_MODE=fake SP_AUTH_MODE=dev SP_LOG_LEVEL=INFO
export SP_DATABASE_URL="${SP_DATABASE_URL:-postgresql+asyncpg://storypanel@127.0.0.1:5432/storypanel}"
export SP_REDIS_URL="${SP_REDIS_URL:-redis://127.0.0.1:6379/0}"
export SP_S3_ENDPOINT_URL=http://127.0.0.1:9000 SP_S3_ACCESS_KEY_ID=local SP_S3_SECRET_ACCESS_KEY=local
export SP_TEMPORAL_ADDRESS=127.0.0.1:7233 SP_CORS_ORIGINS='["http://localhost:3000"]'
export SP_SIGNUP_BONUS_CREDITS="${SP_SIGNUP_BONUS_CREDITS:-60}" NO_PROXY=127.0.0.1,localhost
if [ "${SP_STACK_LIVE:-0}" = 1 ]; then
  set -a
  . "$ROOT/config/live-smoke.env"
  set +a
  # Only the provider keys are taken from .env (parsed, not executed), so a copied .env.example
  # cannot switch the stack back to fake providers or other endpoints.
  if [ -f "$ROOT/.env" ]; then
    while IFS='=' read -r k v; do
      case "$k" in SP_ANTHROPIC_API_KEY|SP_FAL_KEY|SP_VOYAGE_API_KEY)
        v="${v%%#*}"; v="${v%"${v##*[![:space:]]}"}"; v="${v#[\"\']}"; v="${v%[\"\']}"
        if [ -n "$v" ]; then export "$k=$v"; fi ;; esac
    done < "$ROOT/.env"
  fi
fi
