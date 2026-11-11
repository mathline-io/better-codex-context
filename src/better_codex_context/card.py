"""Task card: a budgeted, structured view of every captured user request.

Original user words stay archived as R-numbered ledger rows. The card groups them
into goal, standing constraints, tasks and lessons. Items backed by user rows derive
their status from those rows, so the legacy ledger API, direct ledger updates and
card operations never disagree. Model-created items (lessons, split tasks, the goal)
keep their own status. Rendering degrades in steps instead of failing: every row
remains accounted for by id range, and the full text stays recallable.
"""
from __future__ import annotations

from contextlib import contextmanager

import json
import re

from .classify import is_correction, is_redirect, message_class, normalized, paragraph_class
from .util import ContextError, canonical, digest, token_measure, utc_now

TASK_STATUSES = ("pending", "in_progress", "needs_verification", "blocked", "done", "cancelled", "superseded")
RULE_STATUSES = ("active", "cancelled", "superseded")
OPEN = ("in_progress", "needs_verification", "blocked", "pending")
PREFIX = {"task": "T", "constraint": "C", "lesson": "L", "goal": "G"}
ROW_PRECEDENCE = ("in_progress", "blocked", "needs_verification", "pending", "done", "superseded", "cancelled")
HISTORICAL_NOTE = re.compile(r"Recovered historical user message (\d+)\. Its original sequence predates this maintenance task\. "
                             r"Retained for traceability; status not inferred and historical side effects must not be replayed\. "
                             r"Later explicit user instructions and current system policy apply\.")
DEFAULT_BUDGET = 4000
DECISION = "Decision: "  # how requirements.record_answers words an answer to a plan-mode question
# Work an ingest leaves for after the message's ledger rows exist (statuses derive from those rows):
# (scope, source) → what to settle. requirements.capture settles it in the same transaction.
_DEFERRED: dict[tuple[str, str], dict] = {}


# ---------------------------------------------------------------- storage helpers

def _transaction(store):
    class Nested:
        def __enter__(self):
            self.own = not store.db.in_transaction
            if self.own:
                store.db.execute("BEGIN IMMEDIATE")

        def __exit__(self, kind, value, traceback):
            if self.own:
                store.db.execute("COMMIT" if kind is None else "ROLLBACK")
    return Nested()


def _revision(store):
    return store.row()["revision"]


def _new_item(store, kind, text, sources=(), status=None, origin="user", tags=(), note=""):
    prefix = PREFIX[kind]
    number = store.db.execute("SELECT coalesce(max(CAST(substr(id,2) AS INTEGER)),0) FROM card_items WHERE scope=? AND kind=?",
                              (store.scope, kind)).fetchone()[0] + 1
    ordinal = store.db.execute("SELECT coalesce(max(ordinal),0) FROM card_items WHERE scope=?", (store.scope,)).fetchone()[0] + 1
    identifier = f"{prefix}{number}"
    status = status or ("pending" if kind == "task" else "active")
    now = utc_now()
    store.db.execute("INSERT INTO card_items(scope,id,kind,status,text,sources,note,tags,origin,ordinal,touched,created,status_at,"
                     "seen_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (store.scope, identifier, kind, status, text, canonical(list(sources)), note, canonical(list(tags)),
                      origin, ordinal, _revision(store), now, now, status))
    store.db.execute("INSERT INTO card_changes(scope,item,old_status,new_status,note,at) VALUES(?,?,?,?,?,?)",
                     (store.scope, identifier, "", status, "", now))
    return identifier


def record_changes(store):
    """Stamp every card item whose status differs from the one last recorded, and keep the change in
    card_changes. A task's status is derived from its ledger rows, and card ops, user messages and the
    ledger API all change it, so the statuses are compared here, once, at the end of each of those writes.
    Items stored before times existed are adopted silently: their past is not invented."""
    seen = {r["id"]: r for r in store.db.execute("SELECT id,seen_status,created FROM card_items WHERE scope=?", (store.scope,))}
    now = utc_now()
    for item in items(store, include_merged=True):
        old = seen[item["id"]]
        if old["seen_status"] == item["status"]:
            continue
        if old["seen_status"] or old["created"]:
            store.db.execute("INSERT INTO card_changes(scope,item,old_status,new_status,note,at) VALUES(?,?,?,?,?,?)",
                             (store.scope, item["id"], old["seen_status"], item["status"], item["note"] or "", now))
            store.db.execute("UPDATE card_items SET seen_status=?,status_at=? WHERE scope=? AND id=?",
                             (item["status"], now, store.scope, item["id"]))
        else:
            store.db.execute("UPDATE card_items SET seen_status=? WHERE scope=? AND id=?", (item["status"], store.scope, item["id"]))


@contextmanager
def tracked(store):
    """Around a write that can change card statuses: records are brought up to date first (so an item from
    before times existed is adopted with its old status, not with the one this write gives it), then the
    changes the write made are recorded with the time."""
    record_changes(store)
    yield
    record_changes(store)


def history(store, identifier):
    """Every recorded status change of a card item, oldest first, with its UTC time."""
    return [dict(r) for r in store.db.execute("SELECT old_status AS old,new_status AS new,note,at FROM card_changes "
                                              "WHERE scope=? AND item=? ORDER BY seq", (store.scope, identifier))]


def _item(store, identifier):
    row = store.db.execute("SELECT * FROM card_items WHERE scope=? AND id=?", (store.scope, identifier)).fetchone()
    if row is None:
        existing = [r["id"] for r in store.db.execute("SELECT id FROM card_items WHERE scope=? AND merged_into IS NULL", (store.scope,))]
        raise ContextError(f"unknown task card item: {identifier} (existing: {id_ranges(existing) or 'none'}; an item added in a "
                           "call that failed was never created, and add assigns the id itself)")
    return row


def _resolve(store, identifier):
    seen = set()
    while identifier and identifier not in seen:
        seen.add(identifier)
        row = store.db.execute("SELECT merged_into FROM card_items WHERE scope=? AND id=?", (store.scope, identifier)).fetchone()
        if row is None or not row["merged_into"]:
            return identifier
        identifier = row["merged_into"]
    return identifier


def _rows(store, identifier):
    return store.db.execute("SELECT * FROM requirements WHERE scope=? AND item=? ORDER BY CAST(substr(id,2) AS INTEGER)",
                            (store.scope, identifier)).fetchall()


def _touch(store, identifier):
    store.db.execute("UPDATE card_items SET touched=? WHERE scope=? AND id=?", (_revision(store), store.scope, identifier))


# ---------------------------------------------------------------- ingestion

