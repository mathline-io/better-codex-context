"""Shared fixtures for the unit tests. Everything here is synthetic and lives in temporary directories."""
from __future__ import annotations
import _isolation  # noqa: F401  (must run before the plugin reads any configuration)

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from better_codex_context.hooks import process
from better_codex_context.store import Store

PROJECT = Path(__file__).resolve().parents[1]


class WorkspaceCase(unittest.TestCase):
    """A fresh temporary workspace in self.root, with its own Codex home (self.codex_home), plugin
    settings directory and per-user state directory (hook error log, session-root registry)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "workspace"
        self.codex_home = self.base / "codex-home"
        self.root.mkdir()
        self.codex_home.mkdir()
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home), "XDG_STATE_HOME": str(self.base / "state"),
                                              "XDG_CONFIG_HOME": str(self.base / "config")})
        environment.start()
        self.addCleanup(environment.stop)

    def codex_config(self, text: str):
        (self.codex_home / "config.toml").write_text(text)

    def plugin_config(self, text: str):
        path = self.base / "config" / "better-codex-context" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class StoreCase(WorkspaceCase):
    """A workspace plus an open Store for session `self.session` (agent "main")."""

    session = "s1"

    def setUp(self):
        super().setUp()
        self.store = Store(self.root, self.session)
        self.addCleanup(lambda: self.store.close())


def untimed(text: str) -> str:
    """Card text without the UTC status times ("T1 [done · 2026-11-09 15:20:33 UTC]" -> "T1 [done]"), for tests
    that check the rest of a line."""
    import re
    return re.sub(r" · \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\]", "]", text)


def hook(root, event: str, session: str = "s1", **payload) -> dict:
    """Run one native hook event the way Codex delivers it (session cwd = root)."""
    return process({"hook_event_name": event, "session_id": session, "cwd": str(root), **payload})


def tool_payload(cwd, session: str = "s1", turn_id: str = "t1", call: str = "c1", **extra) -> dict:
    """A PostToolUse payload for a finished shell command; cwd is the command's directory."""
    return {"hook_event_name": "PostToolUse", "session_id": session, "turn_id": turn_id, "cwd": str(cwd),
            "tool_name": "Bash", "tool_use_id": call, "tool_input": {"command": "ls"}, "tool_response": "out", **extra}


def fake_executable(directory: Path, name: str, body: str) -> str:
    """A Python script standing in for an external program such as `codex`."""
    path = Path(directory) / name
    if os.name == "nt":  # Windows runs no shebang scripts: a .cmd wrapper starts the script with this Python
        script = path.with_name(name + ".py")
        script.write_text(body, encoding="utf-8")
        wrapper = path.with_name(name + ".cmd")
        wrapper.write_text(f'@"{sys.executable}" -X utf8 "{script}" %*\r\n')
        return str(wrapper)
    path.write_text("#!" + sys.executable + "\n" + body)
    path.chmod(0o700)
    return str(path)

