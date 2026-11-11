"""Hook entry point that never loses failure details.

Codex discards a hook's stderr when it exits non-zero and shows only "hook exited with code N".
So hooks always exit 0: a failure is written with its full traceback to a per-user log, and a
one-line summary (exception, location, log reference) goes back as `systemMessage`, which Codex
shows as a warning on that hook run for every event this plugin handles (verified on 0.167).
"""
from __future__ import annotations

import _thread
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import traceback
import uuid

from . import __version__
from .store import user_state_home

DEADLINE_SECONDS = 4.0  # hooks.json gives Codex a 5 s timeout; fail with a stack trace before the kill
# SessionStart restores the shadow context after a replacement; hooks.json gives it 15 s.
EVENT_DEADLINES = {"SessionStart": 13.0}
SLOW_SECONDS = 2.0
LOG_LIMIT = 1 << 20


class HookDeadline(Exception):
    pass


def log_path() -> Path:
    return user_state_home() / "hook-errors.log"


def _write(record: dict) -> Path | None:
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode()
    # The fallback when the state directory is unwritable: the temp directory (per user on Windows).
    fallback = Path(tempfile.gettempdir()) / f"better-codex-context-{getattr(os, 'getuid', lambda: 'user')()}"
    for path in (log_path(), fallback / "hook-errors.log"):
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size + len(line) > LOG_LIMIT:
                path.replace(path.with_name(path.name + ".1"))
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(descriptor, line)
            finally:
                os.close(descriptor)
            return path
        except OSError:
            continue
    return None


def _payload_summary(payload) -> dict:
    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__}
    tool_input = payload.get("tool_input")
    command = tool_input.get("command", tool_input.get("cmd")) if isinstance(tool_input, dict) else tool_input
    return {"event": payload.get("hook_event_name"), "source": payload.get("source"), "session_id": payload.get("session_id"),
            "turn_id": payload.get("turn_id"), "payload_cwd": payload.get("cwd"), "transcript_path": payload.get("transcript_path"),
            "tool_name": payload.get("tool_name"), "tool_use_id": payload.get("tool_use_id"),
            "tool_input_preview": None if command is None else str(command)[:200],
            "payload_keys": sorted(payload), "payload_bytes": len(json.dumps(payload, ensure_ascii=False, default=str))}


