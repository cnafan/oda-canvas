FROM node:20-alpine AS build

ARG VITE_BASE=""
ENV VITE_BASE=$VITE_BASE

WORKDIR /app

COPY package.json package-lock.json ./

RUN npm ci

COPY . .

RUN npm run build

FROM nginx:1.27-alpine

ARG FRONTEND_BASE=""
ENV FRONTEND_PORT=80 \
    NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    BACKEND_UPSTREAM=http://canvas-portal-backend:8080 \
    KEYCLOAK_UPSTREAM=http://127.0.0.1:65535/auth \
    APP_API_BASE_URL= \
    APP_KEYCLOAK_URL=/auth \
    APP_KEYCLOAK_REALM=canvas \
    APP_KEYCLOAK_CLIENT_ID=canvas-portal \
    APP_BASE= \
    OPENAN_PORTAL_URL= \
    FRONTEND_BASE=$FRONTEND_BASE

COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY docker-entrypoint.d/30-runtime-config.sh /docker-entrypoint.d/30-runtime-config.sh
COPY --from=build /app/dist /tmp/dist

RUN chmod +x /docker-entrypoint.d/30-runtime-config.sh && \
    if [ -n "$FRONTEND_BASE" ]; then \
      mkdir -p "/usr/share/nginx/html${FRONTEND_BASE}" && \
      cp -a /tmp/dist/. /usr/share/nginx/html/ && \
      cp -a /tmp/dist/. "/usr/share/nginx/html${FRONTEND_BASE}/" && \
      sed -i "s|src=\"/config.js\"|src=\"${FRONTEND_BASE}/config.js\"|; s|href=\"/vite.svg\"|href=\"${FRONTEND_BASE}/vite.svg\"|" "/usr/share/nginx/html${FRONTEND_BASE}/index.html"; \
    else \
      cp -a /tmp/dist/. /usr/share/nginx/html/; \
    fi && rm -rf /tmp/dist

EXPOSE 80
