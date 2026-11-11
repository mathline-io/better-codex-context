"""Loop guard: notice when the model is going around in circles, and say so while it happens.

Two patterns burn the most tokens in long sessions:

* **stuck**: the same command keeps failing with the same error, even though the model edits
  code between attempts (patch, rerun, same failure, patch again …);
* **repeat**: the same command keeps returning identical output since the user's last message,
  typically re-reading an unchanged file or re-running the same query.

Both are recognized from the archive alone (Codex does not pass exit codes to hooks, so the
error signature comes from the output). On every `loop_guard`-th occurrence (3 by default) the
PostToolUse hook gives the model a short note telling it to stop and change approach.
"""
from __future__ import annotations

import json
import re

from .loop_signals import command_key, oscillating, passing, progress

ERROR = re.compile(r"\b(error|errors? occurred|failed|failure|exception|traceback|panic|fatal|assert(ion)?|not found|"
                   r"denied|refused|timed? ?out|cannot|can't|unable to)\b|错误|失败|异常", re.I)
WINDOW = 200


def normalize(command) -> str:
    return " ".join(str(command or "").split())


SPECIFIC = re.compile(r"(\b\w*(Error|Exception)\b\s*:|^E\s|^error(\[\w+\])?:|\bpanicked\b|^FAILED\b|^FAIL\b|"
                      r"\bAssertionError\b|\bfatal:)", re.I)
VOLATILE = re.compile(r"\b0x[0-9a-f]+\b|\b\d+(\.\d+)?\s*(ms|s|sec|seconds)\b|\b\d{2}:\d{2}:\d{2}(\.\d+)?\b", re.I)


def error_line(output: str) -> str | None:
    """The most specific error line of an output: the last exception or assertion line, else the
    last line that looks like an error. None when nothing looks like an error."""
    lines = [line.strip() for line in output.splitlines()[-200:] if line.strip()]
    found = next((line for line in reversed(lines) if SPECIFIC.search(line)), None) \
        or next((line for line in reversed(lines) if ERROR.search(line)), None)
    return " ".join(found.split())[:200] if found else None


def signature(line: str | None) -> str | None:
    """An error line without timings, addresses and clock times, for comparing runs."""
    return VOLATILE.sub("#", line) if line else None


# Commands that only look at things. Their output can mention "error" without anything failing,
# so they can only ever be repeats, never stuck.
LOOKERS = {"cat", "nl", "head", "tail", "less", "more", "bat", "sed", "awk", "rg", "grep", "egrep", "find", "fd", "ls", "tree",
           "git", "jq", "wc", "diff", "echo", "printf", "stat", "file", "cd", "pwd", "which", "type", "env", "sort", "uniq", "cut"}
SEGMENTS = re.compile(r"&&|\|\||[;|\n]")


def _looks_only(command: str) -> bool:
    words = [segment.split()[0] for segment in SEGMENTS.split(command) if segment.split()]
    return bool(words) and all(word.rsplit("/", 1)[-1] in LOOKERS for word in words)


LEVELS = ("warning", "rethink", "escalate")
# The latest user message asked for repetition ("run it 5 times", "keep checking until …"):
# identical results are then expected, not a loop.
REPEAT_ASK = re.compile(r"再(跑|运行|执行|试|测)|多(跑|运行|执行|测)|重复|反复|多次|\d+\s*(次|遍)|每隔|轮询|监控|直到|等(到|待)|"
                        r"\bagain\b|\brepeat|several times|\d+ times|\buntil\b|\bpoll|\bwatch|\bmonitor|keep (running|checking)", re.I)


def thresholds(every: int) -> dict:
    """`loop_guard` = N: a warning at N-1 runs (from 2), rethink at N, escalate at N+2 and every N+2 after."""
    return {"warning": every - 1 if every - 1 >= 2 else None, "rethink": every, "escalate": every + 2}


