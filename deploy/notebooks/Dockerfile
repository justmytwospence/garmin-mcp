# The notebooks profile: marimo's image plus what deploy/notebooks/garmin.py imports.
# DuckDB here only reads the snapshot, so its version need not match the server's; it
# must be at least the server's major version to read the file format. The pins cover the
# direct dependencies; the release cooldown holds back fresh releases of what they pull in.
FROM ghcr.io/marimo-team/marimo:0.25.1
USER root
RUN uv pip install --system --no-cache --exclude-newer "7 days" \
      duckdb==1.5.6 \
      polars==1.44.2 \
      altair==6.3.0 \
      pyarrow==25.0.1 \
      pytz==2026.5
USER appuser
WORKDIR /app
# The notebook is bind-mounted at /app/notebooks/garmin.py by compose, so edits need no rebuild.