def ingest(store, source: str, text: str, parts: list[str], forced: str | None = None):
    """Classify one user message and attach its paragraphs to card items.

    Called inside the capture transaction. Returns (class, item) per paragraph. `forced` sets the
    class of every paragraph (answers to plan questions are decisions, i.e. standing constraints).
    """
    if not forced:
        from . import plans
        plan = plans.handover(store, text)
        if plan:
            return _ingest_plan(store, source, text, parts, *plan)
    whole = None if forced else message_class(text)
    sha = digest(normalized(text))
    if whole is None and not forced:
        earlier = store.db.execute("SELECT source,item FROM user_messages WHERE scope=? AND sha=? AND source<>? AND item IS NOT NULL "
                                   "ORDER BY rowid DESC LIMIT 1", (store.scope, sha, source)).fetchone()
        if earlier:
            target = _resolve(store, earlier["item"])
            # Same words, same places: a repeated constraint paragraph stays with its constraint.
            original = [_resolve(store, r["item"]) for r in store.db.execute(
                "SELECT item FROM requirements WHERE scope=? AND source=? ORDER BY CAST(substr(id,2) AS INTEGER)",
                (store.scope, earlier["source"]))]
            targets = original if len(original) == len(parts) and all(original) else [target] * len(parts)
            for item in dict.fromkeys(targets):
                store.db.execute("UPDATE card_items SET repeats=repeats+1 WHERE scope=? AND id=?", (store.scope, item))
                _touch(store, item)
            _record_message(store, source, sha, "duplicate", target)
            if store.agent == "main":  # saying it again is feedback too
                _DEFERRED.setdefault((store.scope, source), {})["feedback"] = True
            return [("duplicate", item) for item in targets]
    if whole is not None:
        _record_message(store, source, sha, whole, None)
        return [(whole, None)] * len(parts)
    classes = [forced or paragraph_class(part) for part in parts]
    # A redirect stops the current direction as a whole: its paragraphs are the new direction, and what is still
    # open from before must be decided (settle → _redirect). Only the user's own messages (main agent) redirect.
    redirect = not forced and store.agent == "main" and is_redirect(text)
    if redirect:
        classes = ["redirect" if kind in ("task", "correction") else kind for kind in classes]
    assignments = [None] * len(parts)
    correction = not redirect and any(is_correction(part) for part in parts)
    relates = [row["id"] for row in store.db.execute(
        "SELECT id FROM card_items WHERE scope=? AND kind='task' AND merged_into IS NULL ORDER BY touched DESC, ordinal DESC LIMIT 3",
        (store.scope,))] if correction else []
    tags = (["correction"] + ["relates:" + r for r in relates]) if correction else ["redirect"] if redirect else []
    for index, (part, kind) in enumerate(zip(parts, classes)):
        if kind == "constraint":
            text = _unbullet(part)
            same = store.db.execute("SELECT id FROM card_items WHERE scope=? AND kind='constraint' AND text=? AND merged_into IS NULL "
                                    "ORDER BY ordinal LIMIT 1", (store.scope, text)).fetchone()
            if same:  # a rule restated in a later message is the same rule, said with more emphasis
                store.db.execute("UPDATE card_items SET repeats=repeats+1 WHERE scope=? AND id=?", (store.scope, same["id"]))
                _touch(store, same["id"])
                assignments[index] = ("constraint", same["id"])
            else:
                decision = forced == "constraint" and text.startswith(DECISION)
                assignments[index] = ("constraint", _new_item(store, "constraint", text, [source],
                                                             tags=["correction"] if is_correction(part) else
                                                             ["decision"] if decision else []))
                if decision:  # the same question answered again: settle supersedes the earlier answer
                    _DEFERRED.setdefault((store.scope, source), {}).setdefault("decisions", []).append(assignments[index][1])
    rest = [index for index, kind in enumerate(classes) if kind != "constraint"]
    main = None
    if rest:
        body = "\n\n".join(parts[index].strip() for index in rest)
        if len(rest) == 1:
            body = _unbullet(body)
        main = _new_item(store, "task", body, [source], tags=tags)
        for index in rest:
            assignments[index] = (classes[index], main)
    _record_message(store, source, sha, "redirect" if redirect and main else "correction" if correction else "request",
                    main or next((item for _, item in assignments if item), None))
    if redirect and main:
        _DEFERRED.setdefault((store.scope, source), {})["redirect"] = main
    elif not forced and store.agent == "main":
        _DEFERRED.setdefault((store.scope, source), {})["feedback"] = True
    return assignments


def _ingest_plan(store, source, text, parts, plan_id, body):
    """Codex started implementing an approved plan (plans.handover): one task for the plan and one per step,
    so each step gets its own status. Step paragraphs of the message go to their step (an indented line to
    the step above it); headings, prose and the descriptive sections to the plan's task."""
    from . import plans
    tag = "plan:" + plan_id
    sha = digest(normalized(text))
    existing = next((row["id"] for row in store.db.execute(
        "SELECT * FROM card_items WHERE scope=? AND kind='task' AND merged_into IS NULL ORDER BY ordinal DESC", (store.scope,))
        if tag in json.loads(row["tags"]) and "plan-step" not in json.loads(row["tags"])
        and _derived(store, row)[0] not in ("cancelled", "superseded")), None)
    if existing:  # the same plan approved again while its tasks stand: they exist already
        store.db.execute("UPDATE card_items SET repeats=repeats+1 WHERE scope=? AND id=?", (store.scope, existing))
        _touch(store, existing)
        _record_message(store, source, sha, "duplicate", existing)
        return [("duplicate", existing)] * len(parts)
    main = _new_item(store, "task", f"Implement plan {plan_id}: {plans.title(body)}", [source], tags=[tag])
    step_items, assignments, current = {}, [], None
    for text in plans.steps(body):
        if text not in step_items:
            step_items[text] = _new_item(store, "task", text, [source], tags=[tag, "plan-step"])
    for part in parts:
        flat = " ".join(_unbullet(part).split())
        if flat in step_items and not part[:1].isspace():
            current = step_items[flat]
        elif not (part[:1].isspace() and current):
            current = None
        assignments.append(("task", current or main))
    _record_message(store, source, sha, "request", main)
    _DEFERRED.setdefault((store.scope, source), {})["plan"] = {"plan": plan_id, "main": main, "steps": step_items}
    return assignments


def _unbullet(text):
    return re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s+", "", text.strip(), count=1)


def _record_message(store, source, sha, kind, item):
    store.db.execute("INSERT OR IGNORE INTO user_messages(scope,source,sha,class,item) VALUES(?,?,?,?,?)",
                     (store.scope, source, sha, kind, item))


PEER_NOTE = "From another Codex task or an automation, not from the user (the session log shows no client's id on it)."


def from_peer(store, source) -> list[str]:
    """The message stored under `source` came from another Codex task or an automation, not from the user: the session
    log, written after the prompt hook, shows it without a client's message id (shadow._ingest). Its paragraphs are no
    requests of the user, and the open items only it created are withdrawn. Returns those item ids."""
    rows = store.db.execute("SELECT id,item,class FROM requirements WHERE scope=? AND source=?", (store.scope, source)).fetchall()
    if not rows or all(r["class"] in ("peer", "automated") for r in rows):
        return []
    withdrawn = []
    with _transaction(store), tracked(store):
        store.db.execute("UPDATE requirements SET class='peer' WHERE scope=? AND source=? AND class<>'automated'",
                         (store.scope, source))
        store.db.execute("UPDATE user_messages SET class='peer' WHERE scope=? AND source=?", (store.scope, source))
        for item in dict.fromkeys(r["item"] for r in rows if r["item"]):
            rest = store.db.execute("SELECT 1 FROM requirements WHERE scope=? AND item=? AND source<>?",
                                    (store.scope, item, source)).fetchone()
            if rest or not any(r["status"] in OPEN for r in _rows(store, item)):
                continue  # an item the user asked for too, or one already finished, stays as it is
            _write_rows(store, item, "cancelled", PEER_NOTE)
            store.db.execute("UPDATE card_items SET note=?,origin='peer' WHERE scope=? AND id=?", (PEER_NOTE, store.scope, item))
            _touch(store, item)
            withdrawn.append(item)
        store.bump()
    return withdrawn


# ---------------------------------------------------------------- changes of direction

def _plan_of(item) -> str | None:
    return next((t[5:] for t in item["tags"] if t.startswith("plan:")), None)


