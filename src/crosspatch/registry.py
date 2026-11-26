"""Members (named sessions) and threads (who is bridged to whom), stored as JSON in the bridge folder."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from crosspatch import fsutil
from crosspatch.agreement import Agreement
from crosspatch.errors import UsageError
from crosspatch.project import Project, check_name

PLATFORMS = ("claude-code", "codex", "human", "other")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Wake:
    adapter: str = "poll"
    target: str = ""
    options: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"adapter": self.adapter, "target": self.target, "options": dict(self.options)}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Wake:
        data = data or {}
        return cls(str(data.get("adapter", "poll")), str(data.get("target", "")), dict(data.get("options") or {}))

    def __str__(self) -> str:
        text = self.adapter + (f":{self.target}" if self.target else "")
        if self.options:
            text += " (" + ", ".join(f"{k}={v}" for k, v in sorted(self.options.items())) + ")"
        return text


@dataclass
class Member:
    name: str
    role: str
    platform: str
    session: str = ""
    wake: Wake = field(default_factory=Wake)
    joined: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "platform": self.platform,
            "session": self.session,
            "wake": self.wake.to_json(),
            "joined": self.joined,
        }

    @classmethod
    def from_json(cls, name: str, data: dict[str, Any]) -> Member:
        return cls(
            name=name,
            role=str(data.get("role", "")),
            platform=str(data.get("platform", "other")),
            session=str(data.get("session", "")),
            wake=Wake.from_json(data.get("wake")),
            joined=str(data.get("joined", "")),
        )


@dataclass
class Thread:
    name: str
    roles: dict[str, list[str]]
    status: str = "open"
    created: str = ""
    updated: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "roles": {role: list(names) for role, names in self.roles.items() if names},
            "status": self.status,
            "created": self.created,
            "updated": self.updated,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Thread:
        return cls(
            name=str(data["name"]),
            roles={str(k): [str(n) for n in v] for k, v in (data.get("roles") or {}).items()},
            status=str(data.get("status", "open")),
            created=str(data.get("created", "")),
            updated=str(data.get("updated", "")),
        )

    @property
    def members(self) -> list[str]:
        seen: list[str] = []
        for names in self.roles.values():
            for name in names:
                if name not in seen:
                    seen.append(name)
        return seen

    def role_of(self, member: str) -> str | None:
        for role, names in self.roles.items():
            if member in names:
                return role
        return None

    def resolve(self, address: str, sender: str) -> list[str]:
        """The members a `to` address reaches: a member, a role, or `all` (everyone but the sender)."""
        if address == "all":
            return [m for m in self.members if m != sender]
        if address in self.roles:
            return [m for m in self.roles[address] if m != sender]
        if address in self.members:
            return [address]
        return []


@contextlib.contextmanager
def _registry_lock(project: Project) -> Iterator[None]:
    with fsutil.locked(project.state_dir / "registry.lock"):
        yield


def load_members(project: Project) -> dict[str, Member]:
    data = fsutil.read_json(project.members_path, {"members": {}})
    return {name: Member.from_json(name, spec) for name, spec in (data.get("members") or {}).items()}


def _save_members(project: Project, members: dict[str, Member]) -> None:
    fsutil.atomic_write_json(
        project.members_path,
        {"version": 1, "members": {name: m.to_json() for name, m in sorted(members.items())}},
    )


def human_member(agreement: Agreement) -> Member:
    return Member(name=agreement.human, role="human", platform="human")


def get_member(project: Project, agreement: Agreement, name: str) -> Member:
    members = load_members(project)
    if name in members:
        return members[name]
    if name == agreement.human:
        return human_member(agreement)
    known = ", ".join(sorted(members)) or "none yet"
    raise UsageError(f"no member named {name!r}. Members: {known}. Add one with `crosspatch join`.")


def join(project: Project, agreement: Agreement, member: Member, replace: bool = False) -> Member:
    check_name(member.name, "member")
    agreement.role(member.role)
    if member.platform not in PLATFORMS:
        raise UsageError(f"platform must be one of {', '.join(PLATFORMS)}")
    if member.name == agreement.human and member.role != "human":
        raise UsageError(f"{member.name!r} is the human in bridge.toml and can only have the human role")
    with _registry_lock(project):
        members = load_members(project)
        if member.name in members and not replace:
            raise UsageError(f"{member.name!r} has already joined. Use --replace to change it, or pick another name.")
        if member.session:
            for other in members.values():
                if other.name != member.name and other.session == member.session:
                    raise UsageError(f"session {member.session} has already joined as {other.name!r}")
        member.joined = member.joined or now_iso()
        members[member.name] = member
        _save_members(project, members)
    return member


def leave(project: Project, name: str, force: bool = False) -> None:
    with _registry_lock(project):
        members = load_members(project)
        if name not in members:
            raise UsageError(f"no member named {name!r}")
        linked = [t.name for t in list_threads(project) if t.status == "open" and name in t.members]
        if linked and not force:
            raise UsageError(f"{name!r} is linked in {', '.join(linked)}. Unlink it first, or use --force.")
        for thread_name in linked:
            _remove_from_thread(project, thread_name, name)
        del members[name]
        _save_members(project, members)


def thread_path(project: Project, thread: str) -> Path:
    return project.thread_dir(thread) / "thread.json"


def load_thread(project: Project, thread: str) -> Thread:
    path = thread_path(project, thread)
    data = fsutil.read_json(path, None)
    if data is None:
        known = ", ".join(t.name for t in list_threads(project)) or "none yet"
        raise UsageError(f"no thread named {thread!r}. Threads: {known}. Pair sessions with `crosspatch link`.")
    return Thread.from_json(data)


def list_threads(project: Project) -> list[Thread]:
    threads: list[Thread] = []
    if not project.threads_dir.is_dir():
        return threads
    for child in sorted(project.threads_dir.iterdir()):
        data = fsutil.read_json(child / "thread.json", None)
        if data is not None:
            threads.append(Thread.from_json(data))
    return threads


def link(project: Project, agreement: Agreement, thread: str, assignments: dict[str, list[str]]) -> Thread:
    """Create a thread, or add members to one, with each member in one role."""
    check_name(thread, "thread")
    for role in assignments:
        agreement.role(role)
    members = load_members(project)
    for role, names in assignments.items():
        for name in names:
            if name not in members and name != agreement.human:
                raise UsageError(f"no member named {name!r}. Add it with `crosspatch join` first.")
            if name == agreement.human and role != "human":
                raise UsageError(f"{name!r} is the human and can only be linked with --human")
    with _registry_lock(project):
        path = thread_path(project, thread)
        existing = fsutil.read_json(path, None)
        current = Thread.from_json(existing) if existing else Thread(thread, {}, created=now_iso())
        for role, names in assignments.items():
            for name in names:
                held = current.role_of(name)
                if held and held != role:
                    raise UsageError(f"{name!r} is already the {held} in {thread!r}. Unlink it first.")
                current.roles.setdefault(role, [])
                if name not in current.roles[role]:
                    current.roles[role].append(name)
        if "human" in agreement.roles and agreement.human not in current.members:
            current.roles.setdefault("human", []).append(agreement.human)
        order = list(agreement.roles)
        current.roles = dict(sorted(current.roles.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order)))
        current.status = "open"
        current.updated = now_iso()
        _save_thread(project, current)
    return current


def _save_thread(project: Project, thread: Thread) -> None:
    fsutil.atomic_write_json(thread_path(project, thread.name), thread.to_json(), sort_keys=False)


def _remove_from_thread(project: Project, thread: str, member: str) -> Thread:
    current = load_thread(project, thread)
    for role in list(current.roles):
        current.roles[role] = [m for m in current.roles[role] if m != member]
    current.roles = {role: names for role, names in current.roles.items() if names}
    current.updated = now_iso()
    _save_thread(project, current)
    return current


def unlink(project: Project, thread: str, member: str | None = None) -> Thread:
    """Take one member out of a thread, or close the whole pairing. Messages are kept."""
    load_thread(project, thread)
    with _registry_lock(project):
        if member:
            current = load_thread(project, thread)
            if member not in current.members:
                raise UsageError(f"{member!r} is not in {thread!r}")
            return _remove_from_thread(project, thread, member)
        current = load_thread(project, thread)
        current.status = "closed"
        current.updated = now_iso()
        _save_thread(project, current)
        return current


def threads_of(project: Project, member: str) -> list[Thread]:
    return [t for t in list_threads(project) if t.status == "open" and member in t.members]
