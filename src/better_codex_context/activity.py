"""Short activity log lines shown in the Codex UI after the plugin acts.

Each line goes out as the hook's `systemMessage`. Codex prints it on the hook run
(`↳ Hook · BCC · …` in the terminal UI); the first line is the summary and the following lines
are the details (expanded with the hook run, or under "show details" where the client folds
them). Wording is deliberate: the plugin *organizes* the task card and *optimizes* the context
window. Nothing is compressed away; every original stays archived and recallable.

The `activity_log` setting selects how much is shown: "normal" (default), "verbose" or "off".
Colours (`activity_color`, on by default) are ANSI codes that the Codex terminal UI passes through:
the title up to its first colon in bold tech blue (#1E90FF), the rest of the title in the default
colour, and every detail line in grey. Hook failures (hookrun.py) stay red. When the VS Code
extension's Codex runs the hook, the same lines go out as plain text: the extension lists hook
messages in a "Hook stats" dialog on the assistant message, which prints ANSI codes verbatim.
"""
from __future__ import annotations

import json
import re
import os

from . import card, settings

PREFIX = "BCC · "
MAX_DETAILS = 10
WIDTH = 150
CLASS_LABEL = {"task": "task", "constraint": "standing rule", "correction": "correction", "duplicate": "repeat",
               "redirect": "new direction (user redirect)",
               "ack": "acknowledgement, not a new task", "automated": "automated notice, not a new task",
               "peer": "from another Codex task, not a request of yours",
               "heading": "heading"}


def level() -> str:
    return settings.load()["activity_log"]


# The VS Code extension lists hook messages in a "Hook stats" dialog (assistant message menu) that
# prints ANSI codes verbatim; the terminal UI renders the colours. What decides is the Codex running
# this hook now, not the session log (whose originator names the client that created the session, so
# a VS Code session resumed in the terminal would lose its colours). The extension starts its Codex
# with CODEX_INTERNAL_ORIGINATOR_OVERRIDE=codex_vscode, and Codex passes its own environment to hooks:
# the same check on Linux, macOS and Windows.
_plain_host = False


def use_host() -> None:
    """Choose colours for this hook run from the Codex process that runs it."""
    global _plain_host
    _plain_host = vscode_host()


def vscode_host() -> bool:
    return os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE") == "codex_vscode"


TITLE = "\x1b[1;38;2;30;144;255m"   # bold tech blue
IN_ORANGE = "\x1b[38;2;255;140;0m"    # session tokens: input
OUT_PINK = "\x1b[38;2;255;105;180m"   # output
CACHED_GREEN = "\x1b[38;2;46;204;113m"  # cached share
DETAIL = "\x1b[38;5;245m"            # grey
RESET = "\x1b[0m"


def message(summary: str, details=(), tone: str = "ok") -> str:
    """One activity line: a title and optional detail lines. `tone` (ok, warn, info) is kept for
    callers; all titles share one colour."""
    details = [_clip(d) for d in details if d]
    if len(details) > MAX_DETAILS:
        details = details[:MAX_DETAILS] + [f"… {len(details) - MAX_DETAILS} more"]
    if settings.load()["activity_color"] != "on" or _plain_host:
        return "\n".join([PREFIX + summary] + ["  " + d for d in details])
    head, rest = _split(PREFIX + summary, 90)
    return "\n".join([f"{TITLE}{head}{RESET}{rest}"] + [f"  {DETAIL}{detail}{RESET}" for detail in details])


def plain(text: str | None) -> str:
    """The text without colour codes (for tests and logs)."""
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", text or "")


def _split(text: str, limit: int) -> tuple[str, str]:
    """Split before the value after the first ': ' (a label), when the label is short."""
    index = text.find(": ")
    return (text[:index + 1], text[index + 1:]) if 0 < index <= limit else (text, "")


def _clip(text: str, limit: int = WIDTH) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _items(store) -> dict:
    """Card items with their effective status (user items derive it from their ledger rows)."""
    return {item["id"]: item for item in card.items(store, include_merged=True)}


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


# ---------------------------------------------------------------- UserPromptSubmit

