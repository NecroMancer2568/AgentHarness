#!/bin/sh
set -e

export PORT="${PORT:-80}"
export BACKEND_URL="${BACKEND_URL:-http://backend:8000}"

# Strip any trailing slash from BACKEND_URL
export BACKEND_URL=$(echo "$BACKEND_URL" | sed 's:/*$::')

# If BACKEND_URL does not start with http:// or https://, format as http://<host>:10000 for Render private networking
if ! echo "$BACKEND_URL" | grep -qE '^https?://'; then
    export BACKEND_URL="http://${BACKEND_URL}:10000"
fi

# Discover system DNS resolver from /etc/resolv.conf, default to Docker internal DNS 127.0.0.11
DNS_SERVERS=$(awk '/^nameserver/{print $2}' /etc/resolv.conf 2>/dev/null | tr '\n' ' ')
export RESOLVER="${DNS_SERVERS:-127.0.0.11 8.8.8.8}"

echo "Starting Nginx on port ${PORT} (resolver: ${RESOLVER}) proxying /api/ to ${BACKEND_URL}..."
envsubst '${PORT} ${BACKEND_URL} ${RESOLVER}' < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf

exec nginx -g "daemon off;"
