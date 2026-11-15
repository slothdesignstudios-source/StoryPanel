# Sourced by native-stack.sh and live-env.sh: the local stack's configuration.
# With SP_STACK_LIVE=1 it adds config/live-smoke.env (public) and the untracked .env (only your provider keys and image-provider choice).
export SP_ENV=local SP_PROVIDER_MODE=fake SP_AUTH_MODE=dev SP_LOG_LEVEL=INFO
export SP_DATABASE_URL="${SP_DATABASE_URL:-postgresql+asyncpg://storypanel@127.0.0.1:5432/storypanel}"
export SP_REDIS_URL="${SP_REDIS_URL:-redis://127.0.0.1:6379/0}"
# Ports and names can be overridden (scripts/persistence-check.sh runs an isolated copy this way).
SP_S3_PORT="${SP_S3_PORT:-9000}" SP_TEMPORAL_PORT="${SP_TEMPORAL_PORT:-7233}" SP_API_PORT="${SP_API_PORT:-8000}"
SP_WEB_PORT="${SP_WEB_PORT:-3000}"
# Local image storage: "versity" (default) keeps objects as files under $SP_DATA/s3 and survives
# restarts; "moto" keeps them in memory and loses them when it stops (simulated runs only, and only
# with SP_ALLOW_VOLATILE_STORAGE=1).
export SP_LOCAL_S3="${SP_LOCAL_S3:-versity}"
export SP_S3_ENDPOINT_URL="http://127.0.0.1:$SP_S3_PORT" SP_S3_BUCKET="${SP_S3_BUCKET:-storypanel-assets}"
if [ "$SP_LOCAL_S3" = versity ]; then
  # Same local credentials as docker-compose.yml; the local gateway verifies SigV4 but has no SSE.
  export SP_S3_ACCESS_KEY_ID=storypanel SP_S3_SECRET_ACCESS_KEY=storypanel-local-secret SP_S3_SERVER_SIDE_ENCRYPTION=none
else
  export SP_S3_ACCESS_KEY_ID=local SP_S3_SECRET_ACCESS_KEY=local
fi
export SP_TEMPORAL_ADDRESS="127.0.0.1:$SP_TEMPORAL_PORT" SP_TEMPORAL_NAMESPACE="${SP_TEMPORAL_NAMESPACE:-default}"
export SP_CORS_ORIGINS="[\"http://localhost:$SP_WEB_PORT\"]"
export SP_SIGNUP_BONUS_CREDITS="${SP_SIGNUP_BONUS_CREDITS:-60}" NO_PROXY=127.0.0.1,localhost
if [ "${SP_STACK_LIVE:-0}" = 1 ]; then
  set -a
  . "$ROOT/config/live-smoke.env"
  set +a
  # Only the provider keys (and the default image provider) are taken from .env (parsed, not
  # executed), so a copied .env.example cannot switch the stack back to fake providers or other
  # endpoints. fal/Voyage keys are read but unused unless config enables those providers.
  if [ -f "$ROOT/.env" ]; then
    while IFS='=' read -r k v; do
      case "$k" in SP_ANTHROPIC_API_KEY|SP_OPENAI_API_KEY|SP_GEMINI_API_KEY|SP_IMAGE_PROVIDER_DEFAULT|SP_FAL_KEY|SP_VOYAGE_API_KEY)
        v="${v%%#*}"; v="${v%"${v##*[![:space:]]}"}"; v="${v#[\"\']}"; v="${v%[\"\']}"
        if [ -n "$v" ]; then export "$k=$v"; fi ;; esac
    done < "$ROOT/.env"
  fi
fi