def _location(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    own = [f for f in frames if "better_codex_context" in f.filename] or frames
    if not own:
        return "unknown"
    frame = own[-1]
    return f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"


# The deadline interrupts the main thread through a signal handler that raises HookDeadline. A watchdog
# thread times it on every platform; it pokes the main thread with a real SIGALRM where threads can be
# signalled (also interrupting a blocking call), and with interrupt_main on Windows (no SIGALRM there).
DEADLINE_SIGNAL = getattr(signal, "SIGALRM", signal.SIGINT)


class _Watchdog(threading.Thread):
    def __init__(self, seconds: float):
        super().__init__(daemon=True)
        self.deadline, self.stopped, self.armed = time.monotonic() + seconds, threading.Event(), True

    def run(self):
        while not self.stopped.wait(0.02):
            if time.monotonic() >= self.deadline:
                if hasattr(signal, "pthread_kill"):
                    signal.pthread_kill(threading.main_thread().ident, DEADLINE_SIGNAL)
                else:
                    _thread.interrupt_main(DEADLINE_SIGNAL)
                return

    def stop(self):
        self.armed = False
        self.stopped.set()


def _on_deadline(watchdog):
    def handler(signum, frame):
        if watchdog.armed:  # a late poke after the hook finished is ignored
            raise HookDeadline("hook ran into its time limit; Codex would kill it at the timeout set in hooks.json")
    return handler


_WATCHDOG = None  # the running hook's watchdog (extend_deadline)
_EXTENDED = False  # this hook took on a long task on purpose (a supervisor review): not reported as slow
STOP_SUPERVISOR_SECONDS = 420.0  # a Stop that reviews at once (supervisor = auto); hooks.json gives Stop 480 s


def extend_deadline(seconds: float) -> None:
    """Let this hook run up to `seconds` from now: the Stop hook when the supervisor reviews the turn at once."""
    global _EXTENDED
    _EXTENDED = True
    if _WATCHDOG is not None:
        _WATCHDOG.deadline = max(_WATCHDOG.deadline, time.monotonic() + seconds)


def run(stream=None) -> dict:
    """Process one hook payload. Never raises and never needs a non-zero exit code."""
    from .hooks import process, resolve_root
    started, payload, incident = time.monotonic(), None, uuid.uuid4().hex[:12]
    global _WATCHDOG, _EXTENDED
    _EXTENDED = False
    watchdog = _WATCHDOG = _Watchdog(DEADLINE_SECONDS)
    previous = signal.signal(DEADLINE_SIGNAL, _on_deadline(watchdog))
    watchdog.start()
    try:
        raw = (stream or sys.stdin).read()
        payload = json.loads(raw) if raw.strip() else None
        if not isinstance(payload, dict):
            raise ValueError(f"hook stdin is not a JSON object ({len(raw)} bytes)")
        deadline = EVENT_DEADLINES.get(payload.get("hook_event_name"))
        if deadline:
            watchdog.deadline = max(time.monotonic() + 0.5, started + deadline)
        result = process(payload)
        elapsed = time.monotonic() - started
        if elapsed > SLOW_SECONDS and not _EXTENDED:
            _write({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "id": incident, "kind": "slow", "version": __version__,
                    "elapsed_ms": round(elapsed * 1000), **_payload_summary(payload)})
        return result
    except BaseException as exc:  # noqa: BLE001 - every failure must be reported, including SystemExit from deep code
        watchdog.stop()
        try:
            root = resolve_root(payload) if isinstance(payload, dict) else None
        except BaseException as inner:  # noqa: BLE001
            root = f"unresolved: {type(inner).__name__}: {inner}"
        where = _location(exc)
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "id": incident, "kind": "error", "version": __version__,
                  "elapsed_ms": round((time.monotonic() - started) * 1000),
                  "error_type": type(exc).__name__, "error": str(exc), "location": where, "resolved_root": root,
                  **_payload_summary(payload), "python": sys.version.split()[0], "pid": os.getpid(),
                  "traceback": "".join(traceback.format_exception(exc))}
        written = _write(record)
        event = (payload or {}).get("hook_event_name") if isinstance(payload, dict) else None
        detail = (f"details: {written} (id {incident})" if written else
                  "log unavailable; traceback: " + " | ".join(traceback.format_exception(exc)[-3:]).replace("\n", " ")[:600])
        message = f"Better Codex Context {event or 'unknown'} hook failed: {type(exc).__name__}: {exc} [at {where}]; {detail}"
        try:
            from .activity import vscode_host
            from .settings import load
            if load()["activity_color"] == "on" and not vscode_host():
                head, _, rest = message.partition(": ")
                message = f"\x1b[1;31m{head}\x1b[0m: {rest}"
        except Exception:  # noqa: BLE001 - the plain message is still reported
            pass
        return {"systemMessage": message[:2000]}
    finally:
        watchdog.stop()
        signal.signal(DEADLINE_SIGNAL, previous)


def recent(limit: int = 5, since_seconds: float = 7 * 86400) -> dict:
    """Recent hook incidents for `bcc doctor`."""
    path, rows = log_path(), []
    for candidate in (path.with_name(path.name + ".1"), path):
        try:
            lines = candidate.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    cutoff = time.time() - since_seconds

    def stamp(row):
        try:
            return time.mktime(time.strptime(row["time"][:19], "%Y-%m-%dT%H:%M:%S"))
        except (KeyError, ValueError):
            return 0
    rows = [r for r in rows if stamp(r) >= cutoff]
    errors = [r for r in rows if r.get("kind") == "error"]
    return {"log": str(path), "errors_last_7_days": len(errors), "slow_runs_last_7_days": len(rows) - len(errors),
            "recent": [{k: r.get(k) for k in ("time", "id", "kind", "event", "error_type", "error", "location", "payload_cwd",
                                              "resolved_root", "elapsed_ms")} for r in rows[-limit:]]}
