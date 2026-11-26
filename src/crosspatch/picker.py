"""`crosspatch link` with no arguments: pick sessions for a thread with plain terminal prompts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from crosspatch.agreement import Agreement
from crosspatch.discovery import Session, age, guess_platform, short_path
from crosspatch.errors import UsageError
from crosspatch.project import NAME_RE, Project
from crosspatch.registry import Member, Thread, join, link, load_members
from crosspatch.wake import parse_wake

Ask = Callable[[str], str]
Say = Callable[[str], None]


@dataclass
class Choice:
    label: str
    member: Member | None = None
    session: Session | None = None


def default_wake(platform: str, session: Session | None) -> str:
    if session is None:
        return "poll"
    if session.platform == "tmux":
        return f"tmux:{session.id}"
    if platform == "codex":
        return f"codex-queue:{session.id}"
    if platform == "claude-code":
        return "claude-hook" if session.live else f"claude-resume:{session.id}"
    return "poll"


def suggest_name(platform: str, role: str, taken: set[str]) -> str:
    stem = {"claude-code": "claude", "codex": "codex"}.get(platform, "agent")
    name = f"{stem}-{role}"
    number = 2
    while name in taken:
        name = f"{stem}-{role}-{number}"
        number += 1
    return name


def build_choices(project: Project, sessions: list[Session]) -> list[Choice]:
    members = load_members(project)
    joined = {m.session for m in members.values() if m.session}
    choices = [Choice(f"{m.name}  (joined: {m.role}, {m.platform})", member=m) for m in members.values()]
    for session in sessions:
        if session.ref in joined:
            continue
        live = "live" if session.live else "stored"
        label = f"{session.ref[:20]}  {session.platform} {live} {age(session.updated)} ago  {short_path(session.cwd)}  {session.title[:50]}"
        choices.append(Choice(label, session=session))
    return choices


def _numbers(raw: str, count: int) -> list[int]:
    picked: list[int] = []
    for part in raw.replace(" ", ",").split(","):
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= count:
            raise UsageError(f"{part!r} is not a number from the list")
        if int(part) - 1 not in picked:
            picked.append(int(part) - 1)
    return picked


def run(project: Project, agreement: Agreement, sessions: list[Session], ask: Ask, say: Say, thread: str | None = None) -> Thread:
    choices = build_choices(project, sessions)
    if not choices:
        raise UsageError("no members and no sessions found. Start your agents, or join them by id with `crosspatch join`.")
    while not thread:
        answer = ask("Thread name (lower-case letters, digits and hyphens): ").strip()
        if NAME_RE.match(answer):
            thread = answer
        else:
            say("Use lower-case letters, digits and hyphens, such as art-review.")
    say("")
    for number, choice in enumerate(choices, 1):
        say(f"{number:>3}. {choice.label}")
    say("")
    plan: dict[str, list[int]] = {}
    for role, required, multiple in (("lead", True, False), ("builder", True, True), ("reviewer", False, True)):
        if role not in agreement.roles:
            continue
        while True:
            hint = "numbers, comma separated" if multiple else "a number"
            hint += "" if required else ", Enter for none"
            raw = ask(f"{role.capitalize()} ({hint}): ").strip()
            if not raw and not required:
                plan[role] = []
                break
            try:
                picked = _numbers(raw, len(choices))
            except UsageError as exc:
                say(str(exc))
                continue
            if not picked or (not multiple and len(picked) != 1):
                say("Pick one." if not multiple else "Pick at least one.")
                continue
            plan[role] = picked
            break
    used = [i for picks in plan.values() for i in picks]
    if len(used) != len(set(used)):
        raise UsageError("one session cannot hold two roles in the same thread")
    taken = set(load_members(project))
    assignments: dict[str, list[str]] = {}
    for role, picks in plan.items():
        for index in picks:
            choice = choices[index]
            if choice.member is not None:
                assignments.setdefault(role, []).append(choice.member.name)
                continue
            session = choice.session
            assert session is not None
            platform = guess_platform(session)
            suggested = suggest_name(platform, role, taken)
            while True:
                name = ask(f"Name for {session.ref[:20]} [{suggested}]: ").strip() or suggested
                if NAME_RE.match(name) and name not in taken:
                    break
                say("That name is taken or not lower-case letters, digits and hyphens.")
            member = Member(name, role, platform, session=session.ref, wake=parse_wake(default_wake(platform, session)))
            join(project, agreement, member)
            taken.add(name)
            say(f"Joined {name} ({role}, {platform}, wake {member.wake}).")
            assignments.setdefault(role, []).append(name)
    summary = "; ".join(f"{role}: {', '.join(names)}" for role, names in assignments.items())
    if ask(f"Link {thread} with {summary}? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        raise UsageError("nothing linked")
    return link(project, agreement, thread, assignments)
