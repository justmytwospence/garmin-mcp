# syntax=docker/dockerfile:1

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /bin/
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so source edits do not invalidate the layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=http \
    PORT=8000 \
    GARMIN_DB_PATH=/data/garmin.duckdb \
    GARMINTOKENS=/data/tokens \
    # Where download_activity_file and download_course_gpx save files, and where
    # set_fit_download_dir keeps its setting: on the volume, so they survive a restart.
    GARMIN_FIT_DOWNLOAD_DIR=/data/downloads \
    GARMIN_FIT_CONFIG=/data/fit_config.json \
    # DuckDB otherwise sizes its buffer pool to 80% of host RAM and the container gets
    # OOM-killed. Peak RSS is about 100 MB + this limit + 300 MB of transient parsing
    # memory, so 512 MB fits a 1.5 GB container (see docs/operations.md).
    DUCKDB_MEMORY_LIMIT=512MB \
    DUCKDB_THREADS=2 \
    PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home --uid 1000 app \
    && mkdir /data && chown app:app /data
WORKDIR /app
COPY --from=build --chown=app:app /app /app
# The mirror (garmin.duckdb), its snapshots (snapshot/), the FIT files (fit/) and the
# token store (tokens/); a named volume mounted here inherits the ownership.
VOLUME ["/data"]
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=2s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health', timeout=4).status==200 else 1)"]
ENTRYPOINT ["garmin-mcp"]
