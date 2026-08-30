ARG NODE_BASE_IMAGE=node:24-alpine@sha256:e67514e5d0f6c46656005e1b693b2ec9d52e80b641307de684d4a015ba7a4eaf
FROM ${NODE_BASE_IMAGE} AS build

ARG NPM_CONFIG_REGISTRY=https://registry.npmjs.org
ENV NPM_CONFIG_REGISTRY=$NPM_CONFIG_REGISTRY

RUN corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/package.json
RUN pnpm install --frozen-lockfile
COPY apps/web ./apps/web
ENV QUANTSIEVE_API_INTERNAL_URL=http://api:8000
RUN pnpm --filter @quantsieve/web build

FROM ${NODE_BASE_IMAGE} AS runtime
ENV NODE_ENV=production \
    QUANTSIEVE_API_INTERNAL_URL=http://api:8000
WORKDIR /app
COPY --from=build /app/apps/web/.next/standalone ./
COPY --from=build /app/apps/web/.next/static ./apps/web/.next/static
USER node
EXPOSE 3000
CMD ["node", "apps/web/server.js"]
