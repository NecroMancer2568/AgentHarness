# Stage 1: Build React/Vite application
FROM node:20-alpine AS build

WORKDIR /app

# Copy dependency manifests
COPY package.json package-lock.json ./

# Install dependencies deterministically
RUN npm ci

# Copy application sources
COPY tsconfig.json tsconfig.node.json vite.config.ts index.html ./
COPY src/ ./src/

# Optional build-time argument for direct API URL (defaults to empty so Nginx reverse-proxies /api/)
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

# Build production bundle
RUN npm run build

# Stage 2: Production Nginx web server
FROM nginx:alpine

# Remove default nginx configs
RUN rm -rf /etc/nginx/conf.d/* /etc/nginx/templates

# Copy custom Nginx configuration template and map
COPY 00-map.conf /etc/nginx/conf.d/00-map.conf
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Copy built frontend assets
COPY --from=build /app/dist /usr/share/nginx/html

# Expose default HTTP port
EXPOSE 80

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://127.0.0.1:${PORT:-80}/healthz || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]

