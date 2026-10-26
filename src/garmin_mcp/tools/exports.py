"""Filesystem export boundary: no storage/config overwrite or symlink traversal.

Use directory descriptors for the entire walk and atomic replacement, rather than
check-then-open of a resolved path. Existing exports may be replaced, never followed
or truncated (including hardlinks). No caller-controlled path is used for a temp file.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from ..garmin.auth import DEFAULT_TOKEN_DIR, token_dir
from ..mirror import Mirror
from .base import ToolFailure


def validate_export(mirror: Mirror, path: str, config_path: str) -> Path:
    target = Path(os.path.abspath(os.path.expanduser(path)))
    data = mirror.fetch.data_dir
    roots = [data / "fit", data / "duckdb-tmp", Path(mirror.db.options.snapshot_dir), Path(DEFAULT_TOKEN_DIR).expanduser(), Path(token_dir())]
    if getattr(mirror, "tokenstore", None):
        roots.append(Path(mirror.tokenstore).expanduser())
    files = [Path(config_path).expanduser(), Path("~/.garminconnect_fit_config.json").expanduser(), Path.cwd() / ".env"]
    if mirror.db.options.path != ":memory:":
        db = Path(mirror.db.options.path)
        files.extend([db, Path(str(db) + ".wal")])
    # Resolve protected locations too: the operator may have configured a symlink.
    resolved = target.resolve()
    if any(resolved.is_relative_to(root.resolve()) for root in roots) or any(resolved == f.resolve() for f in files):
        raise ToolFailure("Export destination is protected mirror, authentication or configuration storage.")
    # Reject even harmless symlinks; the descriptor walk below enforces this again.
    for p in (target, *target.parents):
        if p.is_symlink():
            raise ToolFailure("Export destinations must not traverse symlinks.")
    return target


def write_export(mirror: Mirror, path: str, payload: bytes, config_path: str) -> str:
    target = validate_export(mirror, path, config_path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(target.anchor, flags)
    tmp = ".garmin-export-" + secrets.token_hex(16)
    created = False
    try:
        for part in target.parts[1:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        # Revalidate protected paths after the walk, then use only the held directory.
        validate_export(mirror, str(target), config_path)
        try:
            info = os.stat(target.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise ToolFailure("Export destination must be a regular file, not a symlink or device.")
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        created = True
        with os.fdopen(out, "wb") as f:
            f.write(payload)
        os.replace(tmp, target.name, src_dir_fd=fd, dst_dir_fd=fd)
        created = False
        return str(target)
    finally:
        if created:
            os.unlink(tmp, dir_fd=fd)
        os.close(fd)
