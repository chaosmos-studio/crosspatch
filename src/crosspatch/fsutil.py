"""Small filesystem primitives: atomic writes, exclusive publishing and advisory locks."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

TMP_PREFIX = ".tmp-"


def _tmp_path(directory: Path) -> Path:
    return directory / f"{TMP_PREFIX}{os.getpid()}-{secrets.token_hex(4)}"


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_tmp(directory: Path, data: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(directory)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return tmp


def atomic_write_text(path: Path, text: str) -> None:
    """Replace a file so a reader sees either the old or the new content, never half of it."""
    tmp = _write_tmp(path.parent, text.encode("utf-8"))
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def atomic_write_json(path: Path, value: Any, sort_keys: bool = True) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=sort_keys) + "\n")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def publish_exclusive(
    directory: Path,
    first: int,
    name_for: Callable[[int], str],
    content_for: Callable[[int], str],
    attempts: int = 1000,
) -> tuple[int, Path]:
    """Write a new file under the lowest free number at or above `first`.

    The content goes to a temporary file first and is then hard-linked to its final name.
    A link never replaces an existing file, so two writers racing for the same number cannot
    both win, and a reader never sees a partly written file under a final name.
    """
    number = first
    for _ in range(attempts):
        tmp = _write_tmp(directory, content_for(number).encode("utf-8"))
        target = directory / name_for(number)
        try:
            os.link(tmp, target)
        except FileExistsError:
            number += 1
            continue
        finally:
            tmp.unlink(missing_ok=True)
        _fsync_dir(directory)
        return number, target
    raise RuntimeError(f"no free name in {directory} after {attempts} attempts")


def stale_temp_files(directory: Path, older_than: float) -> list[Path]:
    """Temporary files left behind by a writer that died, older than `older_than` seconds."""
    if not directory.is_dir():
        return []
    cutoff = time.time() - older_than
    found: list[Path] = []
    for path in directory.iterdir():
        if path.name.startswith(TMP_PREFIX):
            with contextlib.suppress(FileNotFoundError):
                if path.stat().st_mtime < cutoff:
                    found.append(path)
    return found


@contextlib.contextmanager
def locked(path: Path, blocking: bool = True, timeout: float = 30.0) -> Iterator[bool]:
    """Hold an advisory lock on `path`. Yields False when non-blocking and already held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not blocking:
                    yield False
                    return
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not lock {path}") from None
                time.sleep(0.02)
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def lock_is_held(path: Path) -> bool:
    """True when another process holds the lock on `path` right now."""
    if not path.exists():
        return False
    with locked(path, blocking=False) as got:
        return not got
