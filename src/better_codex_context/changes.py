"""Change notes: why code changed, written by Codex itself in a tag the plugin captures.

A replaced window keeps the conversation and short excerpts, not every diff and the reasoning
behind it. What a later window most needs about a change is its purpose: the task it served, why it
was needed, why this approach, what it risks and whether it still needs tests. Codex writes that
right after each change, in its next message while it works (not collected at the end of the
answer), as plain markdown so the terminal renders it as a short list (Codex prints HTML-like tags
verbatim, which is why the older `<bcc-change>` tag looked raw):

    **BCC Change note** · T3 · 2026-11-09 15:20:33 UTC · `src/auth/session.py`, `src/auth/token.py`
    - **Symbols:** add `Session.refresh`; edit `Session.is_expired`, `TOKEN_TTL`; del `legacy_ttl`
    - **Motive:** why the change was needed
    - **Reason:** why this approach (alternatives rejected)
    - **Risk:** what could break, who is affected
    - **Tests:** needed: … | done: … | none: …

The older `<bcc-change files="…" task="T3"> motive: … </bcc-change>` tag is still read. The plugin
reads notes from the session log at every tool call (shadow sync; announced right then, so the user
sees them during the turn) and from the final answer (Stop's last_assistant_message), stores them
as K1, K2 … (recallable with context_recall), puts the newest notes into every
restored window, links them from the working set, and replaces each block in the shadow
conversation with a one-line reference. The first file change of a turn reminds Codex to end the answer
with a note (a finished turn is never held for one). The title carries the current UTC time
(`2026-11-09 15:20:33 UTC`); a note without one gets the time the plugin captured it. A note names what
changed by its symbols, never by line numbers, which shift with the next edit.
"""
from __future__ import annotations

import json
import os
import re

from .util import canonical, digest, token_measure, utc_now

TAG = re.compile(r"<bcc-change\b([^>]*)>(.*?)</bcc-change>", re.S | re.I)   # the older tag form
ATTR = re.compile(r"(\w+)\s*=\s*[\"“]([^\"”]*)[\"”]")
# The markdown form: a title line "**BCC Change note** · T3 · `a.py`, `b.ts`" ("BCC" optional; bold, heading or list
# marker optional) followed by field lines. A title without a field line right after it is ordinary prose.
HEADER = re.compile(r"^[ \t]*(?:[-*+>][ \t]+)?(?:#{1,6}[ \t]+)?\*{0,2}[ \t]*(?:bcc[ \t]+)?change[ \t]+note\b[ \t]*\*{0,2}"
                    r"(?P<rest>[^\n]*)$", re.I | re.M)
# "- **Motive:** …", "**Motive**: …", "motive: …"
FIELD = re.compile(r"^\s*(?:[-*+]|\d+[.)])?\s*\**\s*(symbols?|motive|reason|risk|tests?)\s*\**\s*[:：]\s*\**\s*(.*)$", re.I)
# Line numbers a model may still write next to a symbol ("foo:42", "L40-L58", "lines 40-58", "第 40 行"): dropped.
LINE_NUMBERS = re.compile(r"(?::\d+(?:[-–:]\d+)*|#?L\d+(?:-L?\d+)?|\(?\b(?:lines?|ln)\.?\s*\d+(?:\s*[-–]\s*\d+)?\)?|"
                          r"第\s*\d+(?:\s*[-–至到]\s*\d+)?\s*行)", re.I)
TICKED = re.compile(r"`([^`]+)`")
# A line location after a path (`a.py:42`, `a.py:42-58`, `a.py:42:7`, `a.py#L42-L58`): a note names files,
# and a kept suffix would make "a.py:42" a different file that never covers the edited a.py.
LOCATION = re.compile(r"(?::\d+(?:[-–:]\d+)*|#L\d+(?:-L?\d+)?)$")
# Only a leading "./" goes: lstrip("./") also ate the dot of ".github/…" and the "/" of absolute paths.
DOT_SLASH = re.compile(r"^(?:\./)+")
TASK = re.compile(r"\b(?:task\s+)?(T\d+)\b", re.I)
# The UTC time in the title: "2026-11-09 15:20:33 UTC", also ISO ("2026-11-09T15:20:33.5Z"), stored as the first form.
TIME = re.compile(r"`?(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:\s*UTC|Z)?`?", re.I)
FIELDS = ("motive", "reason", "risk", "tests")
SYMBOLS_LIMIT = 400
# What happened to each symbol: added, edited or deleted. A deleted function can no longer be found in the code, only
# in the note that says so (materials.search_state searches notes). Keywords in English or Chinese, or +/~/- before it.
ACTIONS = {"add": re.compile(r"^(?:add(?:ed)?|new|created?|新增|增加|添加)(?=\W|$)", re.I),
           "edit": re.compile(r"^(?:edit(?:ed)?|changed?|modif(?:y|ied)|mod|updated?|修改|改动|更新)(?=\W|$)", re.I),
           "del": re.compile(r"^(?:del(?:eted)?|remov(?:e|ed)|drop(?:ped)?|删除|移除|删掉)(?=\W|$)", re.I)}