def prompt(store, key: str) -> str | None:
    """Where the paragraphs of the new user message landed on the task card."""
    event = store.db.execute("SELECT artifact FROM events WHERE scope=? AND source_key=?", (store.scope, key)).fetchone()
    if event is None:
        return None
    rows = store.db.execute("SELECT id,class,item,text FROM requirements WHERE scope=? AND source=? "
                            "ORDER BY CAST(substr(id,2) AS INTEGER)", (store.scope, event["artifact"])).fetchall()
    if not rows:
        return None
    placed, details, known = [], [], _items(store)
    for row in rows:
        item = known.get(row["item"]) if row["item"] else None
        label = CLASS_LABEL.get(row["class"] or "task", row["class"] or "task")
        repeated = f" (repeated ×{item['repeats']})" if item is not None and item["repeats"] else ""
        if item is None:
            target = label
        elif row["class"] == "duplicate":
            target = f"repeat of {item['id']}{repeated}, reopened" if item["status"] in card.OPEN else f"repeat of {item['id']}{repeated}"
        else:
            target = f"{item['id']} {label}{repeated}"
        if target not in placed:
            placed.append(target)
        details.append(f"{row['id']} → {target}: {row['text']}")
    ids = card.id_ranges([r["id"] for r in rows])
    if all((r["class"] or "task") in ("ack", "automated", "peer") for r in rows):
        return message(f"Archived your message ({ids}): {placed[0]}", tone="info")
    # One paragraph: the title already says where it went, and the user just typed the text.
    return message(f"Organized your message onto the task card ({ids}): " + ", ".join(placed[:4])
                   + (f" +{len(placed) - 4}" if len(placed) > 4 else ""), details if len(rows) > 1 else [])


# ---------------------------------------------------------------- SessionStart

def restored(store, source: str, parts: dict) -> str | None:
    overflow, restored_shadow = parts.get("overflow"), parts.get("shadow")
    if not parts.get("has_state"):
        return instructions_only(overflow) if overflow else None
    card_info, work_info, current = parts.get("card"), parts.get("work"), parts.get("current")
    every = card.items(store)
    goal = next((i for i in every if i["kind"] == "goal" and i["status"] == "active"), None)
    rules = [i for i in every if i["kind"] == "constraint" and i["status"] == "active"]
    open_tasks = [i for i in every if i["kind"] == "task" and i["status"] in card.OPEN]
    closed = [i for i in every if i["kind"] == "task" and i["status"] not in card.OPEN]
    lessons = [i for i in every if i["kind"] == "lesson" and i["status"] == "active"]
    tokens = sum((info or {}).get("tokens", 0) for info in (card_info, work_info, overflow, restored_shadow))
    if restored_shadow:
        summary = "Context optimized: fresh window with the shadow context and the task card"
    elif source == "compact":
        summary = "Context optimized: continuing in a fresh window with the task card restored"
    elif source == "resume":
        summary = "Task card restored for the resumed session"
    else:
        summary = "Task card restored for this session"
    counts = f"{_count(len(rules), 'rule')}, {_count(len(open_tasks), 'open task')}, {_count(len(lessons), 'lesson')}"
    details = [_shadow_detail(restored_shadow) if restored_shadow else "",
               f"Goal: {goal['text']}" if goal else "",
               f"Standing rules: {card.id_ranges([i['id'] for i in rules])}" if rules else "",
               "Open tasks: " + ", ".join(f"{i['id']} {i['status'].replace('_', ' ')}" for i in open_tasks[:8])
               + (f" +{len(open_tasks) - 8}" if len(open_tasks) > 8 else "") if open_tasks else "",
               f"Closed tasks kept as ids: {card.id_ranges([i['id'] for i in closed])}" if closed else "",
               f"Lessons: {card.id_ranges([i['id'] for i in lessons])}" if lessons else ""]
    if work_info and work_info.get("files"):
        details.append(f"Working set: {_count(work_info['files'], 'file')} with live version check")
    if current and not restored_shadow:
        details.append("Unfinished request resumed: " + current.split(":\n", 1)[-1].split("\n[/segment]")[0])
    elif current:
        details.append("Unfinished request: continues from the restored conversation")
    if overflow:
        details.append(_overflow_detail(overflow))
    if parts.get("resident_skills"):
        details.append("Resident skills in full: " + ", ".join(parts["resident_skills"]))
    if parts.get("plan"):
        details.append(f"Plan of record: {parts['plan']['id']} restored " + ("in full" if parts["plan"]["complete"] else "from its beginning"))
    if (parts.get("change_notes") or {}).get("notes"):
        info = parts["change_notes"]
        details.append(f"Change notes: {info['shown']} of {info['notes']} restored in full"
                       + (f" ({card.id_ranges(info['ids'])})" if info.get("ids") else ""))
    if parts.get("protected"):
        details.append("Protected references: " + ", ".join(f"{d['path']} ({d['state']})" for d in parts["protected"][:6])
                       + (f" +{len(parts['protected']) - 6}" if len(parts["protected"]) > 6 else ""))
    if parts.get("constraints_guarded"):
        details.append("Standing rules restored in full outside the over-budget card: "
                       + card.id_ranges(parts["constraints_guarded"]))
    details.append(f"Injected about {tokens:,} tokens; all originals stay archived for context_recall")
    return message(f"{summary} ({counts})", details)


