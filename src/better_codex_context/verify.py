"""State integrity checks for one session (`bcc verify`).

Read-only invariants over the plugin's own state and the Codex session log:

* database       SQLite reports no corruption.
* ledger         every user message in the session log since recording began is in the ledger
                 (a missing one means a UserPromptSubmit hook did not record it).
* card links     ledger paragraphs point at existing card items; merged items point at existing targets;
                 task, rule and correction paragraphs are on the card.
* constraints    every active standing constraint reaches a fresh window in words, card plus guard.
* references     protected reference documents still exist in the workspace.
* change notes   edited files that no change note names (worth a look, nothing lost).
* archive        every archived original that the ledger, card and shadow refer to is readable and
                 matches its hash.
* hooks          hook failures recorded for this session; turns that never got a Stop beyond the
                 interruptions the log shows.

Each check is "ok", "warn" (worth a look, nothing lost) or "fail" (an invariant is broken).
"""
from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

from . import card, settings
from .shadow import NEVER_RESTORE, OWN_MARKERS, SKIP_USER_PREFIXES
from .util import ContextError

MAX_LISTED = 10


def find_transcript(session: str) -> str | None:
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    for folder in ("sessions", "archived_sessions"):
        found = glob.glob(str(home / folder / "**" / f"rollout-*{session}.jsonl"), recursive=True)
        if found:
            return max(found, key=os.path.getmtime)
    return None


IMAGE_TAG = re.compile(r"<image\b[^>]*>(?:</image>)?")


def _normal(text: str) -> str:
    """Comparable text: the log wraps attached images in <image …> tags that the hook prompt lacks."""
    return " ".join(IMAGE_TAG.sub("", text).split())


def _log_messages(transcript: str) -> tuple[list[str], int]:
    """User messages typed in the session (Codex's own and this plugin's injections left out) and the
    number of interrupted turns."""
    messages, aborted = [], 0
    with open(transcript, "rb") as handle:
        for raw in handle:
            if b'"turn_aborted"' in raw[:400]:
                aborted += 1
                continue
            if b'"role":"user"' not in raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            payload = record.get("payload") or {}
            if record.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "user":
                continue
            text = "".join(c.get("text", "") for c in payload.get("content") or [] if isinstance(c, dict)).strip()
            if not text or text.startswith(SKIP_USER_PREFIXES) or any(m in text[:300] for m in OWN_MARKERS) \
                    or any(m in text for m in NEVER_RESTORE[:1]):
                continue
            messages.append(text)
    return messages, aborted


def _check(name, status, detail, **extra):
    return {"name": name, "status": status, "detail": detail, **extra}


def database(store):
    result = store.db.execute("PRAGMA quick_check").fetchone()[0]
    return _check("database", "ok" if result == "ok" else "fail", "SQLite quick_check: " + str(result))


def ledger(store, transcript):
    if not transcript or not os.path.isfile(transcript):
        return _check("ledger", "warn", "session log not found; pass --transcript to compare the ledger with it")
    recorded = [_normal(store.artifact(r["artifact"])[1].decode("utf-8", errors="replace"))
                for r in store.db.execute("SELECT artifact FROM events WHERE scope=? AND kind='user' ORDER BY seq", (store.scope,))]
    messages, _ = _log_messages(transcript)
    known = set(recorded)

    def captured(text):
        text = _normal(text)
        return text in known or any(text and (text in r or r in text) for r in recorded if len(r) > 8)

    first = next((i for i, text in enumerate(messages) if captured(text)), None)
    if first is None:
        status = "warn" if messages else "ok"
        return _check("ledger", status, f"{len(messages)} user messages in the log, none recorded yet"
                      + ("; the plugin may have been installed after them" if messages else ""))
    missing = [text for text in messages[first:] if not captured(text)]
    detail = (f"{len(messages) - first} user messages since recording began, {len(missing)} missing from the ledger"
              + (f" ({first} earlier messages predate the plugin)" if first else ""))
    return _check("ledger", "fail" if missing else "ok", detail,
                  missing=[_normal(t)[:120] for t in missing[:MAX_LISTED]])


def card_links(store):
    items = {row["id"]: row for row in store.db.execute("SELECT id,merged_into FROM card_items WHERE scope=?", (store.scope,))}
    dangling = [f"{r['id']} → {r['item']}" for r in store.db.execute(
        "SELECT id,item FROM requirements WHERE scope=? AND item IS NOT NULL", (store.scope,)) if r["item"] not in items]
    dangling += [f"{i} merged into missing {row['merged_into']}" for i, row in items.items()
                 if row["merged_into"] and row["merged_into"] not in items]
    unlinked = [r["id"] for r in store.db.execute(
        "SELECT id FROM requirements WHERE scope=? AND item IS NULL AND class IN ('task','constraint','correction','redirect')",
        (store.scope,))]
    status = "fail" if dangling else "warn" if unlinked else "ok"
    detail = f"{len(items)} card items; {len(dangling)} broken links; {len(unlinked)} task/rule paragraphs not on the card"
    return _check("card links", status, detail, broken=dangling[:MAX_LISTED], unlinked=card.id_ranges(unlinked) if unlinked else "")


