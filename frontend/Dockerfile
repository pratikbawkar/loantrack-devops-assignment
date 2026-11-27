# syntax=docker/dockerfile:1

# ---- build stage: syntax-check the JavaScript and collect only the served files ----
# Node is used only here; it never reaches the final image.
FROM node:24.22.3-alpine3.24 AS build
WORKDIR /src
COPY index.html ./
COPY css/ ./css/
COPY js/ ./js/
RUN node --check js/app.js \
 && mkdir -p /dist \
 && cp -r index.html css js /dist/


# ---- runtime stage: unprivileged nginx serving static files on port 8080 ----
FROM nginxinc/nginx-unprivileged:1.30.6-alpine3.24

# NGINX_ENTRYPOINT_LOCAL_RESOLVERS makes the image export the container's DNS
# server as NGINX_LOCAL_RESOLVERS; nginx.conf is rendered from a template with
# it at startup. The filter limits substitution to our own variables so nginx
# variables such as $host are left untouched.
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    NGINX_ENVSUBST_FILTER="BACKEND_HOST|BACKEND_PORT|NGINX_LOCAL_RESOLVERS" \
    BACKEND_HOST=backend \
    BACKEND_PORT=8000

# The stock server block would also listen on 8080; remove it (needs root once).
USER root
RUN rm -f /etc/nginx/conf.d/default.conf
COPY --chown=101:101 nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build --chown=101:101 /dist/ /usr/share/nginx/html/

# Explicit non-root user (uid 101 = nginx in this image).
USER 101:101

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"]