def settle(store, source):
    """Finish what ingesting `source` started, now that its ledger rows exist (requirements.capture calls it in
    the same transaction): a plan approval replaces the earlier plans, a question answered again replaces the
    earlier answer, a redirect lists what is open from before."""
    work = _DEFERRED.pop((store.scope, source), None)
    if not work:
        return
    # Never at the cost of the message: should this fail, its ledger rows and card items stay, only the change
    # of direction is not applied (and the failure goes to the hook error log).
    store.db.execute("SAVEPOINT settle")
    try:
        if "plan" in work:
            _approve(store, source, **work["plan"])
        for new in work.get("decisions", []):
            _answered_again(store, source, new)
        if "redirect" in work:
            _redirect(store, source, work["redirect"])
        elif work.get("feedback"):
            _feedback(store, source)
    except Exception as exc:  # noqa: BLE001
        store.db.execute("ROLLBACK TO settle")
        import time
        import traceback
        from .hookrun import _location, _write
        _write({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": "card-settle", "error_type": type(exc).__name__,
                "error": str(exc)[:500], "location": _location(exc), "session_id": store.session,
                "traceback": "".join(traceback.format_exception(exc))})
    store.db.execute("RELEASE settle")


def _close(store, identifier, status, note, source, superseded_by=None, evidence=()):
    """A status the plugin sets on the user's behalf: `source` is the later user message that decided it (a plan
    approval, a question answered again) and becomes its evidence; a superseded item names its successor."""
    target = None
    if superseded_by:
        first = _rows(store, superseded_by)
        target = first[0]["id"] if first else superseded_by
    if _rows(store, identifier):
        _write_rows(store, identifier, status, note, [*evidence, source], target)
    else:
        store.db.execute("UPDATE card_items SET status=? WHERE scope=? AND id=?", (status, store.scope, identifier))
    store.db.execute("UPDATE card_items SET note=? WHERE scope=? AND id=?", (note, store.scope, identifier))
    _touch(store, identifier)


def _approve(store, source, plan, main, steps):
    """A plan was approved for implementation (plans.handover), so it replaces every earlier plan: a step it
    repeats word for word carries its status over (done stays done, work in progress continues), every other open
    task of an earlier plan is superseded by the new plan's task, and the requests that asked for this plan
    (plans.requests) join that task. Done work stays done; the approval message is the authorization."""
    from . import plans
    new = {main, *steps.values()}  # everything else of a plan is earlier, an earlier approval of this plan too
    earlier = [i for i in items(store) if i["kind"] == "task" and _plan_of(i) and i["id"] not in new]
    by_text = {normalized(_unbullet(i["text"])): i for i in sorted(earlier, key=lambda i: i["ordinal"]) if "plan-step" in i["tags"]}
    carried, superseded, replaced = [], [], set()
    for text, step in steps.items():
        old = by_text.get(normalized(_unbullet(text)))
        if old is None or old["status"] in ("cancelled", "superseded"):
            continue
        origin = f"{old['id']} (step of {_plan_of(old)})"
        if old["status"] == "done":
            _close(store, step, "done", f"Done earlier as {origin}" + (f": {old['note']}" if old["note"] else ""), source,
                   evidence=old["evidence"])
        else:
            if old["status"] != "pending":
                _close(store, step, old["status"], f"Continued from {origin}" + (f": {old['note']}" if old["note"] else ""), source)
            _close(store, old["id"], "superseded", f"The same step of {plan}: {step}", source, superseded_by=step)
            superseded.append(old["id"])
            replaced.add(_plan_of(old))
        carried.append([old["id"], step, old["status"]])
    for item in items(store):
        if item["kind"] == "task" and _plan_of(item) and item["id"] not in new and item["status"] in OPEN:
            why = f"Plan {plan} was approved again: {main}" if _plan_of(item) == plan else f"Plan {_plan_of(item)} was replaced by {plan}"
            _close(store, item["id"], "superseded", why, source, superseded_by=main)
            superseded.append(item["id"])
            replaced.add(_plan_of(item))
    wanted = set(plans.requests(store, plan)) - {main}
    asked_for = review(store)
    if asked_for:  # the plan of the new direction the user asked for takes over the redirect's task
        wanted.add(asked_for["item"])
    asked = [i["id"] for i in items(store) if i["id"] in wanted and i["kind"] == "task" and i["status"] in OPEN and not _plan_of(i)]
    if asked:
        _merge(store, asked, main)
    if superseded:
        _set_direction(store, "plan", main, source, plan=plan, replaced=sorted(replaced), superseded=superseded,
                       carried=carried, merged=asked)
    else:
        state = store.state()
        state["approval"] = {"plan": plan, "item": main, "carried": carried, "merged": asked, "source": source}
        store.db.execute("UPDATE scopes SET state=? WHERE scope=?", (canonical(state), store.scope))


def _answered_again(store, source, new):
    """Re-planning asks a plan-mode question again: the new answer supersedes the earlier one."""
    question = _item(store, new)["text"][len(DECISION):].split(" → ", 1)[0]
    for item in items(store):
        if (item["kind"] == "constraint" and item["id"] != new and item["status"] == "active" and item["text"].startswith(DECISION)
                and item["text"][len(DECISION):].split(" → ", 1)[0] == question):
            _close(store, item["id"], "superseded", f"Answered again: {new}", source, superseded_by=new)


def _redirect(store, source, task):
    """The user stopped the current direction (classify.is_redirect). What is still open from before, with the
    decisions taken while planning the plan in progress, is listed for the model to decide (undecided). Nothing
    is closed here: a stop often spares part of the work, which only the model can tell from the message."""
    earlier = review(store)
    current = {i["id"]: i for i in items(store)}
    if earlier and current.get(earlier["item"], {}).get("status") in OPEN:  # redirected again: the newer one replaces it
        _close(store, earlier["item"], "superseded", f"Redirected again: {task}", source, superseded_by=task)
    current = items(store)
    listed = [i["id"] for i in current if i["kind"] == "task" and i["id"] != task and i["status"] in OPEN]
    approvals = sorted((i for i in current if i["kind"] == "task" and _plan_of(i) and "plan-step" not in i["tags"]),
                       key=lambda i: i["ordinal"])
    plan = None
    if approvals and approvals[-1]["status"] in OPEN:
        plan = _plan_of(approvals[-1])
        after = approvals[-2]["ordinal"] if len(approvals) > 1 else 0
        listed += [i["id"] for i in current if i["kind"] == "constraint" and i["status"] == "active"
                   and i["text"].startswith(DECISION) and after < i["ordinal"] < approvals[-1]["ordinal"]]
    _set_direction(store, "redirect", task, source, plan=plan)
    # Kept apart from the direction: a plan approved next replaces the direction, while what the model has not
    # decided yet stays to decide (the approval itself decides the earlier plan's tasks).
    state = store.state()
    state["review"] = {"item": task, "source": source, "at": state["direction"]["at"], "revision": _revision(store),
                       "plan": plan, "items": listed, "held": False}
    store.db.execute("UPDATE scopes SET state=? WHERE scope=?", (canonical(state), store.scope))


