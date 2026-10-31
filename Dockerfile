# syntax=docker/dockerfile:1.7
FROM node:22-slim AS pdfjs-assets
WORKDIR /assets
COPY efile_app/package.json efile_app/package-lock.json ./
COPY efile_app/scripts/copy-pdfjs.mjs ./scripts/copy-pdfjs.mjs
RUN npm ci --omit=dev

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system deps (curl for the uv installer; git because MACourts and
# VTCourts are installed straight from GitHub until they are on PyPI)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git poppler-utils tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app/efile_app

# Copy project metadata first for better layer caching (project now rooted at efile_app/)
COPY efile_app/pyproject.toml efile_app/uv.lock* ./

# Install dependencies without installing the project code yet (faster rebuilds)
RUN uv sync --frozen --no-install-project

# Copy the rest of the source code into /app
COPY . /app
COPY --from=pdfjs-assets /assets/efile/static/vendor/pdfjs /app/efile_app/efile/static/vendor/pdfjs

# Install the project itself (editable-like install)
RUN uv sync --frozen

# Collect static files at build time so WhiteNoise can serve them at runtime
# Use staging settings to ensure DEBUG=False and STATIC_ROOT is set
# Provide a non-insecure SECRET_KEY for build to satisfy settings_staging
RUN DJANGO_SETTINGS_MODULE=efile.settings_staging \
    DJANGO_SECRET_KEY=build-static-collect-key \
    DATABASE_URL=sqlite:////tmp/build-collectstatic.sqlite3 \
    uv run python manage.py collectstatic --noinput

EXPOSE 8000

# Use Gunicorn with Uvicorn workers (ASGI) and log to stdout/stderr (debug level)
CMD ["uv", "run", "gunicorn", "efile.asgi:application", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-", "--log-level", "debug", "--access-logformat", "%(h)s - %(l)s %(u)s [%(t)s] \"%(r)s\" %(s)s %(b)s \"%(f)s\" \"%(a)s\" in %(L)ss"]
