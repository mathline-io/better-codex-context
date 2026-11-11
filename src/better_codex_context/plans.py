"""The plan of record: the latest plan Codex presented in plan mode, kept word for word.

In plan mode Codex explores read-only, asks its questions and ends with one `<proposed_plan>` block
(a complete replacement of any earlier one). That block is the spec the implementation must follow,
yet in a long session it would drift into the summarized part of the conversation. The plugin keeps
each block once as P1, P2 … (recallable with context_recall), restores the latest one in full in
every fresh window and replaces the block in the shadow conversation with a one-line reference.

Approving a plan (handover) replaces the earlier ones on the task card (card._approve); the plan segment
says whether the latest plan is approved, still a proposal, or no longer in force (replaced, or stopped by
the user after a redirect).
"""
from __future__ import annotations

import itertools
import json
import re
import time

from .util import canonical, digest, token_measure

# Codex's own format: the tags on their own lines, the plan in markdown between them.
BLOCK = re.compile(r"<proposed_plan>[ \t]*\n(.*?)\n[ \t]*</proposed_plan>", re.S)


def keep(store, body: str) -> tuple[str, bool]:
    """Store one plan; returns its id and whether it is new (a plan seen again keeps its id)."""
    body = body.strip()
    sha = digest(canonical(body))
    proposed = store.db.execute("SELECT coalesce(max(proposed),0) FROM plans WHERE scope=?", (store.scope,)).fetchone()[0] + 1
    row = store.db.execute("SELECT id FROM plans WHERE scope=? AND sha=?", (store.scope, sha)).fetchone()
    if row:
        latest = current(store)
        if latest and latest["id"] != row["id"]:  # proposed again after another plan: the latest proposal again
            store.db.execute("UPDATE plans SET proposed=? WHERE scope=? AND id=?", (proposed, store.scope, row["id"]))
        return row["id"], False
    number = store.db.execute("SELECT count(*) FROM plans WHERE scope=?", (store.scope,)).fetchone()[0] + 1
    seq = store.db.execute("SELECT coalesce(max(seq),0) FROM events WHERE scope=?", (store.scope,)).fetchone()[0]
    identifier = f"P{number}"
    store.db.execute("INSERT INTO plans(scope,id,sha,text,seq,created,proposed) VALUES(?,?,?,?,?,?,?)",
                     (store.scope, identifier, sha, body, seq, time.time(), proposed))
    return identifier, True


def capture(store, text: str) -> list[dict]:
    """Store the plans in `text`; returns the newly stored ones (a plan seen again is not duplicated)."""
    stored = []
    for body in (match.group(1).strip() for match in BLOCK.finditer(text or "")):
        if body:
            identifier, new = keep(store, body)
            if new:
                stored.append({"id": identifier, "text": body})
    return stored


# How Codex starts implementing an approved plan (tui/src/chatwidget/plan_implementation.rs): in the same
# thread it sends exactly IMPLEMENT; with "clear context" it opens a new thread whose first message is
# HANDOVER followed by the plan.
IMPLEMENT = "Implement the plan."
HANDOVER = ("A previous agent produced the plan below to accomplish the user's task. Implement the plan in a fresh "
            "context. Treat the plan as the source of user intent, re-read files as needed, and carry the work through "
            "implementation and verification.")
STEP = re.compile(r"(?:[-*+]|\d+[.)])\s+(.+)", re.S)
SUB_ITEM = re.compile(r"\s+(?:[-*+]|\d+[.)])\s")
NOT_STEPS = re.compile(r"assumption|假设|前提|summary|概要|摘要|overview|背景", re.I)


def handover(store, text: str) -> tuple[str, str] | None:
    """(plan id, plan text) when `text` starts the implementation of a plan, else None."""
    text = (text or "").strip()
    if text.startswith(HANDOVER) and text[len(HANDOVER):].strip():
        body = text[len(HANDOVER):].strip()
        return keep(store, body)[0], body
    if text == IMPLEMENT:
        plan = current(store)
        return (plan["id"], plan["text"]) if plan else None
    return None