def _feedback(store, source):
    """Drift read from the user's feedback (feedback.assess): strong dissatisfaction, or the same problem raised
    again and again, since the latest change of direction or drift signal. It asks the model to stop, find the
    root cause and re-plan (drift_note); a lesson recorded after it, or a change of direction, settles it."""
    from . import feedback
    state = store.state()
    after = max((state.get("direction") or {}).get("seq", 0), (state.get("drift") or {}).get("seq", 0))
    messages = {}
    for row in store.db.execute("SELECT r.id,r.source,r.class,r.text,e.seq FROM requirements r JOIN events e ON e.scope=r.scope "
                                "AND e.artifact=r.source AND e.kind='user' WHERE r.scope=? AND e.seq>? "
                                "ORDER BY e.seq,CAST(substr(r.id,2) AS INTEGER)", (store.scope, after)):
        message = messages.setdefault(row["source"], {"text": [], "classes": set(), "rows": []})
        message["text"].append(row["text"])
        message["classes"].add(row["class"] or "task")
        message["rows"].append(row["id"])
    window = [{**m, "text": "\n\n".join(m["text"])} for key, m in messages.items()
              if key == source or not m["classes"] <= {"ack", "automated", "peer", "heading"}][-feedback.WINDOW:]
    if not window or messages.get(source) is None:
        return
    found = feedback.assess(window)
    if not found:
        return
    item = next((r["item"] for r in store.db.execute("SELECT item FROM requirements WHERE scope=? AND source=? AND item IS NOT NULL "
                                                     "ORDER BY CAST(substr(id,2) AS INTEGER)", (store.scope, source))), None)
    if item:
        tags = json.loads(_item(store, item)["tags"])
        if "drift" not in tags:
            store.db.execute("UPDATE card_items SET tags=? WHERE scope=? AND id=?", (canonical([*tags, "drift"]), store.scope, item))
    seq = store.db.execute("SELECT coalesce(max(seq),0) FROM events WHERE scope=?", (store.scope,)).fetchone()[0]
    state = store.state()
    state["drift"] = {"at": utc_now(), "item": item, "source": source, "seq": seq, "revision": _revision(store),
                      "reasons": found["reasons"], "rows": found["rows"], "count": found["count"], "reminded": False}
    store.db.execute("UPDATE scopes SET state=? WHERE scope=?", (canonical(state), store.scope))


def drift(store, entries=None) -> dict | None:
    """The latest drift signal while it is unsettled: no lesson recorded since, no change of direction after it."""
    state = store.state()
    signal = state.get("drift")
    if not signal or (state.get("direction") or {}).get("seq", 0) > signal["seq"]:
        return None
    if any(i["kind"] == "lesson" and i["touched"] > signal["revision"] for i in (entries if entries is not None else items(store))):
        return None
    return signal


def _drift_reason(signal) -> str:
    return "; ".join(signal["reasons"]) + (f" ({id_ranges(signal['rows'])})" if signal.get("rows") else "")


def drift_note(store) -> str:
    """The prompt note of a message that signals drift: stop, find the root cause, re-plan."""
    signal = drift(store)
    if not signal:
        return ""
    return (f"The user's feedback signals that the work has drifted from what they want: {_drift_reason(signal)}. Stop the "
            "current approach and do not try another quick fix. First re-read the user's messages on this (context_recall), "
            "then answer with what they asked, in their words; where and why the work departed from it (the root cause, not "
            "the symptom); and what you will do instead. If the message gives an exact instruction, carry out exactly that "
            "and check the result against their words; otherwise propose a revised plan (for a large change, in plan mode) "
            "and wait for the user's agreement before changing code. Record the root cause as a lesson on the task card.")


def _set_direction(store, kind, item, source, **details):
    """Remember the latest change of direction (a redirect, or a plan replacing an open one): what predates it
    (the checkpoint, change notes, failing commands) is labelled as such wherever it is restored."""
    seq = store.db.execute("SELECT coalesce(max(seq),0) FROM events WHERE scope=?", (store.scope,)).fetchone()[0]
    state = store.state()
    state.pop("approval", None)
    state["direction"] = {"kind": kind, "item": item, "source": source, "at": utc_now(), "revision": _revision(store),
                          "seq": seq, "held": False, **details}
    store.db.execute("UPDATE scopes SET state=? WHERE scope=?", (canonical(state), store.scope))


def direction(store) -> dict | None:
    return store.state().get("direction")


def review(store) -> dict | None:
    """The latest redirect's list of earlier items to decide (_redirect)."""
    return store.state().get("review")


def direction_label(change: dict) -> str:
    if change["kind"] == "redirect":
        return f"the user's redirect at {change['at']} UTC ({change['item']})"
    return f"plan {change['plan']} replacing {', '.join(change.get('replaced') or ['an earlier plan'])} at {change['at']} UTC"


def _undecided(asked, entries) -> list[dict]:
    if not asked:
        return []
    wanted = set(asked.get("items", []))
    return [i for i in entries if i["id"] in wanted and i["touched"] <= asked["revision"] and i["status"] in OPEN + ("active",)]


def undecided(store) -> list[dict]:
    """What a redirect left to decide and the model has not touched since: cancelled, superseded and merged items
    are decided, and so is an item kept by setting it again (with a note why it still applies)."""
    return _undecided(review(store), items(store))


def _listing(entries) -> str:
    if len(entries) <= 12:
        return ", ".join(f"{i['id']} ({'decision' if i['kind'] == 'constraint' else i['status'].replace('_', ' ')})"
                         for i in entries)
    groups = {}
    for i in entries:
        groups.setdefault("decisions" if i["kind"] == "constraint" else i["status"].replace("_", " "), []).append(i["id"])
    return "; ".join(f"{name} {id_ranges(ids)}" for name, ids in groups.items())


def _decide(change, entries) -> str:
    return (f"{_listing(entries)}. Set each to cancelled, or to superseded with superseded_by ({change['item']} or the new "
            f"plan's task), both with user_authorization=\"{change['source']}\"; or keep it by setting its status again "
            "with a note on why it still applies.")


def redirect_note(store) -> str:
    """The prompt note of a redirect message: stop, decide what is open from before, then set the new direction."""
    change = review(store)
    if not change:
        return ""
    entries = undecided(store)
    plan = f" Plan {change['plan']} stands only if you keep its task." if change.get("plan") else ""
    return (f"The user stopped the current direction ({change['item']}): do not continue the earlier work."
            + (" First decide, in one context_state(action=\"card\") call, what is still open from before: "
               + _decide(change, entries) + plan if entries else "")
            + " Work already done stays done: tell the user whether its changes stay or get reverted. Say in a sentence "
            "why the earlier direction went wrong and record it as a lesson. Then set the goal and a new checkpoint (focus, "
            "next steps) for the new direction; for a large change, propose a plan first.")


def ensure_synced(store):
    """Classify rows written by older plugin versions. Derivation only: no revision bump."""
    pending = store.db.execute("SELECT source,min(CAST(substr(id,2) AS INTEGER)) AS first FROM requirements "
                               "WHERE scope=? AND class IS NULL GROUP BY source ORDER BY first", (store.scope,)).fetchall()
    if not pending:
        return 0
    with _transaction(store):
        for group in pending:
            rows = store.db.execute("SELECT id,text FROM requirements WHERE scope=? AND source=? AND class IS NULL "
                                    "ORDER BY CAST(substr(id,2) AS INTEGER)", (store.scope, group["source"])).fetchall()
            parts = [row["text"] for row in rows]
            for row, (kind, item) in zip(rows, ingest(store, group["source"], "\n\n".join(parts), parts)):
                store.db.execute("UPDATE requirements SET class=?,item=? WHERE scope=? AND id=?", (kind, item, store.scope, row["id"]))
            _DEFERRED.pop((store.scope, group["source"]), None)  # history is classified, not acted on again
    return len(pending)


# ---------------------------------------------------------------- derived view

def _derived(store, row):
    rows = _rows(store, row["id"])
    if not rows or row["kind"] in ("lesson", "goal"):
        return row["status"], row["note"], [], []
    statuses = {r["status"] for r in rows}
    if row["kind"] == "task":
        status = next(s for s in ROW_PRECEDENCE if s in statuses)
    else:
        status = "superseded" if statuses == {"superseded"} else "cancelled" if statuses <= {"cancelled", "superseded"} else "active"
    note = next((r["note"] for r in reversed(rows) if r["note"]), row["note"])
    evidence = list(dict.fromkeys(e for r in rows for e in json.loads(r["evidence"])))
    return status, note, [r["id"] for r in rows], evidence


