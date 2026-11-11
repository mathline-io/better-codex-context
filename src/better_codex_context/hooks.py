"""Native Codex hook handlers: record every user message and tool result, restore the task card
after resets, and end large finished turns with a zero-model reset."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

from . import (activity, card, changes, evidence, instructions, loops, native_budget, plans, protected, scopes, settings, shadow,
               sidecar, skills, supervisor, workset)
from .card import id_ranges
from .materials import is_context_tool, record_tool
from .requirements import hook_prompt, record_answers
from .store import Store, user_state_home
from .util import ContextError, atomic_write, canonical, digest


def session_context(store: Store, source: str, payload: dict | None = None) -> str:
    """What a fresh window gets: the shadow context (after a replacement) and the task card."""
    return _session_parts(store, source, payload or {})["text"]


def _restore_shadow(store: Store, source: str, payload: dict, config: dict, resident: dict | None = None) -> dict | None:
    """The shadow block for this SessionStart, or None when the card alone is right.

    Only a replacement (source=compact) gets the conversation back: on resume or /clear Codex has
    already rebuilt the window from its log (which holds the earlier injection), so restoring the
    body again would duplicate it, unless the log shows a reset that never got its shadow.
    Without TokenBudget, Codex's own summary is in the window and the body would duplicate it.
    """
    if config["shadow_mode"] != "full" or not native_budget.active():
        return None
    transcript = payload.get("transcript_path")
    shadow.sync(store, transcript, time.monotonic() + SYNC_SECONDS_AT_RESTORE)
    if source != "compact" and not (source in ("resume", "startup") and shadow.needs_reinject(store)):
        return None
    usage = native_budget.last_usage(transcript) or {}
    window = usage.get("window")
    threshold = native_budget.idle_threshold(native_budget.read_config().get("model_auto_compact_token_limit"), window)
    # The resident skills are never shortened: the shadow gives way by their size instead.
    rendered = shadow.render(store, window, threshold or config["idle_reset_tokens"],
                             reserve=shadow.tokens(resident["text"]) if resident else 0, resident=resident["names"] if resident else ())
    if rendered:
        evidence.mark_restore(store)  # summaries finishing after this are delivered late (_late_evidence)
    return rendered


# Repeated at the bottom of every replaced window, the last thing the model reads before it acts (only
# the line saying what to do right now follows it): a fresh window has lost the investigation that led
# to the current code, which is when quick surface fixes pile up. The skill carries the same rule at
# its top.
CODING_RULES = ("ATTENTION! ATTENTION! ATTENTION! Before writing any code, always analyze the problem in depth: find its "
                "root cause and the path along which it arises. Do not be misled by surface symptoms or by the code where "
                "they show up, and never keep piling patches onto surface-level problems instead of tracking down the root "
                "cause. Replace rather than append: before you add code, look for inefficient, redundant or piled-up code "
                "(earlier patches, duplicates, dead paths) that the change can replace, delete or consolidate, keeping the "
                "behaviour that is still needed; a good fix often removes more lines than it adds, so do not keep growing "
                "the line count. "
                "Then choose lean, efficient algorithms, frameworks and solutions: the most economical design that "
                "fully removes the root cause, without needless dependencies and without trading away correctness or "
                "robustness. Before writing an algorithm, above all heavy computation or complex decision logic, look "
                "for the best known one that cuts CPU and memory (a better complexity class, the right data structure, "
                "streaming, caching, early exit), with the hardware in mind. While a test, download, build or long "
                "computation runs, do not wait idle: go on with other high-value work that does not depend on it. "
                "Keep the abstraction layers consistent: when the change can go through an existing "
                "abstraction layer, plug into it instead of building a separate branch path beside it. Only the cases "
                "that can happen: unless the user asks for it, do not write branches for cases that can never occur, or "
                "fallback and safety-net designs for them; handle the failures that really can occur (input, I/O, "
                "external calls) and let an impossible state fail loudly instead of quietly covering it. First things "
                "first: until the core functionality and its framework work, do not spend significant time on minor "
                "issues; build the big parts first, then small details, minor bugs and low-impact tasks. Frontend and "
                "backend: when a backend feature interacts with the frontend UI, test the real user interface after the "
                "backend work in Chrome through CDP (install the latest Chrome if it is missing; Playwright only when CDP "
                "cannot be used), checking the page result, console errors and failed requests, "
                "together with a sub-agent when you can start one and yourself otherwise; until that UI test passes, the "
                "task is not accepted as done. Resources: before a test run check the load, memory and disk, stop orphans "
                "your earlier runs left, and below 5% free disk clean up first (tmp-registry.jsonl, then what is safe to "
                "delete); read the CPU topology from the system, never guess; bind tests to one thread per physical core (never both hyper-threads of a core), at most 60% "
                "of the cores when all are idle, idle cores only and at least one left free when some are busy; never "
                "use every core for any task, stay within physical memory and out of swap, and keep test code "
                "CPU- and memory-friendly; before creating anything that takes disk space, add one JSON line for it to "
                "tmp-registry.jsonl with path, creator, the code that uses it, purpose, expiry (UTC) and deletion "
                "condition. The full "
                "engineering discipline is in the "
                "context-discipline skill.")


def _session_parts(store: Store, source: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    budgets = settings.load()
    # Resident skills in every new window (not on resume: Codex rebuilds that window from its log, which holds them).
    resident = skills.segment(store.root) if source != "resume" else None
    skill = resident["text"] if resident else ""
    try:
        overflow = instructions.overflow(store, budget=budgets["instructions_budget"])
    except (OSError, ValueError, ContextError):
        overflow = None  # the task card is restored even if the instruction files cannot be read
    try:
        restored = _restore_shadow(store, source, payload, budgets, resident)
    except Exception as exc:  # noqa: BLE001 - a broken shadow must never leave the new window empty
        restored = None
        _log_problem("shadow-restore", exc, payload)
    has_state = store.db.execute("SELECT 1 FROM requirements WHERE scope=? LIMIT 1", (store.scope,)).fetchone() \
        or store.db.execute("SELECT 1 FROM card_items WHERE scope=? LIMIT 1", (store.scope,)).fetchone() or store.state()
    if not has_state and not restored:
        return {"text": "\n".join(part for part in (skill, overflow["text"] if overflow else "") if part), "card": None,
                "work": None, "current": "", "overflow": overflow, "has_state": False, "shadow": None,
                "resident_skills": resident["names"] if resident else []}
    text, card_info = card.render(store, budgets["card_budget"])
    work, work_info = workset.render(store, budgets["workset_budget"]) if budgets["workset_budget"] else ("", None)
    reason = {"compact": "after a context reset", "resume": "on resume", "clear": "after /clear"}.get(source, "at session start")
    hint = (f"Better Codex Context restored this task state {reason}. Obey every standing constraint. Files marked unchanged need no "
            "re-reading for conclusions already recorded. Update the card once per turn with context_state(action=card, ops=[...]); "
            "read exact archived originals with context_recall.")
    current = in_progress_request(store) if source == "compact" else ""
    if current and restored:
        # The request is already in the restored conversation; say only that it is unfinished.
        current = "The context was replaced in the middle of the latest request in the conversation above; continue it."
    answered = ""
    if source == "compact" and not current and answered_before_reset(store):
        answered = ("The previous turn was finished before this reset, which only keeps later requests fast. Right now reply "
                    f"with exactly this one line, in English whatever language the conversation uses: {REFRESHED} Do not "
                    "repeat, summarize or continue earlier work. Handle the next user message normally.")
    # Most stable first, most volatile last. Codex puts the new window's id before any hook injection,
    # so this does not carry the cache across a replacement; it keeps the order predictable.
    # Integrity guard: every active standing constraint reaches the new window in words, even when
    # the card had to fall back to id ranges.
    guard, guarded = card.constraint_guard(store, text)
    try:
        references, docs = protected.render(store)
    except (OSError, ContextError) as exc:
        references, docs = "", []
        _log_problem("protected-references", exc, payload)
    notes, notes_info = changes.render(store, budgets["change_notes_budget"]) if budgets["change_notes"] != "off" else ("", None)
    plan, plan_info = plans.render(store, budgets["plan_budget"])
    parts = (skill, hint, overflow["text"] if overflow else "", restored["text"] if restored else "", text, guard, plan, notes,
             references, work, CODING_RULES if source == "compact" else "", current or answered)
    return {"text": "\n".join(part for part in parts if part), "card": card_info, "work": work_info, "current": current,
            "overflow": overflow, "has_state": True, "shadow": restored, "constraints_guarded": guarded, "protected": docs,
            "change_notes": notes_info, "plan": plan_info, "resident_skills": resident["names"] if resident else []}


def interface_warnings(source: str, payload: dict) -> list[str]:
    """Signs that Codex's experimental interfaces this plugin depends on changed underneath it."""
    from .diagnostics import untested_newer
    warnings = []
    transcript = payload.get("transcript_path")
    if source == "compact" and native_budget.last_reset_kind(transcript) == "model-summary":
        warnings.append("Codex replaced the window with its own model-written summary, not the zero-model reset; "
                        "run `bcc doctor` (features.token_budget may be off, or this Codex version changed it)")
    if source == "startup":
        version = None
        try:
            with open(transcript, "rb") as handle:
                first = json.loads(handle.readline(8 * 1024 * 1024) or b"null")
            version = (first.get("payload") or {}).get("cli_version") if isinstance(first, dict) else None
        except (OSError, ValueError, TypeError):
            pass
        if untested_newer(version):
            warnings.append(f"Codex {version} has not been verified with Better Codex Context yet; if replacements "
                            "misbehave, run python3 scripts/e2e_smoke.py from the plugin checkout")
    return warnings


