"""Build the installable plugin directory from a checkout.

Codex expands no variables in the MCP and hook commands of a locally installed plugin, and
`codex plugin add` copies the plugin into a cache that the next install deletes. So the package
is a versioned directory whose commands use absolute paths to itself and to the Python that
built it; it stays in place, and hooks of clients that have not reloaded yet keep working.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

from .util import ContextError, atomic_write, command_line, python_command

NAME = "better-codex-context"
MARKETPLACE = "mathline-research"  # shown as "Mathline Research"
OLD_MARKETPLACES = ("mathline-context",)  # earlier names; an install retires them so BCC is never installed twice
PLUGIN = f"{NAME}@{MARKETPLACE}"


def native_package(destination: Path, source: Path | None = None, runs_from: Path | None = None):
    """Build the plugin package in `destination`. Its MCP and hook commands point at `runs_from`
    (default: the destination itself), so a verification build can match the installed one exactly."""
    source = (source or Path(__file__).resolve().parents[2]).resolve()
    destination = destination.absolute()
    runs_from = (runs_from or destination).absolute()
    if destination.exists():
        raise ContextError("package destination must not already exist")
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise ContextError("package destination must not traverse symlinks")
    if any(destination.is_relative_to(source / name) for name in ("src", "scripts", "skills", "hooks", "assets", ".codex-plugin")):
        raise ContextError("package destination must be outside the packaged source directories")
    if not (source / ".codex-plugin/plugin.json").is_file():
        raise ContextError("packaging requires the complete repository checkout")
    # A strict allowlist excludes histories, local state, Git data and credentials.
    files = [source / ".codex-plugin/plugin.json", source / ".mcp.json", source / "scripts/launch.py"]
    for directory in ("src/better_codex_context", "skills", "hooks", "assets"):  # assets: the app icons of the manifest
        files.extend(path for path in (source / directory).rglob("*") if path.is_file()
                     and "__pycache__" not in path.parts and path.suffix != ".pyc")
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ContextError("package must contain only regular repository files")
    destination.mkdir(parents=True, mode=0o700)
    for path in files:
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    mcp_path = destination / ".mcp.json"
    mcp = json.loads(mcp_path.read_text())
    command = python_command(runs_from / "scripts" / "launch.py", "serve")
    mcp["mcpServers"]["better_context"]["command"], mcp["mcpServers"]["better_context"]["args"] = command[0], command[1:]
    atomic_write(mcp_path, (json.dumps(mcp, indent=2) + "\n").encode())
    hooks_path = destination / "hooks/hooks.json"
    hooks = json.loads(hooks_path.read_text())
    for groups in hooks["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                handler["command"] = hook_command(runs_from)
    atomic_write(hooks_path, (json.dumps(hooks, indent=2) + "\n").encode())
    marketplace = {"name": MARKETPLACE, "interface": {"displayName": "Mathline Research"},
                   "plugins": [{"name": NAME, "source": {"source": "local", "path": "./"}}]}
    atomic_write(destination / ".agents/plugins/marketplace.json", (json.dumps(marketplace, indent=2) + "\n").encode())
    return {"marketplace": str(destination), "plugin": PLUGIN, "python": sys.executable}


def hook_command(destination: Path) -> str:
    return command_line(python_command(destination / "scripts" / "launch.py", "hook"))