def _shadow_detail(block: dict) -> str:
    parts = []
    if block.get("summary_tokens"):
        parts.append(f"summary {_k(block['summary_tokens'])}")
    parts.append(f"{block['entries']} recent " + ("entry" if block["entries"] == 1 else "entries"))
    if block.get("references"):
        parts.append(_count(block["references"], "reference"))
    if block.get("left_out"):
        parts.append(f"{block['left_out']} older entries left out to fit")
    return "Shadow context: " + ", ".join(parts) + f" ({_k(block['tokens'])} tokens)"


def _k(tokens: int) -> str:
    if tokens < 1000:
        return str(tokens)
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    return f"{tokens / 1000:.1f}k" if tokens < 100000 else f"{tokens // 1000}k"


def _paint(text: str, colour: str) -> str:
    """`text` in `colour` where colours are shown (the terminal with activity_color on)."""
    return f"{colour}{text}{RESET}" if settings.load()["activity_color"] == "on" and not _plain_host else text


def session_tokens(usage: dict) -> str:
    """The session's token totals at the end of a turn: input with its cached share, output, window."""
    rate = usage["cached"] / usage["input"] * 100 if usage["input"] else 0.0
    parts = [f"{_paint(_k(usage['input']) + ' in', IN_ORANGE)} ({_paint(f'{rate:.1f}% cached', CACHED_GREEN)}, "
             f"{_k(usage['input'] - usage['cached'])} uncached)",
             _paint(f"{_k(usage['output'])} out", OUT_PINK) + (f" ({_k(usage['reasoning'])} reasoning)" if usage["reasoning"] else "")]
    if usage.get("window_tokens") and usage.get("window"):  # what the model held at the last request, of its maximum
        parts.append(f"context {_k(usage['window_tokens'])} of {_k(usage['window'])} "
                     f"({usage['window_tokens'] * 100 // usage['window']}%)")
    return message("Session tokens: " + " · ".join(parts))


def model_name(model: str) -> str:
    """A model id as people write it: gpt-6-luna → GPT-6 Luna, gpt-6.1-terra → GPT-6.1 Terra."""
    parts = model.split("-")
    if len(parts) >= 2 and parts[0].lower() == "gpt":
        return " ".join([f"GPT-{parts[1]}", *(p.capitalize() for p in parts[2:])])
    return model


def skill_partial(missing: list[dict]) -> str:
    return PREFIX + "Skill read in part: " + ", ".join(f"{m['name']} (missing from about line {m['from_line']})" for m in missing) \
        + " · asked Codex to read the rest"


def supervisor_tokens(usage: dict) -> str:
    """What the supervisor's reviews of this session used, per model (Luna screens, Sol judges)."""
    parts = [f"{_paint(_k(v['input']) + ' in', IN_ORANGE)} {_paint(_k(v['output']) + ' out', OUT_PINK)} {model_name(m)}"
             for m, v in usage["models"].items()]
    return message("Supervisor tokens: " + " · ".join(parts + [_count(usage["runs"], "review")]))


def evidence_tokens(usage: dict) -> str:
    """What the evidence summaries of this session used of their own model (a separate codex exec)."""
    reasoning = usage.get("reasoning_output_tokens", 0)
    parts = [_paint(_k(usage.get("input_tokens", 0)) + " in", IN_ORANGE),
             _paint(f"{_k(usage.get('output_tokens', 0))} out", OUT_PINK) + (f" ({_k(reasoning)} reasoning)" if reasoning else ""),
             _count(usage["runs"], "run"), model_name(usage["model"])]
    return message("Evidence tokens: " + " · ".join(parts))


def instructions_only(overflow: dict) -> str:
    return message("Project instructions: added what Codex cannot load from AGENTS.md", [
        _overflow_detail(overflow),
        *(f"{f['path']}: Codex loads {f['loaded_bytes'] / 1024:.0f} of {f['bytes'] / 1024:.0f} KiB" for f in overflow["files"]),
        f"Injected about {overflow['tokens']:,} tokens; every cut section is archived for context_recall"])


