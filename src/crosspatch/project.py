"""Where a project's bridge lives, and the user-level list of known projects."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from crosspatch import fsutil
from crosspatch.errors import UsageError

BRIDGE_DIR = ".crosspatch"
AGREEMENT_FILE = "bridge.toml"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


def check_name(value: str, what: str) -> str:
    if not NAME_RE.match(value or ""):
        raise UsageError(f"a {what} name is lower-case letters, digits and hyphens, such as 'art-builder' (got {value!r})")
    return value


@dataclass(frozen=True)
class Project:
    root: Path

    @property
    def dir(self) -> Path:
        return self.root / BRIDGE_DIR

    @property
    def agreement_path(self) -> Path:
        return self.dir / AGREEMENT_FILE

    @property
    def members_path(self) -> Path:
        return self.dir / "members.json"

    @property
    def threads_dir(self) -> Path:
        return self.dir / "threads"

    @property
    def state_dir(self) -> Path:
        return self.dir / "state"

    def thread_dir(self, thread: str) -> Path:
        return self.threads_dir / check_name(thread, "thread")

    def relative(self, path: Path) -> str:
        """A path inside the project as the project-relative string stored in messages."""
        return path.resolve().relative_to(self.root).as_posix()

    def contains(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
        except ValueError:
            return False
        return True


def find_project(start: Path | None = None, explicit: str | None = None) -> Project:
    """The project for this command: --project, then CROSSPATCH_PROJECT, then the nearest ancestor with a bridge."""
    chosen = explicit or os.environ.get("CROSSPATCH_PROJECT")
    if chosen:
        root = Path(chosen).expanduser().resolve()
        if not (root / BRIDGE_DIR / AGREEMENT_FILE).is_file():
            raise UsageError(f"{root} has no bridge yet. Run `crosspatch init` there first.")
        return Project(root)
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / BRIDGE_DIR / AGREEMENT_FILE).is_file():
            return Project(candidate)
    raise UsageError("no bridge here or in any parent folder. Run `crosspatch init` in the project's root first.")


def state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "crosspatch"


def known_projects() -> list[Project]:
    """Projects this user has initialised, that still have a bridge."""
    listed = fsutil.read_json(state_home() / "projects.json", {"projects": []})
    found: list[Project] = []
    for item in listed.get("projects", []):
        project = Project(Path(item))
        if project.agreement_path.is_file() and project not in found:
            found.append(project)
    return found


def remember_project(project: Project) -> None:
    index = state_home() / "projects.json"
    with fsutil.locked(state_home() / "projects.lock"):
        listed = fsutil.read_json(index, {"projects": []})
        entries = [p for p in listed.get("projects", []) if p != str(project.root)]
        entries.append(str(project.root))
        fsutil.atomic_write_json(index, {"projects": entries})