def constraints(store):
    text, info = card.render(store, settings.load()["card_budget"])
    guard, guarded = card.constraint_guard(store, text)
    shown = text + "\n" + guard
    rules = [i for i in card.items(store) if i["kind"] == "constraint" and i["status"] == "active"]
    missing = [r["id"] for r in rules if not re.search(rf"(?m)^{re.escape(r['id'])} \[active\]", shown)]
    detail = (f"{len(rules)} active standing constraints; card level {info['level']}"
              + (f"; {len(guarded)} restored by the guard" if guarded else "")
              + (f"; {len(missing)} would reach a fresh window as ids only" if missing else "; all reach a fresh window in words"))
    return _check("constraints", "fail" if missing else "ok", detail, ids_only=card.id_ranges(missing) if missing else "")


def archive(store):
    refs = set()
    refs.update(r[0] for r in store.db.execute("SELECT artifact FROM events WHERE scope=?", (store.scope,)))
    refs.update(r[0] for r in store.db.execute("SELECT source FROM requirements WHERE scope=?", (store.scope,)))
    refs.update(r[0] for r in store.db.execute("SELECT artifact FROM shadow_refs WHERE scope=? AND artifact IS NOT NULL", (store.scope,)))
    for row in store.db.execute("SELECT sources FROM card_items WHERE scope=?", (store.scope,)):
        refs.update(json.loads(row[0]))
    for row in store.db.execute("SELECT evidence FROM requirements WHERE scope=?", (store.scope,)):
        refs.update(json.loads(row[0]))
    broken = []
    for ref in sorted(refs):
        try:
            store.artifact(ref)
        except (ContextError, OSError) as exc:
            broken.append(f"{ref}: {exc}")
    return _check("archive", "fail" if broken else "ok", f"{len(refs)} archived originals referenced, {len(broken)} unreadable",
                  unreadable=broken[:MAX_LISTED])


def hooks(store, transcript, session):
    from .hookrun import log_path
    errors = []
    try:
        for line in log_path().read_text(errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            owner = entry.get("session_id") or (entry.get("payload") or {}).get("session_id")
            if owner == session and entry.get("kind") != "slow":
                errors.append(f"{entry.get('time')} {entry.get('error_type')}: {entry.get('error')}")
    except OSError:
        pass
    rows = store.db.execute("SELECT turn_id,closed FROM hook_turns WHERE scope=? ORDER BY opened", (store.scope,)).fetchall()
    unclosed = sum(1 for r in rows[:-1] if r["closed"] is None)  # the latest turn may still be running
    aborted = _log_messages(transcript)[1] if transcript and os.path.isfile(transcript) else None
    missed = unclosed - aborted if aborted is not None else 0
    status = "warn" if errors or missed > 0 else "ok"
    detail = (f"{len(rows)} turns recorded; {unclosed} ended without a Stop"
              + (f" ({aborted} interrupted in the log)" if aborted is not None else "")
              + f"; {len(errors)} hook failures logged for this session")
    return _check("hooks", status, detail, failures=errors[-MAX_LISTED:])


def references(store):
    from .protected import status
    docs = status(store)
    missing = [d["path"] for d in docs if d["state"] == "missing"]
    detail = (f"{len(docs)} protected reference documents" + (": " + ", ".join(f"{d['path']} {d['state']}" for d in docs[:6]) if docs else "")
              + (f"; {len(missing)} missing from the workspace" if missing else ""))
    return _check("protected references", "warn" if missing else "ok", detail, missing=missing)


def change_notes(store):
    from .changes import latest_for, note_for, notes
    index = latest_for(store)
    edited = [r["path"] for r in store.db.execute("SELECT path FROM work_files WHERE scope=? AND edits>0 AND inside=1", (store.scope,))]
    every = notes(store)
    unexplained = [p for p in edited if not note_for(p, index)] if not any(not n["files"] for n in every) else []
    detail = f"{len(every)} change notes; {len(edited)} edited files, {len(unexplained)} without a note naming them"
    return _check("change notes", "warn" if unexplained else "ok", detail, unexplained=unexplained[:MAX_LISTED])


def run(store, session: str, transcript: str | None = None) -> dict:
    transcript = transcript or find_transcript(session)
    checks = []
    for build in (lambda: database(store), lambda: ledger(store, transcript), lambda: card_links(store),
                  lambda: constraints(store), lambda: references(store), lambda: change_notes(store), lambda: archive(store),
                  lambda: hooks(store, transcript, session)):
        try:
            checks.append(build())
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
            checks.append(_check(getattr(build, "__name__", "check"), "fail", f"{type(exc).__name__}: {exc}"))
    worst = "fail" if any(c["status"] == "fail" for c in checks) else "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    return {"session": session, "transcript": transcript, "result": worst, "checks": checks}
