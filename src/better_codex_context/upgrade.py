"""`bcc install` / `bcc upgrade`: one step from a checkout to a working plugin, without breaking
sessions that are already running.

`codex plugin add` replaces the plugin cache directory. A running app-server keeps the old plugin
root and hook trust in memory, so every hook of an open session then fails ("No such file or
directory (os error 2)" or "plugin contents changed; hook approval required") until that
app-server reloads its user config. Verified on Codex 0.167: `config/batchWrite` with
`reloadUserConfig: true` sent to the running app-server restores hooks for the same thread.
The managed daemon behind the TUI is reachable on its control socket; app-servers owned by an
IDE over stdio are not, and need a window reload.
"""
from __future__ import annotations

import filecmp
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

from .packaging import MARKETPLACE, NAME, OLD_MARKETPLACES, PLUGIN, hook_command, native_package
from .util import ContextError, atomic_write


def _same_tree(left: Path, right: Path) -> bool:
    files = lambda root: sorted(p.relative_to(root) for p in root.rglob("*")
                                if p.is_file() and "__pycache__" not in p.parts)  # hooks run from dist and compile there
    return files(left) == files(right) and all(filecmp.cmp(left / f, right / f, shallow=False) for f in files(left))


def _package(repo: Path, dest: Path) -> bool:
    """Build dest, or reuse it when an identical build already exists. Returns True when built."""
    if not dest.exists():
        native_package(dest, repo)
        return True
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".verify-") as tmp:
        fresh = Path(tmp) / dest.name
        native_package(fresh, repo, runs_from=dest)  # commands embed the path they run from
        if not _same_tree(fresh, dest):
            raise ContextError(f"{dest} exists with different content (an earlier build of the same version). If no running "
                               "Codex session uses it, delete that folder and install again; otherwise bump the plugin version")
    return False


def control_socket(home: Path) -> Path | None:
    """The managed app-server daemon's Unix socket. Windows has no such socket (and no getuid): there a
    running Codex picks up the new install when it restarts."""
    if not hasattr(os, "getuid"):
        return None
    link = home / "app-server-control" / "app-server-control.sock"
    try:
        target = Path(os.path.realpath(link))
        info = target.lstat()
    except OSError:
        return None
    return target if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid() else None


def reload_running(home: Path) -> dict:
    """Ask the managed app-server daemon to reload user config (plugins, hooks and their trust)."""
    target = control_socket(home)
    if target is None:
        return {"daemon": "not running"}
    from .rpc import UnixRPC
    try:
        rpc = UnixRPC(str(target))
    except (OSError, ContextError, EOFError) as exc:
        return {"daemon": f"unreachable: {type(exc).__name__}: {exc}"}
    try:
        rpc.call("config/batchWrite", {"edits": [{"keyPath": "hooks.state", "value": {}, "mergeStrategy": "upsert"}],
                                       "reloadUserConfig": True})
        return {"daemon": "reloaded", "daemon_version": rpc.version}
    finally:
        rpc.close()


def _trust(codex: str, cwd: str, expected: str, allow_missing: bool = False) -> list[str] | None:
    """Trust the plugin's four hooks as this Codex lists them, after checking they run exactly the
    packaged command; returns their trust states. None when this Codex lists none and allow_missing."""
    from . import native_budget
    server = native_budget._AppServer(codex, cwd)
    try:
        hooks = [h for p in server.call("hooks/list", {"cwds": [cwd]})["data"] for h in p["hooks"]
                 if h["key"].startswith(PLUGIN + ":")]
        if not hooks and allow_missing:
            return None
        unexpected = [h["command"] for h in hooks if h["command"] != expected]
        if len(hooks) != 4 or unexpected:
            raise ContextError(f"refusing to trust hooks: expected 4 plugin hooks running {expected!r}, got "
                               f"{len(hooks)} with unexpected commands {unexpected}")
        server.call("config/batchWrite", {"edits": [{"keyPath": "hooks.state", "value": {h["key"]: {"trusted_hash": h["currentHash"]}
                                                     for h in hooks}, "mergeStrategy": "upsert"}], "reloadUserConfig": False})
        return sorted({h.get("trustStatus") for p in server.call("hooks/list", {"cwds": [cwd]})["data"] for h in p["hooks"]
                       if h["key"].startswith(PLUGIN + ":")})
    finally:
        server.close()