def _overflow_detail(overflow: dict) -> str:
    shown = overflow["rules_shown"]
    return (f"AGENTS.md beyond Codex's {overflow['limit'] // 1024} KiB limit: {_count(shown, 'rule')} restored"
            + (f" (of {overflow['rules_found']})" if overflow["rules_found"] > shown else "")
            + f", {_count(overflow['sections'], 'cut section')} indexed")


# ---------------------------------------------------------------- PostToolUse

def tool(store, event: dict | None, command: str, tool_name: str, verbose: bool) -> str | None:
    """What a tool result added to the working set, printed as it happens so the user sees the plugin
    at work. Codex prints every line of a hook message (its hook cell has no "Show details" fold), so
    normal mode keeps one line per edit, failing command or long output (a failure keeps its error line);
    verbose mode adds the details and also reports plain reads. Lines say what the plugin did with the
    result. For a long output that is archiving and indexing the copy Codex passed on; Codex shortens
    such outputs itself, and its "original token count" is the size before that, not an amount lost."""
    if event is None:
        return None
    meta = json.loads(event["metadata"]) if isinstance(event.get("metadata"), str) else event.get("metadata", {})
    files = store.db.execute("SELECT path,last_action,reads,edits FROM work_files WHERE scope=? AND last_seq=?",
                             (store.scope, event["seq"])).fetchall()
    edited = [f["path"] for f in files if f["last_action"] in ("edit", "delete")]
    patch = command.lstrip().startswith("*** Begin Patch")
    shown = tool_name if patch or not command else _clip(command, 70)
    details, summary = [], None
    if meta.get("exit_code") not in (None, 0):
        summary = f"Failing command noted (exit {meta['exit_code']}): {shown}"
        if meta.get("diagnostic"):
            details.append(meta["diagnostic"])
    elif edited:
        summary = "Edit recorded: " + ", ".join(edited[:3]) + (f" +{len(edited) - 3}" if len(edited) > 3 else "")
    elif meta.get("host_truncated_tokens") is not None:
        summary = f"Long output archived and indexed: {shown}"
        details.append(f"Codex passed on a shortened copy (≈{meta['host_truncated_tokens']:,} tokens in full); "
                       "rerun through capture_command_prefix to archive the full text")
    elif verbose:
        summary = "Archived tool result" + (": " + ", ".join(f["path"] for f in files[:3]) if files else "")
    if summary is None:
        return None
    if not verbose:
        return message(summary, details[:1] if summary.startswith("Failing") else [])
    details.insert(0, f"Tool: {tool_name}" if patch or not command else f"Command: {command}")
    details.extend(f"{f['path']}: {f['last_action']} (reads {f['reads']}, edits {f['edits']})" for f in files)
    details.append(f"Exact output: context_recall(ref=\"{event['artifact']}\")")
    return message(summary, details)


LOOP_ASK = {"warning": "asked the model to check its fix", "rethink": "asked the model to change approach",
            "escalate": "asked the model to stop, rethink the cause and record a lesson"}


def loop(found: dict) -> str:
    if found["kind"] == "stuck":
        ask = LOOP_ASK.get(found.get("level"), LOOP_ASK["rethink"])
        summary = f"Loop guard: `{_clip(found['command'], 60)}` failed {found['count']}× in a row with the same error; {ask}"
        details = [f"Error: {found['error']}"] + (
            ["Went back to an earlier version: " + ", ".join(found["oscillating"][:5])] if found.get("oscillating") else [])
    else:
        summary = f"Loop guard: `{_clip(found['command'], 60)}` returned the identical result {found['count']}×; asked the model to stop repeating it"
        details = []
    return message(summary, tone="warn", details=details + [f"Output: context_recall(ref=\"{found['ref']}\")",
                                       f"Threshold loop_guard = {settings.load()['loop_guard']}"
                                       f"{_setting_origin('loop_guard', settings.load()['loop_guard'])}; ask Codex to change it"])


def constraint_check(hits) -> str:
    """A changed file falls under scoped standing constraints; the model was reminded of them."""
    paths = list(dict.fromkeys(h["path"] for h in hits))
    ids = card.id_ranges(list(dict.fromkeys(h["item"]["id"] for h in hits)))
    where = f"`{_clip(paths[0], 70)}`" if len(paths) == 1 else _count(len(paths), "changed file")
    details = []
    for hit in hits:
        details.append(f"{hit['item']['id']} {hit['item']['text']}")
        details.append(f"   {hit['path']} matched {hit['pattern']}")
    return message(f"Constraint check: {where} falls under {ids}; reminded the model to comply", details, tone="warn")