def title(body: str) -> str:
    """The plan's first heading, else its first line."""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    heading = next((line.lstrip("#").strip() for line in lines if line.startswith("#")), None)
    return (heading or (lines[0] if lines else "the plan"))[:160]


def steps(body: str) -> list[str]:
    """The plan's work items: its top-level list items (indented lines stay with theirs), except in sections
    that only describe (summary, assumptions)."""
    found, section = [], ""
    for block in re.split(r"\n(?=\S)", body):
        line = block.strip()
        if line.startswith("#"):
            section = line
            continue
        # The item itself and its wrapped lines; an indented sub-item is a paragraph of its own.
        lines = block.strip("\n").splitlines()
        head = [lines[0]] + list(itertools.takewhile(lambda line: not SUB_ITEM.match(line), lines[1:]))
        match = STEP.match("\n".join(head))
        if match and not NOT_STEPS.search(section):
            found.append(" ".join(match.group(1).split()))
    return found


def absorb(store, text: str) -> str:
    """Capture the plans in an answer and replace each block with a reference (shadow body)."""
    if "<proposed_plan>" not in (text or ""):
        return text
    capture(store, text)

    def reference(match):
        row = store.db.execute("SELECT id FROM plans WHERE scope=? AND sha=?",
                               (store.scope, digest(canonical(match.group(1).strip())))).fetchone()
        return f"⟦{row['id'] if row else 'plan'}⟧ proposed plan (kept word for word; the latest one is restored in full)"
    return BLOCK.sub(reference, text)


def since(store, known: int) -> list[dict]:
    """Plans stored after the first `known` ones, oldest first."""
    return [dict(row) for row in store.db.execute("SELECT id,text FROM plans WHERE scope=? ORDER BY rowid LIMIT -1 OFFSET ?",
                                                  (store.scope, known))]


def count(store) -> int:
    return store.db.execute("SELECT count(*) FROM plans WHERE scope=?", (store.scope,)).fetchone()[0]


def current(store) -> dict | None:
    """The latest plan proposed (what Codex's approval implements in the same thread)."""
    row = store.db.execute("SELECT id,text FROM plans WHERE scope=? ORDER BY proposed DESC, rowid DESC LIMIT 1",
                           (store.scope,)).fetchone()
    return dict(row) if row else None


def requests(store, identifier: str) -> list[str]:
    """Card items of the user messages that asked for this plan (link_requests)."""
    row = store.db.execute("SELECT requests FROM plans WHERE scope=? AND id=?", (store.scope, identifier)).fetchone()
    return json.loads(row["requests"]) if row else []


def link_requests(store, fresh: list[str], turn_items: list[str]) -> None:
    """The requests behind newly proposed plans: the user messages of the turn that proposed them, and those
    behind the proposal a plan revises (one that was never approved). Approval merges them into the plan's task."""
    for identifier in fresh:
        previous = store.db.execute("SELECT id,requests FROM plans WHERE scope=? AND id<>? ORDER BY proposed DESC, rowid DESC "
                                    "LIMIT 1", (store.scope, identifier)).fetchone()
        inherited = json.loads(previous["requests"]) if previous and not _approval(store, previous["id"]) else []
        merged = list(dict.fromkeys([*requests(store, identifier), *inherited, *turn_items]))
        store.db.execute("UPDATE plans SET requests=? WHERE scope=? AND id=?", (canonical(merged), store.scope, identifier))


def _approval(store, identifier: str, entries: list[dict] | None = None) -> dict | None:
    """The task card item of the plan's latest approval (its "Implement plan P#" task), or None."""
    from . import card
    found = [i for i in (entries if entries is not None else card.items(store))
             if i["kind"] == "task" and "plan-step" not in i["tags"] and card._plan_of(i) == identifier]
    return found[-1] if found else None


