from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from crosspatch import cli  # noqa: E402

CLEARED = ("CROSSPATCH_PROJECT", "CROSSPATCH_AS", "CROSSPATCH_WAIT", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")


class BridgeCase(unittest.TestCase):
    """A fresh project with its own state folder, and the platform homes pointed at empty folders."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.root = self.tmp / "project"
        self.root.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in CLEARED}
        env.update(
            XDG_STATE_HOME=str(self.tmp / "state"),
            CODEX_HOME=str(self.tmp / "codex-home"),
            CLAUDE_CONFIG_DIR=str(self.tmp / "claude-home"),
            CROSSPATCH_TMUX=str(self.tmp / "no-tmux"),
        )
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()
        self._cwd = os.getcwd()
        os.chdir(self.root)
        self.cli_ok("init", "--human", "sam")

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        self._env.stop()
        self._tmp.cleanup()

    def cli(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        fake_in = io.StringIO(stdin if stdin is not None else "")
        fake_in.isatty = lambda: stdin is None  # type: ignore[method-assign]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", fake_in):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def cli_ok(self, *args: str, stdin: str | None = None) -> str:
        code, out, err = self.cli(*args, stdin=stdin)
        self.assertEqual(code, 0, f"crosspatch {' '.join(args)} failed: {err or out}")
        return out

    def subprocess_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        return env

    def spawn(self, *args: str, cwd: Path | None = None) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-m", "crosspatch", *args],
            cwd=cwd or self.root,
            env=self.subprocess_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def pair(self, thread: str = "art", builders: tuple[str, ...] = ("maker",)) -> None:
        self.cli_ok("join", "lead", "--role", "lead", "--platform", "claude-code")
        for name in builders:
            self.cli_ok("join", name, "--role", "builder", "--platform", "codex")
        args = ["link", thread, "--lead", "lead"]
        for name in builders:
            args += ["--builder", name]
        self.cli_ok(*args)

    def write_script(self, name: str, body: str) -> Path:
        path = self.tmp / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path
