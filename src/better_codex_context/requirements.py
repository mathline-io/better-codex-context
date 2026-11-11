"""The ledger of original user requests (R ids). Words are never rewritten or dropped."""
from __future__ import annotations

import json
import re
from contextlib import contextmanager

from .store import Store
from .util import ContextError, canonical, digest, file_lock, utc_now

STATUSES = {"pending", "in_progress", "needs_verification", "done", "blocked", "cancelled", "superseded"}


@contextmanager
def input_lock(store):
    """Serializes concurrent prompt hooks of one session/agent (one lock file per scope)."""
    with file_lock(store.directory / f"input-{store.scope}.lock"):
        yield


def message_key(store: Store, key: str, text: str) -> str:
    """Messages sent while a turn is still running arrive with the same turn id; each gets its own
    key instead of colliding with the first message of the turn."""
    row = store.db.execute("SELECT artifact FROM events WHERE scope=? AND source_key=?", (store.scope, key)).fetchone()
    if row is None or store.artifact(row["artifact"])[1].decode("utf-8", errors="replace") == text:
        return key
    return f"{key}:{digest(text)[:12]}"


def hook_prompt(store: Store, text: str, key: str) -> str:
    """Archive one UserPromptSubmit message; returns the source key it was stored under."""
    with input_lock(store):  # concurrent hooks of one turn must not race for the same key
        key = message_key(store, key, text)
        capture(store, text, key)
    return key


def capture(store: Store, prompt: str, source_key: str, forced: str | None = None) -> list[dict]:
    """Keep every original user paragraph; never replace earlier requests with the latest."""
    source = store.append(source_key, "user", prompt)["artifact"]
    # Splitting changes no source text; prose and explicit bullet requirements both survive.
    parts = [p for p in re.split(r"\n\s*\n|\n(?=\s*(?:[-*]|\d+[.)、])\s*)", prompt) if p.strip()]
    from .card import ensure_synced, ingest
    ensure_synced(store)  # rows from older plugin versions are classified before new ids are allocated
    with store.transaction():
        if store.db.execute("SELECT 1 FROM requirements WHERE scope=? AND source=?", (store.scope, source)).fetchone():
            return store.requirements()
        number = len(store.requirements())
        from .card import settle, tracked
        with tracked(store):  # a correction or repeat can reopen a task
            # Every paragraph keeps its R id; classification only decides its place on the task card.
            for part, (kind, item) in zip(parts, ingest(store, source, prompt, parts, forced)):
                number += 1
                store.db.execute("INSERT INTO requirements(id,scope,source,text,class,item) VALUES(?,?,?,?,?,?)",
                                 (f"R{number:04}", store.scope, source, part, kind, item))
            settle(store, source)  # a plan approval, an answer given again or a redirect, now that the rows exist
        if parts:
            store.bump()
    return store.requirements()


def record_answers(store: Store, call_id: str, tool_input, tool_response) -> list[str]:
    """Answers the user gave through request_user_input (how plan mode asks its questions) are the user's
    own decisions, but they reach Codex as a tool result, not as a message. They enter the ledger like a
    message, one paragraph per answered question, and stand on the card as constraints; returns their
    card item ids."""
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    given = _answers(tool_response)
    paragraphs = []
    for question in questions if isinstance(questions, list) else []:
        if not isinstance(question, dict):
            continue
        answers = [a.strip() for a in given.get(question.get("id"), []) if isinstance(a, str) and a.strip()]
        if not answers:
            continue
        described = {o.get("label"): o.get("description") for o in question.get("options") or [] if isinstance(o, dict)}
        reply = "; ".join(a + (f" ({described[a]})" if described.get(a) else "") for a in answers)
        paragraphs.append(f"Decision: {' '.join(str(question.get('question', '')).split())} → {reply}")
    if not paragraphs:
        return []
    text = "\n\n".join(paragraphs)
    with input_lock(store):
        key = message_key(store, f"answers:{call_id}", text)
        capture(store, text, key, forced="constraint")
    source = store.db.execute("SELECT artifact FROM events WHERE scope=? AND source_key=?", (store.scope, key)).fetchone()["artifact"]
    return list(dict.fromkeys(row["item"] for row in store.db.execute(
        "SELECT item FROM requirements WHERE scope=? AND source=? ORDER BY CAST(substr(id,2) AS INTEGER)", (store.scope, source))
        if row["item"]))


def _answers(response) -> dict:
    """question id → answers, from the tool result ({"answers": {id: {"answers": [...]}}}, as JSON text,
    an MCP-style content list or a mapping)."""
    if isinstance(response, dict) and isinstance(response.get("content"), list):
        response = "".join(part.get("text", "") for part in response["content"] if isinstance(part, dict))
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return {}
    answers = response.get("answers") if isinstance(response, dict) else None
    return {key: value.get("answers") or [] for key, value in answers.items() if isinstance(value, dict)} \
        if isinstance(answers, dict) else {}