def _log_problem(kind: str, exc: BaseException, payload: dict):
    from .hookrun import _write, _location
    import traceback
    _write({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": kind, "error_type": type(exc).__name__, "error": str(exc)[:500],
            "location": _location(exc), "session_id": payload.get("session_id"), "event": payload.get("hook_event_name"),
            "traceback": "".join(traceback.format_exception(exc))})


# The one line the model answers after an end-of-turn replacement; the terminal stays in English.
REFRESHED = "Context refreshed."
SYNC_SECONDS_AT_RESTORE = 1.5
SYNC_SECONDS = 0.6


CURRENT_REQUEST_LIMIT = 4000


def in_progress_request(store: Store) -> str:
    """The user request of a turn that a mid-turn reset cut off; native resets drop it entirely."""
    row = store.db.execute("SELECT turn_id,source_key FROM hook_turns WHERE scope=? ORDER BY opened DESC LIMIT 1",
                           (store.scope,)).fetchone()
    if row is None:
        return ""
    state = store.db.execute("SELECT closed,worked FROM hook_turns WHERE scope=? AND turn_id=?", (store.scope, row["turn_id"])).fetchone()
    if state["closed"] or not state["worked"]:
        return ""  # finished turns are on the card; a reset before any work keeps the native user message
    events = store.db.execute("SELECT artifact FROM events WHERE scope=? AND kind='user' AND (source_key=? OR source_key LIKE ?) "
                              "ORDER BY seq", (store.scope, row["source_key"], row["source_key"] + ":%")).fetchall() \
        or store.db.execute("SELECT artifact FROM events WHERE scope=? AND kind='user' AND source_key LIKE ? ORDER BY seq DESC LIMIT 1",
                            (store.scope, "%" + row["turn_id"] + "%")).fetchall()
    if not events:
        return ""
    texts = [store.artifact(e["artifact"])[1].decode("utf-8", errors="replace") for e in events]
    text = "\n\n[later message in the same request]\n".join(texts)
    ids = [r["id"] for e in events for r in store.db.execute(
        "SELECT id FROM requirements WHERE scope=? AND source=? ORDER BY CAST(substr(id,2) AS INTEGER)", (store.scope, e["artifact"]))]
    clipped = text if len(text) <= CURRENT_REQUEST_LIMIT else (text[:CURRENT_REQUEST_LIMIT] + "…(full text: context_recall "
                                                               + ", ".join(e["artifact"] for e in events) + ")")
    return ("[segment type=current-request relevance=in-progress]\nThe context was reset in the middle of this user request; "
            "continue it from the card checkpoint" + (f" ⟨{id_ranges(ids)}⟩" if ids else "") + ":\n" + clipped + "\n[/segment]")


