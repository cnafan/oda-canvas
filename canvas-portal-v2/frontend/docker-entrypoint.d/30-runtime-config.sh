#!/bin/sh
set -eu

APP_BASE="${APP_BASE:-}"
APP_API_BASE_URL="${APP_API_BASE_URL:-}"
APP_KEYCLOAK_URL="${APP_KEYCLOAK_URL:-/auth}"
APP_KEYCLOAK_REALM="${APP_KEYCLOAK_REALM:-canvas}"
APP_KEYCLOAK_CLIENT_ID="${APP_KEYCLOAK_CLIENT_ID:-canvas-portal}"
OPENAN_PORTAL_URL="${OPENAN_PORTAL_URL:-}"

write_config() {
  cat > "$1" <<EOF
window.__APP_CONFIG__ = {
  APP_BASE: "${APP_BASE}",
  API_BASE_URL: "${APP_API_BASE_URL}",
  KEYCLOAK_URL: "${APP_KEYCLOAK_URL}",
  KEYCLOAK_REALM: "${APP_KEYCLOAK_REALM}",
  KEYCLOAK_CLIENT_ID: "${APP_KEYCLOAK_CLIENT_ID}",
  OPENAN_PORTAL_URL: "${OPENAN_PORTAL_URL}"
};
EOF
}

write_config /usr/share/nginx/html/config.js
if [ -n "${APP_BASE}" ]; then
  mkdir -p "/usr/share/nginx/html${APP_BASE}"
  write_config "/usr/share/nginx/html${APP_BASE}/config.js"
fi
