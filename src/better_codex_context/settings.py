"""User settings of the plugin itself.

Read from `$XDG_CONFIG_HOME/better-codex-context/config.toml` (default
`~/.config/better-codex-context/config.toml`) on every use, so changes apply to the next hook run
without reinstalling or restarting Codex. An environment variable `BCC_<NAME>` (upper case)
overrides the file; list values are given there as JSON. Codex's own settings (reset threshold,
reminders) stay in ~/.codex/config.toml.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tomllib

from .util import ContextError, atomic_write, user_directory

DEFAULTS = {
    "idle_reset_tokens": 300000,
    "card_budget": 3500,
    "workset_budget": 800,
    "instructions_budget": 2000,
    "loop_guard": 3,
    "activity_log": "normal",
    "activity_color": "on",
    "change_notes": "require",
    "code_comments": "require",
    "card_updates": "require",
    "comment_min_lines": 12,
    "change_notes_budget": 1500,
    "plan_budget": 4000,
    "shadow_mode": "full",
    "sidecar": "auto",
    "sidecar_first_tokens": 80000,
    "sidecar_growth_factor": 3,
    "sidecar_switch_tokens": 120000,
    "sidecar_late_growth_factor": 2,
    "sidecar_min_trigger_tokens": 60000,
    "sidecar_max_trigger_tokens": 160000,
    "sidecar_quiet_ratio": 0.9,
    "sidecar_model": "gpt-6.1-terra",
    "sidecar_effort": "medium",
    "sidecar_timeout_seconds": 300,
    "sidecar_max_input_tokens_per_hour": 1000000,
    "sidecar_codex": "codex",
    "sidecar_extra_args": [],
    "evidence": "auto",
    "evidence_model": "gpt-6-luna",
    "evidence_effort": "high",
    "evidence_min_tokens": 3000,
    "evidence_group_tokens": 8000,
    "evidence_summary_tokens": 600,
    "evidence_max_input_tokens_per_hour": 600000,
    "resident_skills": ["context-discipline"],
    "supervisor": "off",
    "supervisor_offer": "on",
    "supervisor_screen_model": "gpt-6-luna",
    "supervisor_screen_effort": "max",
    "supervisor_judge_model": "gpt-6.1-sol",
    "supervisor_judge_effort": "xhigh",
    "supervisor_max_runs": 40,
    "body_message_max_tokens": 4000,
    "reference_inline_tokens": 600,
    "reference_budget": 16000,
}
# What each setting does, in words a user can act on: the single source for `describe()` (shown to
# Codex through context_state(action="settings") and from there to the user).
DESCRIPTIONS = {
    "idle_reset_tokens": "Window size (tokens) at which the end of a turn replaces the window with the shadow context "
                         "(a zero-model reset); lower means smaller, faster windows and more replacements; 0 disables. "
                         "With a model window under 475k it scales down to 63% of that window.",
    "card_budget": "Tokens of the task card restored in every fresh window; over it, items collapse to id ranges.",
    "workset_budget": "Tokens of the working set (files read and edited, failing commands) in every fresh window.",
    "instructions_budget": "Tokens for the rules and section index of AGENTS.md content beyond Codex's 32 KiB limit; 0 disables.",
    "loop_guard": "Loop guard threshold N: the same failure after edits with no progress warns at N-1 runs, asks for a "
                  "rethink at N, escalates at N+2; identical output with nothing changed is noted every N-th run. 0 disables.",
    "activity_log": "BCC's lines in the Codex UI: normal (one line per action), verbose (with details and every "
                    "archived tool result) or off.",
    "activity_color": "Colours in BCC's terminal lines (VS Code always gets plain text): on or off.",
    "change_notes": "Change notes after code changes (task, motive, reason, risk, tests): require or ask (the first file "
                    "change of a turn reminds the model to end its answer with one; a finished turn is never held) or off.",
    "code_comments": "Comments in new code: require or ask (the first patch of a turn adding comment_min_lines lines of "
                     "code to a file without a comment reminds the model, while it writes) or off.",
    "comment_min_lines": "Lines of new code in one file without a comment before code_comments reminds the model.",
    "card_updates": "Task statuses: require (the statuses a working turn left unset, and what a redirect left undecided, "
                    "are asked for with the next message; a finished turn is never held) or off. The turn's own pending tasks become in_progress at its first "
                    "tool call either way.",
    "change_notes_budget": "Tokens of change notes restored in every fresh window (newest first, the rest as ids).",
    "plan_budget": "Tokens of the plan of record (the latest plan-mode plan) restored in every fresh window.",
    "shadow_mode": "What a replacement restores: full (conversation summary, the recent conversation, task card) or "
                   "card (only the task card and working set).",
    "sidecar": "Background compression of the shadow context through a separate codex exec: auto or off.",
    "sidecar_first_tokens": "Shadow size (tokens) at which the first background compression starts.",
    "sidecar_growth_factor": "After a compression, the next starts at this multiple of its result.",
    "sidecar_switch_tokens": "Above this trigger size, sidecar_late_growth_factor applies instead.",
    "sidecar_late_growth_factor": "Growth factor for large shadows (above sidecar_switch_tokens).",
    "sidecar_min_trigger_tokens": "Lowest shadow size that starts a compression.",
    "sidecar_max_trigger_tokens": "Highest compression trigger: a compression always starts by this size.",
    "sidecar_quiet_ratio": "No compression starts once the window holds this share of idle_reset_tokens (a replacement is near).",
    "sidecar_model": "Model of the background compression.",
    "sidecar_effort": "Reasoning effort of the background compression: low, medium, high or xhigh.",
    "sidecar_timeout_seconds": "A background compression that runs longer is stopped.",
    "sidecar_max_input_tokens_per_hour": "Most shadow tokens sent to background compression per hour.",
    "sidecar_codex": "Codex executable for background compression; codex = on PATH, else the VS Code extension's.",
    "sidecar_extra_args": "Extra arguments for every background codex exec (for example a test provider).",
    "evidence": ("Evidence summaries of large tool outputs and screenshots at the end of each turn, in the background through "
                 "codex exec: auto (default) or off. Off saves the tokens they cost, but restored windows then show those outputs "
                 "only as rule-based excerpts, so the model sees less of earlier evidence; context_recall still returns every "
                 "original."),
    "evidence_model": "Model of the evidence summaries.",
    "evidence_effort": "Reasoning effort of the evidence summaries: low, medium, high, xhigh or max.",
    "evidence_min_tokens": "A tool output of at least this many tokens (and every output with images) is summarized on its own.",
    "evidence_group_tokens": ("Smaller consecutive outputs of one step (no message between them) are summarized together once "
                              "they reach this many tokens; 0 = never."),
    "evidence_summary_tokens": "Length asked for one summary, in tokens (a summary may run a little over).",
    "evidence_max_input_tokens_per_hour": "Most tokens sent to evidence summaries per hour.",
    "supervisor": ("The supervisor: a second model that watches this session's shadow context and says when the work drifts "
                   "from what the user asked (building more than needed, guarding cases that cannot happen, growing scope, "
                   "'done' without proof, stopping before the work is done, asking what it could decide itself). off (default), "
                   "advise (it reviews in the background; its finding reaches Codex at the next tool call or message) or auto "
                   "(at the end of a turn it reviews, also whether the open tasks on the card are really unfinished, and Codex goes "
                   "on with its finding, answer or restart steps: it is woken with codex queue where the session runs in the "
                   "local daemon, else the turn is held; questions are answered for the user when the supervisor knows the "
                   "answer; only what it cannot know goes to the user). "
                   "It adds about 0.1% to the tokens of a long session, a few percent to a short one (one review reads some "
                   "40k-55k tokens)."),
    "resident_skills": ("Skills whose whole text stays in every fresh window, never shortened (names as their folders, "
                        "JSON list; [] for none). For rules that apply to all work, such as the coding discipline; a skill "
                        "used once, such as making a deck, is read again when needed."),
    "supervisor_offer": ("While the supervisor is off, Codex offers it once per session when the work is an engineering "
                         "project or a complex request: on, or off to stop the offers."),
    "supervisor_screen_model": "Model of the supervisor's first, cheap screen (it filters, it does not judge).",
    "supervisor_screen_effort": "Reasoning effort of the screen: low, medium, high, xhigh or max.",
    "supervisor_judge_model": "Model that judges what the screen passes.",
    "supervisor_judge_effort": "Reasoning effort of the judge: low, medium, high, xhigh or max.",
    "supervisor_max_runs": "Most supervisor reviews in one session (each is a screen, and a judgement when the screen passes it).",
    "body_message_max_tokens": "Most tokens one message keeps in the restored conversation (the archive keeps all of it).",
    "reference_inline_tokens": "Tool output up to this size is shown in full in the restored conversation, larger as an excerpt.",
    "reference_budget": "Tokens of tool-output excerpts in a restored conversation (newest first).",
}
LIMITS = {"idle_reset_tokens": (0, 10_000_000), "card_budget": (300, 20000), "workset_budget": (0, 8000),
          "instructions_budget": (0, 20000), "loop_guard": (0, 50),
          "sidecar_first_tokens": (1, 10_000_000), "sidecar_growth_factor": (1, 20), "sidecar_switch_tokens": (1, 10_000_000),
          "sidecar_late_growth_factor": (1, 20), "sidecar_min_trigger_tokens": (1, 10_000_000),
          "sidecar_max_trigger_tokens": (1, 10_000_000), "sidecar_quiet_ratio": (0.05, 1.0),
          "sidecar_timeout_seconds": (10, 3600), "sidecar_max_input_tokens_per_hour": (0, 100_000_000),
          "evidence_min_tokens": (100, 1_000_000), "evidence_group_tokens": (0, 1_000_000), "evidence_summary_tokens": (50, 8000),
          "evidence_max_input_tokens_per_hour": (0, 100_000_000), "supervisor_max_runs": (0, 1000),
          "body_message_max_tokens": (100, 200000), "change_notes_budget": (0, 20000), "plan_budget": (0, 40000), "comment_min_lines": (1, 10000), "reference_inline_tokens": (0, 20000), "reference_budget": (0, 200000)}
CHOICES = {"supervisor": ("off", "advise", "auto"), "supervisor_offer": ("on", "off"), "supervisor_screen_effort": ("low", "medium", "high", "xhigh", "max"),
           "supervisor_judge_effort": ("low", "medium", "high", "xhigh", "max"), "change_notes": ("require", "ask", "off"), "code_comments": ("require", "ask", "off"),
           "card_updates": ("require", "off"), "activity_log": ("normal", "verbose", "off"), "activity_color": ("on", "off"), "shadow_mode": ("full", "card"), "sidecar": ("auto", "off"),
           "sidecar_effort": ("low", "medium", "high", "xhigh"), "evidence": ("auto", "off"),
           "evidence_effort": ("low", "medium", "high", "xhigh", "max")}


def path() -> Path:
    return user_directory("XDG_CONFIG_HOME", ".config") / "better-codex-context" / "config.toml"


def load() -> dict:
    """Effective settings; invalid values fall back to the default instead of breaking a hook."""
    try:
        raw = tomllib.loads(path().read_text())
    except (OSError, ValueError):
        raw = {}
    return {key: _value(key, default, raw) for key, default in DEFAULTS.items()}


def _value(key, default, raw):
    value = _coerce(key, os.environ.get("BCC_" + key.upper(), raw.get(key, default)))
    return default if value is None else value


def _coerce(key, value):
    """The value in the setting's type when it is allowed, else None."""
    default = DEFAULTS[key]
    if key in CHOICES:
        value = str(value).strip().lower()
        return value if value in CHOICES[key] else None
    if isinstance(default, list):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return None
        return [str(v) for v in value] if isinstance(value, list) else None
    if isinstance(default, str):
        return str(value).strip() or None
    if isinstance(value, bool):
        return None
    try:
        value = float(value) if isinstance(default, float) else int(value)
    except (TypeError, ValueError):
        return None
    low, high = LIMITS[key]
    return value if low <= value <= high else None