IDLE_RETRY_GROWTH = 50000  # after an ignored reminder, ask again only once the window grew this much
IDLE_MAX_REMINDERS = 2     # consecutive reminders without a reset before giving up (the native reset takes over)


def idle_reset_reason(payload: dict, store: Store | None = None) -> str | None:
    """Block a finished turn once the window is large, so the next request starts in a fresh one."""
    due = idle_reset_due(payload, store)
    return native_budget.IDLE_REASON.format(tokens=f"{due['tokens'] // 1000}k") if due else None


def idle_reset_due(payload: dict, store: Store | None = None) -> dict | None:
    if payload.get("stop_hook_active") or payload.get("agent_id"):
        return None  # never loop, and never reset a sub-agent's window from its own Stop
    config = native_budget.read_config()
    if not native_budget.active(config):
        return None  # without TokenBudget there is no zero-model new_context tool
    limit = config.get("model_auto_compact_token_limit")
    usage = native_budget.last_usage(payload.get("transcript_path"))
    threshold = native_budget.idle_threshold(limit, usage.get("window") if usage else None)
    if threshold is None or usage is None or usage["tokens"] < threshold:
        return None
    if isinstance(limit, int) and usage["tokens"] >= limit:
        return None  # the native pre-turn reset already happens before the next request, at no model cost
    if store is not None:
        row = store.db.execute("SELECT * FROM idle_resets WHERE scope=?", (store.scope,)).fetchone()
        if row and row["blocked_at"] > row["last_reset"]:  # the last reminder was not followed by a reset
            if row["ignored"] + 1 >= IDLE_MAX_REMINDERS or usage["tokens"] < row["blocked_tokens"] + IDLE_RETRY_GROWTH:
                return None
    return {"tokens": usage["tokens"], "threshold": threshold}


def note_idle_block(store: Store, tokens: int):
    row = store.db.execute("SELECT * FROM idle_resets WHERE scope=?", (store.scope,)).fetchone()
    ignored = row["ignored"] + 1 if row and row["blocked_at"] > row["last_reset"] else 0
    store.db.execute("INSERT INTO idle_resets(scope,blocked_at,blocked_tokens,ignored) VALUES(?,?,?,?) ON CONFLICT(scope) DO UPDATE "
                     "SET blocked_at=excluded.blocked_at,blocked_tokens=excluded.blocked_tokens,ignored=excluded.ignored",
                     (store.scope, time.time(), tokens, ignored))


def note_reset(store: Store):
    store.db.execute("INSERT INTO idle_resets(scope,last_reset,ignored) VALUES(?,?,0) ON CONFLICT(scope) DO UPDATE "
                     "SET last_reset=excluded.last_reset,ignored=0", (store.scope, time.time()))


def mark_idle_reset(store: Store, turn_id: str | None):
    if turn_id:
        store.db.execute("UPDATE hook_turns SET idle_reset=1 WHERE scope=? AND turn_id=?", (store.scope, turn_id))


def answered_before_reset(store: Store) -> bool:
    row = store.db.execute("SELECT closed,idle_reset FROM hook_turns WHERE scope=? ORDER BY opened DESC LIMIT 1", (store.scope,)).fetchone()
    return bool(row and row["closed"] and row["idle_reset"])


