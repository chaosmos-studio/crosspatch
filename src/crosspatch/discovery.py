"""Find sessions this machine can wake: Codex threads, Claude Code sessions and tmux panes.

The files read here are the platforms' own local records. They are not public interfaces, so discovery
is best effort: when it misses a session, join it by its id instead.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

UUID_TAIL = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
HEAD_BYTES = 256 * 1024
TAIL_BYTES = 512 * 1024


@dataclass
class Session:
    platform: str
    id: str
    cwd: str
    title: str
    updated: float
    live: bool = False
    detail: str = ""

    @property
    def ref(self) -> str:
        prefix = {"claude-code": "claude", "codex": "codex", "tmux": "tmux"}.get(self.platform, self.platform)
        return f"{prefix}:{self.id}"

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["ref"] = self.ref
        return data


def _parse_time(value: str) -> float:
    with contextlib.suppress(ValueError, TypeError):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return 0.0


def _first_line(path: Path, limit: int = 4 * 1024 * 1024) -> str:
    with path.open("rb") as handle:
        return handle.readline(limit).decode("utf-8", "replace")


def _json_lines(data: bytes) -> Iterator[dict]:
    for raw in data.split(b"\n"):
        raw = raw.strip()
        if not raw.startswith(b"{"):
            continue
        with contextlib.suppress(ValueError):
            value = json.loads(raw)
            if isinstance(value, dict):
                yield value


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def discover_codex(home: Path | None = None, limit: int = 200) -> list[Session]:
    """Codex threads, newest first, from its session index and the first line of each rollout file."""
    home = home or codex_home()
    entries: dict[str, tuple[str, float]] = {}
    index = home / "session_index.jsonl"
    if index.is_file():
        for item in _json_lines(index.read_bytes()):
            sid = str(item.get("id", ""))
            if not sid:
                continue
            updated = _parse_time(str(item.get("updated_at", "")))
            if sid not in entries or updated >= entries[sid][1]:
                entries[sid] = (str(item.get("thread_name") or ""), updated)
    files: dict[str, Path] = {}
    for folder in (home / "sessions", home / "archived_sessions"):
        if folder.is_dir():
            for path in folder.rglob("rollout-*.jsonl"):
                match = UUID_TAIL.search(path.name)
                if match:
                    files.setdefault(match.group(1), path)
    for sid, path in files.items():
        if sid not in entries:
            with contextlib.suppress(OSError):
                entries[sid] = ("", path.stat().st_mtime)
    ordered = sorted(entries.items(), key=lambda kv: kv[1][1], reverse=True)[:limit]
    sessions: list[Session] = []
    for sid, (title, updated) in ordered:
        cwd, detail = "", ""
        path = files.get(sid)
        if path is not None:
            with contextlib.suppress(OSError, ValueError):
                meta = json.loads(_first_line(path)).get("payload") or {}
                if isinstance(meta.get("source"), dict):
                    continue
                cwd = str(meta.get("cwd") or "")
                detail = " ".join(str(meta.get(k)) for k in ("originator", "source") if meta.get(k))
                if "archived_sessions" in path.parts:
                    detail = (detail + " archived").strip()
        sessions.append(Session("codex", sid, cwd, title or "(untitled)", updated, False, detail))
    return sessions


def _claude_title(path: Path) -> tuple[str, str]:
    """The newest title record and the working folder of a Claude Code transcript."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(HEAD_BYTES)
        handle.seek(max(0, size - TAIL_BYTES))
        tail = handle.read(TAIL_BYTES)
    cwd, first_prompt = "", ""
    for record in _json_lines(head):
        cwd = cwd or str(record.get("cwd") or "")
        if not first_prompt and record.get("type") == "user":
            content = (record.get("message") or {}).get("content")
            if isinstance(content, str) and not content.startswith("<"):
                first_prompt = " ".join(content.split())[:80]
        if cwd and first_prompt:
            break
    custom, generated = "", ""
    for record in _json_lines(head + b"\n" + tail):
        if record.get("type") == "custom-title" and record.get("customTitle"):
            custom = str(record["customTitle"])
        elif record.get("type") == "ai-title" and record.get("aiTitle"):
            generated = str(record["aiTitle"])
    return custom or generated or first_prompt, cwd


