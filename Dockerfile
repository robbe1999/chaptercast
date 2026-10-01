# syntax=docker/dockerfile:1

# ---- 1. Build the frontend -------------------------------------------------
FROM node:26-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. Runtime ------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg stitches MP3 chunks losslessly. Without it the app falls back to byte
# concatenation, so it is an optimisation rather than a hard dependency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged user with no login shell and no home directory.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir /data && chown app:app /data && chmod 700 /data

WORKDIR /build
# Install the locked, pinned dependency set first (cached layer), then the app itself.
COPY backend/requirements.lock backend/pyproject.toml ./
COPY backend/chaptercast ./chaptercast
RUN pip install -r requirements.lock && pip install --no-deps .

COPY --from=web /web/dist /srv/static

ENV CHAPTERCAST_STATIC_DIR=/srv/static \
    CHAPTERCAST_DATA_DIR=/data

USER 10001:10001
WORKDIR /srv
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

# Secrets are never baked into the image: pass them at run time (env or secret store).
CMD ["uvicorn", "chaptercast.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
