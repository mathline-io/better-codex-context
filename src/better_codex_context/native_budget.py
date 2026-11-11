"""Context resets through Codex's native TokenBudget mode.

With `features.token_budget` enabled, Codex's automatic compaction becomes a reset of the same
thread that calls no model, and `SessionStart(source="compact")` fires afterwards; the plugin
restores the task card, working set and the request in progress from that hook. It works for
every way of starting Codex (terminal, VS Code, the background daemon). Codex's built-in
guidance mentions `notes`/`history` tools that it does not expose, so the three texts are
replaced with ones that point at the task card. Verified on Codex 0.164 alpha, 0.166 and 0.167
with a synthetic model (scripts/e2e_smoke.py).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

from .util import ContextError

MARKER = "Better Codex Context"
# Two thresholds (475k-window models). A turn is one user message and everything Codex does until it
# answers; a task usually spans many turns. The main replacement happens when a turn ends: the Stop
# hook asks for it once the window holds idle_reset_tokens (300k by default, settings.py), so the
# next turn starts small. While a turn runs, the native reset fires only at IN_TASK_LIMIT, a safety
# net: over 519 measured turns that limit resets about 4% of them mid-way (10% at 300k, 18% at
# 200k), and those continue from the restored card, checkpoint and in-progress request.
# Every install sets it, with a bilingual comment carrying LIMIT_MARKER; a user who deletes the marker
# word keeps their own value on later installs (limit_choice). The plugin never checks it otherwise.
IN_TASK_LIMIT = 440000
DEFAULT_COMPACT_LIMIT = IN_TASK_LIMIT
LIMIT_KEY = "model_auto_compact_token_limit"
LIMIT_MARKER = "BCC-MANAGED"
LIMIT_COMMENT = (f"# {LIMIT_MARKER}: Better Codex Context sets this to {IN_TASK_LIMIT} on every install. To keep your own "
                 f"value, delete the {LIMIT_MARKER} marker at the start of this line.\n"
                 f"# Better Codex Context 每次安装都会把它设为 {IN_TASK_LIMIT}。想保留你自己的值，只需删掉上一行开头的 "
                 f"{LIMIT_MARKER} 标记。\n")

GUIDANCE = (f"{MARKER}: long tasks continue in fresh context windows of this same thread. The durable state is the task "
            "card (goal, standing constraints, tasks, lessons, focus/next steps), the working set and the archived "
            "originals; it is restored automatically after every reset together with the user request still being "
            "handled. Keep the card current with the better_context MCP tool context_state(action=\"card\", ops=[...]) "
            "and read exact archived text with context_recall. There are no separate notes or history tools. Right "
            "after each coherent change is applied, write its change note in your next message, while you work (not "
            "collected or repeated at the end of the answer), so later windows keep why the code changed, in this markdown "
            "shape: a line \"**BCC Change note** · T# · 2026-11-09 15:20:33 UTC · `path/a.py`, `path/b.ts`\" (the current "
            "UTC time, always in the form YYYY-MM-DD HH:MM:SS UTC) then the bullets \"- **Motive:** …\", "
            "\"- **Reason:** …\", \"- **Risk:** …\", \"- **Tests:** …\" (see the context-discipline skill). Always write "
            "the title exactly as **BCC Change note** and the four field names exactly as shown, in English (the plugin "
            "reads them); write each field's content in the language of the user's messages, as one to three concrete "
            "sentences that a model without this conversation could act on. Comment the code you write "
            "where it is not obvious (why, constraints, invariants, edge cases), not line by line. While you work, tell the user "
            "in short messages what you are about to do and why, what each step showed, and any problem you hit: they stay "
            "in the conversation as the reasoning trail later windows continue from.")
REMINDER = ("<context_window_reminder>\nOnly {n_remaining} tokens remain in this context window. Before continuing, save "
            "progress with one context_state(action=\"card\", ops=[...]) call: set the status of tasks you touched (done "
            "needs a verification note), add lessons for root causes found, and a checkpoint with focus, next_steps and a "
            "short summary of what is done and what remains in the current request. Then call new_context to continue in "
            "a fresh window; the task card, working set and the current user request are restored automatically.\n"
            "</context_window_reminder>")
FALLBACK = ("<context_window_reminder>\nThis context window is exhausted. Do not continue the task or answer here. Make "
            "exactly one context_state(action=\"card\", ops=[...]) call with a checkpoint (focus, next_steps, summary of "
            "done and remaining work for the current request), then call new_context. Use no other tools.\n"
            "</context_window_reminder>")

# Always enforced: the reset mode itself and the texts that point the model at the task card.
REQUIRED = {
    "features.token_budget.enabled": True,
    "features.token_budget.use_history_notes_extension": False,
    "features.token_budget.reminder_message_template": REMINDER,
    "features.token_budget.guidance_message": GUIDANCE,
    "features.token_budget.auto_compact_fallback_prompt": FALLBACK,
}
# Written only when missing, so a user's own numbers survive every later install.
TUNABLE = {
    "features.token_budget.reminder_threshold_tokens": 24000,
    "features.token_budget.auto_compact_fallback_buffer_tokens": 8192,
}
SETTINGS = {**REQUIRED, **TUNABLE}
# Top-level keys written only when absent. TokenBudget is an under-development Codex feature, so
# without this every Codex start prints "Under-development features enabled: token_budget …".
# A user who sets it to false keeps the warning.
QUIET = {"suppress_unstable_features_warning": True}


def codex_home() -> Path:
    from .util import user_directory
    return user_directory("CODEX_HOME", ".codex")


def _limit_line(lines: list[str]) -> int | None:
    """Index of the top-level `model_auto_compact_token_limit =` line (before the first [table])."""
    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            return None
        if re.match(rf"\s*{LIMIT_KEY}\s*=", line):
            return index
    return None


def _comment_above(lines: list[str], index: int) -> range:
    """The contiguous comment lines directly above line `index`."""
    start = index
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    return range(start, index)


def limit_choice(home: Path | None = None) -> str | None:
    """Who owns the in-task reset limit, from its comment in config.toml: "managed" (the marker is
    there), "own" (the plugin's comment is there but the user deleted the marker) or None (the
    plugin never commented it: the next install sets it once and adds the comment)."""
    try:
        lines = ((home or codex_home()) / "config.toml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    index = _limit_line(lines)
    if index is None:
        return None
    ours = [lines[i] for i in _comment_above(lines, index) if "Better Codex Context" in lines[i]]
    if not ours:
        return None
    # The marker counts only at the start of a comment line (the instructions mention it too).
    return "managed" if any(re.match(rf"\s*#\s*{LIMIT_MARKER}\b", line) for line in ours) else "own"


def mark_limit(home: Path | None = None) -> bool:
    """Put the plugin's comment (with the marker) directly above the limit, replacing an earlier
    version of it; the value itself is written through the config API, never here."""
    path = ((home or codex_home()) / "config.toml").resolve()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.splitlines(keepends=True)
    index = _limit_line([line.rstrip("\r\n") for line in lines])
    if index is None:
        return False
    block = _comment_above([line.rstrip("\r\n") for line in lines], index)
    kept = [lines[i] for i in block if "Better Codex Context" not in lines[i]]
    updated = lines[:block.start] + kept + [LIMIT_COMMENT] + lines[index:]
    if "".join(updated) != text:
        from .util import atomic_write
        atomic_write(path, "".join(updated).encode("utf-8"))
    return True


def read_config(home: Path | None = None) -> dict:
    import tomllib
    try:
        return tomllib.loads(((home or codex_home()) / "config.toml").read_text())
    except (OSError, ValueError):
        return {}


def lookup(config: dict, dotted: str):
    value = config
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def active(config: dict | None = None) -> bool:
    """True when native TokenBudget owns resets for ordinary sessions."""
    value = lookup(read_config() if config is None else config, "features.token_budget")
    return value is True or (isinstance(value, dict) and value.get("enabled") is True)


def plan(config: dict, own_limit: bool = False) -> list[dict]:
    """Settings that differ from the desired state. Required settings are enforced; numeric ones
    are only filled in when missing. The in-task reset limit is set to IN_TASK_LIMIT unless the user
    took it over (own_limit: the marker was deleted from its comment) and it has a value."""
    wanted = dict(REQUIRED)
    wanted.update({key: value for key, value in TUNABLE.items() if not isinstance(lookup(config, key), int)})
    wanted.update({key: value for key, value in QUIET.items() if not isinstance(lookup(config, key), bool)})
    if not (own_limit and isinstance(config.get(LIMIT_KEY), int)):
        wanted[LIMIT_KEY] = DEFAULT_COMPACT_LIMIT
    return [{"key": key, "current": lookup(config, key), "desired": value}
            for key, value in wanted.items() if lookup(config, key) != value]


def status(config: dict | None = None) -> dict:
    own = config is None and limit_choice() == "own"
    config = read_config() if config is None else config
    missing = plan(config, own)
    limit = config.get("model_auto_compact_token_limit")
    return {"ready": not missing, "native_token_budget": active(config), "pending_settings": [m["key"] for m in missing],
            "model_auto_compact_token_limit": limit, "idle_reset_tokens": idle_threshold(limit)}


WINDOW_SHARE = 0.632  # keeps 300k on a 475k window; smaller windows scale down with it


def idle_threshold(limit=None, window=None) -> int | None:
    """Window tokens at which the end of a turn triggers a replacement; None when disabled or when the
    native limit is already lower. Set by `idle_reset_tokens` (see settings.py). With a smaller
    model window it scales down to WINDOW_SHARE of that window, so it still fires before Codex's
    own limit (300k → about 163k on a 258k window)."""
    from .settings import load
    wanted = load()["idle_reset_tokens"]
    if wanted > 0 and isinstance(window, int) and window > 0:
        wanted = min(wanted, int(window * WINDOW_SHARE))
    if wanted <= 0 or (isinstance(limit, int) and wanted >= limit - 20000):
        return None
    return wanted


IDLE_REASON = ("This turn is finished; the window holds about {tokens} tokens, so the next "
               "request starts in a fresh one. If the task card is not current, update it with one "
               "context_state(action=\"card\") call, then call new_context. Do not repeat or continue the answer.")


RESET_SCAN_BYTES = 32 * 1024 * 1024


def last_reset_kind(transcript) -> str | None:
    """How Codex replaced the window last: "zero-model" (TokenBudget reset, what this plugin relies on),
    "model-summary" (Codex fell back to its own compaction, whose record keeps a `compaction` item),
    or None when the end of the log holds no reset."""
    if not isinstance(transcript, str) or not os.path.isfile(transcript):
        return None
    with open(transcript, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - RESET_SCAN_BYTES)
        handle.seek(start)
        block = handle.read(size - start)
    position = len(block)
    while True:
        found = block.rfind(b'"type":"compacted"', 0, position)
        if found < 0:
            return None
        line_start = block.rfind(b"\n", 0, found) + 1
        if found - line_start < 120:  # a top-level record, not text inside another line
            line_end = block.find(b"\n", found)
            line = block[line_start:line_end if line_end >= 0 else len(block)]
            return "model-summary" if b'"type":"compaction"' in line else "zero-model"
        position = line_start


def _token_info(transcript) -> dict | None:
    """The `info` of the session's latest token_count event (read from the end of the log)."""
    if not isinstance(transcript, str) or not transcript:
        return None
    try:
        with open(transcript, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 4 * 1024 * 1024))
            tail = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        if '"token_count"' not in line:
            continue
        try:
            info = json.loads(line)["payload"]["info"]
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(info, dict) and isinstance(info.get("last_token_usage"), dict):
            return info
    return None


