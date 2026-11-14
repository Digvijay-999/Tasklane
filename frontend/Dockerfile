# Dashboard dev server (Vite) for docker compose. Node 24 LTS, the same major as the developer's
# local Node, so package-lock.json resolves identically in both places.
FROM node:24.21.0-bookworm-slim

# Run as the image's unprivileged `node` user. Vite writes into /app (a temporary bundle of
# vite.config.ts, and its cache under node_modules/.vite), so that user owns the folder.
WORKDIR /app
RUN chown node:node /app
USER node

# Dependencies first (cached until package*.json change). npm ci installs exactly the lockfile;
# the Linux builds of native packages (rolldown, lightningcss) are listed in it.
COPY --chown=node:node package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# Source is bind-mounted by compose.yaml; this copy makes the image usable on its own too.
COPY --chown=node:node . .

EXPOSE 5173
CMD ["npx", "vite"]
