#!/bin/sh
set -e

export PORT="${PORT:-80}"
export BACKEND_URL="${BACKEND_URL:-http://backend:8000}"

# Strip any trailing slash from BACKEND_URL
export BACKEND_URL=$(echo "$BACKEND_URL" | sed 's:/*$::')

# Discover system DNS resolver from /etc/resolv.conf, default to Docker internal DNS 127.0.0.11
DNS_SERVERS=$(awk '/^nameserver/{print $2}' /etc/resolv.conf 2>/dev/null | tr '\n' ' ')
export RESOLVER="${DNS_SERVERS:-127.0.0.11 8.8.8.8}"

echo "Starting Nginx on port ${PORT} (resolver: ${RESOLVER}) proxying /api/ to ${BACKEND_URL}..."
envsubst '${PORT} ${BACKEND_URL} ${RESOLVER}' < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf

exec nginx -g "daemon off;"