def discover_claude(home: Path | None = None, limit: int = 200) -> list[Session]:
    """Claude Code sessions: running ones from its session registry, then stored transcripts, newest first."""
    home = home or claude_home()
    live: dict[str, dict] = {}
    registry = home / "sessions"
    if registry.is_dir():
        for path in registry.glob("*.json"):
            with contextlib.suppress(OSError, ValueError):
                record = json.loads(path.read_text(encoding="utf-8"))
                pid = int(record.get("pid", 0))
                if record.get("sessionId") and pid and _pid_alive(pid):
                    live[str(record["sessionId"])] = record
    transcripts: list[tuple[float, Path]] = []
    projects = home / "projects"
    if projects.is_dir():
        for path in projects.glob("*/*.jsonl"):
            with contextlib.suppress(OSError):
                transcripts.append((path.stat().st_mtime, path))
    transcripts.sort(reverse=True)
    sessions: dict[str, Session] = {}
    for sid, record in live.items():
        detail = " ".join(str(record.get(k)) for k in ("status", "entrypoint", "kind") if record.get(k))
        updated = float(record.get("updatedAt") or record.get("startedAt") or 0) / 1000.0
        sessions[sid] = Session("claude-code", sid, str(record.get("cwd") or ""), str(record.get("name") or ""), updated, True, detail)
    for mtime, path in transcripts[:limit]:
        sid = path.stem
        with contextlib.suppress(OSError):
            title, cwd = _claude_title(path)
            if sid in sessions:
                current = sessions[sid]
                current.title = title or current.title
                current.cwd = current.cwd or cwd
                current.updated = max(current.updated, mtime)
            else:
                sessions[sid] = Session("claude-code", sid, cwd, title or "(untitled)", mtime, False, "stored")
    return sorted(sessions.values(), key=lambda s: s.updated, reverse=True)[:limit]


def tmux_command(socket: str | None = None) -> list[str]:
    base = [os.environ.get("CROSSPATCH_TMUX") or "tmux"]
    if socket:
        base += ["-S", socket] if "/" in socket else ["-L", socket]
    return base


def discover_tmux(socket: str | None = None) -> list[Session]:
    command = tmux_command(socket)
    if shutil.which(command[0]) is None:
        return []
    fmt = "\t".join(
        ["#{pane_id}", "#{session_name}:#{window_index}.#{pane_index}", "#{pane_current_path}",
         "#{pane_current_command}", "#{pane_title}", "#{window_activity}"]
    )
    try:
        out = subprocess.run([*command, "list-panes", "-a", "-F", fmt], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    sessions = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        pane_id, where, cwd, command_name, title, activity = parts[:6]
        updated = float(activity) if activity.isdigit() else time.time()
        sessions.append(Session("tmux", pane_id, cwd, f"{where} {command_name}".strip(), updated, True, title))
    return sessions


def discover(platforms: Iterable[str], limit: int = 200, tmux_socket: str | None = None) -> list[Session]:
    found: list[Session] = []
    for platform in platforms:
        if platform == "codex":
            found += discover_codex(limit=limit)
        elif platform == "claude-code":
            found += discover_claude(limit=limit)
        elif platform == "tmux":
            found += discover_tmux(tmux_socket)
    return sorted(found, key=lambda s: s.updated, reverse=True)


def guess_platform(session: Session) -> str:
    """The agent platform behind a session. A tmux pane is guessed from the command running in it."""
    if session.platform != "tmux":
        return session.platform
    command = session.title.split(" ")[-1].lower()
    if "claude" in command:
        return "claude-code"
    if "codex" in command:
        return "codex"
    return "other"


def age(updated: float, now: float | None = None) -> str:
    seconds = max(0, int((now or time.time()) - updated))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def short_path(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path