def codex_targets() -> list[str]:
    """Every Codex on this machine: the CLI on PATH, then the newest Codex bundled with each VS Code
    install. Duplicates (a PATH entry that is a bundled binary) are listed once."""
    from .util import bundled_codexes
    targets, seen = [], set()
    for path in [shutil.which("codex"), *bundled_codexes()]:
        if path and os.path.realpath(path) not in seen:
            seen.add(os.path.realpath(path))
            targets.append(path)
    return targets


def install(codexes: list[str], repo: Path | None = None, dist_root: Path | None = None, cwd: str | None = None) -> dict:
    """Install for every Codex in `codexes` (by default all of them on this machine, codex_targets).
    They share ~/.codex: the plugin, its hook trust and the settings. The first one does the full
    install; each further one must list the plugin's four hooks (it adds the plugin itself when it
    does not), trusts them, and writes the settings through its own config API, which checks them
    against its own version."""
    if not codexes:
        raise ContextError("no Codex found: install the Codex CLI or the Codex extension for VS Code, or pass --codex")
    result = upgrade(codexes[0], repo, dist_root, cwd)
    clients = [_client(codexes[0], result["hooks_trusted"], result["settings"]["ready"])]
    for codex in codexes[1:]:
        try:  # one Codex that cannot be set up (wrong binary, too old) never undoes the others
            clients.append(attach(codex, Path(result["dist"]), cwd))
        except (ContextError, OSError, subprocess.SubprocessError) as exc:
            clients.append({**_client(codex, [], False), "error": f"{type(exc).__name__}: {exc}"[:600]})
    result["clients"] = clients
    return result


def attach(codex: str, dest: Path, cwd: str | None = None) -> dict:
    """Make one more Codex use an install that another Codex on this machine has just done."""
    from . import native_budget
    cwd = cwd or str(Path.home())
    trust = _trust(codex, cwd, hook_command(dest), allow_missing=True)
    if trust is None:
        added = subprocess.run([codex, "plugin", "add", PLUGIN], cwd=cwd, capture_output=True, text=True, timeout=120)
        if added.returncode:
            raise ContextError(f"{codex} plugin add failed: {(added.stderr or added.stdout).strip()[-600:]}")
        trust = _trust(codex, cwd, hook_command(dest))
    return _client(codex, trust, native_budget.apply(codex, cwd)["ready"])


def _client(codex: str, trust: list[str], ready: bool) -> dict:
    try:
        version = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        version = "unknown"
    kind = "VS Code extension" if "/openai.chatgpt-" in codex.replace(os.sep, "/") else "CLI"
    return {"client": kind, "codex": codex, "version": version, "hooks_trusted": trust, "settings_ready": ready}


def _host_marketplace(source, dist_root: Path) -> Path | None:
    """An existing Mathline Research marketplace that is not one of BCC's own packages (it holds other
    Mathline plugins too); None when there is none and BCC registers its own."""
    if not isinstance(source, str) or not source:
        return None
    path = Path(source).expanduser().resolve()
    if path.is_relative_to(dist_root) or not (path / ".agents" / "plugins" / "marketplace.json").is_file():
        return None
    try:  # BCC's own package built elsewhere (another checkout or --dist-root): replace it, never join it
        if json.loads((path / ".codex-plugin" / "plugin.json").read_text()).get("name") == NAME:
            return None
    except (OSError, ValueError):
        pass
    return path


