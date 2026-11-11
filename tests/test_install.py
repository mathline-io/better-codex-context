"""Installation and configuration: packaging, upgrade, native TokenBudget activation, plugin
settings and `bcc doctor`. External programs (`codex`) are synthetic stand-ins."""
from __future__ import annotations
import _isolation  # noqa: F401

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from _support import PROJECT, WorkspaceCase, fake_executable
from better_codex_context import native_budget, settings
from better_codex_context.packaging import MARKETPLACE, PLUGIN, hook_command, native_package
from better_codex_context.upgrade import upgrade
from better_codex_context.util import ContextError


# A stand-in for `codex app-server` (JSON lines on stdio) and `codex plugin add`. State is kept in a
# JSON file next to the script so that consecutive processes see each other's writes.
FAKE_CODEX = r'''
import json, os, sys
from pathlib import Path
STATE = Path(__file__).with_name("fake-codex-state.json")
PLUGIN = "better-codex-context@mathline-research"

def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {"config": {}, "installed": None, "reloads": []}

def save(state):
    STATE.write_text(json.dumps(state))

def write(config, edit):
    *parents, leaf = edit["keyPath"].split(".")
    for part in parents:
        config = config.setdefault(part, {})
    if edit["mergeStrategy"] == "upsert" and isinstance(config.get(leaf), dict):
        config[leaf].update(edit["value"])
    else:
        config[leaf] = edit["value"]

def hooks(state):
    if not state["installed"]:
        return []
    groups = json.loads((Path(state["installed"]) / "hooks" / "hooks.json").read_text())["hooks"]
    trusted = state["config"].get("hooks", {}).get("state", {})
    listed = []
    for event, entries in groups.items():
        key = PLUGIN + ":" + event
        listed.append({"key": key, "currentHash": "sha-" + event,
                       "command": os.environ.get("FAKE_CODEX_HOOK_COMMAND") or entries[0]["hooks"][0]["command"],
                       "trustStatus": "trusted" if trusted.get(key, {}).get("trusted_hash") == "sha-" + event else "untrusted"})
    return listed

if sys.argv[1:3] == ["plugin", "add"]:
    if os.environ.get("FAKE_CODEX_ADD_FAILS"):
        sys.exit("marketplace not found")
    state = load()
    root = Path(state["config"]["marketplaces"]["mathline-research"]["source"])
    index = json.loads((root / ".agents" / "plugins" / "marketplace.json").read_text())
    entry = next(p for p in index["plugins"] if p["name"] == "better-codex-context")  # like Codex: through the index
    state["installed"] = str((root / entry["source"]["path"]).resolve())
    save(state)
    print("Installed " + sys.argv[3])
elif sys.argv[1:] == ["app-server"]:
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        state, method, params = load(), request["method"], request.get("params", {})
        if method == "config/read":
            result = {"config": state["config"]}
        elif method == "config/batchWrite":
            for edit in params["edits"]:
                write(state["config"], edit)
            state["reloads"].append(params["reloadUserConfig"])
            result = {"status": "ok"}
        elif method == "hooks/list":
            result = {"data": [{"cwd": params["cwds"][0], "hooks": hooks(state)}]}
        else:
            result = {}
        save(state)
        print(json.dumps({"id": request["id"], "result": result}), flush=True)
else:
    sys.exit(2)
'''


def checkout_copy(destination: Path, version: str | None = None) -> Path:
    """The packaged parts of this repository, copied so concurrent edits cannot change them mid-test."""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in ("src/better_codex_context", "skills", "hooks", "assets", ".codex-plugin"):
        shutil.copytree(PROJECT / name, destination / name, ignore=ignore)
    for name in (".mcp.json", "scripts/launch.py"):
        (destination / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT / name, destination / name)
    if version:
        manifest = destination / ".codex-plugin" / "plugin.json"
        manifest.write_text(json.dumps({**json.loads(manifest.read_text()), "version": version}))
    return destination


class PackagingTests(WorkspaceCase):
    def test_package_is_self_contained_and_excludes_private_state(self):
        result = native_package(self.base / "dist" / "package")
        package = Path(result["marketplace"])
        self.assertEqual({"marketplace": str(package), "plugin": PLUGIN, "python": sys.executable}, result)
        self.assertTrue((package / ".codex-plugin" / "plugin.json").is_file())
        self.assertTrue((package / "src" / "better_codex_context" / "hooks.py").is_file())
        interface = json.loads((package / ".codex-plugin" / "plugin.json").read_text())["interface"]
        for icon in (interface["logo"], interface["composerIcon"]):  # the app icons ship with the package
            self.assertTrue((package / icon).is_file(), icon)
        for private in (".git", ".better-codex-context", "tests", "dist", "docs", "README.md"):
            self.assertFalse((package / private).exists(), private)
        self.assertEqual([], [p for p in package.rglob("*") if "__pycache__" in p.parts or p.suffix == ".pyc"])


    def test_packaged_hook_and_server_run_without_the_checkout(self):
        package = Path(native_package(self.base / "package", checkout_copy(self.base / "checkout"))["marketplace"])
        environment = {**os.environ, "PYTHONPATH": ""}
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "turn_id": "t1",
                              "cwd": str(self.root), "prompt": "实现导出按钮"})
        # Through a shell, as Codex runs it (sh here, PowerShell on Windows); UTF-8 like the plugin itself.
        shell = ["powershell", "-NoProfile", "-Command"] if os.name == "nt" else ["sh", "-c"]
        hooked = subprocess.run([*shell, hook_command(package)], input=payload, capture_output=True, encoding="utf-8",
                                env=environment, cwd=self.base, timeout=60)
        self.assertEqual((0, {}), (hooked.returncode, json.loads(hooked.stdout)))
        mcp = json.loads((package / ".mcp.json").read_text())["mcpServers"]["better_context"]
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "context_state", "arguments": {"action": "status", "view": "ledger"}, "_meta": {"threadId": "s1"}}})
        served = subprocess.run([mcp["command"], *mcp["args"]], input=request + "\n", capture_output=True, encoding="utf-8",
                                env=environment, cwd=self.root, timeout=60)
        body = json.loads(json.loads(served.stdout)["result"]["content"][0]["text"])
        self.assertEqual(["实现导出按钮"], [row["text"] for row in body["ledger"]])


class UpgradeTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.checkout = checkout_copy(self.base / "checkout", version="1.2.3-test")
        self.dist = self.base / "dist"
        tools = self.base / "bin"
        tools.mkdir()
        self.codex = fake_executable(tools, "codex", FAKE_CODEX)
        self.state_file = tools / "fake-codex-state.json"

    def install(self):
        return upgrade(self.codex, repo=self.checkout, dist_root=self.dist, cwd=str(self.root))

    def codex_state(self):
        return json.loads(self.state_file.read_text())

    def test_install_packages_installs_trusts_hooks_and_applies_settings(self):
        result = self.install()
        dest = self.dist / "better-codex-context-1.2.3-test"
        self.assertEqual(("1.2.3-test", str(dest), True, None),
                         (result["version"], result["dist"], result["built"], result["previous_marketplace_source"]))
        self.assertEqual(["trusted"], result["hooks_trusted"])
        self.assertTrue(result["settings"]["ready"])
        self.assertEqual({"daemon": "not running"}, result["running_sessions"])
        state = self.codex_state()
        self.assertEqual({"source_type": "local", "source": str(dest)}, state["config"]["marketplaces"][MARKETPLACE])
        self.assertTrue(native_budget.status(state["config"])["ready"])
        self.assertNotIn(True, state["reloads"])  # private app-servers never reload anyone else's configuration

    def test_reinstalling_the_same_version_reuses_an_identical_build_only(self):
        first = self.install()
        again = self.install()
        self.assertEqual((False, first["dist"]), (again["built"], again["previous_marketplace_source"]))
        (Path(first["dist"]) / "scripts" / "launch.py").write_text("# changed after the release\n")
        with self.assertRaisesRegex(ContextError, "delete that folder and install again"):
            self.install()

    def test_a_package_built_in_another_folder_is_replaced_never_overwritten(self):
        self.install()
        first = self.dist / "better-codex-context-1.2.3-test"
        newer = checkout_copy(self.base / "checkout-2", version="1.2.4-test")  # another checkout, another --dist-root
        result = upgrade(self.codex, repo=newer, dist_root=self.base / "releases", cwd=str(self.root))
        dest = self.base / "releases" / "better-codex-context-1.2.4-test"
        self.assertEqual({"source_type": "local", "source": str(dest)}, self.codex_state()["config"]["marketplaces"][MARKETPLACE])
        self.assertEqual(str(first), result["previous_marketplace_source"])
        self.assertTrue((first / ".agents" / "plugins" / "marketplace.json").is_file())  # the earlier package is intact
        self.assertEqual("1.2.3-test", json.loads((first / ".codex-plugin" / "plugin.json").read_text())["version"])

    def test_hooks_with_unexpected_commands_are_never_trusted(self):
        with patch.dict(os.environ, {"FAKE_CODEX_HOOK_COMMAND": "python3 /old/launch.py hook"}):
            with self.assertRaisesRegex(ContextError, "refusing to trust hooks"):
                self.install()
        self.assertNotIn("hooks", self.codex_state()["config"])


class NativeBudgetTests(WorkspaceCase):


    def test_install_sets_the_reset_limit_unless_the_user_deleted_the_marker(self):
        def limit(config, own=False):
            return next((c["desired"] for c in native_budget.plan(config, own) if c["key"] == "model_auto_compact_token_limit"), None)
        self.assertEqual(440000, native_budget.IN_TASK_LIMIT)
        self.assertEqual(440000, limit({"model_auto_compact_token_limit": 300000}))  # managed: every install sets it
        self.assertEqual(440000, limit({}))
        self.assertIsNone(limit({"model_auto_compact_token_limit": 300000}, own=True))  # the user took it over
        self.assertEqual(440000, limit({}, own=True))  # taken over but missing: set again


class SettingsChangeTests(WorkspaceCase):
    """Codex lists and changes the plugin's settings for the user (context_state action=settings)."""


    def test_change_writes_checks_and_keeps_the_users_other_lines(self):
        settings.path().parent.mkdir(parents=True, exist_ok=True)
        settings.path().write_text("# my settings\nidle_reset_tokens = 200000\nloop_guard = 4\n")
        result = settings.change({"idle_reset_tokens": 300000, "change_notes": "ask"})
        self.assertEqual({"idle_reset_tokens": [200000, 300000], "change_notes": ["require", "ask"]}, result["settings_changed"])
        text = settings.path().read_text()
        self.assertIn("# my settings", text)
        self.assertIn("loop_guard = 4", text)
        self.assertNotIn("idle_reset_tokens", text)  # back at the default: the line is dropped
        self.assertIn('change_notes = "ask"', text)
        settings.change({"change_notes": None})  # null restores the default
        self.assertEqual("require", settings.load()["change_notes"])
        for bad in ({"idle_reset_tokens": -5}, {"activity_log": "loud"}, {"no_such_setting": 1}, {}):
            with self.assertRaises(ContextError):
                settings.change(bad)
        self.assertEqual(4, settings.load()["loop_guard"])  # a rejected change writes nothing


if __name__ == "__main__":
    unittest.main()