def open_turn(store: Store, turn_id: str, key: str):
    # The first message of a turn keeps the turn's key; later messages share its prefix.
    store.db.execute("INSERT INTO hook_turns(scope,turn_id,source_key,opened,closed) VALUES(?,?,?,?,NULL) "
                     "ON CONFLICT(scope,turn_id) DO NOTHING", (store.scope, turn_id, key, time.time()))


def mark_worked(store: Store, turn_id: str | None) -> bool:
    """Record that the turn did work; True on its first tool call."""
    if not turn_id:
        return False
    return store.db.execute("UPDATE hook_turns SET worked=1 WHERE scope=? AND turn_id=? AND closed IS NULL AND worked=0",
                            (store.scope, turn_id)).rowcount > 0


def close_turn(store: Store, turn_id: str | None):
    if turn_id:
        store.db.execute("UPDATE hook_turns SET closed=? WHERE scope=? AND turn_id=? AND closed IS NULL", (time.time(), store.scope, turn_id))


def prompt_note(store: Store, key: str) -> str:
    """One short line when the new message repeats, corrects or adds a standing rule."""
    row = store.db.execute("SELECT artifact FROM events WHERE scope=? AND source_key=?", (store.scope, key)).fetchone()
    if row is None:
        return ""
    rows = store.db.execute("SELECT class,item FROM requirements WHERE scope=? AND source=?", (store.scope, row["artifact"])).fetchall()
    by_class = {}
    for r in rows:
        if r["item"]:
            by_class.setdefault(r["class"], []).append(r["item"])
    notes = []
    if by_class.get("duplicate"):
        ids = ", ".join(dict.fromkeys(by_class["duplicate"]))
        notes.append(f"The user repeated an earlier request ({ids}). Treat it as not yet satisfied and re-check it before replying.")
    if by_class.get("redirect"):
        notes.append(card.redirect_note(store))
    signal = card.drift(store)
    drifted = bool(signal and signal["source"] == row["artifact"])
    if drifted:
        notes.append(card.drift_note(store))
    corrected = by_class.get("correction")
    if corrected and not drifted:
        tags = store.db.execute("SELECT tags FROM card_items WHERE scope=? AND id=?", (store.scope, corrected[0])).fetchone()
        related = [t.split(":", 1)[1] for t in json.loads(tags["tags"]) if t.startswith("relates:")] if tags else []
        notes.append(f"This is a user correction ({corrected[0]}" + (f", likely about {', '.join(related)}" if related else "")
                     + "). Re-check that work; once fixed, record the root cause as a lesson on the task card.")
    if by_class.get("constraint"):
        ids = ", ".join(dict.fromkeys(by_class["constraint"]))
        notes.append(f"Recorded as standing constraint {ids}; it applies to all later work in this task.")
    return ("[task card] " + " ".join(notes)) if notes else ""


HOOK_BUSY_TIMEOUT = 2.5  # below the hook deadline, so a locked database is reported instead of killed


def _registry(session: str) -> Path:
    return user_state_home() / "sessions" / digest(session)[:32]


def _usable(path) -> bool:
    return isinstance(path, str) and bool(path) and os.path.isdir(path)


def _transcript_cwd(transcript) -> str | None:
    """The session root Codex recorded in the rollout's first line (session_meta)."""
    if not isinstance(transcript, str) or not transcript:
        return None
    try:
        with open(transcript, "rb") as handle:
            first = json.loads(handle.readline(8 * 1024 * 1024) or b"null")
    except (OSError, ValueError):
        return None
    meta = first.get("payload") if isinstance(first, dict) and first.get("type") == "session_meta" else None
    return meta.get("cwd") if isinstance(meta, dict) else None


def resolve_root(payload: dict) -> str:
    """The session's workspace root for this hook event.

    PostToolUse reports the tool call's working directory as `cwd` (verified on Codex 0.167),
    not the session root; using it scattered one session's state over every directory a command
    ran in, and failed outright in unwritable or deleted directories. Every other event carries
    the session cwd, which is remembered per session for the PostToolUse lookups.
    """
    cwd, session = payload.get("cwd"), payload.get("session_id")
    if payload.get("hook_event_name") != "PostToolUse" and _usable(cwd):
        if isinstance(session, str) and session:
            record = _registry(session)
            try:
                if not record.exists() or record.read_text() != cwd:
                    record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    atomic_write(record, cwd.encode())
            except (OSError, ContextError):
                pass  # the transcript fallback still finds the root
        return cwd
    if isinstance(session, str) and session:
        try:
            remembered = _registry(session).read_text()
        except OSError:
            remembered = None
        if _usable(remembered):
            return remembered
    recorded = _transcript_cwd(payload.get("transcript_path"))
    if _usable(recorded):
        return recorded
    if _usable(cwd):
        return cwd
    raise FileNotFoundError(f"no usable session root: tool cwd {cwd!r} is gone and the session root is unknown")


