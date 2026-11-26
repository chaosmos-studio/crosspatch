"""The crosspatch command line."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import sys
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path

from crosspatch import __version__, discovery, fsutil, hook, instructions, messages, picker, registry, viewer, wake
from crosspatch.agreement import Agreement, load_agreement, write_default
from crosspatch.errors import CrossError, TimedOut, UsageError
from crosspatch.project import BRIDGE_DIR, Project, check_name, find_project, known_projects, remember_project, state_home

DEFAULT_WAIT = 540
POLL = 2.0
PLATFORM_ALIASES = {"claude": "claude-code", "claude-code": "claude-code", "codex": "codex", "tmux": "tmux"}


def out(text: str = "") -> None:
    print(text, flush=True)


def _project(args: argparse.Namespace) -> Project:
    return find_project(explicit=args.project)


def _context(args: argparse.Namespace) -> tuple[Project, Agreement]:
    project = _project(args)
    return project, load_agreement(project)


def _cli_prefix(project: Project) -> str:
    return f"crosspatch -C {shlex.quote(str(project.root))}"


def resolve_self(project: Project, agreement: Agreement, explicit: str | None) -> str:
    """Who is running this command: --as, CROSSPATCH_AS, or the agent session the command runs inside."""
    name = explicit or os.environ.get("CROSSPATCH_AS")
    if name:
        registry.get_member(project, agreement, name)
        return name
    members = registry.load_members(project)
    for variable, prefix in (("CLAUDE_CODE_SESSION_ID", "claude"), ("CODEX_THREAD_ID", "codex")):
        session = os.environ.get(variable)
        if session:
            for member in members.values():
                if member.session == f"{prefix}:{session}":
                    return member.name
    raise UsageError("say who you are with --as <member>. `crosspatch members` lists the members.")


def _body(args: argparse.Namespace) -> str:
    if getattr(args, "message", None):
        return str(args.message)
    if getattr(args, "file", None) and args.file != "-":
        path = Path(args.file)
        if not path.is_file():
            raise UsageError(f"cannot read {args.file}")
        return path.read_text(encoding="utf-8")
    if args.file == "-" or not sys.stdin.isatty():
        return sys.stdin.read()
    raise UsageError("give the message as a file, with -m, or on stdin")


def _print_messages(project: Project, thread: registry.Thread, found: list[messages.Message], reader: str, agreement: Agreement) -> None:
    if reader != agreement.human and any(m.sender != agreement.human for m in found):
        out(messages.AGENT_NOTICE)
        out()
    for message in found:
        out(messages.format_message(project, thread, message))


def _wake_all(project: Project, agreement: Agreement, message: messages.Message, reached: list[str]) -> None:
    for name in reached:
        try:
            member = registry.get_member(project, agreement, name)
        except CrossError:
            continue
        if member.wake.adapter == "poll":
            out(f"  {name}: will see it when it watches or checks pending")
            continue
        notice = wake.Notice(project, message.thread, message.id, message.sender, message.kind, name)
        result = wake.wake_member(project, member, notice)
        state = "woken" if result.ok else "not woken"
        out(f"  {name}: {state} through {member.wake.adapter} ({result.detail})")


# Setup


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.dir or ".").resolve()
    project = Project(root)
    if project.agreement_path.exists():
        out(f"{project.agreement_path} already exists. Nothing changed.")
    else:
        check_name(args.human, "human")
        write_default(project.agreement_path, args.human)
        (project.dir / ".gitignore").write_text("state/\n.tmp-*\n", encoding="utf-8")
        out(f"Created {project.agreement_path}, the working agreement. Edit it in plain words.")
    remember_project(project)
    out("Next: `crosspatch sessions` to see the sessions on this machine, then `crosspatch link` to pair them.")
    return 0


def _scope_filter(project: Project | None, sessions: list[discovery.Session]) -> list[discovery.Session]:
    if project is None:
        return sessions
    root = str(project.root)
    kept = []
    for session in sessions:
        cwd = session.cwd.rstrip("/")
        if cwd and (cwd == root or cwd.startswith(root + "/") or root.startswith(cwd + "/")):
            kept.append(session)
    return kept


def _platforms(raw: list[str] | None) -> list[str]:
    if not raw:
        return ["claude-code", "codex", "tmux"]
    chosen = []
    for item in raw:
        if item not in PLATFORM_ALIASES:
            raise UsageError(f"platform must be one of {', '.join(sorted(PLATFORM_ALIASES))}")
        chosen.append(PLATFORM_ALIASES[item])
    return chosen


def _maybe_project(args: argparse.Namespace) -> Project | None:
    try:
        return _project(args)
    except CrossError:
        return None


def cmd_sessions(args: argparse.Namespace) -> int:
    project = None if args.all else _maybe_project(args)
    found = discovery.discover(_platforms(args.platform), limit=max(args.limit * 5, 50), tmux_socket=args.tmux_socket)
    found = _scope_filter(project, found)[: args.limit]
    joined: dict[str, str] = {}
    if project is not None:
        joined = {m.session: m.name for m in registry.load_members(project).values() if m.session}
    fsutil.atomic_write_json(state_home() / "last-sessions.json", {"sessions": [s.to_json() for s in found]})
    if args.json:
        out(json.dumps([{**s.to_json(), "joined_as": joined.get(s.ref, "")} for s in found], indent=2))
        return 0
    if not found:
        scope = f" in {project.root}" if project else ""
        out(f"No sessions found{scope}. Try --all, or join a session by its id.")
        return 0
    for number, session in enumerate(found, 1):
        state = "live" if session.live else "stored"
        member = f"  joined as {joined[session.ref]}" if session.ref in joined else ""
        out(f"{number:>3}. {session.ref}")
        out(f"     {session.platform}, {state}, {discovery.age(session.updated)} ago, {discovery.short_path(session.cwd) or '?'}{member}")
        out(f"     {session.title[:100]}" + (f"  [{session.detail}]" if session.detail else ""))
    out()
    out("Join one with `crosspatch join <name> --role <role> --session <number or ref>`, or pair them with `crosspatch link`.")
    return 0


def _find_session(ref: str) -> discovery.Session | None:
    if ref.isdigit():
        listed = fsutil.read_json(state_home() / "last-sessions.json", {"sessions": []})["sessions"]
        index = int(ref) - 1
        if not 0 <= index < len(listed):
            raise UsageError(f"there is no session {ref} in the last `crosspatch sessions` listing")
        item = listed[index]
        return discovery.Session(item["platform"], item["id"], item["cwd"], item["title"], item["updated"], item["live"], item["detail"])
    prefix, _, value = ref.partition(":")
    platform = PLATFORM_ALIASES.get(prefix)
    if platform is None or not value:
        raise UsageError("a session is a number from `crosspatch sessions`, or claude:<id>, codex:<id> or tmux:<pane>")
    matches = [s for s in discovery.discover([platform], limit=2000) if s.id == value or s.id.startswith(value)]
    exact = [s for s in matches if s.id == value]
    if exact:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise UsageError(f"{ref} matches {len(matches)} sessions. Give more of the id.")
    if platform == "tmux":
        raise UsageError(f"no tmux pane {value}")
    return discovery.Session(platform, value, "", "", time.time(), False, "not found locally")


def cmd_join(args: argparse.Namespace) -> int:
    project, agreement = _context(args)
    session: discovery.Session | None = None
    if args.me:
        for variable, prefix in (("CLAUDE_CODE_SESSION_ID", "claude"), ("CODEX_THREAD_ID", "codex")):
            if os.environ.get(variable):
                session = _find_session(f"{prefix}:{os.environ[variable]}")
                break
        if session is None:
            raise UsageError("--me needs a session that names itself (Claude Code and Codex do). Use --session instead.")
    elif args.session:
        session = _find_session(args.session)
    platform = PLATFORM_ALIASES.get(args.platform or "", args.platform or "")
    if not platform:
        platform = discovery.guess_platform(session) if session else ("human" if args.role == "human" else "other")
    spec = args.wake or picker.default_wake(platform, session)
    member = registry.Member(
        name=args.name,
        role=args.role,
        platform=platform,
        session=session.ref if session else "",
        wake=wake.parse_wake(spec, args.wake_option),
    )
    registry.join(project, agreement, member, replace=args.replace)
    out(f"Joined {member.name}: {member.role}, {member.platform}, wake {member.wake}.")
    if session and session.detail == "not found locally":
        out(f"  {session.ref} was not found on this machine. Check the id if wakes fail.")
    check = wake.get_adapter(member.wake.adapter).check(project, member)
    out(f"  wake check: {'ok' if check.ok else 'not ready'}, {check.detail}")
    return 0


def cmd_leave(args: argparse.Namespace) -> int:
    project = _project(args)
    registry.leave(project, args.name, force=args.force)
    out(f"{args.name} left the bridge.")
    return 0


def cmd_members(args: argparse.Namespace) -> int:
    project, agreement = _context(args)
    members = registry.load_members(project)
    threads = registry.list_threads(project)
    if args.json:
        out(json.dumps({n: {**m.to_json(), "threads": [t.name for t in threads if n in t.members]} for n, m in members.items()}, indent=2))
        return 0
    if not members:
        out("No members yet. Add one with `crosspatch join`, or run `crosspatch link` to pick sessions.")
        return 0
    for member in members.values():
        linked = [t.name for t in threads if t.status == "open" and member.name in t.members]
        out(f"{member.name}: {member.role}, {member.platform}, wake {member.wake}")
        out(f"  session {member.session or '-'}, threads {', '.join(linked) or '-'}")
    out(f"The human is {agreement.human}.")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    project, agreement = _context(args)
    name = resolve_self(project, agreement, args.as_member)
    member = registry.get_member(project, agreement, name)
    linked = [t.name for t in registry.threads_of(project, name)]
    out(f"{name}: {member.role}, {member.platform}, threads {', '.join(linked) or '-'}")
    return 0


# Pairing


def cmd_link(args: argparse.Namespace) -> int:
    project, agreement = _context(args)
    assignments: dict[str, list[str]] = {}
    for role in ("lead", "builder", "reviewer"):
        if getattr(args, role):
            assignments[role] = list(getattr(args, role))
    if args.human:
        assignments["human"] = [args.human]
    for item in args.add or []:
        role, sep, name = item.partition("=")
        if not sep:
            raise UsageError(f"--add takes role=member (got {item!r})")
        assignments.setdefault(role, []).append(name)
    if not assignments:
        if not sys.stdin.isatty():
            raise UsageError("give the members, such as --lead <member> --builder <member>, or run it in a terminal to pick")
        project_sessions = _scope_filter(project, discovery.discover(["claude-code", "codex", "tmux"], limit=100))[:20]
        thread = picker.run(project, agreement, project_sessions, input, out, args.thread)
    else:
        if not args.thread:
            raise UsageError("give the thread name")
        thread = registry.link(project, agreement, args.thread, assignments)
    out(f"Linked {thread.name}:")
    for role, names in thread.roles.items():
        out(f"  {role}: {', '.join(names)}")
    out("Each agent reads `crosspatch instructions --as <member>`, or the files `crosspatch instructions --write` makes.")
    return 0


def cmd_unlink(args: argparse.Namespace) -> int:
    project = _project(args)
    thread = registry.unlink(project, args.thread, args.member)
    if args.member:
        out(f"{args.member} left {thread.name}.")
    else:
        out(f"{thread.name} is closed. Its messages are kept. Link it again to reopen it.")
    return 0


def _thread_lines(project: Project, agreement: Agreement, thread: registry.Thread) -> list[str]:
    listed = messages.list_messages(project, thread.name)
    roles = "; ".join(f"{role}: {', '.join(names)}" for role, names in thread.roles.items() if names)
    last = f"last {listed[-1].id} {listed[-1].kind} from {listed[-1].sender}, {listed[-1].created[:16].replace('T', ' ')}" if listed else "no messages yet"
    waiting = [f"{m} owes {len(messages.unanswered(listed, thread, m, agreement))}" for m in thread.members]
    waiting = [w for w in waiting if not w.endswith(" owes 0")]
    lines = [f"{thread.name} ({thread.status}): {roles}", f"  {len(listed)} messages, {last}"]
    if waiting:
        lines.append(f"  waiting for replies: {', '.join(waiting)}")
    return lines


def cmd_threads(args: argparse.Namespace) -> int:
    projects = known_projects() if args.all else [_project(args)]
    shown = False
    for project in projects:
        agreement = load_agreement(project)
        threads = registry.list_threads(project)
        if args.all:
            out(f"{discovery.short_path(str(project.root))}")
        for thread in threads:
            if thread.status == "closed" and not args.closed:
                continue
            shown = True
            for line in _thread_lines(project, agreement, thread):
                out(("  " if args.all else "") + line)
    if not shown:
        out("No threads yet. Pair sessions with `crosspatch link`.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    project, agreement = _context(args)
    threads = [registry.load_thread(project, args.thread)] if args.thread else [t for t in registry.list_threads(project) if t.status == "open"]
    if not threads:
        out("No open threads. Pair sessions with `crosspatch link`.")
        return 0
    for thread in threads:
        for line in _thread_lines(project, agreement, thread):
            out(line)
        listed = messages.list_messages(project, thread.name)
        for name in thread.members:
            try:
                member = registry.get_member(project, agreement, name)
            except CrossError:
                out(f"  {name}: no longer a member")
                continue
            check = wake.get_adapter(member.wake.adapter).check(project, member)
            new = len(messages.unseen(project, listed, thread, name))
            owes = len(messages.unanswered(listed, thread, name, agreement))
            out(f"  {name} ({thread.role_of(name)}, {member.platform}): {new} unread, owes {owes} replies")
            out(f"    wake {member.wake}: {'ok' if check.ok else 'not ready'}, {check.detail}")
        out()
    return 0


# Messages


def _post(args: argparse.Namespace, reply_to: str | None = None) -> tuple[Project, Agreement, messages.Message]:
    project, agreement = _context(args)
    sender = resolve_self(project, agreement, args.as_member)
    kind = args.kind
    if kind is None:
        thread = registry.load_thread(project, args.thread)
        role = agreement.role(thread.role_of(sender) or "")
        kind = "REPLY" if reply_to and "REPLY" in agreement.kinds and role.allows("REPLY") else None
        if kind is None:
            raise UsageError("give the message kind with --kind, such as --kind REVIEW")
    message, reached = messages.send(
        project, agreement, args.thread, sender, kind, _body(args),
        to=args.to, reply_to=reply_to or getattr(args, "reply_to", None), attach=args.attach or [],
    )
    out(f"Posted {message.id} ({message.kind}) to {message.to} in {message.thread}.")
    if not args.no_wake:
        _wake_all(project, agreement, message, reached)
    return project, agreement, message


def _await_reply(project: Project, agreement: Agreement, thread_name: str, message_id: str, member: str, timeout: float) -> int:
    found = messages.wait_for_reply(project, thread_name, message_id, member, timeout, POLL)
    if not found:
        out(f"No reply to {message_id} yet. The other side may be checking with {agreement.human}.")
        out(f"Keep waiting with: {_cli_prefix(project)} wait {thread_name} {message_id} --as {member}")
        raise TimedOut()
    thread = registry.load_thread(project, thread_name)
    _print_messages(project, thread, found, member, agreement)
    messages.mark_seen(project, thread_name, member, [m.id for m in found])
    return 0


def _wait_seconds(args: argparse.Namespace) -> float:
    if args.timeout is not None:
        return float(args.timeout)
    return float(os.environ.get("CROSSPATCH_WAIT", DEFAULT_WAIT))


def cmd_send(args: argparse.Namespace) -> int:
    _post(args)
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    project, agreement, message = _post(args)
    out(f"Waiting for the reply to {message.id}.")
    return _await_reply(project, agreement, args.thread, message.id, message.sender, _wait_seconds(args))


def cmd_reply(args: argparse.Namespace) -> int:
    message_id = messages.normalize_id(args.id)
    project, agreement, _ = _post(args, reply_to=message_id)
    sender = resolve_self(project, agreement, args.as_member)
    messages.mark_seen(project, args.thread, sender, [message_id])
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    project, agreement = _context(args)
    member = resolve_self(project, agreement, args.as_member)
    return _await_reply(project, agreement, args.thread, messages.normalize_id(args.id), member, _wait_seconds(args))


def _new_messages(project: Project, member: str, thread_name: str | None) -> list[tuple[registry.Thread, list[messages.Message]]]:
    threads = [registry.load_thread(project, thread_name)] if thread_name else registry.threads_of(project, member)
    found = []
    for thread in threads:
        new = messages.unseen(project, messages.list_messages(project, thread.name), thread, member)
        if new:
            found.append((thread, new))
    return found


def cmd_watch(args: argparse.Namespace) -> int:
    project, agreement = _context(args)
    member = resolve_self(project, agreement, args.as_member)
    if not args.thread and not registry.threads_of(project, member):
        raise UsageError(f"{member} is not linked in any open thread")
    deadline = time.monotonic() + args.timeout if args.timeout else None
    while True:
        found = _new_messages(project, member, args.thread)
        if found:
            for thread, new in found:
                _print_messages(project, thread, new, member, agreement)
                messages.mark_seen(project, thread.name, member, [m.id for m in new])
            out(f"Answer with: {_cli_prefix(project)} reply <thread> <id> --as {member}")
            return 0
        if deadline is not None and time.monotonic() >= deadline:
            out(f"No new messages for {member}.")
            return args.exit_code_on_timeout
        time.sleep(POLL)


def cmd_pending(args: argparse.Namespace) -> int:
    project, agreement = _context(args)
    member = resolve_self(project, agreement, args.as_member)
    threads = [registry.load_thread(project, args.thread)] if args.thread else registry.threads_of(project, member)
    rows = []
    for thread in threads:
        listed = messages.list_messages(project, thread.name)
        owed = {m.id for m in messages.unanswered(listed, thread, member, agreement)}
        new = {m.id for m in messages.unseen(project, listed, thread, member)}
        for message in listed:
            if message.id in owed or message.id in new:
                state = ", ".join(s for s, on in (("unread", message.id in new), ("waits for your reply", message.id in owed)) if on)
                rows.append({"thread": thread.name, "id": message.id, "kind": message.kind, "from": message.sender, "state": state})
    if args.json:
        out(json.dumps(rows, indent=2))
        return 0
    if not rows:
        out(f"Nothing waits for {member}.")
        return 0
    for row in rows:
        out(f"{row['thread']} {row['id']} {row['kind']} from {row['from']}: {row['state']}")
    out(f"Read one with: {_cli_prefix(project)} read <thread> <id> --as {member}")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    project, agreement = _context(args)
    thread = registry.load_thread(project, args.thread)
    message = messages.get_message(project, args.thread, args.id)
    reader = None
    with contextlib.suppress(CrossError):
        reader = resolve_self(project, agreement, args.as_member)
    _print_messages(project, thread, [message], reader or "", agreement)
    if reader:
        messages.mark_seen(project, args.thread, reader, [message.id])
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    project, agreement = _context(args)
    thread = registry.load_thread(project, args.thread)
    listed = messages.list_messages(project, args.thread)
    if args.since:
        listed = [m for m in listed if m.number >= int(messages.normalize_id(args.since))]
    if not listed:
        out(f"No messages in {args.thread} yet.")
        return 0
    for message in listed:
        out(messages.format_message(project, thread, message))
    return 0


# Agents' instructions and hooks


def cmd_instructions(args: argparse.Namespace) -> int:
    project, agreement = _context(args)
    member = registry.get_member(project, agreement, args.as_member) if args.as_member and not args.write else None
    text = instructions.render(project, agreement, registry.list_threads(project), registry.load_members(project), member)
    if args.write:
        writer = {"skill": instructions.write_skill, "agents-md": instructions.write_agents_block, "generic": instructions.write_generic}
        path = writer[args.format](project, text)
        out(f"Wrote {path}.")
        return 0
    rendered = {"skill": instructions.as_skill, "agents-md": instructions.as_agents_block, "generic": lambda t: t}[args.format](text)
    sys.stdout.write(rendered)
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    if args.hook_command == "claude":
        return hook.run_wake_hook(sys.stdin, sys.stderr)
    if args.hook_command == "claude-stop":
        return hook.run_stop_hook(sys.stdin, sys.stdout)
    project = _project(args)
    if args.print:
        out(json.dumps(hook.settings_snippet(args.momentum), indent=2))
        return 0
    path = hook.install(project, args.momentum)
    out(f"Added the crosspatch hooks to {path}.")
    out("Claude Code sessions in this project now wake when a message arrives for their member. Restart open sessions to load the hook.")
    return 0


def cmd_adapters(args: argparse.Namespace) -> int:
    for name, cls in sorted(wake.adapters().items()):
        target = f"{name}:<target>" if cls.takes_target else name
        out(f"{target}")
        out(f"  {cls.summary}")
    return 0


def cmd_wake_test(args: argparse.Namespace) -> int:
    project, agreement = _context(args)
    member = registry.get_member(project, agreement, args.member)
    adapter = wake.get_adapter(member.wake.adapter)
    check = adapter.check(project, member)
    out(f"check: {'ok' if check.ok else 'not ready'}, {check.detail}")
    if args.check_only:
        return 0 if check.ok else 1
    notice = wake.TestNotice(project, "test", "0000", "crosspatch", "TEST", member.name)
    result = wake.wake_member(project, member, notice)
    out(f"wake: {'ok' if result.ok else 'failed'}, {result.detail}")
    return 0 if result.ok else 1


def cmd_view(args: argparse.Namespace) -> int:
    projects = known_projects() if args.all else [_project(args)]
    if not projects:
        raise UsageError("no projects with a bridge yet")
    server, url = viewer.serve(projects, args.port)
    out(f"crosspatch view: {url}")
    out("It listens on 127.0.0.1 only. Press Ctrl+C to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    project, agreement = _context(args)
    problems = 0
    out(f"crosspatch {__version__}, Python {sys.version.split()[0]}, project {project.root}")
    out(f"bridge.toml: {len(agreement.roles)} roles, {len(agreement.kinds)} kinds, human {agreement.human}")
    members = registry.load_members(project)
    for member in members.values():
        check = wake.get_adapter(member.wake.adapter).check(project, member)
        problems += 0 if check.ok else 1
        out(f"{member.name}: wake {member.wake}: {'ok' if check.ok else 'NOT READY'}, {check.detail}")
    for thread in registry.list_threads(project):
        missing = [n for n in thread.members if n not in members and n != agreement.human]
        if missing and thread.status == "open":
            problems += 1
            out(f"{thread.name}: members that left the bridge: {', '.join(missing)}")
        stale = fsutil.stale_temp_files(project.thread_dir(thread.name), older_than=3600)
        if stale:
            out(f"{thread.name}: {len(stale)} unfinished writes older than an hour" + (", removed" if args.clean else ". Remove them with --clean."))
            if args.clean:
                for path in stale:
                    path.unlink(missing_ok=True)
    out("All good." if not problems else f"{problems} problems.")
    return 0 if not problems else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crosspatch",
        description="Bridge coding-agent sessions under a written working agreement.",
    )
    parser.add_argument("--version", action="version", version=f"crosspatch {__version__}")
    parser.add_argument("-C", "--project", help="the project folder (default: the nearest folder with a bridge)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def command(name: str, handler: Callable[[argparse.Namespace], int], help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument("-C", "--project", default=argparse.SUPPRESS, help="the project folder")
        p.set_defaults(handler=handler)
        return p

    def as_option(p: argparse.ArgumentParser) -> None:
        p.add_argument("--as", dest="as_member", help="your member name (default: CROSSPATCH_AS, or the session you run in)")

    p = command("init", cmd_init, f"create {BRIDGE_DIR}/bridge.toml, the working agreement, in a project")
    p.add_argument("dir", nargs="?", help="the project folder (default: here)")
    p.add_argument("--human", default="human", help="the human's member name (default: human)")

    p = command("sessions", cmd_sessions, "list sessions on this machine that a bridge can wake")
    p.add_argument("--platform", action="append", help="claude, codex or tmux (repeatable, default: all)")
    p.add_argument("--all", action="store_true", help="sessions from every folder, not only this project")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--tmux-socket", help="a tmux socket name (-L) or path (-S)")
    p.add_argument("--json", action="store_true")

    p = command("join", cmd_join, "register a session as a named member")
    p.add_argument("name")
    p.add_argument("--role", required=True, help="a role from bridge.toml, such as lead, builder or reviewer")
    p.add_argument("--session", help="a number from `crosspatch sessions`, or claude:<id>, codex:<id>, tmux:<pane>")
    p.add_argument("--me", action="store_true", help="the Claude Code or Codex session running this command")
    p.add_argument("--platform", help="claude-code, codex, human or other (default: from the session)")
    p.add_argument("--wake", help="how to wake it, such as codex-queue:<id>, claude-hook, tmux:<pane>, command:<cmd>, poll")
    p.add_argument("--wake-option", action="append", help="key=value for the wake adapter (repeatable)")
    p.add_argument("--replace", action="store_true", help="replace a member with the same name")

    p = command("leave", cmd_leave, "remove a member")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="also take it out of its threads")

    p = command("members", cmd_members, "list the members and their threads")
    p.add_argument("--json", action="store_true")

    p = command("whoami", cmd_whoami, "which member this session is")
    as_option(p)

    p = command("link", cmd_link, "pair members into a thread (no arguments: pick in the terminal)")
    p.add_argument("thread", nargs="?")
    p.add_argument("--lead", action="append")
    p.add_argument("--builder", action="append")
    p.add_argument("--reviewer", action="append")
    p.add_argument("--human", help="the human's member name, if it is not the one in bridge.toml")
    p.add_argument("--add", action="append", help="role=member for other roles in bridge.toml (repeatable)")

    p = command("unlink", cmd_unlink, "take a member out of a thread, or close the thread")
    p.add_argument("thread")
    p.add_argument("member", nargs="?")

    p = command("threads", cmd_threads, "list threads and who is in them")
    p.add_argument("--all", action="store_true", help="threads in every project on this machine")
    p.add_argument("--closed", action="store_true", help="include closed threads")

    p = command("status", cmd_status, "who is bridged to whom, what waits, and whether each wake works")
    p.add_argument("thread", nargs="?")

    def message_options(p: argparse.ArgumentParser, kind_required: bool) -> None:
        p.add_argument("file", nargs="?", help="the message as a markdown file, or - for stdin")
        p.add_argument("-m", "--message", help="the message text")
        p.add_argument("--kind", required=kind_required, help="the message kind, such as PLAN, REVIEW or QUESTION")
        p.add_argument("--to", help="a member, a role, or all (default: the other side)")
        p.add_argument("--attach", action="append", help="a file inside the project (repeatable)")
        p.add_argument("--no-wake", action="store_true", help="post without waking anyone")
        as_option(p)

    p = command("send", cmd_send, "post a message")
    p.add_argument("thread")
    message_options(p, kind_required=True)
    p.add_argument("--reply-to", help="the id this message answers")

    p = command("ask", cmd_ask, "post a message, then wait for the reply")
    p.add_argument("thread")
    message_options(p, kind_required=True)
    p.add_argument("--reply-to", help="the id this message answers")
    p.add_argument("--timeout", type=float, help=f"seconds to wait (default: CROSSPATCH_WAIT or {DEFAULT_WAIT})")

    p = command("reply", cmd_reply, "answer a message")
    p.add_argument("thread")
    p.add_argument("id")
    message_options(p, kind_required=False)

    p = command("wait", cmd_wait, "keep waiting for the reply to a message")
    p.add_argument("thread")
    p.add_argument("id")
    p.add_argument("--timeout", type=float, help=f"seconds to wait (default: CROSSPATCH_WAIT or {DEFAULT_WAIT})")
    as_option(p)

    p = command("watch", cmd_watch, "wait until a message arrives for you, print it, and exit")
    p.add_argument("thread", nargs="?")
    p.add_argument("--timeout", type=float, default=0, help="give up after this many seconds (default: never)")
    p.add_argument("--exit-code-on-timeout", type=int, default=2)
    as_option(p)

    p = command("pending", cmd_pending, "messages that wait for you")
    p.add_argument("thread", nargs="?")
    p.add_argument("--json", action="store_true")
    as_option(p)

    p = command("read", cmd_read, "print one message")
    p.add_argument("thread")
    p.add_argument("id")
    as_option(p)

    p = command("log", cmd_log, "print a whole thread, oldest first")
    p.add_argument("thread")
    p.add_argument("--since", help="start at this id")

    p = command("instructions", cmd_instructions, "write the instructions agents read, from bridge.toml")
    p.add_argument("--as", dest="as_member", help="make them for one member")
    p.add_argument("--format", choices=instructions.FORMATS, default="generic")
    p.add_argument("--write", action="store_true",
                   help="write them into the project: .claude/skills/crosspatch (skill), AGENTS.md (agents-md), or .crosspatch/INSTRUCTIONS.md")

    p = command("hook", cmd_hook, "Claude Code hooks: install them, or run them")
    p.add_argument("hook_command", choices=("install", "claude", "claude-stop"))
    p.add_argument("--momentum", action="store_true", help="also stop a turn from ending while a reply is owed")
    p.add_argument("--print", action="store_true", help="print the settings instead of writing them")

    command("adapters", cmd_adapters, "list the wake adapters, including installed plugins")

    p = command("wake-test", cmd_wake_test, "send a test notice through a member's wake adapter")
    p.add_argument("member")
    p.add_argument("--check-only", action="store_true")

    p = command("view", cmd_view, "a local web page of the threads, on 127.0.0.1 only")
    p.add_argument("--port", type=int, default=0, help="the port (default: any free one)")
    p.add_argument("--all", action="store_true", help="every project on this machine")
    p.add_argument("--open", action="store_true", help="open it in the browser")

    p = command("doctor", cmd_doctor, "check the bridge and the wake adapters")
    p.add_argument("--clean", action="store_true", help="remove unfinished writes older than an hour")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 64
    try:
        return int(args.handler(args) or 0)
    except TimedOut:
        return TimedOut.code
    except CrossError as exc:
        print(f"crosspatch: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
