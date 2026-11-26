"""Claude Code hooks that wake a session when a message arrives, and keep a builder from stopping early.

`crosspatch hook claude` runs as an `asyncRewake` hook on SessionStart and Stop. Claude Code documents that
such a hook wakes the session when it exits with code 2, even when the session is idle, and shows the
hook's stderr to Claude. The hook waits in the background until a message for this session's member
arrives, then prints a notice and exits 2. A lock keeps one waiting hook per member.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from crosspatch import fsutil, messages
from crosspatch.agreement import load_agreement
from crosspatch.errors import CrossError
from crosspatch.project import Project, find_project
from crosspatch.registry import Member, load_members, threads_of
from crosspatch.wake import Notice, claude_hook_lock

WAKE_EXIT = 2
HOOK_TIMEOUT = 86400
POLL = 2.0


def _member_for_session(project: Project, session_id: str) -> Member | None:
    for member in load_members(project).values():
        if member.platform != "claude-code":
            continue
        if member.session == f"claude:{session_id}" or member.wake.target == session_id:
            return member
    return None


def _read_input(stream: TextIO) -> dict[str, Any]:
    try:
        data = json.loads(stream.read() or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _context(data: dict[str, Any]) -> tuple[Project, Member] | None:
    session_id = str(data.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "")
    cwd = data.get("cwd") or os.getcwd()
    if not session_id:
        return None
    try:
        project = find_project(Path(cwd))
    except CrossError:
        return None
    member = _member_for_session(project, session_id)
    return (project, member) if member else None


def _new_for(project: Project, member: Member) -> list[tuple[str, messages.Message]]:
    found = []
    for thread in threads_of(project, member.name):
        listed = messages.list_messages(project, thread.name)
        for message in messages.unannounced(project, listed, thread, member.name):
            found.append((thread.name, message))
    return found


def run_wake_hook(stdin: TextIO, stderr: TextIO, max_seconds: float = HOOK_TIMEOUT - 60, poll: float = POLL) -> int:
    context = _context(_read_input(stdin))
    if context is None:
        return 0
    project, member = context
    with fsutil.locked(claude_hook_lock(project, member.name), blocking=False) as got:
        if not got:
            return 0
        deadline = time.monotonic() + max_seconds
        while time.monotonic() < deadline:
            found = _new_for(project, member)
            if found:
                for thread, message in found:
                    messages.mark_notified(project, thread, member.name, [message.id])
                    notice = Notice(project, thread, message.id, message.sender, message.kind, member.name)
                    print(notice.text(), file=stderr)
                stderr.flush()
                return WAKE_EXIT
            time.sleep(poll)
    return 0


def run_stop_hook(stdin: TextIO, stdout: TextIO) -> int:
    """Block the end of a turn once while a message still waits for this member's reply."""
    data = _read_input(stdin)
    if data.get("stop_hook_active"):
        return 0
    context = _context(data)
    if context is None:
        return 0
    project, member = context
    agreement = load_agreement(project)
    waiting = []
    for thread in threads_of(project, member.name):
        listed = messages.list_messages(project, thread.name)
        waiting += [f"{thread.name} {m.id} ({m.kind} from {m.sender})" for m in messages.unanswered(listed, thread, member.name, agreement)]
    if not waiting:
        return 0
    reason = (
        "These bridge messages still wait for your reply: " + ", ".join(waiting) + ". "
        f"Answer them with `crosspatch reply <thread> <id> --as {member.name}`, or say why you cannot."
    )
    json.dump({"decision": "block", "reason": reason}, stdout)
    return 0


def executable() -> str:
    found = shutil.which("crosspatch")
    if found:
        return found
    return f"{sys.executable} -m crosspatch"


def settings_snippet(momentum: bool = False) -> dict[str, Any]:
    command = executable()
    wake = {"type": "command", "command": f"{command} hook claude", "asyncRewake": True, "timeout": HOOK_TIMEOUT}
    hooks: dict[str, Any] = {
        "SessionStart": [{"hooks": [dict(wake)]}],
        "Stop": [{"hooks": [dict(wake)]}],
    }
    if momentum:
        hooks["Stop"][0]["hooks"].append({"type": "command", "command": f"{command} hook claude-stop", "timeout": 30})
    return {"hooks": hooks}


def install(project: Project, momentum: bool = False) -> Path:
    """Merge the hooks into the project's .claude/settings.local.json, replacing earlier crosspatch hooks."""
    path = project.root / ".claude" / "settings.local.json"
    current = fsutil.read_json(path, {})
    if not isinstance(current, dict):
        raise CrossError(f"{path} does not hold a JSON object")
    hooks = current.setdefault("hooks", {})
    for event, groups in settings_snippet(momentum)["hooks"].items():
        kept = []
        for group in hooks.get(event, []):
            entries = [h for h in group.get("hooks", []) if "crosspatch" not in str(h.get("command", ""))]
            if entries:
                kept.append({**group, "hooks": entries})
        hooks[event] = kept + groups
    fsutil.atomic_write_json(path, current)
    return path
