"""Protected reference documents: architecture notes, contracts, ADRs and other files the work must
stay consistent with.

A fresh window cannot carry every document the agent once read, and should not: that would undo
what the replacement saves. What must survive is the knowledge that the document exists, which
version the agent last read, and where that read is archived. So a protected document is listed in
every restored window with its live version status, and the agent is reminded to re-read it when
it changed or when the user's message refers to it while the agent has not read the current
version. Documents are never injected in full.

A document is protected when the model marks it (`{op: "protect", paths: [...]}` on the card) or,
automatically, the first time the agent reads a file whose name says it is an architecture,
design, ADR, contract or specification document.
"""
from __future__ import annotations

import os
import re

from .util import ContextError

AUTO = re.compile(r"(?i)(?:^|/)(?:[^/]*(?:architecture|arch[-_.]|design|adr|contract|specification|invariant)[^/]*"
                  r"|adrs?/[^/]+|decisions/[^/]+)\.(?:md|mdx|markdown|rst|adoc|txt)$")
MAX_DOCS = 12


def _clean(paths) -> list[str]:
    from .scopes import clean
    paths = clean(paths)
    if any(c in p for p in paths for c in "*?["):
        raise ContextError("protect takes exact file paths, not patterns")
    return paths


def protect(store, paths, note: str = "", origin: str = "model") -> list[str]:
    paths = _clean(paths)
    if not isinstance(note, str) or len(note) > 400:
        raise ContextError("note must be a string of at most 400 characters")
    for path in paths:
        store.db.execute("INSERT INTO protected_docs(scope,path,origin,note) VALUES(?,?,?,?) ON CONFLICT(scope,path) "
                         "DO UPDATE SET note=CASE WHEN excluded.note<>'' THEN excluded.note ELSE protected_docs.note END,"
                         "origin=CASE WHEN excluded.origin='model' THEN 'model' ELSE protected_docs.origin END",
                         (store.scope, path, origin, note))
    return paths


def unprotect(store, paths) -> list[str]:
    paths = _clean(paths)
    for path in paths:
        store.db.execute("DELETE FROM protected_docs WHERE scope=? AND path=?", (store.scope, path))
    return paths


def auto_protect(store, path: str):
    """Called by the working set for every file the agent reads inside the workspace."""
    if AUTO.search(path.replace(os.sep, "/")):
        store.db.execute("INSERT OR IGNORE INTO protected_docs(scope,path,origin,note) VALUES(?,?,'auto','')", (store.scope, path))


def status(store) -> list[dict]:
    """Every protected document with its live state: unchanged, changed, unread or missing, and the
    archived output of the agent's last read of it (when there is one)."""
    from .workset import _version
    docs = []
    for row in store.db.execute("SELECT * FROM protected_docs WHERE scope=? ORDER BY origin='auto', rowid LIMIT ?",
                                (store.scope, MAX_DOCS)):
        seen = store.db.execute("SELECT * FROM work_files WHERE scope=? AND path=?", (store.scope, row["path"])).fetchone()
        current = _version(store, row["path"])
        if current is None:
            state = "missing"
        elif seen is None or not seen["sha"]:
            state = "unread"
        else:
            state = "unchanged" if current == seen["sha"] else "changed"
        ref = None
        if seen and seen["last_action"] == "read":
            event = store.db.execute("SELECT artifact FROM events WHERE scope=? AND seq=?", (store.scope, seen["last_seq"])).fetchone()
            ref = event["artifact"] if event else None
        docs.append({"path": row["path"], "note": row["note"], "origin": row["origin"], "state": state, "current": current,
                     "ref": ref, "noticed": row["noticed"]})
    return docs


STATE_TEXT = {"unchanged": "unchanged since you last read it",
              "changed": "CHANGED since you last read it — read the current file before relying on it",
              "unread": "not read in this session — read it before decisions it governs",
              "missing": "missing from the workspace now"}


def render(store) -> tuple[str, list[dict]]:
    """The segment a fresh window gets: one line per protected document, never its content."""
    docs = status(store)
    if not docs:
        return "", []
    lines = ["[segment type=protected-references relevance=consult-before-decisions]",
             "Protected reference documents (architecture, contracts, decisions). Their content is not in this window; "
             "read the parts you need before a decision they govern:"]
    for doc in docs:
        line = f"- {doc['path']} — {STATE_TEXT[doc['state']]}"
        if doc["state"] == "unchanged" and doc["ref"]:
            line += f"; your last read is archived: context_recall(ref=\"{doc['ref']}\")"
        if doc["note"]:
            line += f" ({doc['note']})"
        lines.append(line)
    lines.append("[/segment]")
    return "\n".join(lines), docs


def prompt_note(store, prompt: str) -> tuple[str, list[str]]:
    """Reminders for a new user message: protected documents that changed since they were last read
    (each version reported once), and documents the message names whose current version was not read."""
    due, names = [], []
    text = (prompt or "").casefold()
    for doc in status(store):
        if doc["state"] == "changed" and doc["noticed"] != doc["current"]:
            due.append(f"{doc['path']} changed since you last read it")
            store.db.execute("UPDATE protected_docs SET noticed=? WHERE scope=? AND path=?", (doc["current"], store.scope, doc["path"]))
            names.append(doc["path"])
        elif doc["state"] in ("changed", "unread") and _mentioned(doc["path"], text):
            due.append(f"{doc['path']} is named in this message and you have not read its current version")
            names.append(doc["path"])
    if not due:
        return "", []
    return ("[protected references] " + "; ".join(due) + ". Read the relevant part of the current file before "
            "relying on it."), names


def _mentioned(path: str, text: str) -> bool:
    base = os.path.basename(path).casefold()
    stem = os.path.splitext(base)[0]
    return path.casefold() in text or base in text or (len(stem) >= 6 and re.search(rf"(?<![\w-]){re.escape(stem)}(?![\w-])", text) is not None)