def items(store, include_merged=False):
    ensure_synced(store)
    result = []
    for row in store.db.execute("SELECT * FROM card_items WHERE scope=? ORDER BY ordinal", (store.scope,)):
        if row["merged_into"] and not include_merged:
            continue
        status, note, ledger, evidence = _derived(store, row)
        superseded = [r["superseded_by"] for r in _rows(store, row["id"]) if r["superseded_by"]]
        result.append({"id": row["id"], "kind": row["kind"], "status": "merged" if row["merged_into"] else status,
                       "text": row["text"], "note": note, "ledger": ledger, "evidence": evidence,
                       "tags": json.loads(row["tags"]), "repeats": row["repeats"], "origin": row["origin"],
                       "merged_into": row["merged_into"], "superseded_by": superseded,
                       "ordinal": row["ordinal"], "touched": row["touched"], "created": row["created"],
                       "status_at": row["status_at"]})
    return result


# ---------------------------------------------------------------- operations

def _later_user_source(store, authorization, sources):
    if not authorization:
        return False
    later = store.db.execute("SELECT seq FROM events WHERE scope=? AND artifact=? AND kind='user'",
                             (store.scope, authorization)).fetchone()
    if later is None:
        return False
    for source in sources:
        original = store.db.execute("SELECT seq FROM events WHERE scope=? AND artifact=? AND kind='user'",
                                    (store.scope, source)).fetchone()
        if original is None or later[0] <= original[0]:
            return False
    return True


def _write_rows(store, identifier, status=None, note=None, evidence=None, superseded_by=None):
    for row in _rows(store, identifier):
        target = status or row["status"]
        if row["status"] in ("cancelled", "superseded") and status in OPEN + ("done",):
            continue  # an explicitly withdrawn paragraph is not silently revived by a sibling update
        new_note = row["note"] if note is None else note
        new_evidence = list(dict.fromkeys([*json.loads(row["evidence"]), *(evidence or [])]))
        new_superseded = superseded_by if target == "superseded" else row["superseded_by"]
        value = (target, canonical(new_evidence), new_note, new_superseded)
        if (row["status"], row["evidence"], row["note"], row["superseded_by"]) != value:
            store.db.execute("UPDATE requirements SET status=?,evidence=?,note=?,superseded_by=? WHERE scope=? AND id=?",
                             (*value, store.scope, row["id"]))
            store.db.execute("INSERT INTO requirement_changes(scope,requirement,old_status,new_status,evidence,note,at) "
                             "VALUES(?,?,?,?,?,?,?)", (store.scope, row["id"], row["status"], target, canonical(new_evidence),
                                                       new_note, utc_now()))


def _set(store, op):
    identifier = _resolve(store, op.get("id"))
    row = _item(store, identifier)
    status, text, note = op.get("status"), op.get("text"), op.get("note")
    allowed = TASK_STATUSES if row["kind"] == "task" else RULE_STATUSES
    if status is not None and status not in allowed:
        raise ContextError(f"status for {row['kind']} must be one of: " + ", ".join(allowed))
    for name, value in (("text", text), ("note", note)):
        if value is not None and (not isinstance(value, str) or len(value) > 4000):
            raise ContextError(name + " must be a string of at most 4000 characters")
    if text is not None and not text.strip():
        raise ContextError("text cannot be empty")
    evidence = op.get("evidence") or []
    if not isinstance(evidence, list) or not all(isinstance(ref, str) for ref in evidence):
        raise ContextError("evidence must be a list of artifact references")
    for ref in evidence:
        store.artifact_metadata(ref)
    ledger = _rows(store, identifier)
    if status == "done" and row["kind"] == "task" and not (note or "").strip():
        raise ContextError("done needs a short verification note (what was checked, what was not)")
    superseded_by = op.get("superseded_by")
    if status in ("cancelled", "superseded") and (ledger or row["origin"] == "user"):
        sources = json.loads(row["sources"]) + [r["source"] for r in ledger]
        if not _later_user_source(store, op.get("user_authorization"), sources):
            raise ContextError("withdrawing a user request requires user_authorization: a later actual user message ref")
        evidence = [*evidence, op["user_authorization"]]
        if status == "superseded":
            target = _resolve(store, superseded_by) if superseded_by else None
            if not target or target == identifier:
                raise ContextError("superseded_by must name another card item")
            _item(store, target)
            first = _rows(store, target)
            superseded_by = first[0]["id"] if first else target
    if ledger and row["kind"] in ("task", "constraint"):
        _write_rows(store, identifier, status if row["kind"] == "task" or status != "active" else None, note, evidence, superseded_by)
    elif status is not None:
        store.db.execute("UPDATE card_items SET status=? WHERE scope=? AND id=?", (status, store.scope, identifier))
    if note is not None:
        store.db.execute("UPDATE card_items SET note=? WHERE scope=? AND id=?", (note, store.scope, identifier))
    if text is not None:
        store.db.execute("UPDATE card_items SET text=? WHERE scope=? AND id=?", (text.strip(), store.scope, identifier))
    if op.get("paths") is not None:
        _set_paths(store, identifier, op["paths"])
    _touch(store, identifier)
    return identifier


def _set_paths(store, identifier, paths):
    """Scope a standing constraint to workspace paths (globs); an empty list makes it global again."""
    from . import scopes
    row = _item(store, identifier)
    if row["kind"] != "constraint":
        raise ContextError("paths can only be set on a constraint")
    store.db.execute("UPDATE card_items SET tags=? WHERE scope=? AND id=?",
                     (scopes.tags_with(json.loads(row["tags"]), scopes.clean(paths)), store.scope, identifier))


def _merge(store, ids, into):
    target = _resolve(store, into)
    _item(store, target)
    merged = []
    for identifier in ids:
        identifier = _resolve(store, identifier)
        if identifier == target:
            continue
        row = _item(store, identifier)
        target_row = _item(store, target)
        if row["kind"] != target_row["kind"]:
            raise ContextError("merge requires items of the same kind; use move to reclassify first")
        store.db.execute("UPDATE requirements SET item=? WHERE scope=? AND item=?", (target, store.scope, identifier))
        sources = list(dict.fromkeys(json.loads(target_row["sources"]) + json.loads(row["sources"])))
        store.db.execute("UPDATE card_items SET sources=?,repeats=repeats+? WHERE scope=? AND id=?",
                         (canonical(sources), row["repeats"], store.scope, target))
        store.db.execute("UPDATE card_items SET merged_into=? WHERE scope=? AND id=?", (target, store.scope, identifier))
        merged.append(identifier)
    _touch(store, target)
    return target, merged


def _move(store, identifier, kind):
    identifier = _resolve(store, identifier)
    row = _item(store, identifier)
    if kind not in ("task", "constraint", "lesson") or kind == row["kind"]:
        raise ContextError("move requires a different kind: task, constraint or lesson")
    if row["kind"] == "goal":
        raise ContextError("the goal cannot be moved")
    new = _new_item(store, kind, row["text"], json.loads(row["sources"]), origin=row["origin"], tags=json.loads(row["tags"]),
                    note=row["note"])
    store.db.execute("UPDATE requirements SET item=? WHERE scope=? AND item=?", (new, store.scope, identifier))
    store.db.execute("UPDATE card_items SET merged_into=? WHERE scope=? AND id=?", (new, store.scope, identifier))
    return new


