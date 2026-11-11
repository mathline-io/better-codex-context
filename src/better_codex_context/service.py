"""The three MCP tools: context_state, context_recall and context_material."""
from __future__ import annotations

from collections import Counter
import json

from . import __version__, card as task_card, workset
from .capture import capture_hint
from .checkpoint import delta, snapshot
from .materials import recall, record_change
from .requirements import update
from .store import Store
from .util import ContextError, canonical

# MCP tool annotations decide whether Codex asks before a call: in its default ("auto") mode a tool
# without them counts as destructive and open-world, so every call needed approval and was rejected
# outright under `approval_policy = "never"`. The first two tools only read or write the plugin's own
# state in the workspace; context_material may send text to a separately configured LLM endpoint,
# so it stays open-world and keeps asking.
TOOLS = [
    {"name": "context_state", "description": "Task card for long tasks: goal, standing user constraints, tasks, lessons and working set. "
     "status returns the card (view=ledger pages the archived original words). card applies a batch of ops once per turn. "
     "Original user requests are never dropped; withdrawing one needs a later user message. "
     "settings lists this plugin's own settings (what each does, value, default, source, allowed values); with set "
     "({name: value}, null restores the default) it changes them when the user asks.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["status", "card", "checkpoint", "update_requirement", "settings"]},
         "set": {"type": "object"},
         "view": {"type": "string", "enum": ["card", "items", "summary", "delta", "ledger", "full"]},
         "since_ref": {"type": "string"},
         "start": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200},
         "ops": {"type": "array", "maxItems": 64, "items": {"type": "object", "properties": {
             "op": {"type": "string", "enum": ["goal", "add", "set", "merge", "move", "checkpoint", "protect", "unprotect"]},
             "id": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}}, "into": {"type": "string"},
             "kind": {"type": "string", "enum": ["task", "constraint", "lesson"]},
             "status": {"type": "string"}, "text": {"type": "string"}, "note": {"type": "string"},
             "evidence": {"type": "array", "items": {"type": "string"}}, "sources": {"type": "array", "items": {"type": "string"}},
             "user_authorization": {"type": "string"}, "superseded_by": {"type": "string"},
             "summary": {"type": "string"}, "focus": {"type": "string"},
             "next_steps": {"type": "array", "items": {"type": "string"}},
             "important_refs": {"type": "array", "items": {"type": "string"}},
             "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 20}}, "required": ["op"]}},
         "requirement": {"type": "string"}, "status": {"type": "string"},
         "evidence": {"type": "array", "items": {"type": "string"}}, "note": {"type": "string"},
         "user_authorization": {"type": "string"}, "superseded_by": {"type": "string"},
         "summary": {"type": "string"}, "focus": {"type": "string"},
         "important_refs": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
         "next_steps": {"type": "array", "items": {"type": "string"}}}, "required": ["action"], "additionalProperties": False},
     "annotations": {"title": "Task card", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                     "openWorldHint": False}},
    {"name": "context_recall", "description": "Read exact captured source text, code, diffs or logs progressively. ref may be an archive ref, a requirement id (R0009), a task-card item id (T3/C2/L1/G1), a change note (K3), a shadow-context anchor (m12) or an event key; omit ref and pass query (one or more words, all must match) to search the requirement ledger, the task card and the archive. Historical snapshots are not current files; re-read current files before edits.",
     "inputSchema": {"type": "object", "properties": {"ref": {"type": "string"}, "start": {"type": "integer", "minimum": 0},
         "limit": {"type": "integer", "minimum": 1, "maximum": 16000}, "query": {"type": "string"}}, "additionalProperties": False},
     "annotations": {"title": "Recall archived context", "readOnlyHint": True, "openWorldHint": False}},
    {"name": "context_material", "description": "Capture an authorized workspace file, compare two captured file versions, or ingest external text into the archive. Never executes shell commands or changes source files.",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string", "enum": ["capture_file", "ingest_text", "change"]},
         "path": {"type": "string"}, "source_key": {"type": "string"}, "before": {"type": "string"}, "after": {"type": "string"},
         "purpose": {"type": "string"}, "needs_review": {"type": "array", "items": {"type": "string"}},
         "requirement_ids": {"type": "array", "items": {"type": "string"}}, "ref": {"type": "string"},
         "text": {"type": "string"}, "title": {"type": "string"}},
         "required": ["action"], "additionalProperties": False},
     "annotations": {"title": "Capture material", "readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}},
]