PREFIX = {"+": "add", "~": "edit", "-": "del"}


# A symbol is a name from the code: written without backticks, only an identifier-like token counts (a dotted, scoped
# or path-like name, or a key), so a sentence of description ("新增隔离回退目录") never becomes a symbol.
IDENTIFIER = re.compile(r"[+~-]?[A-Za-z_$][\w$]*(?:(?:\.|::|->|/|#)[\w$]+)*(?:\(\))?")


def _symbols(text: str) -> str:
    """'add `a`; edit `b`, `c`; del `d`' from what the model wrote (keywords, or +/~/- before a symbol). When any name is
    in backticks, only names in backticks count; without backticks only identifier-like tokens do. Prose is dropped,
    and unmarked symbols stay as written, without a guessed action. (Seen on a real session: a Chinese sentence under
    Symbols was stored as one symbol.)"""
    groups, plain = {"add": [], "edit": [], "del": []}, []
    ticked = "`" in text
    for part in re.split(r"[;；|]\s*", text):
        part = part.strip(" ,，。.")
        action = next((name for name, pattern in ACTIONS.items() if pattern.match(part)), None)
        if action:
            part = ACTIONS[action].sub("", part, count=1).lstrip(" :：")
        found = (re.findall(r"([+~-]?)`([^`]+)`", part) if ticked else
                 [(t[0], t[1:]) if t[0] in PREFIX else ("", t) for t in IDENTIFIER.findall(part)
                  if not ACTIONS["add"].match(t) and not ACTIONS["edit"].match(t) and not ACTIONS["del"].match(t)])
        for mark, name in found:
            if not mark and name[:1] in PREFIX and len(name) > 1:
                mark, name = name[0], name[1:]
            name = name.strip()
            if name:
                (groups[PREFIX.get(mark) or action] if (mark or action) else plain).append(f"`{name}`")
    out = [f"{name} " + ", ".join(dict.fromkeys(items)) for name, items in groups.items() if items]
    return "; ".join(out + ([", ".join(dict.fromkeys(plain))] if plain else []))
FIELD_LIMIT = 600


def _fields(lines: list[str]) -> dict:
    """Field values from note lines; an indented line without a field name continues the previous field."""
    fields, current = {}, None
    for line in lines:
        match = FIELD.match(line)
        if match:
            name = match.group(1).lower()
            current = "tests" if name.startswith("test") else "symbols" if name.startswith("symbol") else name
            fields[current] = match.group(2).strip()
        elif current and line.strip():
            fields[current] = (fields[current] + " " + line.strip()).strip()
    # Bold markers the model left around a value ("**Motive:** **text**") are formatting, not content.
    return {k: v.replace("**", "").strip() for k, v in fields.items()}


def _markdown_blocks(text: str) -> list[tuple[int, int, str, dict]]:
    """(start, end, title rest, fields) for each markdown note: the title line, then field lines and
    their indented continuations; blank lines inside are allowed when another field follows."""
    blocks = []
    for header in HEADER.finditer(text):
        if blocks and header.start() < blocks[-1][1]:
            continue
        lines, end, position, pending = [], header.end(), header.end(), []
        for line in text[header.end():].split("\n")[1:]:
            position += len(line) + 1
            if not line.strip():
                pending.append(line)
                continue
            if FIELD.match(line) or (lines and not pending and line[:1].isspace()):
                lines += pending + [line]
                pending, end = [], position
                continue
            break
        if any(FIELD.match(line) for line in lines):
            blocks.append((header.start(), end, header.group("rest"), _fields(lines)))
    return blocks