def plan_recorded(plan) -> str:
    """The plan of record: a plan-mode <proposed_plan> kept word for word."""
    return message(f"Plan recorded: {plan['id']} is the plan of record; restored in full in every fresh window")


def direction(store, key: str) -> str | None:
    """What the new message changed beyond its own items: a plan approval that replaced earlier plans (or carried
    steps and requests over), or a redirect that left earlier items to decide."""
    event = store.db.execute("SELECT artifact FROM events WHERE scope=? AND source_key=?", (store.scope, key)).fetchone()
    state = store.state()
    change = state.get("direction") if state.get("direction", {}).get("source") == (event and event["artifact"]) else None
    approval = state.get("approval") if state.get("approval", {}).get("source") == (event and event["artifact"]) else None
    if change and change["kind"] == "redirect":
        open_ = card.undecided(store)  # the redirect's review (card.review), fresh from this message
        return message(f"Redirect: the earlier direction stops ({change['item']})"
                       + (f"; {card.id_ranges([i['id'] for i in open_])} to decide: cancel, supersede or keep" if open_ else ""))
    info = change or approval
    if not info or not (change or info.get("carried") or info.get("merged")):
        return None
    plan = info.get("plan")
    parts = []
    if change:
        parts.append(f"replaces {', '.join(change.get('replaced') or [])}: {card.id_ranges(change.get('superseded', []))} superseded")
    done = [new for _, new, status in info.get("carried", []) if status == "done"]
    if done:
        parts.append(f"{card.id_ranges(done)} already done earlier")
    if info.get("merged"):
        parts.append(f"your request {card.id_ranges(info['merged'])} joined {info['item']}")
    return message(f"Plan {plan} approved: " + "; ".join(parts))


def drift_signal(store, key: str) -> str | None:
    """The user's feedback signalled drift with this message (card._feedback)."""
    event = store.db.execute("SELECT artifact FROM events WHERE scope=? AND source_key=?", (store.scope, key)).fetchone()
    signal = card.drift(store)
    if not event or not signal or signal["source"] != event["artifact"]:
        return None
    return message(f"Feedback: {'; '.join(signal['reasons'])} — asked Codex to stop, find the root cause and re-plan",
                   tone="warn")


# ---------------------------------------------------------------- supervisor

# The supervisor's eye, drawn in pixels two to a terminal cell (upper and lower half): an almond with slim corners,
# a round iris, the pupil and a glint. '#' lid, 'o' iris, '@' pupil, '~' glint.
EYE_PIXELS = ("......#########......",
              "...###..ooooo..###...",
              ".##....oo@@@~o....##.",
              ".##....oo@@@oo....##.",
              "...###..ooooo..###...",
              "......#########......")
EYE_COLOURS = {"#": (70, 130, 220), "o": (255, 140, 0), "@": (255, 105, 180), "~": (255, 255, 255)}
# The iris tells the mode: yellow while it only advises, green when it acts on its own (auto).
IRIS = {"advise": (255, 200, 0), "auto": (46, 204, 113)}
WHITE = "\x1b[38;5;252m"


def _eye(colour: bool, mode: str = "advise") -> list[str]:
    """Three terminal rows. With colour, a cell whose halves differ takes the upper one as foreground (▀) and the lower
    one as background, and the iris shows the mode; without colour (VS Code shows plain text) only the shape is left,
    and the pupil is drawn as ◉."""
    palette = {**EYE_COLOURS, "o": IRIS.get(mode, IRIS["auto"])}
    rows = []
    for t, (top, bottom) in enumerate(zip(EYE_PIXELS[0::2], EYE_PIXELS[1::2])):
        cells = []
        for a, b in zip(top, bottom):
            upper, lower = palette.get(a), palette.get(b)
            if not colour:
                cells.append("◉" if a == "@" and b == "@" and t == 1 and top.index("@") + 1 == len(cells)
                             else "█" if upper and lower else "▀" if upper else "▄" if lower else " ")
            elif upper and lower:
                fg = "\x1b[38;2;%d;%d;%dm" % upper
                cells.append(f"{fg}█{RESET}" if upper == lower else fg + "\x1b[48;2;%d;%d;%dm▀" % lower + RESET)
            elif upper or lower:
                cells.append(("\x1b[38;2;%d;%d;%dm" % (upper or lower)) + ("▀" if upper else "▄") + RESET)
            else:
                cells.append(" ")
        rows.append("".join(cells))
    return rows