def _checkpoint(store, op):
    state = store.state()
    value = dict(state)
    for key in ("summary", "focus"):
        if key in op:
            if not isinstance(op[key], str) or len(op[key]) > 4000:
                raise ContextError(key + " must be a string of at most 4000 characters")
            value[key] = op[key]
    for key, limit in (("next_steps", 20), ("important_refs", 32)):
        if key in op:
            if not isinstance(op[key], list) or len(op[key]) > limit or not all(isinstance(x, str) for x in op[key]):
                raise ContextError(f"{key} must be a list of at most {limit} strings")
            value[key] = list(dict.fromkeys(op[key]))
    for ref in value.get("important_refs", []) if "important_refs" in op else []:
        if store.artifact_metadata(ref)["kind"] == "user":
            raise ContextError("original requests are already on the card; pin material references instead")
    if value != state:
        value["checkpoint_revision"] = _revision(store)  # a later change of direction makes it stale (render)
        store.db.execute("UPDATE scopes SET state=? WHERE scope=?", (canonical(value), store.scope))
        return True
    return False


def apply(store, ops):
    """Apply a batch of card operations atomically; one call per turn is the norm."""
    if not isinstance(ops, list) or not ops or len(ops) > 64 or not all(isinstance(op, dict) for op in ops):
        raise ContextError("ops must be a non-empty list of at most 64 operation objects")
    ensure_synced(store)
    changed = []
    with store.transaction():
        before = _revision(store)
        with tracked(store):
            for index, op in enumerate(ops, 1):
                try:
                    _apply_one(store, _with_note(op), changed)
                except ContextError as exc:
                    label = " ".join(str(v) for v in (op.get("op"), op.get("kind") or op.get("id") or op.get("ids")) if v)
                    raise ContextError(f"op {index} ({label}) failed: {exc}. Nothing in this call was applied; fix that op "
                                       "and send the whole ops list again") from None
        if changed and _revision(store) == before:
            store.bump()
    return {"changed": list(dict.fromkeys(changed)), "revision": _revision(store)}


NOTE_ALIASES = ("summary", "verification", "verification_note")


def _with_note(op):
    """Models often put the verification text under summary; treat it as the note."""
    if op.get("op") in ("add", "set") and not isinstance(op.get("note"), str):
        alias = next((op[k] for k in NOTE_ALIASES if isinstance(op.get(k), str) and op[k].strip()), None)
        if alias is not None:
            return {**op, "note": alias}
    return op


def _apply_one(store, op, changed):
    """One card operation; apply() makes the batch atomic."""
    kind = op.get("op")
    if kind == "goal":
        text = op.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ContextError("goal text must be a non-empty string of at most 2000 characters")
        for old in store.db.execute("SELECT id FROM card_items WHERE scope=? AND kind='goal' AND status='active'", (store.scope,)):
            store.db.execute("UPDATE card_items SET status='superseded' WHERE scope=? AND id=?", (store.scope, old["id"]))
        changed.append(_new_item(store, "goal", text.strip(), op.get("sources") or [], origin="model"))
    elif kind == "add":
        item_kind, text = op.get("kind"), op.get("text")
        if item_kind not in ("task", "constraint", "lesson"):
            raise ContextError("add requires kind task, constraint or lesson")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ContextError("text must be a non-empty string of at most 4000 characters")
        sources = op.get("sources") or []
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            raise ContextError("sources must be a list of artifact references")
        for ref in sources:
            store.artifact_metadata(ref)
        identifier = _new_item(store, item_kind, text.strip(), sources, origin="model", note=op.get("note") or "")
        if op.get("paths") is not None:
            _set_paths(store, identifier, op["paths"])
        if op.get("status"):
            _set(store, {"id": identifier, "status": op["status"], "note": op.get("note")})
        changed.append(identifier)
    elif kind == "set":
        targets = op.get("ids") if op.get("ids") is not None else [op.get("id")]
        if not isinstance(targets, list) or not targets or len(targets) > 500 or not all(isinstance(t, str) for t in targets):
            raise ContextError("set requires id or ids (at most 500)")
        for target in targets:
            changed.append(_set(store, {**op, "id": target}))
    elif kind == "merge":
        ids = op.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise ContextError("merge requires ids")
        target, merged = _merge(store, ids, op.get("into"))
        changed.extend([target, *merged])
    elif kind == "move":
        changed.append(_move(store, op.get("id"), op.get("kind")))
    elif kind == "checkpoint":
        if _checkpoint(store, op):
            changed.append("checkpoint")
    elif kind in ("protect", "unprotect"):
        from . import protected
        if kind == "protect":
            protected.protect(store, op.get("paths"), op.get("note") or "")
        else:
            protected.unprotect(store, op.get("paths"))
        changed.append(kind)
    else:
        raise ContextError("unknown card op; use goal, add, set, merge, move, checkpoint, protect or unprotect")


def sync_after_ledger_update(store, requirement_id):
    """Legacy update_requirement already wrote the row; just mark its item touched."""
    row = store.db.execute("SELECT item FROM requirements WHERE scope=? AND id=?", (store.scope, requirement_id)).fetchone()
    if row and row["item"]:
        _touch(store, row["item"])


# ---------------------------------------------------------------- rendering

def id_ranges(ids):
    """['R0001','R0002','R0004'] -> 'R0001–R0002, R0004'."""
    parsed = []
    for identifier in ids:
        match = re.fullmatch(r"([A-Z])(\d+)", identifier)
        if match:
            parsed.append((match[1], int(match[2]), len(match[2])))
    parsed.sort()
    parts, index = [], 0
    while index < len(parsed):
        prefix, start, width = parsed[index]
        end = start
        while index + 1 < len(parsed) and parsed[index + 1][0] == prefix and parsed[index + 1][1] == end + 1:
            index += 1
            end = parsed[index][1]
        fmt = lambda n: f"{prefix}{n:0{width}d}"
        parts.append(fmt(start) if start == end else f"{fmt(start)}–{fmt(end)}")
        index += 1
    return ", ".join(parts)


def _clip(text, limit):
    text = " ".join(text.split()) if limit and limit < 400 else text.strip()
    if limit is None or len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"


def _note(note):
    match = HISTORICAL_NOTE.fullmatch(note or "")
    return f"Historical source message {match[1]}; unverified, traceability only." if match else note


LEVELS = (
    {"text": 1200, "closed": "full", "closed_text": 240, "backlog_after": None, "active_after": None, "lessons": None, "rule_text": 1200, "notes": True, "evidence": True},
    {"text": 600, "closed": "line", "closed_text": 80, "backlog_after": 12, "backlog_text": 60, "active_after": 25, "lessons": 12, "rule_text": 400, "notes": True},
    {"text": 300, "closed": "ids", "backlog_after": 8, "backlog_text": 40, "active_after": 15, "lessons": 8, "rule_text": 240, "notes": True},
    {"text": 160, "closed": "ids", "backlog_after": 5, "backlog_text": 0, "active_after": 10, "lessons": 5, "rule_text": 160, "notes": False},
    {"text": 80, "closed": "ids", "backlog_after": 3, "backlog_text": 0, "active_after": 6, "lessons": 3, "rule_text": 160, "notes": False},
    # Standing constraints are the last to lose their words: tasks and lessons collapse to ids first.
    {"text": 0, "closed": "ids", "backlog_after": 0, "backlog_text": 0, "active_after": 3, "lessons": 0, "rule_text": 120, "notes": False},
    {"text": 0, "closed": "ids", "backlog_after": 0, "backlog_text": 0, "active_after": 3, "lessons": 0, "rule_text": 0, "notes": False},
)


