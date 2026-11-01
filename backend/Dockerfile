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
RUN useradd --create-home --uid 1000 app
WORKDIR /app
# Copy only the dependency files first, so Docker can cache the install layer and skip it
# when only application code changes (like copying package.json before `npm ci`).
COPY pyproject.toml uv.lock ./

# ---- dev: includes test tools; source is bind-mounted by compose.yaml for live reload ----
FROM base AS dev
# --frozen = install exactly what uv.lock says and fail if it is out of date (like `npm ci`).
RUN uv sync --frozen --no-install-project
COPY app ./app
COPY tests ./tests
RUN chown -R app:app /app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "app", "--no-access-log"]

# ---- runtime: no dev tools, no reload ----
FROM base AS runtime
RUN uv sync --frozen --no-install-project --no-dev
# Code stays owned by root, so the server process (user "app") can read but not modify it.
COPY app ./app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