def process(payload: dict, root: str | None = None) -> dict:
    """Handle one native hook event. Hooks record and restore; they never rewrite history."""
    if os.environ.get("BCC_SIDECAR_RUN") == "1":
        return {}  # a sidecar compression run never records or restores anything
    project = root or resolve_root(payload)
    store = Store(project, payload.get("session_id") or "default", payload.get("agent_id") or "main", timeout=HOOK_BUSY_TIMEOUT)
    try:
        activity.use_host()  # plain text when the VS Code extension's Codex runs this hook
        output, log = _handle(store, payload, activity.level())
        if log:
            output["systemMessage"] = log
        return output
    finally:
        store.close()


def _handle(store: Store, payload: dict, show: str) -> tuple[dict, str | None]:
    """Returns the hook output and an optional activity line for the Codex UI."""
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        turn = payload.get("turn_id")
        key = "user:" + (turn or "unidentified:" + uuid.uuid4().hex)
        if turn:
            open_turn(store, turn, key)
        key = hook_prompt(store, payload.get("prompt", ""), key)
        note = prompt_note(store, key)
        try:  # once per session, while the supervisor is off: offer it on an engineering project or a complex request
            offer = supervisor.offer_note(store, str(payload.get("prompt") or ""))
            note = "\n".join(part for part in (note, offer) if part)
        except Exception as exc:  # noqa: BLE001
            _log_problem("supervisor-offer", exc, payload)
        asked = []
        if settings.load()["card_updates"] != "off" and not payload.get("agent_id"):
            try:  # what the last turn left unset, asked now: a finished turn is never held for it
                row = store.db.execute("SELECT artifact FROM events WHERE scope=? AND source_key=?", (store.scope, key)).fetchone()
                followup, asked = card.followups(store, turn, row["artifact"] if row else None)
                note = "\n".join(part for part in (note, "[task card] " + followup if followup else "") if part)
            except Exception as exc:  # noqa: BLE001
                _log_problem("card-followups", exc, payload)
        try:
            reread, names = protected.prompt_note(store, payload.get("prompt", ""))
        except (OSError, ContextError) as exc:
            reread, names = "", []
            _log_problem("protected-references", exc, payload)
        late, delivered = _late_evidence(store, payload)
        watched, overseen = _supervisor_findings(store, payload, show)
        context = "\n".join(part for part in (note, reread, watched, late) if part)
        output = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}} if context else {}
        return output, _join(_activity(show, activity.prompt, store, key), _activity(show, activity.direction, store, key),
                             _activity(show, activity.drift_signal, store, key),
                             _activity(show, activity.followups, asked) if asked else None,
                             _activity(show, activity.reread, names) if names else None,
                             _activity(show, activity.evidence_late, delivered) if delivered else None, *overseen,
                             _background_reports(store, show))
    if event == "SessionStart":
        # Fires on startup, resume and after every reset (source="compact"). The reset itself
        # drops earlier injections, so the card never accumulates in the window.
        source = str(payload.get("source") or "startup")
        if source == "compact":
            note_reset(store)
        parts = _session_parts(store, source, payload)
        try:
            warnings = [_activity(show, activity.warning, w) for w in interface_warnings(source, payload)]
        except Exception as exc:  # noqa: BLE001 - a diagnostic never breaks the restore
            warnings = []
            _log_problem("interface-check", exc, payload)
        if not parts["text"]:
            return {}, _join(*warnings)
        output = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": parts["text"]}}
        return output, _join(_activity(show, activity.restored, store, source, parts), *warnings)
    if event == "PostToolUse":
        started = []
        # The plugin's own tools (card, recall) are bookkeeping, not work: they neither start tasks nor make
        # the turn one that must account for its task statuses.
        if not is_context_tool(str(payload.get("tool_name", ""))) and mark_worked(store, payload.get("turn_id")):
            try:  # work begins: this turn's pending tasks become in_progress (a fact, not the model's judgement)
                started = card.start_turn_tasks(store, payload.get("turn_id"))
            except Exception as exc:  # noqa: BLE001 - the card stays as it was
                _log_problem("task-start", exc, payload)
        recorded = record_hook_tool(store, payload)
        tool_input_raw = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        if recorded == "card" and tool_input_raw.get("action") == "card":
            store.db.execute("UPDATE hook_turns SET carded=1 WHERE scope=? AND turn_id=?", (store.scope, payload.get("turn_id")))
        known = changes.count(store)
        maintenance = _shadow_maintenance(store, payload, show)
        # Notes Codex wrote since the last tool call (the sync just read them from the session log):
        # announced now, while the turn runs, not only when it ends.
        fresh = changes.notes(store)[:changes.count(store) - known]
        noted = _join(_activity(show, activity.tasks_started, started) if started else None,
                      _activity(show, activity.change_notes_recorded, fresh[::-1]) if fresh else None)
        if payload.get("tool_name") == "request_user_input":  # plan-mode questions: the answers are decisions
            try:
                decided = record_answers(store, payload.get("tool_use_id"), payload.get("tool_input"), payload.get("tool_response"))
            except Exception as exc:  # noqa: BLE001 - the answer itself is already archived
                decided = []
                _log_problem("plan-answers", exc, payload)
            noted = _join(noted, _activity(show, activity.decisions_recorded, decided) if decided else None)
        if recorded == "card":
            action = (payload.get("tool_input") or {}).get("action", "card") if isinstance(payload.get("tool_input"), dict) else "card"
            return {}, _join(noted, _activity(show, activity.card_update, store, payload.get("tool_response"), action), maintenance)
        event = recorded if isinstance(recorded, dict) else None
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        command = str(tool_input.get("command", tool_input.get("cmd", tool_input.get("input", ""))))
        tool_name = str(payload.get("tool_name", ""))
        try:
            loop = loops.check(store, event, settings.load()["loop_guard"]) if event else None
        except Exception as exc:  # noqa: BLE001 - a detector error never interrupts the work
            loop = None
            _log_problem("loop-guard", exc, payload)
        hits = []
        if event:
            try:
                hits = scopes.check(store, command, tool_name, payload.get("turn_id"))
            except Exception as exc:  # noqa: BLE001 - the receipt is already durable
                _log_problem("constraint-check", exc, payload)
        late, delivered = _late_evidence(store, payload)
        reminders, reminded = [], []
        if event:
            try:
                reminders, reminded = edit_reminders(store, payload.get("turn_id"), command, tool_name, show)
            except Exception as exc:  # noqa: BLE001 - a reminder never interrupts the work
                _log_problem("edit-reminders", exc, payload)
        watched, overseen = _supervise_mid_turn(store, payload, show) if event else ("", [])
        cut = []
        try:
            cut = skills.partial(command, payload.get("tool_response"), store.root) if event else []
        except Exception as exc:  # noqa: BLE001 - a check never interrupts the work
            _log_problem("skill-read", exc, payload)
        context = "\n\n".join(note for note in (loops.note(loop) if loop else "", scopes.note(hits) if hits else "", *reminders,
                                                 skills.partial_note(cut) if cut else "", watched, late) if note)
        output = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}} if context else {}
        first = _activity(show, activity.loop, loop) if loop else \
            _activity(show, activity.tool, store, event, command, tool_name, show == "verbose")
        checked = _activity(show, activity.constraint_check, hits) if hits else None
        return output, _join(noted, first, checked, *reminded, *overseen,
                             _activity(show, activity.skill_partial, cut) if cut else None,
                             _activity(show, activity.evidence_late, delivered) if delivered else None, maintenance)
    if event == "Stop":
        # A finished turn is never held to add anything (change notes, comments, task statuses): written after the
        # work they are worth little. The reminders come while the work happens (edit_reminder) or with the next
        # message (card.followups). The one continuation left is the end-of-turn replacement below.
        config = settings.load()
        recorded_notes = []
        try:
            if config["change_notes"] != "off":
                recorded_notes = changes.capture(store, str(payload.get("last_assistant_message") or ""))
        except Exception as exc:  # noqa: BLE001
            _log_problem("change-notes", exc, payload)
        # A plan-mode turn ends with its <proposed_plan>, the plan of record. Codex strips the block from
        # last_assistant_message in plan mode, so it arrives through the session log the Stop sync reads:
        # whatever was stored by the end of this hook is announced (_plans_announced).
        known_plans = plans.count(store)
        try:
            plans.capture(store, str(payload.get("last_assistant_message") or ""))
        except Exception as exc:  # noqa: BLE001
            _log_problem("plans", exc, payload)
        known_plans = (known_plans, payload.get("turn_id"))
        noted = _activity(show, activity.change_notes_recorded, recorded_notes) if recorded_notes else None
        supervised, overseen = _supervise_turn_end(store, payload, config, show)
        if supervised:
            # The supervisor (auto) found the turn ended too early or asked what it could answer: the turn goes on
            # with its finding in place of an end-of-turn replacement, which waits for the next real end.
            close_turn(store, payload.get("turn_id"))
            maintenance = _shadow_maintenance(store, payload, show, compress=False)
            return {"decision": "block", "reason": supervisor.note(supervised)}, _join(
                _activity(show, activity.hold_notice), noted, _plans_announced(store, known_plans, show), *overseen, maintenance)
        due = idle_reset_due(payload, store)
        close_turn(store, payload.get("turn_id"))
        if not due:
            maintenance = _shadow_maintenance(store, payload, show, turn_ended=True)
            return {}, _join(noted, _plans_announced(store, known_plans, show), *overseen, maintenance,
                             _tokens_line(store, payload, show))
        # A replacement follows: bring the shadow up to date, but never start a compression now.
        maintenance = _shadow_maintenance(store, payload, show, compress=False, turn_ended=True)
        mark_idle_reset(store, payload.get("turn_id"))
        note_idle_block(store, due["tokens"])
        # The only continuation this plugin asks for: the turn is finished, and one new_context
        # call (zero-model reset) keeps the next request fast. The loop guard is stop_hook_active,
        # which Codex sets on the Stop that follows this one, plus the backoff in idle_reset_due.
        reason = native_budget.IDLE_REASON.format(tokens=f"{due['tokens'] // 1000}k")
        return {"decision": "block", "reason": reason}, _join(
            _activity(show, activity.hold_notice), noted, _plans_announced(store, known_plans, show), *overseen,
            _activity(show, activity.end_of_task_reset, due["tokens"],
                                                                         due["threshold"]), maintenance, _tokens_line(store, payload, show))
    return {}, None