TRIGGER_WORDS = {"scope_growth": "the work spreads over many files", "explore_without_edit": "many steps without a change",
                 "repeat_command": "the same command again", "long_without_user": "a long run since your request",
                 "asks_user": "a question for you", "stopped_unfinished": "the turn ended with work open",
                 "claim_done": "a claim of success", "open_tasks": "the turn ended with tasks open on the card"}


# Severity colours of the supervisor's lines: green fine, cyan at work, yellow a warning, red serious.
SEVERITY = {"ok": "\x1b[1;38;2;46;204;113m", "info": "\x1b[1;38;2;0;210;255m", "warn": "\x1b[1;38;2;255;200;0m",
            "serious": "\x1b[1;38;2;255;80;80m"}
SERIOUS_KINDS = ("drift_from_request", "false_completion")


def _supervisor(status: str, rows: list[tuple[str, str]], tone: str = "ok", mode: str | None = None) -> str:
    """Every time the supervisor steps in: a title whose status word carries the severity colour (Codex puts its hook
    prefix before the first line, so the picture starts after it), a blank line, then the eye with the rows beside it:
    a grey label and white text, the first row of a finding in the severity colour."""
    config = settings.load()
    mode = mode or config["supervisor"]
    colour = config["activity_color"] == "on" and not _plain_host
    head = f"{PREFIX}Supervisor · {mode} mode · "  # "auto mode": never read together with the status after it
    lines = [f"{TITLE}{head}{RESET}{SEVERITY[tone]}{status}{RESET}" if colour else head + status, ""]
    for index, (art, (label, text)) in enumerate(zip(_eye(colour, mode), (rows + [("", "")] * 3)[:3])):
        text = _clip(text, 110)
        if not colour:
            right = f"{label}: {text}" if label and text else text
        elif label:
            right = f"{DETAIL}{label}:{RESET} {WHITE}{text}{RESET}" if text else ""
        else:
            emphasis = SEVERITY[tone] if index == 0 and tone in ("warn", "serious") else WHITE
            right = f"{emphasis}{text}{RESET}" if text else ""
        lines.append((art + "  " + right).rstrip())
    return "\n".join(lines)


def _moment(trigger: str) -> tuple[str, str]:
    return "moment", TRIGGER_WORDS.get(trigger, trigger)


def supervisor_on(mode: str) -> str:
    """When the supervisor is turned on."""
    config = settings.load()
    return _supervisor("enabled, watching", [("", "watching this session for drift"),
                              ("models", f"{model_name(config['supervisor_screen_model'])} screens · "
                                         f"{model_name(config['supervisor_judge_model'])} judges"),
                              ("", "speaks with quotes, never edits; Codex must follow it")], "ok", mode)


def supervisor_started(trigger: str, wakes: bool = False) -> str:
    then = "Codex is woken if it must go on" if wakes else "the outcome comes at the next step"
    return _supervisor("reviewing", [_moment(trigger), ("", "in the background; " + then)], "info")


def supervisor_failed(trigger: str, error: str) -> str:
    return _supervisor("review failed", [_moment(trigger), ("error", error[:110])], "warn")


def supervisor_verdict(trigger: str, result: dict, found: dict | None) -> str:
    """The turn-end review that ran in the Stop hook (auto)."""
    if found:
        return supervisor_finding({**found, "trigger": trigger})
    verdict = (result.get("verdict") or {}).get("verdict")
    doubt = (result.get("screen") or {}).get("reason", "") if (result.get("screen") or {}).get("problem") else ""
    return supervisor_report({"trigger": trigger, "verdict": verdict, "doubt": doubt})


def supervisor_report(report: dict) -> str:
    """How a review ended: its finding, or that the work is fine (with the doubt it checked)."""
    if report.get("found"):
        return supervisor_finding({**report["found"], "trigger": report["trigger"], "woke": report.get("woke")})
    if report.get("error"):
        return supervisor_failed(report["trigger"], report["error"])
    doubt = ("checked", report["doubt"]) if report.get("doubt") else ("", "nothing worth a closer look")
    if report.get("verdict") == "unsure":
        return _supervisor("not sure", [_moment(report["trigger"]), doubt, ("", "no instruction given")], "warn")
    state = "on track" if report.get("verdict") == "on_track" else "nothing to flag"
    return _supervisor(state, [_moment(report["trigger"]), doubt, ("", "no action needed")], "ok")