def _line(item, limit, notes, evidence=False):
    # A task carries the UTC time its status last changed (the same form as change notes, to compare them).
    stamp = f" · {item['status_at']} UTC" if item["kind"] == "task" and item.get("status_at") else ""
    head = f"{item['id']} [{item['status']}{stamp}]"
    if item["tags"] and "correction" in item["tags"]:
        related = [t.split(":", 1)[1] for t in item["tags"] if t.startswith("relates:")]
        head += " (user correction" + (" re " + ", ".join(related) if related else "") + ")"
    if "redirect" in item["tags"]:
        head += " (user redirect)"
    if "drift" in item["tags"]:
        head += " (drift signal)"
    if item["repeats"]:
        head += f" (user repeated ×{item['repeats']})"
    if "plan-step" in item["tags"]:
        head += " (step of " + next(t[5:] for t in item["tags"] if t.startswith("plan:")) + ")"
    scoped = [t[5:] for t in item["tags"] if t.startswith("path:")]
    if scoped:
        head += " (applies to " + ", ".join(scoped[:6]) + (" …" if len(scoped) > 6 else "") + ")"
    text = _clip(item["text"], limit) if limit else ""
    refs = id_ranges(item["ledger"])
    line = head + (" " + text if text else "") + (f" ⟨{refs}⟩" if refs else "")
    if notes and item["note"]:
        line += "\n   Status/verification scope: " + _clip(_note(item["note"]), 400)
    if notes and item["superseded_by"]:
        line += "\n   Superseded by: " + ", ".join(dict.fromkeys(item["superseded_by"]))
    if evidence and item["evidence"]:
        line += "\n   Evidence: " + ", ".join(item["evidence"][:6])
    return line


def _archive_line(store):
    rows = store.db.execute("SELECT id,coalesce(class,'unclassified') AS class FROM requirements WHERE scope=?", (store.scope,)).fetchall()
    if not rows:
        return "No original user requests captured yet. Do not invent a historical requirement list."
    counts = {}
    for row in rows:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
    summary = " · ".join(f"{name} {count}" for name, count in sorted(counts.items(), key=lambda kv: -kv[1]))
    return (f"All {len(rows)} original request paragraphs are archived as {id_ranges([r['id'] for r in rows])} ({summary}). "
            "Acknowledgements, automation notices and duplicates are kept but are not instructions. "
            "Read exact words with context_state(action=status, view=ledger) or context_recall.")


def _compose(cfg, level, parts, backlog_shown):
    goal, state, rules, active, pending, lessons, closed, archive = parts
    out = ["[segment type=task-card relevance=mandatory-task-contract]",
           "Task card: structured state of every captured user request. Later explicit user changes and current system "
           "policy apply; never replay historical side effects. Ids in ⟨⟩ are archived original paragraphs."]
    out.append("Goal: " + (_clip(goal["text"], max(cfg["rule_text"], 200)) if goal else
                           "not set — set it with context_state(action=card, ops=[{op: goal, text: ...}])."))
    change, asked = state.get("direction"), state.get("review")
    if state.get("_drift"):
        signal = state["_drift"]
        out.append(f"Feedback signal at {signal['at']} UTC ({signal['item']}): {_drift_reason(signal)}. The work drifted from "
                   "what the user wants: find the root cause, record it as a lesson and re-plan before continuing.")
    if state.get("_undecided"):
        out.append(f"Change of direction: the user stopped the earlier work at {asked['at']} UTC ({asked['item']}). Still to "
                   "decide — cancel, supersede, or keep with a note on why it still applies: " + id_ranges(state["_undecided"]))
    if change and change.get("revision", 0) > state.get("checkpoint_revision", 0) and (
            state.get("focus") or state.get("next_steps") or state.get("summary")):
        out.append(f"The checkpoint below predates {direction_label(change)}; where it disagrees with the items, the items win.")
    if state.get("focus"):
        out.append("Current focus: " + _clip(state["focus"], 400))
    if state.get("next_steps"):
        out.append("Next steps:\n" + "\n".join("- " + _clip(step, 240) for step in state["next_steps"][:12]))
    if state.get("summary"):
        out.append("Progress summary: " + _clip(state["summary"], 1200 if level == 0 else 400 if level < 4 else 160))
    if rules:
        out.append("Standing constraints (always apply):")
        if cfg["rule_text"]:
            out.extend(_line(r, cfg["rule_text"], cfg["notes"]) for r in rules)
        else:
            out.append("  " + id_ranges([r["id"] for r in rules]) + " — read them before acting")
    if active:
        # Most recently touched first; a long tail (often legacy needs_verification rows) collapses to id ranges.
        cut = len(active) if cfg.get("active_after") is None else cfg["active_after"]
        out.append("In progress / needs verification / blocked:")
        out.extend(_line(i, cfg["text"] or 60, cfg["notes"], cfg.get("evidence")) for i in active[:cut])
        if active[cut:]:
            groups = {}
            for i in active[cut:]:
                groups.setdefault(i["status"], []).append(i["id"])
            out.append(f"  {len(active) - cut} more (older): " + "; ".join(f"{status} {id_ranges(ids)}" for status, ids in groups.items()))
    if pending:
        cut = len(pending) if cfg["backlog_after"] is None else cfg["backlog_after"]
        if pending[:cut]:
            out.append("Pending (newest first):")
            out.extend(_line(i, cfg["text"], cfg["notes"]) for i in pending[:cut])
        backlog = pending[cut:]
        if backlog:
            out.append(f"Older pending backlog ({len(backlog)}; may already be done — confirm, merge or close in bulk):")
            if backlog_shown:
                out.extend(f"  {i['id']} {_clip(i['text'], cfg.get('backlog_text') or 60)}" for i in backlog[:backlog_shown])
            if len(backlog) > backlog_shown:
                out.append("  " + ("older: " if backlog_shown else "") + id_ranges([i["id"] for i in backlog[backlog_shown:]]))
    if lessons:
        shown = lessons if cfg["lessons"] is None else lessons[:cfg["lessons"]]
        out.append("Lessons (root causes and rejected approaches; do not regress):")
        out.extend(_line(i, max(cfg["text"], 120), False) for i in shown)
        if len(shown) < len(lessons):
            out.append("  more lessons: " + id_ranges([i["id"] for i in lessons[len(shown):]]))
    if closed:
        if cfg["closed"] == "full":
            out.append("Closed:")
            out.extend(_line(i, cfg["closed_text"], True, True) for i in closed)
        elif cfg["closed"] == "line":
            out.append("Closed (recent first):")
            recent = sorted(closed, key=lambda i: -i["touched"])
            out.extend(_line(i, cfg["closed_text"], False) for i in recent[:10])
            if len(recent) > 10:
                out.append("  earlier closed: " + id_ranges([i["id"] for i in recent[10:]]))
        else:
            groups = {}
            for i in closed:
                groups.setdefault(i["status"], []).append(i["id"])
            out.append("Closed: " + "; ".join(f"{status} {id_ranges(ids)}" for status, ids in groups.items()))
    if state.get("important_refs"):
        out.append("Pinned source refs: " + ", ".join(state["important_refs"]))
    out.append(archive)
    out.append("[/segment]")
    return "\n".join(out)