def _join(*lines) -> str | None:
    joined = "\n".join(line for line in lines if line)
    return joined or None


def _shadow_maintenance(store: Store, payload: dict, show: str, compress: bool = True, turn_ended: bool = False) -> str | None:
    """Mirror new log lines into the shadow and start sidecar compression when it is due; when the turn has
    ended, summarize its large outputs and screenshots (evidence.py), one run per turn.
    Bounded in time and never fatal: the hook's own work is already done."""
    config = settings.load()
    if config["shadow_mode"] != "full" or os.environ.get("BCC_SIDECAR_RUN") == "1":
        return None
    lines = []
    try:
        transcript = payload.get("transcript_path")
        shadow.sync(store, transcript, time.monotonic() + SYNC_SECONDS)
        usage = native_budget.last_usage(transcript) or {}
        threshold = native_budget.idle_threshold(native_budget.read_config().get("model_auto_compact_token_limit"), usage.get("window"))
        decision = sidecar.decide(store, usage.get("tokens"), threshold or config["idle_reset_tokens"], store.agent) \
            if compress else {"start": False}
        if decision["start"]:
            sidecar.start(store, decision)
            lines.append(_activity(show, activity.sidecar_started, decision))
    except Exception as exc:  # noqa: BLE001
        _log_problem("shadow-maintenance", exc, payload)
    if turn_ended:
        try:
            decision = evidence.decide(store, store.agent)
            if decision["start"]:
                evidence.start(store, decision)
                lines.append(_activity(show, activity.evidence_started, decision))
        except Exception as exc:  # noqa: BLE001
            _log_problem("evidence", exc, payload)
    lines.append(_background_reports(store, show))
    return _join(*lines)