def supervisor_finding(found: dict) -> str:
    """A finding given to Codex now: an answer for the user (green), or what is off track (yellow; red when serious)."""
    moment = _moment(found["trigger"]) if found.get("trigger") else ("", "")
    if found.get("woke"):
        moment = ("", "woke Codex (codex queue) to go on")
    if found["kind"] == "restart":
        left = "; ".join(f"{t['id']} {t['left']}" for t in (found.get("tasks") or [])[:3])
        first = (found.get("steps") or [""])[0]
        return _supervisor("restarting unfinished work", [("left", left), ("first step", first), moment], "warn")
    if found["kind"] == "answer":
        basis = "; ".join(f"“{q}”" for q in (found.get("basis") or [])[:2])
        return _supervisor("answered for you", [("answer", found["text"]), ("basis", basis) if basis else moment,
                                                moment if basis else ("", "")], "ok")
    tone = "serious" if found["kind"] in SERIOUS_KINDS or (found.get("confidence") or 0) >= 0.8 else "warn"
    return _supervisor(f"off track · {found['kind'].replace('_', ' ')}",
                       [("", found["text"]), ("simple path", found.get("simple_path", "")), moment], tone)


def decisions_recorded(ids) -> str:
    """Answers to plan-mode questions (request_user_input), now standing decisions on the card."""
    return message(f"Your answers recorded as standing decisions: {card.id_ranges(ids)}")


def tasks_started(tasks) -> str:
    """The turn's first tool call moved its pending tasks to in_progress."""
    return message(f"Task card: {card.id_ranges([t['id'] for t in tasks])} in progress (work started)")


def hold_notice() -> str:
    """First line of the end-of-turn replacement, the one Stop continuation. Codex titles a hook that continues the
    turn "Blocked by hook" (fixed text), which reads like a failure; this says it is a routine step."""
    return message("Routine follow-up, not an error: the turn continues once for what is listed below", tone="info")


def followups(ids) -> str:
    """Asked with the user's new message: what the last turn left unset (statuses, a redirect's open items)."""
    return message(f"Task card: asked the model to set {card.id_ranges(ids)} in this turn (left open by the last one)", tone="info")


def change_notes_recorded(notes) -> str:
    """One line: the note itself is already readable in the answer just above."""
    first = notes[0]
    files = ", ".join(f"`{f}`" for f in first["files"][:3]) + (f" +{len(first['files']) - 3}" if len(first["files"]) > 3 else "")
    symbols = f" · {_clip(first['symbols'], 90)}" if first.get("symbols") else ""  # as parsed, with add/edit/del
    return message(f"Change note recorded: {', '.join(n['id'] for n in notes)} for {files or 'this answer'}"
                   + (f" (task {first['task']})" if first["task"] else "") + symbols + "; restored in every fresh window")


def comments_reminded(files) -> str:
    names = ", ".join(f"`{item['path']}`" for item in files[:3]) + (f" +{len(files) - 3}" if len(files) > 3 else "")
    return message(f"Code comments: reminded the model, while it writes, to comment {names}", tone="info")


def change_notes_reminded(paths) -> str:
    return message(f"Change notes: reminded the model to write a change note for "
                   f"{', '.join(f'`{p}`' for p in paths[:3])}" + (f" +{len(paths) - 3}" if len(paths) > 3 else ""), tone="info")


def reread(paths) -> str:
    return message(f"Protected references: asked the model to re-read {', '.join(f'`{p}`' for p in paths[:3])}"
                   + (f" +{len(paths) - 3}" if len(paths) > 3 else ""), ["Changed since the last read, or named in this message"
                                                                         " without its current version read"], tone="warn")


def warning(text: str) -> str:
    return message("Check: " + text, tone="warn")


def card_update(store, response, action: str = "card") -> str | None:
    """What a context_state(action=card|settings) call changed, read back from the plugin's own reply."""
    payload = _tool_json(response)
    if not isinstance(payload, dict):
        what = "Settings change" if action == "settings" else "Task card update"
        return message(f"{what} was rejected", [_tool_text(response)], tone="warn") if _is_error(response) else None
    if payload.get("settings_changed"):  # context_state(action="settings", set=...)
        changes = [f"{key}: {_shown(old)} → {_shown(new)}" for key, (old, new) in payload["settings_changed"].items()]
        line = message("Plugin settings changed: " + "; ".join(changes), [f"Saved in {payload['file']}; applies {payload['applies']}"]
                       + [f"Overridden by the environment variable {name}" for name in payload.get("overridden_by_environment", [])])
        old, new = payload["settings_changed"].get("supervisor", [None, None])
        return line + "\n" + supervisor_on(new) if new in ("advise", "auto") and old != new else line
    changed = payload.get("changed") or []
    if not changed:
        return None
    details, known = [], _items(store)
    for identifier in changed:
        if identifier == "checkpoint":
            state = store.state()
            details.append("Checkpoint saved: " + (state.get("focus") or "") + (
                " → next: " + "; ".join(state.get("next_steps", [])[:3]) if state.get("next_steps") else ""))
            continue
        item = known.get(identifier)
        if item is not None:
            details.append(f"{item['id']} [{item['status'].replace('_', ' ')}] {item['text']}")
    names = card.id_ranges([c for c in changed if c != "checkpoint"])
    parts = [names] if names else []
    if "checkpoint" in changed:
        parts.append("checkpoint")
    return message("Task card organized: " + " + ".join(parts), details)