def last_usage(transcript) -> dict | None:
    """Latest token_count of the session (tokens in the window after the last request)."""
    info = _token_info(transcript)
    if info is None:
        return None
    last = info["last_token_usage"]
    try:
        return {"tokens": int(last.get("total_tokens") or last["input_tokens"]), "window": info.get("model_context_window")}
    except (KeyError, TypeError, ValueError):
        return None


def session_usage(transcript) -> dict | None:
    """The session's running token totals (input, cached input, output) and the current window."""
    info = _token_info(transcript)
    total = info.get("total_token_usage") if info else None
    if not isinstance(total, dict) or not isinstance(total.get("input_tokens"), int):
        return None
    window = last_usage(transcript) or {}
    return {"input": total["input_tokens"], "cached": int(total.get("cached_input_tokens") or 0),
            "output": int(total.get("output_tokens") or 0), "reasoning": int(total.get("reasoning_output_tokens") or 0),
            "window_tokens": window.get("tokens"), "window": window.get("window")}


class _AppServer:
    """Minimal stdio client for a private `codex app-server`; config writes use the public API."""

    def __init__(self, codex: str, cwd: str):
        self.proc = subprocess.Popen([codex, "app-server"], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        # A reader thread instead of select(): Windows cannot select() on a pipe.
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.replies, self.sequence = {}, 0
        self.call("initialize", {"clientInfo": {"name": "better-codex-context-activate", "version": "1"},
                                 "capabilities": {"experimentalApi": True}})
        self._send({"method": "initialized"})

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)  # the app-server exited

    def _send(self, value):
        self.proc.stdin.write(json.dumps(value).encode() + b"\n")
        self.proc.stdin.flush()

    def call(self, method: str, params: dict, timeout: float = 30):
        self.sequence += 1
        identifier = self.sequence
        self._send({"id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while identifier not in self.replies:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ContextError(f"codex app-server did not answer {method}")
            try:
                line = self.lines.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if line is None:
                raise ContextError("codex app-server exited")
            if line.strip():
                value = json.loads(line)
                if "id" in value and "method" not in value:
                    self.replies[value["id"]] = value
        reply = self.replies.pop(identifier)
        if "error" in reply:
            raise ContextError(f"codex rejected {method}: {reply['error'].get('message', reply['error'])}")
        return reply["result"]

    def close(self):
        self.proc.stdin.close()
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)
        self.proc.stdout.close()


def apply(codex: str = "codex", cwd: str | None = None, server_factory=_AppServer) -> dict:
    """Write the missing settings through config/batchWrite and verify them with config/read.

    Uses a private app-server and does not ask running sessions to reload: those keep their
    loaded configuration until they are restarted.
    """
    server = server_factory(codex, cwd or str(Path.home()))
    try:
        before = server.call("config/read", {"cwd": cwd or str(Path.home()), "includeLayers": False})["config"]
        own = limit_choice() == "own"
        changes = plan(before, own)
        if changes:
            server.call("config/batchWrite", {"edits": [{"keyPath": c["key"], "value": c["desired"], "mergeStrategy": "replace"}
                                                       for c in changes], "reloadUserConfig": False})
        after = server.call("config/read", {"cwd": cwd or str(Path.home()), "includeLayers": False})["config"]
        remaining = plan(after, own)
        if remaining:
            raise ContextError("settings did not persist: " + ", ".join(r["key"] for r in remaining))
        if not own:
            mark_limit()
        return {"applied": [c["key"] for c in changes], "ready": True, "running_sessions_reloaded": False,
                "reset_limit": "yours (marker deleted)" if own else f"{IN_TASK_LIMIT} ({LIMIT_MARKER})"}
    finally:
        server.close()