def _blocks(text: str) -> list[tuple[int, int, dict]]:
    """(start, end, note) for every note in `text`, both forms, in order of appearance."""
    found = []
    for match in TAG.finditer(text):
        values = {k.lower(): v.strip() for k, v in ATTR.findall(match.group(1))}
        files = [f for f in re.split(r"[,\s]+", values.get("files", "")) if f.strip()]
        found.append((match.start(), match.end(), files, values.get("task", ""), "", _fields(match.group(2).strip().splitlines())))
    for start, end, rest, fields in _markdown_blocks(text):
        if any(s <= start < e for s, e, *_ in found):
            continue  # a markdown-looking title inside an old tag
        stamp = TIME.search(rest)
        rest = TIME.sub(" ", rest)  # neither a file name nor a task id
        files = TICKED.findall(rest)
        task = TASK.search(rest)
        if not files:
            plain = TASK.sub(" ", rest)
            files = [f for f in re.split(r"[,\s·—–|]+", plain) if re.search(r"[./]", f) and re.search(r"\w", f)]
        found.append((start, end, files, task.group(1).upper() if task else "",
                      f"{stamp.group(1)} {stamp.group(2)}" if stamp else "", fields))
    notes = []
    for start, end, files, task, time, fields in sorted(found, key=lambda item: item[0]):
        files = [DOT_SLASH.sub("", LOCATION.sub("", f.strip().strip("`'\"*"))) for f in files if f.strip().strip("`'\"*")]
        if not files and not any(fields.values()):
            continue
        note = {"files": files[:20], "task": task[:20], "time": time,
                **{name: " ".join(fields.get(name, "").split())[:FIELD_LIMIT] for name in FIELDS}}
        symbols = re.sub(r"\(\s*[,;]?\s*\)", "", LINE_NUMBERS.sub("", fields.get("symbols", "")))
        symbols = _symbols(re.sub(r"\s+([,;])", r"\1", " ".join(symbols.split())).strip(" ,;"))
        if symbols:  # only when present: a note without symbols keeps the sha it had before the field existed
            note["symbols"] = symbols[:SYMBOLS_LIMIT]
        notes.append((start, end, note))
    return notes


def parse(text: str) -> list[dict]:
    return [note for _, _, note in _blocks(text or "")]


def capture(store, text: str) -> list[dict]:
    """Store the notes in `text`; returns the newly stored ones (a note seen again is not duplicated)."""
    stored = []
    for note in parse(text):
        sha = digest(canonical(note))
        if store.db.execute("SELECT 1 FROM change_notes WHERE scope=? AND sha=?", (store.scope, sha)).fetchone():
            continue
        number = store.db.execute("SELECT count(*) FROM change_notes WHERE scope=?", (store.scope,)).fetchone()[0] + 1
        seq = store.db.execute("SELECT coalesce(max(seq),0) FROM events WHERE scope=?", (store.scope,)).fetchone()[0]
        identifier = f"K{number}"
        # The model's own UTC time, or the moment the plugin captured it (never part of the sha: a note read
        # again later must not count as new).
        note = {**note, "time": note["time"] or utc_now()}
        store.db.execute("INSERT INTO change_notes(scope,id,sha,files,task,motive,reason,risk,tests,seq,time,symbols) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (store.scope, identifier, sha, json.dumps(note["files"]), note["task"], note["motive"], note["reason"],
                          note["risk"], note["tests"], seq, note["time"], note.get("symbols", "")))
        stored.append({"id": identifier, **note})
    return stored


def absorb(store, text: str) -> str:
    """Capture the notes in an answer and replace each block with a one-line reference (shadow body)."""
    lowered = (text or "").lower()
    if "<bcc-change" not in lowered and "change note" not in lowered:
        return text
    blocks = _blocks(text)
    if not blocks:
        return text
    capture(store, text)
    for start, end, note in reversed(blocks):
        row = store.db.execute("SELECT id FROM change_notes WHERE scope=? AND sha=?", (store.scope, digest(canonical(note)))).fetchone()
        name = row["id"] if row else "change note"
        files = ", ".join(note["files"][:4]) or "files of this answer"
        symbols = f" — {_clip(note['symbols'], 120)}" if note.get("symbols") else ""
        text = (text[:start] + f"⟦{name}⟧ change note: {files}{symbols}" + (f" (task {note['task']})" if note["task"] else "")
                + text[end:])
    return text


def count(store) -> int:
    return store.db.execute("SELECT count(*) FROM change_notes WHERE scope=?", (store.scope,)).fetchone()[0]