def _setting_origin(key: str, value) -> str:
    """" (your setting; default …)" when the value is not the default, so a surprising number explains itself."""
    default = settings.DEFAULTS[key]
    if value == default:
        return ""
    source = next((row["source"] for row in settings.describe()["settings"] if row["key"] == key), "")
    origin = "scaled to the model window" if source == "default" else f"from the {source}"
    return f" ({origin}; default {_shown(default)})"


def _shown(value) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)


def _tool_text(response) -> str:
    if isinstance(response, dict):
        content = response.get("content")
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return json.dumps(response, ensure_ascii=False)[:400]
    return str(response)[:400]


def _tool_json(response):
    try:
        value = json.loads(_tool_text(response))
    except ValueError:
        return None
    return value


def _is_error(response) -> bool:
    return isinstance(response, dict) and (response.get("isError") or response.get("is_error")) is True


# ---------------------------------------------------------------- Stop

def end_of_task_reset(tokens: int, threshold: int) -> str:
    return message(f"Turn finished with about {tokens // 1000}k tokens in the window: optimizing the context for the next request",
                   ["The model saves the task card, then the shadow context replaces the window (no model-written summary)",
                    "Nothing is lost: every message and tool output stays archived for context_recall",
                    f"Threshold idle_reset_tokens = {threshold:,}{_setting_origin('idle_reset_tokens', threshold)}; "
                    "ask Codex to change it"], tone="info")


def sidecar_started(decision: dict) -> str:
    return message(f"Shadow context at {_k(decision['body'])} (trigger {_k(decision['trigger'])}): optimizing it in the background",
                   [f"Model: {decision['model']} via codex exec, separate from this session",
                    "The current window is not touched; the result is used at the next replacement"], tone="info")


def evidence_started(decision: dict) -> str:
    parts = [part for part in (_count(decision["outputs"], "large tool output") if decision["outputs"] else "",
                               _count(decision["steps"], "step") + " of smaller outputs" if decision["steps"] else "",
                               _count(decision["images"], "screenshot") if decision["images"] else "") if part]
    what = " and ".join([", ".join(parts[:-1]), parts[-1]]) if len(parts) > 1 else "".join(parts)
    return message(f"Evidence: summarizing {what or 'the large outputs'} of this session in the background",
                   [f"Model: {model_name(decision['model'])} ({decision['effort']} effort) via codex exec, about "
                    f"{decision['limit']} tokens per summary",
                    "Restored windows use the summaries; context_recall still returns the originals"], tone="info")


def evidence_late(count: int) -> str:
    return message(f"Evidence: added {_count(count, 'summary')} that finished after the window was replaced",
                   ["They replace the excerpts the restored window showed for those outputs"], tone="info")


def evidence_finished(run: dict) -> str:
    if run["status"] == "done":
        covered = run.get("outputs") or run["summaries"]
        return message(f"Evidence summarized in the background: {_count(run['summaries'], 'summary')} of "
                       f"{_count(covered, 'output')} → {_k(run['summary_tokens'])} tokens",
                       ["Used in every restored window instead of rule-based excerpts"])
    return message("Evidence summary failed; the rule-based excerpts stay, and the next turn tries again",
                   [f"Error: {run.get('error') or 'unknown'}"], tone="warn")


def sidecar_finished(run: dict) -> str:
    digest = run.get("digest")
    if run["status"] == "done" and digest:
        return message(f"Shadow context optimized in the background: {_k(digest['input_tokens'])} → {_k(digest['tokens'])}; "
                       f"next at {_k(digest['next_trigger'])}", ["Used from the next replacement on"])
    return message("Shadow context optimization failed; it will retry when the shadow grows",
                   [f"Error: {run.get('error') or 'unknown'}"], tone="warn")