def update(store: Store, identifier: str, status: str, evidence: list[str] | None = None,
           note: str = "", user_authorization: str | None = None, superseded_by: str | None = None):
    if status not in STATUSES:
        raise ContextError("unknown requirement status")
    evidence = evidence or []
    if not all(isinstance(e, str) for e in evidence):
        raise ContextError("evidence must contain artifact references")
    row = store.db.execute("SELECT * FROM requirements WHERE scope=? AND id=?", (store.scope, identifier)).fetchone()
    if row is None:
        raise ContextError("unknown requirement")
    for ref in evidence:
        store.artifact(ref)
    if status in ("cancelled", "superseded"):
        if not user_authorization:
            raise ContextError("cancellation/supersession requires a later original user source")
        source = store.db.execute("SELECT seq FROM events WHERE scope=? AND artifact=? AND kind='user'",
                                  (store.scope, user_authorization)).fetchone()
        original = store.db.execute("SELECT seq FROM events WHERE scope=? AND artifact=? AND kind='user'",
                                    (store.scope, row["source"])).fetchone()
        if source is None or original is None or source[0] <= original[0]:
            raise ContextError("authorization must be a later actual user message")
        if status == "superseded" and not store.db.execute("SELECT 1 FROM requirements WHERE scope=? AND id=? AND id<>?",
                                                         (store.scope, superseded_by, identifier)).fetchone():
            raise ContextError("superseded_by must identify another requirement")
        evidence = list(dict.fromkeys([*evidence, user_authorization]))
    if status == "done":
        successful = False
        for ref in evidence:
            meta, _ = store.artifact(ref)
            successful |= meta["kind"] == "tool" and meta["metadata"].get("exit_code") == 0
            if meta["kind"] == "user":
                later = store.db.execute("SELECT 1 FROM events acknowledgement JOIN events original ON original.artifact=? "
                                         "AND original.scope=acknowledgement.scope WHERE acknowledgement.scope=? "
                                         "AND acknowledgement.artifact=? AND acknowledgement.kind='user' AND original.kind='user' "
                                         "AND acknowledgement.seq>original.seq", (row["source"], store.scope, ref)).fetchone()
                successful |= later is not None
        if not successful or not note.strip():
            raise ContextError("done requires successful tool/user evidence and a verification-scope note")
    value = (status, canonical(evidence), note, superseded_by)
    with store.transaction():
        old = store.db.execute("SELECT * FROM requirements WHERE scope=? AND id=?", (store.scope, identifier)).fetchone()
        if tuple(old[k] for k in ("status", "evidence", "note", "superseded_by")) != value:
            from .card import sync_after_ledger_update, tracked
            with tracked(store):
                store.db.execute("UPDATE requirements SET status=?,evidence=?,note=?,superseded_by=? WHERE scope=? AND id=?",
                                 (*value, store.scope, identifier))
                store.db.execute("INSERT INTO requirement_changes(scope,requirement,old_status,new_status,evidence,note,at) "
                                 "VALUES(?,?,?,?,?,?,?)", (store.scope, identifier, old["status"], status, canonical(evidence),
                                                           note, utc_now()))
                sync_after_ledger_update(store, identifier)
            store.bump()
    return next(r for r in store.requirements() if r["id"] == identifier)


def import_history(store: Store, path: str):
    """Explicit, workspace-scoped import; event_msg wins over duplicate native response items."""
    from .util import read_scoped
    raw = read_scoped(store.root, path)
    text = raw.decode("utf-8")
    if text.lstrip().startswith("["):
        records = json.loads(text)
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ContextError("history must contain JSON message objects")
    has_native_events = any(r.get("type") == "event_msg" and r.get("payload", {}).get("type") == "user_message" for r in records)
    imported = 0
    version = digest(raw)
    for index, record in enumerate(records):
        body = None
        payload = record.get("payload", {})
        if has_native_events:
            if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                body = payload.get("message")
        elif record.get("role") == "user":
            body = record.get("text", record.get("content"))
        elif record.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            body = payload.get("content")
        if isinstance(body, list):
            body = "\n".join(part.get("text", "") for part in body if isinstance(part, dict) and part.get("type") in ("input_text", "text"))
        if isinstance(body, str) and body.strip():
            from pathlib import Path
            source_path = Path(path)
            if source_path.is_absolute():
                source_path = source_path.relative_to(store.root)
            capture(store, body, f"import:{digest(str(source_path))}:{index}")
            imported += 1
    return {"source_version": version, "user_messages": imported, "requirements": len(store.requirements())}
