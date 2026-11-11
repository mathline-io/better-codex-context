"""Working set: which files this task touched, at which version, and what still fails.

Derived from tool receipts without model effort. Measured native histories re-read
about half of the previously read files right after each compaction; a live version
check lets the model reuse conclusions for unchanged files instead.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex

from .util import ContextError, digest, read_scoped, token_measure

READERS = {"cat", "nl", "head", "tail", "less", "more", "bat"}
WRITERS = {"tee"}
PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
SEPARATOR = re.compile(r"&&|\|\||[;|\n]")
TRUNCATED = re.compile(r"truncated output \(original token count: (\d+)\)")
PERSISTENCE_TRUNCATED = "command output truncated for persistence"
MAX_PATHS = 8
HASH_LIMIT = 4 * 1024 * 1024


def host_truncation(output: str):
    """Tokens the host dropped before the hook saw the output (None if untouched)."""
    if not isinstance(output, str):
        return None
    counts = [int(n) for n in TRUNCATED.findall(output)]
    if counts:
        return sum(counts)
    return 0 if PERSISTENCE_TRUNCATED in output else None


def _strip_heredocs(command: str) -> str:
    lines, out, waiting = command.split("\n"), [], []
    for line in lines:
        if waiting:
            if line.strip() == waiting[0]:
                waiting.pop(0)
            continue
        out.append(line)
        waiting.extend(match[2] for match in HEREDOC.finditer(line))
    return "\n".join(out)


def parse_command(command: str):
    """Return [(action, path, detail)] for plain file reads and shell writes."""
    found = []
    for segment in SEPARATOR.split(_strip_heredocs(command or "")):
        try:
            tokens = shlex.split(segment, comments=False, posix=True)
        except ValueError:
            continue
        while tokens and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[0]):
            tokens = tokens[1:]  # environment assignments
        if not tokens:
            continue
        name = os.path.basename(tokens[0])
        targets, args, skip = [], [], False
        for index, token in enumerate(tokens[1:], 1):
            if skip:
                skip = False
                continue
            if token in (">", ">>") and index + 1 < len(tokens):
                targets.append(tokens[index + 1])
                skip = True
            elif token.startswith(">") and len(token) > 1 and not token.startswith(">&"):
                targets.append(token.lstrip(">"))
            elif token in ("-n", "-c", "-e", "--lines", "--bytes", "-s"):
                skip = name in READERS
                args.append(token)
            else:
                args.append(token)
        for target in targets:
            found.append(("edit", target, ""))
        if name in READERS and not targets:
            for arg in args:
                if not arg.startswith("-") and not arg.isdigit() and not any(c in arg for c in "<>&"):
                    found.append(("read", arg, ""))
        elif name == "sed":
            if any(a == "-i" or a.startswith("-i") for a in args):
                files = [a for a in args if not a.startswith("-")][1:]
                found.extend(("edit", f, "") for f in files)
            elif "-n" in args:
                rest = [a for a in args if not a.startswith("-")]
                if len(rest) >= 2:
                    found.extend(("read", f, rest[0]) for f in rest[1:])
        elif name in WRITERS:
            found.extend(("edit", a, "") for a in args if not a.startswith("-"))
    return found[:MAX_PATHS * 2]


def parse_patch(text: str):
    found = []
    for match in PATCH_FILE.finditer(text or ""):
        if match[3]:
            found.append(("edit", match[3], "moved"))
        else:
            found.append(("delete" if match[1] == "Delete" else "edit", match[2], match[1].lower()))
    return found


def _place(store, raw: str):
    path = Path(os.path.expanduser(raw))
    absolute = Path(os.path.normpath(path if path.is_absolute() else store.root / path))
    try:  # workspace paths always with "/", so they compare, dedupe and display the same on Windows
        return absolute.relative_to(store.root).as_posix(), True
    except ValueError:
        return str(absolute), False


def _version(store, relative: str):
    try:
        return digest(read_scoped(store.root, relative, HASH_LIMIT))
    except ContextError:
        return None


def _record(store, seq: int, observations):
    seen = 0
    for action, raw, detail in observations:
        if not raw or seen >= MAX_PATHS or any(c in raw for c in "*?$`{}"):
            continue
        display, inside = _place(store, raw)
        if inside and display.split("/", 1)[0] == ".better-codex-context":
            continue
        seen += 1
        row = store.db.execute("SELECT * FROM work_files WHERE scope=? AND path=?", (store.scope, display)).fetchone()
        if row and row["last_seq"] >= seq:
            continue  # replayed receipt
        version = _version(store, display) if inside and action != "delete" else None
        reads = (row["reads"] if row else 0) + (action == "read")
        edits = (row["edits"] if row else 0) + (action in ("edit", "delete"))
        if action == "read" and inside:
            from .protected import auto_protect
            auto_protect(store, display)
        if action == "edit" and version:
            # Hashes only (the content is never stored twice): the loop guard spots A → B → A edits.
            # The first edit of a file also keeps the version last seen before it, as the starting point.
            if row and row["sha"] and not store.db.execute("SELECT 1 FROM file_versions WHERE scope=? AND path=? LIMIT 1",
                                                           (store.scope, display)).fetchone():
                store.db.execute("INSERT OR IGNORE INTO file_versions(scope,path,seq,sha) VALUES(?,?,?,?)",
                                 (store.scope, display, row["last_seq"], row["sha"]))
            store.db.execute("INSERT OR IGNORE INTO file_versions(scope,path,seq,sha) VALUES(?,?,?,?)",
                             (store.scope, display, seq, version))
        store.db.execute("INSERT INTO work_files(scope,path,reads,edits,last_seq,last_action,sha,inside,detail) VALUES(?,?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(scope,path) DO UPDATE SET reads=excluded.reads,edits=excluded.edits,last_seq=excluded.last_seq,"
                         "last_action=excluded.last_action,sha=excluded.sha,inside=excluded.inside,detail=excluded.detail",
                         (store.scope, display, reads, edits, seq, action, version, int(inside), detail))


def observe_tool(store, event: dict, command: str, tool_name: str = ""):
    """Best effort: an unparseable command never blocks receipt capture."""
    text = command or ""
    if tool_name == "apply_patch" or text.lstrip().startswith("*** Begin Patch"):
        observations = parse_patch(text)
    else:
        observations = parse_command(text)
    if observations:
        _record(store, event["seq"], observations)


def _failures(store, limit=3):
    rows = store.db.execute("SELECT seq,metadata FROM events WHERE scope=? AND kind='tool' ORDER BY seq DESC LIMIT 300",
                            (store.scope,)).fetchall()
    from .card import direction
    change = direction(store)
    passed, result = set(), []
    for row in rows:
        meta = json.loads(row["metadata"])
        command = " ".join(str(meta.get("command", "")).split())
        if not command:
            continue
        code = meta.get("exit_code")
        if code == 0:
            passed.add(command)
        elif code is not None and command not in passed and command not in {c for c, _, _ in result}:
            before = " (before the change of direction)" if change and row["seq"] <= change["seq"] else ""
            result.append((command, code, (meta.get("diagnostic", "") or "") + before))
            if len(result) >= limit:
                break
    return result


def _truncations(store):
    rows = store.db.execute("SELECT metadata FROM events WHERE scope=? AND kind='tool' "
                            "AND json_extract(metadata,'$.host_truncated_tokens') IS NOT NULL", (store.scope,)).fetchall()
    return len(rows), sum(json.loads(r["metadata"]).get("host_truncated_tokens") or 0 for r in rows)


def render(store, budget: int = 1500):
    rows = store.db.execute("SELECT * FROM work_files WHERE scope=? ORDER BY last_seq DESC LIMIT 200", (store.scope,)).fetchall()
    failures = _failures(store)
    truncated, lost = _truncations(store)
    if not rows and not failures and not truncated:
        return "", {"files": 0, "tokens": 0}
    ranked = sorted(rows, key=lambda r: (r["edits"] * 3 + r["reads"] + r["last_seq"] / 1000.0), reverse=True)
    from .changes import latest_for, note_for
    why = latest_for(store)
    lines = []
    for row in ranked[:20]:
        if row["last_action"] == "delete":
            state = "deleted"
        elif not row["inside"]:
            state = "outside workspace (not version-checked)"
        else:
            current = _version(store, row["path"])
            state = ("missing now" if current is None else "unchanged since last seen" if current == row["sha"]
                     else "CHANGED since last seen — re-read before relying on it")
        counts = ", ".join(part for part in (f"edited ×{row['edits']}" if row["edits"] else "",
                                             f"read ×{row['reads']}" if row["reads"] else "") if part)
        detail = f" ({row['detail']})" if row["detail"] and row["last_action"] == "read" else ""
        reason = note_for(row["path"], why) if row["edits"] else None
        lines.append(f"  {row['path']} — {counts}{detail} — {state}" + (f" — why: {reason}" if reason else ""))
    for limit in (20, 10, 5, 0):
        out = ["[segment type=working-set relevance=avoid-rediscovery]"]
        if lines[:limit]:
            out.append("Files touched in this task (live version check; reuse card conclusions for unchanged files, "
                       "re-read changed ones):")
            out.extend(lines[:limit])
            if len(rows) > limit:
                out.append(f"  …{len(rows) - limit} more touched files not listed")
        if failures:
            out.append("Failing commands not yet seen passing:")
            out.extend(f"  `{command[:160]}` exit {code}" + (f" — {line[:200]}" if line else "") for command, code, line in failures)
        if truncated:
            out.append(f"Host-truncated tool outputs: {truncated} (≈{lost} tokens in full; only Codex's shortened "
                       "copies were archived). Rerun with capture_command_prefix when exact output matters.")
        out.append("[/segment]")
        text = "\n".join(out)
        measured = token_measure(text)["count"]
        if measured <= budget:
            return text, {"files": len(rows), "tokens": measured, "listed": min(limit, len(lines))}
    return "", {"files": len(rows), "tokens": 0, "listed": 0}
