# syntax=docker/dockerfile:1

# uv is the Python package manager (think npm). We copy just its binary from the official image.
FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM python:3.13.18-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
COPY --from=uv /uv /usr/local/bin/uv
# /data/storage is the storage mount point, owned by the app user. An empty named volume mounted
# there copies this ownership, so the server can write uploads without running as root.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/storage && chown app:app /data/storage
WORKDIR /app
# Copy only the dependency files first, so Docker can cache the install layer and skip it
# when only application code changes (like copying package.json before `npm ci`).
COPY pyproject.toml uv.lock ./

# ---- dev: includes test tools; source is bind-mounted by compose.yaml for live reload ----
FROM base AS dev
# --frozen = install exactly what uv.lock says and fail if it is out of date (like `npm ci`).
RUN uv sync --frozen --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY tests ./tests
COPY devtools ./devtools
RUN chown -R app:app /app
USER app
# --timeout-graceful-shutdown: open event streams (SSE) never finish on their own, so on stop or
# reload uvicorn closes them after 5 s instead of waiting forever; clients reconnect and resume.
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "app", "--no-access-log", "--timeout-graceful-shutdown", "5"]

# ---- runtime: no dev tools, no reload ----
FROM base AS runtime
RUN uv sync --frozen --no-install-project --no-dev
# Code stays owned by root, so the server process (user "app") can read but not modify it.
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--timeout-graceful-shutdown", "5"]