def level_for(count: int, every: int) -> str | None:
    limits = thresholds(every)
    if count >= limits["escalate"] and count % limits["escalate"] == 0:
        return "escalate"
    if count == limits["rethink"]:
        return "rethink"
    if limits["warning"] and count == limits["warning"]:
        return "warning"
    return None


def _key(meta: dict) -> str:
    return meta.get("command_key") or command_key(meta.get("command"))


def _identity(meta: dict) -> str | None:
    """What failed: the structured fingerprint, else the error-line signature (older events)."""
    return meta.get("fingerprint") or signature(meta.get("error_line"))


def _passed(meta: dict) -> bool:
    return meta.get("exit_code") == 0 or passing(meta.get("tests"))


def check(store, event: dict, every: int) -> dict | None:
    """The loop this tool result continues when it reaches a reminder level; else None.

    stuck: the same test or command keeps failing with the same failure although files change
    between runs, and no run improved on the one before (fewer failures, more passes). Runs whose
    output the host truncated are unknown: they neither count nor end the streak. The streak spans
    user messages that only correct, acknowledge or repeat; a new task starts it over.
    repeat: the same command returns identical output with nothing changed, since the user's last
    message, unless that message asked for repetition."""
    if every <= 0 or not isinstance(event, dict):
        return None
    meta = json.loads(event["metadata"]) if isinstance(event.get("metadata"), str) else event.get("metadata", {})
    command = normalize(meta.get("command"))
    if not command or command.startswith("*** Begin Patch") or meta.get("tool_name") == "apply_patch":
        return None
    key = _key(meta)
    last_user = store.db.execute("SELECT coalesce(max(seq),0) FROM events WHERE scope=? AND kind='user'", (store.scope,)).fetchone()[0]
    task_start = store.db.execute("SELECT coalesce(max(e.seq),0) FROM events e JOIN requirements r ON r.scope=e.scope AND "
                                  "r.source=e.artifact WHERE e.scope=? AND e.kind='user' AND r.class IN ('task','redirect')", (store.scope,)).fetchone()[0]
    rows = store.db.execute("SELECT events.seq,events.metadata,events.artifact,artifacts.sha FROM events JOIN artifacts "
                            "ON events.artifact=artifacts.id WHERE events.scope=? AND events.kind='tool' AND events.seq>? "
                            "ORDER BY events.seq DESC LIMIT ?", (store.scope, min(task_start, last_user), WINDOW)).fetchall()
    same = [(m, r) for m, r in ((json.loads(r["metadata"]), r) for r in rows) if _key(m) == key]
    if not same or same[0][1]["seq"] != event["seq"]:
        return None
    current, newest = same[0]
    identity = _identity(current)
    if identity and not _looks_only(command) and "host_truncated_tokens" not in current and not _passed(current):
        streak, newer = [newest], current
        for other, row in same[1:]:
            if "host_truncated_tokens" in other:
                continue  # incomplete output: not comparable, so it neither counts nor ends the streak
            if _passed(other) or _identity(other) != identity or progress(other.get("tests"), newer.get("tests")) == "better":
                break
            streak.append(row)
            newer = other
        changed = len({row["sha"] for row in streak}) > 1 or _edited_between(store, streak[-1]["seq"], newest["seq"])
        level = level_for(len(streak), every) if changed else None
        if level:
            files = _oscillating(store, streak[-1]["seq"], newest["seq"])
            if files and level == "warning":
                level = "rethink"  # edits going back and forth while the failure stays: past a simple retry
            return {"kind": "stuck", "level": level, "count": len(streak), "command": command,
                    "error": current.get("error_line") or identity, "ref": event["artifact"], "oscillating": files}
    if _asked_to_repeat(store, last_user):
        return None
    count, later = 0, newest  # identical results with nothing edited in between, since the user's last message
    for _, row in same:
        if row["seq"] <= last_user or row["sha"] != newest["sha"] or (count and _edited_between(store, row["seq"], later["seq"])):
            break
        count, later = count + 1, row
    if count >= every and count % every == 0:
        return {"kind": "repeat", "level": "rethink", "count": count, "command": command, "error": None, "ref": event["artifact"]}
    return None