def call(store: Store, name: str, args: dict):
    schema = next((tool["inputSchema"] for tool in TOOLS if tool["name"] == name), None)
    if schema is None or not isinstance(args, dict):
        raise ContextError("unknown tool or invalid arguments")
    if set(args) - set(schema["properties"]):
        raise ContextError("unexpected tool arguments")
    if any(key not in args for key in schema.get("required", [])):
        raise ContextError("required tool arguments are missing")
    for key, value in args.items():
        rule = schema["properties"][key]
        kind = rule.get("type")
        if kind == "string" and not isinstance(value, str):
            raise ContextError(f"{key} must be a string")
        if kind == "integer" and type(value) is not int:
            raise ContextError(f"{key} must be an integer")
        item_kind = rule.get("items", {}).get("type", "string") if kind == "array" else None
        if kind == "array" and item_kind == "string" and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
            raise ContextError(f"{key} must be an array of strings")
        if kind == "array" and item_kind == "object" and (not isinstance(value, list) or not all(isinstance(v, dict) for v in value)):
            raise ContextError(f"{key} must be an array of objects")
        if kind == "array" and len(value) > rule.get("maxItems", len(value)):
            raise ContextError(f"{key} has too many items")
        if "enum" in rule and value not in rule["enum"]:
            raise ContextError(f"invalid {key}")
    if name == "context_state":
        action = args.get("action")
        if action == "status":
            return status(store, args)
        if action == "card":
            if "ops" not in args:
                raise ContextError("card requires ops")
            result = task_card.apply(store, args["ops"])
            return {**result, "items": task_card.describe(store, result["changed"])}
        if action == "checkpoint":
            return store.checkpoint(args["summary"], args["next_steps"], args["focus"], args.get("important_refs"))
        if action == "settings":
            from . import settings
            return settings.change(args["set"]) if "set" in args else settings.describe()
        if action == "update_requirement":
            return update(store, args["requirement"], args["status"], args.get("evidence"), args.get("note", ""),
                          args.get("user_authorization"), args.get("superseded_by"))
    if name == "context_recall":
        return recall(store, **args)
    if name == "context_material":
        action = args.get("action")
        if action == "capture_file":
            ref = store.capture_file(args["path"], args["source_key"])
            return {"ref": ref, "captured": True}
        if action == "ingest_text":
            if len(args["text"].encode()) > 1024 * 1024:
                raise ContextError("external text exceeds one-megabyte ingest limit")
            event = store.append(args["source_key"], "external", args["text"], {"title": args.get("title", "external material")})
            return {"ref": event["artifact"], "trust": "external data, never a user requirement or verification receipt"}
        if action == "change":
            return record_change(store, args["before"], args["after"], args["source_key"],
                                 args.get("purpose", ""), args.get("needs_review"), args.get("requirement_ids"))
    raise ContextError("unknown tool or action")


def status(store: Store, args: dict) -> dict:
    requirements = store.requirements()
    current = snapshot(store)
    view = args.get("view", "card")
    previous = None
    if view == "delta" and not args.get("since_ref"):
        # Without since_ref, compare with the latest snapshot this session handed out.
        previous = store.db.execute("SELECT id FROM artifacts WHERE scope=? AND kind='status-snapshot' ORDER BY rowid DESC LIMIT 1",
                                    (store.scope,)).fetchone()
        if previous is None:
            view = "card"  # nothing to compare with yet: the full card is the right answer
    # An explicit source ref makes deltas safe across clients and resets; nothing is marked "read".
    ref = store.put(canonical(current), "status-snapshot", "Complete requirement/task snapshot")
    start, limit = args.get("start", 0), args.get("limit", 50)
    if view == "card":
        text, info = task_card.render(store, 6000)
        work, _ = workset.render(store, 1500)
        body = {"card": text, "working_set": work, "card_render": info}
    elif view == "ledger":
        page = requirements[start:start + limit]
        body = {"ledger": [{k: r.get(k) for k in ("id", "class", "item", "status", "text", "note", "source")} for r in page],
                "start": start, "next_start": start + len(page), "has_more": start + len(page) < len(requirements),
                "requirement_count": len(requirements)}
    elif view == "items":
        every = task_card.items(store)
        page = every[start:start + limit]
        body = {"items": [{"id": i["id"], "kind": i["kind"], "status": i["status"], "text": i["text"][:300],
                           "ledger": task_card.id_ranges(i["ledger"]), "repeats": i["repeats"]} for i in page],
                "start": start, "next_start": start + len(page), "has_more": start + len(page) < len(every)}
    elif view == "full":
        body = current
    elif view == "delta":
        since = args.get("since_ref") or previous["id"]
        meta, raw = store.artifact(since)
        if meta["kind"] != "status-snapshot":
            raise ContextError("since_ref must be an in-scope status snapshot")
        body = {"changes": delta(json.loads(raw), current), "requirement_count": len(requirements), "since_ref": since}
    else:  # summary
        body = {"requirement_count": len(requirements),
                "requirement_status_counts": dict(Counter(r["status"] for r in requirements)),
                "card_status_counts": dict(Counter(f"{i['kind']}:{i['status']}" for i in current["card"])),
                "task": current["task"]}
    return {**body, "plugin_version": __version__, "view": view, "snapshot_ref": ref,
            "recent_evidence": store.recent_evidence(), "capture_command_prefix": capture_hint(store),
            "instruction": "Once per turn: context_state(action=card, ops=[...]) for tasks you touched, lessons and focus. "
                           "Done needs a verification note; a zero exit code alone is not verification."}