def _file() -> dict:
    try:
        return tomllib.loads(path().read_text())
    except (OSError, ValueError):
        return {}


def _allowed(key) -> str:
    if key in CHOICES:
        return " | ".join(CHOICES[key])
    if key in LIMITS:
        return f"{LIMITS[key][0]:,} … {LIMITS[key][1]:,}"
    return "list of strings" if isinstance(DEFAULTS[key], list) else "text"


def describe() -> dict:
    """Every setting with its value, default, where the value comes from, allowed values and what it does."""
    raw, effective = _file(), load()
    rows = []
    for key, default in DEFAULTS.items():
        env = "BCC_" + key.upper()
        source = f"environment {env}" if env in os.environ else "config file" if key in raw else "default"
        rows.append({"key": key, "value": effective[key], "default": default, "source": source,
                     "allowed": _allowed(key), "description": DESCRIPTIONS[key]})
    return {"file": str(path()), "settings": rows,
            "note": "Changes apply from the next hook run, without reinstalling or restarting Codex."}


def change(updates: dict) -> dict:
    """Write settings to the config file (None restores the default), keeping the file's other lines.
    Every value is checked first; nothing is written when one is unknown or out of range."""
    if not isinstance(updates, dict) or not updates:
        raise ContextError("set needs at least one setting, for example {\"idle_reset_tokens\": 300000}")
    before, wanted = load(), {}
    for key, value in updates.items():
        if key not in DEFAULTS:
            raise ContextError(f"unknown setting {key!r}; known: {', '.join(DEFAULTS)}")
        coerced = None if value is None else _coerce(key, value)
        if value is not None and coerced is None:
            raise ContextError(f"{key} = {value!r} is not allowed ({_allowed(key)})")
        wanted[key] = coerced
    lines = path().read_text().splitlines() if path().is_file() else []
    for key, value in wanted.items():
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        lines = [line for line in lines if not pattern.match(line)]
        if value is not None and value != DEFAULTS[key]:
            lines.append(f"{key} = {json.dumps(value) if isinstance(value, (str, list)) else value}")
    path().parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path(), ("\n".join(lines) + "\n" if lines else "").encode())
    after = load()
    return {"settings_changed": {key: [before[key], after[key]] for key in wanted},
            "file": str(path()), "applies": "from the next hook run",
            "overridden_by_environment": ["BCC_" + key.upper() for key in wanted if "BCC_" + key.upper() in os.environ]}


def report() -> dict:
    return {"file": str(path()), "file_exists": path().is_file(), "effective": load(), "defaults": DEFAULTS}
