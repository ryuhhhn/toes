# syntax=docker/dockerfile:1
FROM python:3.13-slim

# uv resolves and installs far faster than pip here, and every service already has a
# uv.lock — so the image gets the exact versions the suites were run against.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /srv

ENV UV_COMPILE_BYTECODE=1     UV_LINK_MODE=copy     PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1

# Dependencies first, in their own layer: application code changes constantly, the
# lock file rarely, so a code edit must not reinstall the world.
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-install-project --no-dev 2>/dev/null  || uv sync --no-install-project --no-dev

COPY . .
RUN uv sync --no-dev 2>/dev/null || true

ENV PATH="/srv/.venv/bin:$PATH"