def notes(store, newest_first: bool = True) -> list[dict]:
    rows = store.db.execute("SELECT * FROM change_notes WHERE scope=? ORDER BY rowid " + ("DESC" if newest_first else ""),
                            (store.scope,)).fetchall()
    return [{**dict(row), "files": json.loads(row["files"])} for row in rows]


def get(store, identifier: str) -> dict | None:
    row = store.db.execute("SELECT * FROM change_notes WHERE scope=? AND id=?", (store.scope, identifier.upper())).fetchone()
    return {**dict(row), "files": json.loads(row["files"])} if row else None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def describe(note: dict, limit: int = 240) -> str:
    head = (f"{note['id']} " + (f"[{note['time']} UTC] " if note.get("time") else "")
            + (", ".join(note["files"][:6]) or "(no files named)") + (f" — task {note['task']}" if note["task"] else ""))
    body = ([f"  symbols: {_clip(note['symbols'], limit)}"] if note.get("symbols") else []) + \
        [f"  {name}: {_clip(note[name], limit)}" for name in FIELDS if note.get(name)]
    return "\n".join([head, *body])


def render(store, budget: int) -> tuple[str, dict]:
    """The change-notes segment of a fresh window: newest notes in full within `budget` tokens, the
    rest as id ranges. Every replacement repeats it, so the reasons stay attached to the code."""
    every = notes(store)
    if not every or budget <= 0:
        return "", {"notes": 0, "shown": 0}
    from .card import id_ranges
    head = ["[segment type=change-notes relevance=why-code-changed]",
            "Why code changed in this task (change notes, newest first; context_recall ref=K# reads one in full). Keep "
            "these motives, risks and pending tests in mind before changing the same code again:"]
    shown, used = [], token_measure("\n".join(head))["count"]
    from .card import direction, direction_label
    change, divided = direction(store), False
    for note in every:
        text = describe(note)
        if change and not divided and note.get("seq", 0) <= change["seq"]:  # newest first: the first older note
            divided = True
            text = (f"— The notes below predate {direction_label(change)}: their motives belong to the earlier "
                    "direction. —\n" + text)
        cost = token_measure(text)["count"]
        if used + cost > budget and shown:
            break
        shown.append(text)
        used += cost
    rest = [n["id"] for n in every[len(shown):]]
    lines = head + shown + ([f"Older change notes: {id_ranges(list(reversed(rest)))}"] if rest else []) + ["[/segment]"]
    return "\n".join(lines), {"notes": len(every), "shown": len(shown), "ids": [n["id"] for n in every[:len(shown)]]}


def _covers(note_files: list[str], path: str) -> bool:
    if not note_files:
        return True  # a note without files covers the whole answer
    path = path.replace(os.sep, "/")
    return any(f == path or path.endswith("/" + f) or f.endswith("/" + path) for f in note_files)


def latest_for(store) -> dict:
    """path → id of the newest note that names it (for the working set)."""
    found = {}
    for note in notes(store, newest_first=False):
        for path in note["files"]:
            found[path] = note["id"]
    return found


def note_for(path: str, index: dict) -> str | None:
    path = path.replace(os.sep, "/")
    if path in index:
        return index[path]
    return next((k for f, k in reversed(list(index.items())) if path.endswith("/" + f) or f.endswith("/" + path)), None)


def turn_start(store, turn_id) -> int | None:
    row = store.db.execute("SELECT source_key FROM hook_turns WHERE scope=? AND turn_id=?", (store.scope, turn_id)).fetchone() \
        if turn_id else None
    if row is None:
        return None
    seq = store.db.execute("SELECT min(seq) FROM events WHERE scope=? AND kind='user' AND (source_key=? OR source_key LIKE ?)",
                           (store.scope, row["source_key"], row["source_key"] + ":%")).fetchone()[0]
    return seq


def missing(store, turn_id) -> list[str]:
    """Files changed in this turn that no change note written in this turn names."""
    start = turn_start(store, turn_id)
    if start is None:
        return []
    from .scopes import edited_paths
    changed = []
    for row in store.db.execute("SELECT metadata FROM events WHERE scope=? AND kind='tool' AND seq>? ORDER BY seq", (store.scope, start)):
        meta = json.loads(row["metadata"])
        changed += edited_paths(store, str(meta.get("command") or ""), str(meta.get("tool_name") or ""))
    if not changed:
        return []
    written = [n for n in notes(store) if n["seq"] >= start]
    return [p for p in dict.fromkeys(changed) if not any(_covers(n["files"], p) for n in written)]