def _asked_to_repeat(store, last_user: int) -> bool:
    row = store.db.execute("SELECT artifact FROM events WHERE scope=? AND kind='user' AND seq=?", (store.scope, last_user)).fetchone()
    if row is None:
        return False
    text = store.artifact(row["artifact"])[1][:4000].decode("utf-8", errors="replace")
    return bool(REPEAT_ASK.search(text))


def _oscillating(store, first: int, last: int) -> list[str]:
    """Files edited between two runs that went back to an earlier version (A → B → A)."""
    paths = [r[0] for r in store.db.execute("SELECT DISTINCT path FROM file_versions WHERE scope=? AND seq>? AND seq<? LIMIT 20",
                                           (store.scope, first, last))]
    found = []
    for path in paths:
        before = store.db.execute("SELECT sha FROM file_versions WHERE scope=? AND path=? AND seq<=? ORDER BY seq DESC LIMIT 1",
                                  (store.scope, path, first)).fetchone()
        versions = ([before[0]] if before else []) + [r[0] for r in store.db.execute(
            "SELECT sha FROM file_versions WHERE scope=? AND path=? AND seq>? AND seq<? ORDER BY seq", (store.scope, path, first, last))]
        if oscillating(versions):
            found.append(path)
    return found


def _edited_between(store, first: int, last: int) -> bool:
    """Whether files were changed between two runs (so a repeated failure means a failed fix)."""
    patch = store.db.execute("SELECT 1 FROM events WHERE scope=? AND kind='tool' AND seq>? AND seq<? "
                             "AND json_extract(metadata,'$.tool_name')='apply_patch' LIMIT 1", (store.scope, first, last)).fetchone()
    edit = store.db.execute("SELECT 1 FROM work_files WHERE scope=? AND last_action IN ('edit','delete') AND last_seq>? "
                            "AND last_seq<? LIMIT 1", (store.scope, first, last)).fetchone()
    return bool(patch or edit)


def note(loop: dict) -> str:
    """What the model is told; the wording rises with the level."""
    command = loop["command"] if len(loop["command"]) <= 160 else loop["command"][:159] + "…"
    ref = f"context_recall ref=\"{loop['ref']}\""
    if loop["kind"] == "repeat":
        return (f"[loop guard] `{command}` returned the identical result {loop['count']} times since the user's last "
                f"message. Do not run it again unless something has changed; the output is archived ({ref}). Use what "
                "you already know and move on. If you are waiting for something to change, wait longer between checks "
                "or check a different signal.")
    level, count, error = loop.get("level", "rethink"), loop["count"], loop["error"]
    swing = ""
    if loop.get("oscillating"):
        swing = (" " + ", ".join(loop["oscillating"][:5]) + " went back to an earlier version during these attempts "
                 "(A → B → A): the edits are going in circles.")
    if level == "warning":
        return (f"[loop guard] `{command}` failed again with the same error after your change ({count} runs in a row): "
                f"{error}.{swing} Check that the fix addresses the cause before repeating the same kind of change; the "
                f"full output is archived ({ref}).")
    if level == "escalate":
        return (f"[loop guard] No progress after {count} attempts: `{command}` keeps failing with the same error: {error}."
                f"{swing} Stop patching in the same direction. Question the original assumptions and look for an "
                "architectural cause; record the failed approaches as lessons on the task card (only what you verified); "
                f"if you cannot continue, tell the user what blocks you. Full output: {ref}.")
    return (f"[loop guard] `{command}` has now failed {count} times in a row with the same error: {error}.{swing} The "
            "current approach is not working; do not run the same fix-and-retry again. Stop and rethink: read the full "
            f"output ({ref}), re-examine the root cause, compare the changes you already tried, verify in a different "
            "way, and record the rejected approach as a lesson on the task card, or ask the user if you are blocked.")
