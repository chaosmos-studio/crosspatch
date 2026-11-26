"""A small read-only web page for the human: threads, members and messages with images inline.

It binds to 127.0.0.1 only, answers only requests whose Host is that address, and serves nothing outside a
random path printed at start, so other local users and web pages cannot browse it.
"""

from __future__ import annotations

import html
import mimetypes
import re
import secrets
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

from crosspatch import messages
from crosspatch.agreement import Agreement, load_agreement
from crosspatch.errors import CrossError
from crosspatch.project import Project
from crosspatch.registry import Thread, list_threads, load_members, load_thread

HOST = "127.0.0.1"
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml", "image/avif"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

STYLE = """
:root { --bg:#f7f6f2; --card:#ffffff; --ink:#1d1d1b; --muted:#6b6a64; --line:#e3e1d9; --accent:#2f5d8a;
  --plan:#5b6abf; --review:#2f7d5b; --question:#a8641a; --report:#5f6b73; --gate:#9b2f6f; --blocked:#b3261e; --other:#4a4a46; }
@media (prefers-color-scheme: dark) { :root { --bg:#141413; --card:#1f1f1d; --ink:#ecebe6; --muted:#a3a29b; --line:#33322f;
  --accent:#8fb6e0; --plan:#9aa5f0; --review:#7fcca5; --question:#e5a560; --report:#a5b0b8; --gate:#e58fc2; --blocked:#f08a83; --other:#c9c8c2; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:860px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:28px 0 8px; }
a { color:var(--accent); } .muted { color:var(--muted); font-size:13px; }
table { width:100%; border-collapse:collapse; } td, th { text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); vertical-align:top; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin:14px 0; overflow-wrap:anywhere; }
.head { display:flex; flex-wrap:wrap; gap:8px; align-items:baseline; margin-bottom:6px; }
.kind { font-weight:700; font-size:12px; letter-spacing:.04em; padding:2px 8px; border-radius:99px; color:var(--card); background:var(--other); }
.k-PLAN{background:var(--plan)} .k-REVIEW{background:var(--review)} .k-QUESTION{background:var(--question)}
.k-REPORT{background:var(--report)} .k-GATE{background:var(--gate)} .k-BLOCKED{background:var(--blocked)}
pre { background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:10px; overflow-x:auto; font-size:13px; }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:13px; }
img.att { display:block; max-width:100%; height:auto; border:1px solid var(--line); border-radius:6px; margin:10px 0 4px; }
.pending { color:var(--blocked); font-weight:600; }
"""


def render_markdown(text: str) -> str:
    """A deliberately small markdown subset. Everything is escaped first, so a message cannot inject HTML."""
    out: list[str] = []
    in_code = False
    in_list: str | None = None
    paragraph: list[str] = []

    def inline(raw: str) -> str:
        escaped = html.escape(raw)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        return re.sub(
            r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
            lambda m: f'<a href="{m.group(2)}" rel="noopener noreferrer">{m.group(1)}</a>',
            escaped,
        )

    def flush_paragraph() -> None:
        if paragraph:
            out.append("<p>" + "<br>".join(inline(p) for p in paragraph) + "</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    for line in text.splitlines():
        if line.startswith("```"):
            flush_paragraph()
            close_list()
            out.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(line) + "\n")
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        number = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if heading:
            flush_paragraph()
            close_list()
            level = min(6, len(heading.group(1)) + 2)
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        elif bullet or number:
            flush_paragraph()
            tag = "ul" if bullet else "ol"
            if in_list != tag:
                close_list()
                out.append(f"<{tag}>")
                in_list = tag
            out.append(f"<li>{inline((bullet or number).group(1))}</li>")
        elif not line.strip():
            flush_paragraph()
            close_list()
        else:
            close_list()
            paragraph.append(line)
    flush_paragraph()
    close_list()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def _page(title: str, body: str, refresh: bool = False) -> bytes:
    meta = '<meta http-equiv="refresh" content="15">' if refresh else ""
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>"
    ).encode()


