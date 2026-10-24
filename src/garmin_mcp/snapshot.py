"""Read-only snapshots of the mirror for other readers (notebooks, dashboards, backups).

DuckDB lets only one process open the live file, so after changes the database file is
copied, under the write lock and right after a CHECKPOINT (which folds the write-ahead
log into the file, so the file alone is a complete, consistent database), into a new
file that atomically replaces the previous snapshot. A plain file copy costs no memory
and a few seconds, where COPY FROM DATABASE rebuilt every table through the buffer pool
(minutes, and more memory than a 2 GB mirror has under the container limit). Readers
attach the copy, locally or over HTTP (the route serves byte ranges, which is how
DuckDB's httpfs reads):

    ATTACH 'http://garmin-mcp:8000/snapshot.duckdb' AS garmin (READ_ONLY);
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from email.utils import formatdate
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, Response

from .db.database import Database

log = logging.getLogger("garmin_mcp.snapshot")

HOUR = 3600.0
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class SnapshotPublisher:
    def __init__(self, db: Database, name: str = "garmin.duckdb", *, idle_interval: float = HOUR, backfill_interval: float = 6 * HOUR) -> None:
        self.db = db
        self.path = Path(db.options.snapshot_dir) / name
        self.idle_interval = idle_interval
        self.backfill_interval = backfill_interval
        self.dirty = True
        self.last_published = self.path.stat().st_mtime if self.path.exists() else 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def mark_dirty(self) -> None:
        self.dirty = True

    def publish(self) -> Path:
        """Checkpoint, copy the database file and swap the copy in. Safe to call from any thread."""
        with self._lock:
            tmp = self.path.with_name(self.path.name + ".tmp")
            for stale in (tmp, tmp.with_name(tmp.name + ".wal")):
                if stale.exists():
                    stale.unlink()
            source = self.db.options.path
            if source == ":memory:":
                raise RuntimeError("Snapshots need a file-backed mirror")
            with self.db.write() as tx:
                # Every write goes through this lock, so nothing touches the file during the
                # copy; readers on their own cursors keep running (a checkpoint tolerates them).
                tx.run("CHECKPOINT")
                shutil.copyfile(source, tmp)
            os.replace(tmp, self.path)
            self.dirty = False
            self.last_published = time.time()
            log.info("snapshot: published %s (%d bytes)", self.path, self.path.stat().st_size)
            return self.path

    def due(self) -> bool:
        if not self.dirty:
            return False
        backlog = int(self.db.scalar("SELECT count(*) FROM sync_job WHERE priority >= 20") or 0)
        interval = self.backfill_interval if backlog > 0 else self.idle_interval
        return time.time() - self.last_published >= interval

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.due():
                    self.publish()
            except Exception:  # noqa: BLE001
                log.exception("snapshot: publish failed")
            self._stop.wait(60)

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="garmin-snapshot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------- HTTP

    def serve(self, request: Request) -> Response:
        """GET/HEAD with Range support; 404 until the first snapshot exists."""
        if not self.path.exists():
            return Response("No snapshot yet", status_code=404, media_type="text/plain")
        st = self.path.stat()
        size = st.st_size
        headers = {"accept-ranges": "bytes", "last-modified": formatdate(st.st_mtime, usegmt=True), "content-type": "application/octet-stream"}
        start, end, status = 0, size - 1, 200
        m = _RANGE.match(request.headers.get("range", ""))
        if m:
            if m.group(1) == "" and m.group(2) != "":
                start = max(0, size - int(m.group(2)))
            else:
                start = int(m.group(1) or 0)
                if m.group(2) != "":
                    end = min(end, int(m.group(2)))
            if start > end or start >= size:
                return Response(status_code=416, headers={"content-range": f"bytes */{size}"})
            status = 206
            headers["content-range"] = f"bytes {start}-{end}/{size}"
        length = end - start + 1
        headers["content-length"] = str(length)
        if request.method == "HEAD":
            return Response(status_code=status, headers=headers)
        return _RangeFileResponse(str(self.path), start, length, status, headers)


class _RangeFileResponse(FileResponse):
    """A FileResponse limited to [start, start+length) with the headers we computed."""

    def __init__(self, path: str, start: int, length: int, status: int, headers: dict[str, str]) -> None:
        super().__init__(path, status_code=status, media_type="application/octet-stream")
        self._start = start
        self._length = length
        self.raw_headers = [(k.encode(), v.encode()) for k, v in headers.items()]

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[override]
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        remaining = self._length
        with open(self.path, "rb") as f:
            f.seek(self._start)
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": remaining > 0})
        if remaining > 0:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
