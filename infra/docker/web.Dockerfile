# syntax=docker/dockerfile:1
FROM node:22-slim AS build
# pnpm pinned to the version that wrote pnpm-lock.yaml. Optional build secret `build_ca`:
# extra CA bundle for TLS-intercepting proxies, used only here and never stored in a layer.
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; \
    npm install -g pnpm@10.28.0
WORKDIR /repo
COPY packages/api-types packages/api-types
COPY apps/web/package.json apps/web/pnpm-lock.yaml apps/web/
RUN --mount=type=cache,target=/root/.local/share/pnpm/store --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; \
    cd apps/web && pnpm install --frozen-lockfile
COPY apps/web apps/web
# NEXT_PUBLIC_* values are compiled into the bundle, so they are build arguments.
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_AUTH_MODE=clerk
ARG NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY
ARG NEXT_PUBLIC_ASSET_ORIGINS
ARG NEXT_PUBLIC_SENTRY_DSN
ARG NEXT_PUBLIC_ENV=production
ARG NEXT_PUBLIC_RELEASE=dev
ENV NEXT_TELEMETRY_DISABLED=1
RUN cd apps/web && pnpm build

FROM node:22-slim AS runtime
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build --chown=10001:10001 /repo/apps/web/.next/standalone ./
COPY --from=build --chown=10001:10001 /repo/apps/web/.next/static ./.next/static
# Next's standalone output omits public/ (lettering fonts live there)
COPY --from=build --chown=10001:10001 /repo/apps/web/public ./public
USER 10001:10001
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:3000/sign-in').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "server.js"]