class Site:
    def __init__(self, projects: list[Project], token: str | None = None) -> None:
        self.projects = projects
        self.token = token or secrets.token_urlsafe(18)

    def base(self) -> str:
        return f"/{self.token}"

    def _thread_summary(self, project: Project, agreement: Agreement, thread: Thread) -> tuple[int, str, list[str]]:
        listed = messages.list_messages(project, thread.name)
        last = listed[-1].created.replace("T", " ")[:16] if listed else "no messages yet"
        waiting = []
        for member in thread.members:
            count = len(messages.unanswered(listed, thread, member, agreement))
            if count:
                waiting.append(f"{member} owes {count}")
        return len(listed), last, waiting

    def index(self) -> bytes:
        parts = ["<h1>crosspatch</h1><p class='muted'>Threads on this machine. Read only.</p>"]
        for number, project in enumerate(self.projects):
            parts.append(f"<h2>{html.escape(project.root.name)} <span class='muted'>{html.escape(str(project.root))}</span></h2>")
            try:
                agreement = load_agreement(project)
            except CrossError as exc:
                parts.append(f"<p class='muted'>{html.escape(str(exc))}</p>")
                continue
            threads = list_threads(project)
            if not threads:
                parts.append("<p class='muted'>No threads yet.</p>")
                continue
            parts.append("<table><tr><th>Thread</th><th>Members</th><th>Messages</th><th>Waiting</th></tr>")
            for thread in threads:
                count, last, waiting = self._thread_summary(project, agreement, thread)
                who = ", ".join(f"{html.escape(n)} ({html.escape(r)})" for r, ns in thread.roles.items() for n in ns)
                link = f"{self.base()}/p/{number}/{quote(thread.name)}"
                status = "" if thread.status == "open" else " <span class='muted'>closed</span>"
                parts.append(
                    f"<tr><td><a href='{link}'>{html.escape(thread.name)}</a>{status}</td><td>{who}</td>"
                    f"<td>{count}<br><span class='muted'>{html.escape(last)}</span></td>"
                    f"<td class='pending'>{html.escape(', '.join(waiting))}</td></tr>"
                )
            parts.append("</table>")
        return _page("crosspatch", "".join(parts))

    def thread(self, number: int, name: str) -> bytes:
        project = self.projects[number]
        thread = load_thread(project, name)
        members = load_members(project)
        roles = ", ".join(
            f"{html.escape(n)} ({html.escape(r)}{', ' + html.escape(members[n].platform) if n in members else ''})"
            for r, ns in thread.roles.items()
            for n in ns
        )
        parts = [
            f"<p><a href='{self.base()}/'>All threads</a></p><h1>{html.escape(thread.name)}</h1>",
            f"<p class='muted'>{html.escape(str(project.root))}<br>{roles}</p>",
        ]
        for message in messages.list_messages(project, name):
            created = html.escape(message.created.replace("T", " ")[:16])
            reply = f" · reply to {html.escape(message.reply_to)}" if message.reply_to else ""
            parts.append(
                f"<article class='card' id='m{message.id}'><div class='head'>"
                f"<span class='kind k-{html.escape(message.kind)}'>{html.escape(message.kind)}</span>"
                f"<strong>{html.escape(message.id)}</strong> <span>{html.escape(message.sender)} to {html.escape(message.to)}</span>"
                f"<span class='muted'>{created}{reply}</span></div>{render_markdown(message.body)}"
            )
            for index, item in enumerate(message.attachments):
                url = f"{self.base()}/p/{number}/{quote(name)}/{message.id}/{index}"
                kind = mimetypes.guess_type(item.path)[0] or ""
                status = html.escape(messages.attachment_status(project, item))
                label = f"<span class='muted'>{html.escape(item.path)} · sha256 {status}</span>"
                if kind in IMAGE_TYPES:
                    parts.append(f"<a href='{url}'><img class='att' src='{url}' alt='{html.escape(item.path)}'></a>{label}")
                else:
                    parts.append(f"<p><a href='{url}'>{html.escape(item.path)}</a> {label}</p>")
            parts.append("</article>")
        return _page(f"{thread.name} · crosspatch", "".join(parts), refresh=True)

    def attachment(self, number: int, name: str, message_id: str, index: int) -> tuple[bytes, str]:
        project = self.projects[number]
        message = messages.get_message(project, name, message_id)
        item = message.attachments[index]
        path = (project.root / item.path).resolve()
        if not project.contains(path) or not path.is_file():
            raise FileNotFoundError(item.path)
        kind = mimetypes.guess_type(item.path)[0] or "application/octet-stream"
        if kind not in IMAGE_TYPES:
            kind = "text/plain; charset=utf-8" if kind.startswith("text/") else "application/octet-stream"
        return path.read_bytes(), kind


def make_handler(site: Site, port_ref: Callable[[], int]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "crosspatch"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base class names it so
            return

        def _send(self, status: int, body: bytes, kind: str = "text/html; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self) -> None:  # noqa: N802 - the base class names it so
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802 - the base class names it so
            allowed_hosts = {f"{HOST}:{port_ref()}", f"localhost:{port_ref()}"}
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                return
            path = self.path.split("?", 1)[0]
            prefix = site.base()
            if not (path == prefix or path.startswith(prefix + "/")) or not secrets.compare_digest(path[: len(prefix)], prefix):
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            parts = [unquote(p) for p in path[len(prefix) :].split("/") if p]
            try:
                if not parts:
                    self._send(HTTPStatus.OK, site.index())
                elif len(parts) == 3 and parts[0] == "p":
                    self._send(HTTPStatus.OK, site.thread(int(parts[1]), parts[2]))
                elif len(parts) == 5 and parts[0] == "p":
                    body, kind = site.attachment(int(parts[1]), parts[2], parts[3], int(parts[4]))
                    self._send(HTTPStatus.OK, body, kind)
                else:
                    self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            except (CrossError, ValueError, IndexError, FileNotFoundError):
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    return Handler


def serve(projects: list[Project], port: int = 0, token: str | None = None) -> tuple[ThreadingHTTPServer, str]:
    site = Site(projects, token)
    holder: dict[str, int] = {}
    server = ThreadingHTTPServer((HOST, port), make_handler(site, lambda: holder["port"]))
    holder["port"] = server.server_address[1]
    return server, f"http://{HOST}:{holder['port']}{site.base()}/"
