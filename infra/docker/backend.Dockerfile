# syntax=docker/dockerfile:1
# One image, two commands: the API (default) and the Temporal worker.
FROM python:3.13-slim AS build
# uv from PyPI (pinned; matches the version that wrote uv.lock) rather than the ghcr.io
# image, so builds need only PyPI and Docker Hub.
# Optional build secret `build_ca`: an extra CA bundle for TLS-intercepting proxies. Used
# only by the install steps; never copied into an image layer.
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    pip install --no-cache-dir uv==0.11.32
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY services/backend/pyproject.toml services/backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export UV_NATIVE_TLS=1 SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --frozen --no-dev --no-install-project
COPY services/backend/src ./src
RUN --mount=type=cache,target=/root/.cache/uv --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export UV_NATIVE_TLS=1 SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --frozen --no-dev

FROM python:3.13-slim AS runtime
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
COPY services/backend/alembic.ini ./alembic.ini
COPY services/backend/migrations ./migrations
COPY packages/prompts /app/packages/prompts
COPY packages/layouts /app/packages/layouts
COPY packages/styles /app/packages/styles
COPY packages/fonts /app/packages/fonts
ARG RELEASE=dev
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    SP_CATALOG_DIR=/app/packages SP_RELEASE=$RELEASE
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]
# --timeout-graceful-shutdown lets in-flight requests and SSE streams drain on SIGTERM.
CMD ["uvicorn", "storypanel.api.app:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "25", "--no-server-header"]