def _tokens_line(store: Store, payload: dict, show: str) -> str | None:
    """The session's token totals, once the turn really ends (not while the end-of-turn replacement runs), and on a
    line of its own what the evidence summaries of this session used of their model."""
    usage = native_budget.session_usage(payload.get("transcript_path"))
    try:
        spent = evidence.usage(store)
    except Exception as exc:  # noqa: BLE001 - a statistic never breaks the hook
        spent = None
        _log_problem("evidence-usage", exc, payload)
    try:
        watched = supervisor.usage(store)
    except Exception as exc:  # noqa: BLE001
        watched = None
        _log_problem("supervisor-usage", exc, payload)
    return _join(_activity(show, activity.session_tokens, usage) if usage else None,
                 _activity(show, activity.evidence_tokens, spent) if spent else None,
                 _activity(show, activity.supervisor_tokens, watched) if watched else None)


def edit_reminders(store: Store, turn_id: str | None, command: str, tool_name: str, show: str) -> tuple[list[str], list[str]]:
    """While the turn works, once each: at its first file change, to end the answer with a change note; at its first
    patch that adds comment_min_lines lines of code to a file without a comment, to comment them. (Reminders, model
    context, and activity lines.) The finished turn is never held for either."""
    if not turn_id or not scopes.edited_paths(store, command, tool_name):
        return [], []
    config = settings.load()
    row = store.db.execute("SELECT reminded FROM hook_turns WHERE scope=? AND turn_id=?", (store.scope, turn_id)).fetchone()
    if row is None:
        return [], []
    given, reminders, lines = row["reminded"], [], []
    if config["change_notes"] != "off" and not given & 1:
        unexplained = changes.missing(store, turn_id)
        if unexplained:
            given |= 1
            reminders.append(changes.note_reminder(unexplained))
            lines.append(_activity(show, activity.change_notes_reminded, unexplained))
    if config["code_comments"] != "off" and not given & 2:
        bare = changes.uncommented(store, turn_id, config["comment_min_lines"])
        if bare:
            given |= 2
            reminders.append(changes.comment_reminder(bare))
            lines.append(_activity(show, activity.comments_reminded, bare))
    if given != row["reminded"]:
        store.db.execute("UPDATE hook_turns SET reminded=? WHERE scope=? AND turn_id=?", (given, store.scope, turn_id))
    return reminders, [line for line in lines if line]


def _supervisor_findings(store: Store, payload: dict, show: str) -> tuple[str, list]:
    """Findings of background supervisor reviews that finished since the last hook: (model context, activity lines)."""
    if supervisor.mode(store.agent) == "off":
        return "", []
    try:
        text, found = supervisor.late(store)
    except Exception as exc:  # noqa: BLE001 - a supervisor problem never interrupts the work
        _log_problem("supervisor", exc, payload)
        return "", []
    return text, [_activity(show, activity.supervisor_report, report) for report in found]


def _supervise_mid_turn(store: Store, payload: dict, show: str) -> tuple[str, list]:
    """A mid-turn moment worth a look starts a background review (supervisor.py); finished ones are delivered."""
    run_mode = supervisor.mode(store.agent)
    if run_mode == "off":
        return "", []
    lines = []
    try:
        trigger = supervisor.mid_turn(store)
        if trigger and supervisor.can_run(store, settings.load()):
            supervisor.start(store, trigger, run_mode)
            lines.append(_activity(show, activity.supervisor_started, trigger))
    except Exception as exc:  # noqa: BLE001
        _log_problem("supervisor", exc, payload)
    text, found = _supervisor_findings(store, payload, show)
    return text, lines + found