def note_reminder(paths: list[str]) -> str:
    """Said once per turn, at its first file change (hooks.edit_reminders): while the model still works, not
    after the turn ended, when a note written on request is worth little."""
    listed = ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
    return ("[change notes] This turn changes " + listed + ". Right after each coherent change, write its change note in "
            "your next message (not saved up for the end of the answer): **BCC Change note** · T# · YYYY-MM-DD HH:MM:SS UTC · "
            "`path`, then the bullets - **Symbols:** (the names of the classes, methods, functions, variables or keys as "
            "they are in the code, in backticks, marked add, edit or del in English, e.g. add `a`; edit `b`; del `c`; never a "
            "description, never line numbers) - **Motive:** - "
            "**Reason:** - **Risk:** - **Tests:** (title and field names exactly so), the last four one to three concrete "
            "sentences each in the user's language.")


# ------------------------------------------------------------------ code comments

# Comment syntax by extension; files of other types (Markdown, JSON, lock files …) are not checked.
HASH = ("py", "pyi", "sh", "bash", "zsh", "rb", "pl", "r", "toml", "yaml", "yml", "cfg", "ini", "tf", "ex", "exs", "nim", "cmake")
SLASH = ("js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts", "rs", "go", "java", "kt", "kts", "c", "cc", "cpp", "cxx", "h",
         "hh", "hpp", "cs", "swift", "scala", "dart", "php", "m", "mm", "zig", "sol", "proto", "groovy", "gradle", "css", "scss", "less")
DASH = ("sql", "lua", "hs", "elm", "ada")
TAGS = ("html", "htm", "xml", "vue", "svelte")
PATCH_FILE = re.compile(r"^\*\*\* (Add|Update) File: (.+)$")


def _style(path: str):
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return "hash" if ext in HASH else "slash" if ext in SLASH else "dash" if ext in DASH else "tag" if ext in TAGS else None


def _is_comment(line: str, style: str) -> bool:
    text = line.strip()
    if style == "hash":
        return text.startswith("#") or text.startswith(('"""', "'''", 'r"""', 'f"""')) or "  # " in line
    if style == "slash":
        return text.startswith(("//", "/*", "*", "///", "#[doc")) or " // " in line or "/*" in text
    if style == "dash":
        return text.startswith("--") or " -- " in line
    return "<!--" in text or text.startswith(("//", "/*", "*"))


def added_lines(patch: str) -> dict:
    """path → added lines of an apply_patch text (Add File: every line; Update File: '+' lines)."""
    found, current = {}, None
    for line in (patch or "").splitlines():
        match = PATCH_FILE.match(line)
        if match:
            current = DOT_SLASH.sub("", match.group(2).strip())
            found.setdefault(current, [])
            continue
        if line.startswith("*** "):
            current = None if line.startswith(("*** Delete File", "*** End Patch")) else current
            continue
        if current and line.startswith("+"):
            found[current].append(line[1:])
    return found


def uncommented(store, turn_id, min_lines: int) -> list[dict]:
    """Code files this turn's patches added at least `min_lines` code lines to, without a single
    comment line among the additions."""
    start = turn_start(store, turn_id)
    if start is None or min_lines <= 0:
        return []
    totals = {}
    for row in store.db.execute("SELECT metadata FROM events WHERE scope=? AND kind='tool' AND seq>? ORDER BY seq", (store.scope, start)):
        meta = json.loads(row["metadata"])
        command = str(meta.get("command") or "")
        if meta.get("tool_name") != "apply_patch" and not command.lstrip().startswith("*** Begin Patch"):
            continue
        for path, lines in added_lines(command).items():
            style = _style(path)
            if not style:
                continue
            code, comments = totals.get(path, (0, 0))
            for line in lines:
                if not line.strip():
                    continue
                if _is_comment(line, style):
                    comments += 1
                else:
                    code += 1
            totals[path] = (code, comments)
    return [{"path": p, "code": c} for p, (c, k) in totals.items() if c >= min_lines and k == 0]


def comment_reminder(files: list[dict]) -> str:
    """Said once per turn, when its patches first add comment_min_lines lines of code to a file without a comment."""
    listed = ", ".join(f"{f['path']} (+{f['code']} lines)" for f in files[:4]) + (f" and {len(files) - 4} more" if len(files) > 4 else "")
    return ("[code comments] No comment yet in the code this turn adds to " + listed + ". Comment what is not obvious "
            "(why, constraints, invariants, edge cases) as you write it, not line by line.")
