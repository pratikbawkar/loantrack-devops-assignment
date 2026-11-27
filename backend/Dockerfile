# syntax=docker/dockerfile:1

# ---- build stage: install the Python dependencies into a virtualenv ----
FROM python:3.12.16-slim-trixie AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The venv is created without pip: the build stage's pip installs into it, so
# pip/setuptools never end up inside the venv that ships in the final image.
RUN python -m venv --without-pip /opt/venv

# Dependency layer: only requirements.txt is copied here, so a change to the
# application source reuses this cached layer.
COPY requirements.txt /tmp/requirements.txt
RUN pip --python /opt/venv/bin/python install -r /tmp/requirements.txt


# ---- runtime stage ----
FROM python:3.12.16-slim-trixie AS runtime

# Baked into the image so a :v2 build reports v2; a runtime APP_VERSION env var still overrides it.
ARG APP_VERSION=dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    APP_VERSION=${APP_VERSION}

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY app/ ./app/

# Numeric UID so Kubernetes runAsNonRoot can verify it without resolving names.
USER 10001:10001

EXPOSE 8000

# Liveness only: /healthz never touches the database. slim has no curl, so use Python.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --start-interval=2s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

# Exec form: uvicorn is PID 1 and receives SIGTERM for a graceful shutdown.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