def render(store, budget: int = DEFAULT_BUDGET):
    """Return (text, info). Never raises for size: degrades, then lists ids only.

    Within each level the older backlog is filled greedily, so a large history uses
    the budget for the newest snippets instead of collapsing everything to ids.
    """
    entries = items(store)
    state = store.state()
    state["_undecided"] = [i["id"] for i in _undecided(state.get("review"), entries)]
    state["_drift"] = drift(store, entries)
    goal = next((i for i in reversed(entries) if i["kind"] == "goal" and i["status"] == "active"), None)
    rules = [i for i in entries if i["kind"] == "constraint" and i["status"] == "active"]
    lessons = sorted((i for i in entries if i["kind"] == "lesson" and i["status"] == "active"), key=lambda i: -i["touched"])
    tasks = [i for i in entries if i["kind"] == "task"]
    active = sorted((i for i in tasks if i["status"] in OPEN[:3]), key=lambda i: (-i["touched"], -i["ordinal"]))
    pending = sorted((i for i in tasks if i["status"] == "pending"), key=lambda i: -i["ordinal"])
    closed = [i for i in entries if (i["kind"] == "task" and i["status"] in ("done", "cancelled", "superseded"))
              or (i["kind"] in ("constraint", "lesson") and i["status"] in ("cancelled", "superseded"))]
    parts = (goal, state, rules, active, pending, lessons, closed, _archive_line(store))
    fits = lambda text: token_measure(text)["count"] <= budget
    for level, cfg in enumerate(LEVELS):
        backlog = 0 if cfg["backlog_after"] is None else max(0, len(pending) - cfg["backlog_after"])
        text = _compose(cfg, level, parts, 0)
        if not fits(text):
            continue
        low, high = 0, backlog
        while low < high:  # largest number of backlog snippets that still fits
            middle = (low + high + 1) // 2
            if fits(_compose(cfg, level, parts, middle)):
                low = middle
            else:
                high = middle - 1
        if low:
            text = _compose(cfg, level, parts, low)
        return text, {"level": level, "tokens": token_measure(text)["count"], "items": len(entries), "backlog_snippets": low}
    # Last resort: even id lists exceed the budget. Keep the contract markers and the archive pointer.
    head = ["[segment type=task-card relevance=mandatory-task-contract]",
            "Task card exceeds its budget; read it with context_state(action=status).",
            "Open: " + id_ranges([i["id"] for i in active + pending + rules])]
    text = "\n".join(head + [parts[-1], "[/segment]"])
    if not fits(text):  # tiny budgets: keep the id ranges, drop the explanatory sentences
        text = "\n".join(head + [parts[-1].split(". ")[0].rstrip(".") + ".", "[/segment]"])
    return text, {"level": len(LEVELS), "tokens": token_measure(text)["count"], "items": len(entries)}


def constraint_guard(store, text: str, budget_chars: int = 6000) -> tuple[str, list[str]]:
    """A separate segment with every active standing constraint that `text` does not show in words
    (the card fell back to id ranges). Returns (segment, ids); ("", []) when nothing is missing."""
    rules = [i for i in items(store) if i["kind"] == "constraint" and i["status"] == "active"]
    missing = [r for r in rules if not re.search(rf"(?m)^{re.escape(r['id'])} \[active\]", text)]
    if not missing:
        return "", []
    lines, used = [], 0
    for index, rule in enumerate(missing):
        line = _line(rule, 200, False)
        if used + len(line) > budget_chars:
            lines.append("more (read them with context_recall): " + id_ranges([r["id"] for r in missing[index:]]))
            break
        lines.append(line)
        used += len(line)
    segment = ("[segment type=standing-constraints relevance=mandatory]\nStanding constraints (always apply; the task card "
               "above could only list them by id):\n" + "\n".join(lines) + "\n[/segment]")
    return segment, [r["id"] for r in missing]


def _turn_items(store, turn_id) -> list[str]:
    """Card items the turn's user messages created or reopened (merged items resolved)."""
    row = store.db.execute("SELECT source_key FROM hook_turns WHERE scope=? AND turn_id=?", (store.scope, turn_id)).fetchone() \
        if turn_id else None
    if row is None:
        return []
    sources = [r["artifact"] for r in store.db.execute(
        "SELECT artifact FROM events WHERE scope=? AND kind='user' AND (source_key=? OR source_key LIKE ?)",
        (store.scope, row["source_key"], row["source_key"] + ":%"))]
    found = [r["item"] for source in sources for r in store.db.execute(
        "SELECT item FROM requirements WHERE scope=? AND source=? AND item IS NOT NULL", (store.scope, source))]
    return list(dict.fromkeys(_resolve(store, item) for item in found))


def start_turn_tasks(store, turn_id) -> list[dict]:
    """The turn started working (its first tool call): its pending tasks become in_progress. Only that
    objective step is automatic; done, blocked and the rest stay the model's statement with evidence."""
    mine = _turn_items(store, turn_id)
    current = {item["id"]: item for item in items(store)}
    # A plan's steps wait for the model to reach them; only the plan's own task starts with the work.
    pending = [i for i in mine if current.get(i, {}).get("kind") == "task" and current[i]["status"] == "pending"
               and "plan-step" not in current[i]["tags"]]
    if pending:
        apply(store, [{"op": "set", "id": identifier, "status": "in_progress"} for identifier in pending])
    return [{**current[i], "status": "in_progress"} for i in pending]


def stale_turn_tasks(store, turn_id) -> list[dict]:
    """Open tasks to account for when a turn did work but the model never updated the card in it:
    every task in progress and this turn's other open tasks. Empty when the card was updated, the
    turn did no work, or nothing is open."""
    row = store.db.execute("SELECT worked,carded FROM hook_turns WHERE scope=? AND turn_id=?", (store.scope, turn_id)).fetchone() \
        if turn_id else None
    if row is None or not row["worked"] or row["carded"]:
        return []
    mine = set(_turn_items(store, turn_id))
    return [item for item in items(store) if item["kind"] == "task"
            and (item["status"] == "in_progress" or (item["id"] in mine and item["status"] in OPEN
                                                     and not (item["status"] == "pending" and "plan-step" in item["tags"])))][:6]


def followups(store, turn_id, source) -> tuple[str, list[str]]:
    """Asked with the user's next message, once each, never by holding the finished turn: the statuses the last
    turn that did work left unset, and what a redirect still leaves undecided. Returns (note, item ids)."""
    notes, ids = [], []
    previous = store.db.execute("SELECT turn_id FROM hook_turns WHERE scope=? AND turn_id<>? AND closed IS NOT NULL AND worked=1 "
                                "AND carded=0 AND followed_up=0 ORDER BY closed DESC LIMIT 1", (store.scope, turn_id or "")).fetchone()
    if previous:
        store.db.execute("UPDATE hook_turns SET followed_up=1 WHERE scope=? AND turn_id=?", (store.scope, previous["turn_id"]))
        stale = [t for t in stale_turn_tasks(store, previous["turn_id"]) if t["status"] in OPEN]
        if stale:
            notes.append("The last turn worked on " + ", ".join(f"{t['id']} ({t['status'].replace('_', ' ')})" for t in stale)
                         + " without updating the task card: set their statuses in this turn's context_state(action=\"card\") "
                         "call; done needs a short verification note.")
            ids += [t["id"] for t in stale]
    asked = review(store)
    if asked and asked["source"] != source and not asked.get("held"):
        entries = undecided(store)
        if entries:
            state = store.state()
            state["review"]["held"] = True
            store.db.execute("UPDATE scopes SET state=? WHERE scope=?", (canonical(state), store.scope))
            notes.append(f"Still open since the user's redirect ({asked['item']}): {_decide(asked, entries)}")
            ids += [i["id"] for i in entries if i["id"] not in ids]
    signal = drift(store)
    if signal and signal["source"] != source and not signal.get("reminded"):
        state = store.state()
        state["drift"]["reminded"] = True
        store.db.execute("UPDATE scopes SET state=? WHERE scope=?", (canonical(state), store.scope))
        notes.append(f"No root cause recorded yet for the user's feedback ({_drift_reason(signal)}): record it as a lesson "
                     "and re-plan before going on with the earlier approach.")
    return " ".join(notes), ids


def describe(store, ids):
    """Rendered lines for just the touched items, so a card update stays cheap."""
    wanted = set(ids)
    return [_line(item, 600, True) for item in items(store, include_merged=True) if item["id"] in wanted]