def _supervise_turn_end(store: Store, payload: dict, config: dict, show: str) -> tuple[dict | None, list]:
    """The turn-end review. auto: at once, here (the Stop hook may run minutes); the finding, or the answer to a question
    the supervisor knows, continues the turn. advise: in the background, delivered with the next message. Never holds
    a Stop that follows a hold (stop_hook_active)."""
    run_mode = supervisor.mode(store.agent, config)
    if run_mode == "off" or payload.get("stop_hook_active"):
        return None, []
    last = str(payload.get("last_assistant_message") or "")
    try:
        # The log first: a message the log shows came from another Codex task leaves the card before its open tasks
        # are counted, and the review sees the turn to its end.
        shadow.sync(store, payload.get("transcript_path"), time.monotonic() + 5)
        trigger = supervisor.turn_end(store, payload.get("turn_id"), last)
        if not trigger or not supervisor.can_run(store, config):
            return None, []
        # advise, and auto where `codex queue` can wake this session: the turn ends now and the review runs in the
        # background (auto wakes Codex with its finding). Elsewhere auto reviews here and holds the turn.
        wakes = run_mode == "auto" and supervisor.can_wake(store)
        if run_mode == "advise" or wakes:
            supervisor.start(store, trigger, run_mode)
            return None, [_activity(show, activity.supervisor_started, trigger, wakes)]
        from .hookrun import STOP_SUPERVISOR_SECONDS, extend_deadline
        extend_deadline(STOP_SUPERVISOR_SECONDS)
        try:
            result = supervisor.review(store, trigger, supervisor.material(store), config)
        except Exception as exc:  # noqa: BLE001 - recorded, and paused after three failures in a row
            supervisor.record(store, trigger, run_mode, error=str(exc)[:500], status="failed")
            _log_problem("supervisor", exc, payload)
            return None, [_activity(show, activity.supervisor_failed, trigger, str(exc))]
        supervisor.record(store, trigger, run_mode, result)
        found = supervisor.finding(result)
        if found and found["kind"] == "answer":
            supervisor.decide_answer(store, found, last)
        return found, [_activity(show, activity.supervisor_verdict, trigger, result, found)]
    except Exception as exc:  # noqa: BLE001
        _log_problem("supervisor", exc, payload)
        return None, []


def _plans_announced(store: Store, known: tuple[int, str | None], show: str) -> str | None:
    """The plan stored since the Stop hook began (from the final answer or the session-log sync), linked to the
    requests of this turn that asked for it (their tasks join the plan's task when it is approved)."""
    fresh = plans.since(store, known[0])
    if not fresh:
        return None
    try:
        plans.link_requests(store, [plan["id"] for plan in fresh], card._turn_items(store, known[1]))
    except Exception as exc:  # noqa: BLE001 - the plan itself is stored; only the link is lost
        _log_problem("plan-requests", exc, {})
    return _activity(show, activity.plan_recorded, fresh[-1])


def _late_evidence(store: Store, payload: dict) -> tuple[str, int]:
    """Evidence summaries that missed the last restore, once (evidence.late); never fatal."""
    try:
        return evidence.late(store, settings.load()["reference_budget"] // 2)
    except Exception as exc:  # noqa: BLE001
        _log_problem("evidence-late", exc, payload)
        return "", 0


def _background_reports(store: Store, show: str) -> str | None:
    """Background runs (sidecar compression, evidence summaries) that finished since the last hook."""
    try:
        return _join(*(_activity(show, activity.sidecar_finished, run) for run in sidecar.unreported(store)),
                     *(_activity(show, activity.evidence_finished, run) for run in evidence.unreported(store)))
    except Exception:  # noqa: BLE001
        return None


def _activity(show: str, build, *args) -> str | None:
    """An activity line never decides whether the hook itself succeeds."""
    if show == "off":
        return None
    try:
        return build(*args)
    except Exception:  # noqa: BLE001 - the recorded state is already durable
        return None


def record_hook_tool(store: Store, payload: dict):
    """Archive exactly what the host reports for a finished tool call. Returns the stored event,
    "card" for this plugin's own card updates, or None."""
    tool_name = payload.get("tool_name", "unknown")
    if is_context_tool(tool_name):
        # Never archive this plugin's own ledger, recall and compression responses.
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        return "card" if tool_name.endswith("context_state") and tool_input.get("action") in ("card", "settings") else None
    response = payload.get("tool_response", {})
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    command = tool_input.get("command", tool_input.get("cmd", ""))
    identifier = payload.get("tool_use_id") or digest(canonical(payload))
    if tool_name.startswith("mcp__") and isinstance(response, dict):
        parts = tool_name.split("__")
        server, tool = parts[-2], parts[-1]
        failed = response.get("isError", response.get("is_error", False)) is True
        status = "failed" if failed else "completed"
        store.append("mcp:" + identifier, "native_mcp",
                     canonical({"type": "mcp_tool_call", "server": server, "tool": tool, "result": response, "status": status}),
                     {"server": server, "tool_name": tool, "status": status, "has_error": failed})
        return None
    code = response.get("exit_code") if isinstance(response, dict) else None
    # This is what the host supplies (often already truncated), not a claim of complete process output.
    output = response if isinstance(response, str) else canonical(response)
    return record_tool(store, identifier, str(command), output, code if type(code) is int else None,
                       source_complete=False, tool_name=tool_name)
