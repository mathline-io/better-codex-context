"""`bcc doctor`: a read-only report on everything that decides whether long sessions stay healthy."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tomllib

from . import native_budget
from .instructions import DEFAULT_PROJECT_DOC_BYTES, instruction_files
from .hookrun import recent
from .upgrade import PLUGIN

MINIMUM_CODEX = (0, 164, 0)
TESTED_CODEX = ("0.164.0-alpha.11.1", "0.166.0", "0.167.0")


def sidecar_report(workspace) -> dict:
    """Sidecar compression across the sessions of this workspace (read-only)."""
    import json
    import sqlite3
    from .settings import load
    from .sidecar import codex_path
    from .store import state_directory
    config = load()
    found = codex_path(config)
    result = {"mode": config["sidecar"], "model": config["sidecar_model"], "effort": config["sidecar_effort"], "codex": found,
              "evidence": {"mode": config["evidence"], "model": config["evidence_model"], "effort": config["evidence_effort"],
                           "runs": 0}}
    database = state_directory(Path(workspace).resolve()) / "state.sqlite3"
    if not database.is_file():
        return {**result, "runs": 0}
    try:
        db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        rows = db.execute("SELECT status,usage,error FROM sidecar_runs ORDER BY id DESC LIMIT 200").fetchall()
        digests = db.execute("SELECT input_tokens,tokens FROM shadow_digests ORDER BY id DESC LIMIT 1").fetchone()
        try:
            evidence_rows = db.execute("SELECT status,usage,error,summaries FROM evidence_runs ORDER BY id DESC LIMIT 200").fetchall()
        except sqlite3.Error:
            evidence_rows = []  # state written before evidence summaries existed
        db.close()
    except sqlite3.Error:
        return {**result, "runs": None}
    usage = [json.loads(r[1] or "{}") for r in rows]
    result.update(runs=len(rows), done=sum(r[0] == "done" for r in rows), failed=sum(r[0] == "failed" for r in rows),
                  input_tokens=sum(u.get("input_tokens", 0) for u in usage), cached_input_tokens=sum(u.get("cached_input_tokens", 0) for u in usage),
                  output_tokens=sum(u.get("output_tokens", 0) for u in usage),
                  last_error=next((r[2] for r in rows if r[0] == "failed"), None),
                  last_result={"input_tokens": digests[0], "tokens": digests[1]} if digests else None)
    evidence_usage = [json.loads(r[1] or "{}") for r in evidence_rows]
    result["evidence"] = {"mode": config["evidence"], "model": config["evidence_model"], "effort": config["evidence_effort"],
                          "runs": len(evidence_rows), "done": sum(r[0] == "done" for r in evidence_rows),
                          "failed": sum(r[0] == "failed" for r in evidence_rows), "summaries": sum(r[3] or 0 for r in evidence_rows),
                          "input_tokens": sum(u.get("input_tokens", 0) for u in evidence_usage),
                          "output_tokens": sum(u.get("output_tokens", 0) for u in evidence_usage),
                          "last_error": next((r[2] for r in evidence_rows if r[0] == "failed"), None)}
    if found is None and config["sidecar"] == "auto":
        result["advice"] = f"sidecar compression is on but {config['sidecar_codex']} was not found"
    return result


def untested_newer(version: str | None) -> bool:
    """True for a Codex version newer than every version this plugin was verified on."""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version or "")
    if not match or version in TESTED_CODEX:
        return False
    newest = max(tuple(int(p) for p in re.match(r"(\d+)\.(\d+)\.(\d+)", v).groups()) for v in TESTED_CODEX)
    return tuple(int(p) for p in match.groups()) > newest


def codex_report(codex: str = "codex") -> dict:
    """Version of the Codex executable; runs only `codex --version`."""
    try:
        output = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"executable": codex, "error": f"{type(exc).__name__}: {exc}"}
    match = re.search(r"(\d+)\.(\d+)\.(\d+)\S*", output)
    if not match:
        return {"executable": codex, "version": output.strip() or None, "supported": None}
    version = tuple(int(part) for part in match.groups())
    return {"executable": codex, "version": match.group(0), "supported": version >= MINIMUM_CODEX,
            "tested": match.group(0) in TESTED_CODEX, "minimum": ".".join(map(str, MINIMUM_CODEX))}


def doctor(workspace, codex: str = "codex") -> dict:
    from .settings import report as settings_report
    report = {"codex": codex_report(codex), "plugin_settings": settings_report(), "workspace": workspace_report(workspace),
              "sidecar": sidecar_report(workspace)}
    if report["codex"].get("supported") is False:
        report["workspace"]["advice"].insert(0, f"Codex {report['codex']['version']} is older than "
                                             f"{report['codex']['minimum']}; plugin hooks need 0.164 or later.")
    return report


HEADING = re.compile(rb"^#{1,3} [^\r\n]+", re.M)  # without the \r of CRLF files


def _codex_config():
    """Read only the handful of context-related keys; nothing else leaves this function."""
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {}, False
    features = data.get("features") if isinstance(data.get("features"), dict) else {}
    keys = ("model_auto_compact_token_limit", "model_context_window", "project_doc_max_bytes", "tool_output_token_limit")
    selected = {key: data[key] for key in keys if isinstance(data.get(key), int)}
    selected["features"] = {key: features.get(key) for key in ("token_budget", "context_management", "memories") if key in features}
    selected["mcp_servers"] = sorted(data.get("mcp_servers", {})) if isinstance(data.get("mcp_servers"), dict) else []
    plugin = (data.get("plugins") or {}).get(PLUGIN) if isinstance(data.get("plugins"), dict) else None
    selected["plugin_enabled"] = bool(isinstance(plugin, dict) and plugin.get("enabled", True))
    return selected, True


def workspace_report(workspace) -> dict:
    """Read-only checks for the context problems measured in long native sessions."""
    root = Path(workspace).resolve()
    config, found = _codex_config()
    limit = config.get("project_doc_max_bytes", DEFAULT_PROJECT_DOC_BYTES)
    advice, files, used = [], [], 0
    for path in instruction_files(root):
        data = path.read_bytes()
        kept = max(0, min(len(data), limit - used))
        cut = [h.decode("utf-8", "replace")[:80] for h in HEADING.findall(data[kept:])] if kept < len(data) else []
        used += kept
        files.append({"path": str(path), "bytes": len(data), "injected_bytes": kept, "headings_never_seen": cut[:40],
                      "headings_never_seen_count": len(cut)})
        if kept < len(data):
            advice.append(f"{path.name} is {len(data)} bytes but Codex loads only {kept} ({len(cut)} headings cut). Better Codex "
                          "Context restores the rules from the cut part and an index of its sections in every fresh window "
                          "(instructions_budget). For best results keep the hard rules near the top, or raise "
                          "project_doc_max_bytes in ~/.codex/config.toml.")
    if found and not config.get("plugin_enabled"):
        advice.append(f"{PLUGIN} is not installed or is disabled; run `python3 scripts/launch.py install` from the checkout.")
    state = root / ".better-codex-context"
    ignored = None
    if state.is_dir():
        try:
            code = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", ".better-codex-context/state.sqlite3"],
                                  capture_output=True, timeout=10).returncode
            ignored = {0: True, 1: False}.get(code)  # anything else: not a usable Git repository
        except (OSError, subprocess.SubprocessError):
            ignored = None
        if ignored is False and (root / ".git").exists():
            advice.append(".better-codex-context/ holds archived command output and is not ignored by Git; "
                          "the plugin now writes a nested .gitignore on next open.")
    if len(config.get("mcp_servers", [])) > 3:
        advice.append(f"{len(config['mcp_servers'])} MCP servers are configured globally; every tool definition is resent "
                      "with each request. Disable the ones this project does not use.")
    managed = config.get("features", {}).get("context_management")
    if managed is True or (isinstance(managed, dict) and managed.get("enabled") is True):
        advice.append("features.context_management is on. Better Codex Context does not need it (new_context comes from "
                      "token_budget); it is an under-development Codex feature that may start managing the window on its "
                      "own in a later version. Set `context_management = false` under [features] unless you need it.")
    reset_status = native_budget.status()
    if found and not reset_status["ready"]:
        advice.append("Entry-independent context resets are not fully configured (" + ", ".join(reset_status["pending_settings"])
                      + "). Run `bcc activate --apply` (install does this) so sessions reset without a model summary "
                      "and get the task card restored.")
    hooks = recent()
    if hooks["errors_last_7_days"]:
        last = next((r for r in reversed(hooks["recent"]) if r["kind"] == "error"), {})
        advice.append(f"{hooks['errors_last_7_days']} hook failure(s) in the last 7 days, latest {last.get('time')}: "
                      f"{last.get('error_type')}: {last.get('error')} at {last.get('location')}. Full tracebacks: {hooks['log']}")
    return {"instruction_files": files, "project_doc_max_bytes": limit, "codex_config_found": found,
            "entry_independent_reset": reset_status, "hook_health": hooks,
            "context_settings": config, "state_directory_ignored_by_git": ignored, "advice": advice, "read_only": True}