def _join_marketplace(host: Path, dest: Path) -> None:
    """Put this package into a shared marketplace: refresh the folder its BCC entry names (adding the entry
    when missing) and leave every other plugin and the marketplace registration untouched. The package's
    commands keep pointing at `dest` in BCC's own dist folder."""
    index = host / ".agents" / "plugins" / "marketplace.json"
    manifest = json.loads(index.read_text())
    entry = next((p for p in manifest.get("plugins", []) if p.get("name") == NAME), None)
    if entry is None:
        entry = {"name": NAME, "source": {"source": "local", "path": f"./plugins/{NAME}"},
                 "policy": {"installation": "AVAILABLE", "authentication": "ON_USE"}, "category": "Productivity"}
        manifest.setdefault("plugins", []).append(entry)
        atomic_write(index, (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode())
    target = (host / entry["source"]["path"]).resolve()
    if not target.is_relative_to(host) or target == host:  # the root holds the manifest: replacing it would delete it
        raise ContextError(f"the {NAME} entry of {index} points outside the marketplace or at its root")
    pending = target.with_name(f".{target.name}.pending-{os.getpid()}")
    shutil.copytree(dest, pending, ignore=shutil.ignore_patterns(".agents"))
    if target.exists():
        shutil.rmtree(target)
    os.replace(pending, target)


def upgrade(codex: str = "codex", repo: Path | None = None, dist_root: Path | None = None, cwd: str | None = None) -> dict:
    from . import native_budget
    repo = (repo or Path(__file__).resolve().parents[2]).resolve()
    version = json.loads((repo / ".codex-plugin" / "plugin.json").read_text())["version"]
    dist_root = (dist_root or repo / "dist").resolve()
    dest = dist_root / f"better-codex-context-{version}"
    built = _package(repo, dest)
    cwd = cwd or str(Path.home())
    home = native_budget.codex_home()
    server = native_budget._AppServer(codex, cwd)
    try:
        before = server.call("config/read", {"cwd": cwd, "includeLayers": False})["config"]
        retired = [old for old in OLD_MARKETPLACES if native_budget.lookup(before, f"marketplaces.{old}") is not None]
        previous = native_budget.lookup(before, f"marketplaces.{MARKETPLACE}.source")
        host = _host_marketplace(previous, dist_root)
        if host is None:  # no Mathline Research marketplace yet: BCC's own package is that marketplace
            server.call("config/batchWrite", {"edits": [{"keyPath": f"marketplaces.{MARKETPLACE}",
                                                         "value": {"source_type": "local", "source": str(dest)},
                                                         "mergeStrategy": "replace"}], "reloadUserConfig": False})
    finally:
        server.close()
    if host is not None:  # join the existing Mathline Research marketplace, leaving its other plugins alone
        _join_marketplace(host, dest)
    for old in retired:  # the plugin under an earlier marketplace name would run every hook a second time
        subprocess.run([codex, "plugin", "remove", f"{NAME}@{old}"], cwd=cwd, capture_output=True, text=True, timeout=120)
        subprocess.run([codex, "plugin", "marketplace", "remove", old], cwd=cwd, capture_output=True, text=True, timeout=120)
    added = subprocess.run([codex, "plugin", "add", PLUGIN], cwd=cwd, capture_output=True, text=True, timeout=120)
    if added.returncode:
        raise ContextError(f"codex plugin add failed: {(added.stderr or added.stdout).strip()[-600:]}")
    trust = _trust(codex, cwd, hook_command(dest))  # a fresh app-server process sees the newly installed plugin
    settings = native_budget.apply(codex, cwd)
    running = reload_running(home)
    return {"version": version, "dist": str(dest), "built": built, "previous_marketplace_source": previous,
            "retired_marketplaces": retired, "joined_marketplace": str(host) if host else None,
            "installed": added.stdout.strip().splitlines()[-1:], "hooks_trusted": trust, "settings": settings,
            "running_sessions": running,
            "still_needs_reload": "IDE windows that run their own Codex app-server over stdio (VS Code: Developer: Reload Window)",
            "old_dist_kept": "previous dist directories stay in place; hooks of not-yet-reloaded clients still point there"}
