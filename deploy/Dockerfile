# VeriLoop API image (modular monolith, D-06/D-11). Build from the repo root:
#   docker compose -f deploy/docker-compose.yml up -d --build
# Untested on the dev box (Docker daemon not running there); written to match the local setup.

# ---- frontend build ----------------------------------------------------------------------------
FROM node:20-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- api ---------------------------------------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VERILOOP_HOME=/data \
    HOST=0.0.0.0 \
    PORT=8000

# ffmpeg: frame/audio extraction; libgl/glib: opencv + easyocr; curl: healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/veriloop backend/veriloop
# Editable install keeps the package at /app/backend/veriloop, so repo-relative paths
# (config/tools.yaml, data/fixtures/cassettes) resolve to /app/... exactly as in development.
# CPU-only torch wheels keep the image small enough for a single VPS.
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -e "./backend[ml,c2pa]" \
 && python -m playwright install --with-deps chromium

COPY config config
COPY data data
COPY --from=web /web/dist frontend/dist

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1
CMD ["python", "-m", "veriloop"]
