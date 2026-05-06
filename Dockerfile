# Single-image stack: Starlette landing page + Datasette mounted at /db/.
# Built and deployed via Fly.io on every commit to main that touches the
# DB or the web layer.

FROM python:3.11-slim

# uv binary, pinned indirectly via the rolling :latest tag of the
# astral-sh image. We don't need the full uv source tree, just the bin.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# Install dependencies (cache layer): nothing in this layer depends on
# the project source, so docker layer-caching keeps it fast across
# code-only changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --group web

# Project source + web layer + data file.
# data/matter.db is committed to git by CI on every successful sync, so
# COPY data/ data/ pulls in whatever the latest committed snapshot is.
# changes-latest.json is the diff JSON written alongside.
COPY src/ src/
COPY web/ web/
COPY data/ data/
COPY changes-latest.json ./

# Install the project itself into the venv (was --no-install-project above).
RUN uv sync --frozen --group web

# Use the venv directly so CMD doesn't pay uv-run sync overhead per boot.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    MATTER_DB_PATH=/app/data/matter.db \
    MATTER_CHANGES_PATH=/app/changes-latest.json

EXPOSE 8080

# Fly.io health checks hit /, which is the landing page route.
CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8080"]
