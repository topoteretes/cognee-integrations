# Build and run the Lovable memory connector.
#   docker build -f deploy/Dockerfile -t cognee/lovable-memory .
# Node 22.13+ for node:sqlite (the receipt ledger needs no native module).
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json tsconfig.json ./
RUN npm ci
COPY index.ts ./index.ts
COPY src ./src
RUN npm run build && npm prune --omit=dev

FROM node:22-alpine
ENV NODE_ENV=production \
    NODE_OPTIONS=--disable-warning=ExperimentalWarning \
    LOVABLE_MCP_HOST=0.0.0.0 \
    LOVABLE_MCP_PORT=8787 \
    LOVABLE_RECEIPTS_PATH=/data/receipts.sqlite
WORKDIR /app
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY package.json README.md ./
RUN mkdir -p /data && chown -R node:node /data /app
USER node
VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8787/health || exit 1
ENTRYPOINT ["node", "dist/src/operator-cli.js"]
CMD ["serve"]