def status(store, identifier: str) -> str:
    """One line on where a plan stands, for its recall."""
    task = _approval(store, identifier)
    if task is None:
        return "proposed, not approved through Codex's plan approval"
    when = f"{task['status_at']} UTC" if task.get("status_at") else "earlier"
    if task["status"] in ("cancelled", "superseded"):
        return f"no longer in force: its task {task['id']} was {task['status']} at {when}"
    return f"approved as {task['id']} ({task['status'].replace('_', ' ')})"


def get(store, identifier: str) -> dict | None:
    row = store.db.execute("SELECT id,text FROM plans WHERE scope=? AND id=?", (store.scope, identifier.upper())).fetchone()
    return dict(row) if row else None


def render(store, budget: int) -> tuple[str, dict | None]:
    """The plan segment of a fresh window: the latest plan in full, or its beginning when it exceeds
    `budget` tokens (the rest stays one recall away)."""
    plan = current(store)
    if plan is None or budget <= 0:
        return "", None
    from . import card
    entries = card.items(store)
    task = _approval(store, plan["id"], entries)
    approved = [i for i in entries if i["kind"] == "task" and "plan-step" not in i["tags"] and card._plan_of(i)]
    gone = list(dict.fromkeys(f"{card._plan_of(i)} {i['status']}" for i in approved
                              if i["status"] in ("cancelled", "superseded") and card._plan_of(i) != plan["id"]))
    earlier = f"\nEarlier plans, not in force (context_recall reads them): {', '.join(gone)}." if gone else ""
    if task and task["status"] in ("cancelled", "superseded"):
        text = (f"[segment type=plan relevance=plan-history]\nThe latest plan, {plan['id']}, is no longer in force: its task "
                f"{task['id']} was {task['status']} at {task['status_at']} UTC"
                + (" after the user stopped it" if task["status"] == "cancelled" else "")
                + f". Do not continue it; context_recall(ref=\"{plan['id']}\") still reads it.{earlier}\n[/segment]")
        return text, {"id": plan["id"], "complete": False, "status": task["status"]}
    if task:
        state = f"approved at {task['created']} UTC as {task['id']}" + (", completed" if task["status"] == "done" else "")
        rule = "Implement it as written unless a later user message changes it"
    else:
        others = list(dict.fromkeys(card._plan_of(i) for i in approved if i["status"] in card.OPEN))
        state = ("proposed, not approved through Codex's plan approval; the user may have approved it in words, as the latest "
                 "user messages tell" + (f". {', '.join(others)}, approved earlier, is still open: if the user approved "
                                         f"{plan['id']}, supersede its open tasks; if the user turned {plan['id']} down, it "
                                         "stands" if others else ""))
        rule = "Once approved, implement it as written unless a later user message changes it"
    head = (f"[segment type=plan relevance=plan-of-record]\nThe plan of record ({plan['id']}, the latest plan proposed in "
            f"this session; {state}). {rule}; give the reason for any deviation in the change note, and before declaring "
            "the work done, check it against the plan's tests and acceptance criteria.")
    change = card.review(store)
    if task and change and change.get("plan") == plan["id"] and card._undecided(change, entries):
        head += (f" The user redirected the work at {change['at']} UTC ({change['item']}): the plan holds only where the "
                 "task card keeps its tasks, so decide those first.")
    signal = card.drift(store, entries)
    if task and signal:
        head += (f" The user's feedback at {signal['at']} UTC signals the work drifted: find the root cause and re-plan "
                 "before continuing with this plan.")
    head += earlier
    text = plan["text"]
    shown = text
    if token_measure(text)["count"] > budget:
        cut = len(text) * budget // max(1, token_measure(text)["count"])
        shown = text[:cut].rsplit("\n", 1)[0] + f"\n… (continued: context_recall(ref=\"{plan['id']}\"))"
    return "\n".join([head, shown, "[/segment]"]), {"id": plan["id"], "complete": shown == text}
