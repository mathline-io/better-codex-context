"""Resident skills: rules that apply to all work, kept word for word in every fresh window.

Across a replacement Codex rebuilds its skill list (names, descriptions, paths) but not the text the model read: a
SKILL.md read earlier is gone with the old conversation, the model's own re-read is often cut short
(max_output_tokens), and the shadow shows the old read only as an excerpt or a summary. For a skill used once (making a
deck) that is right: it is read again when needed. A resident skill (the coding discipline) must never degrade, so its
whole text goes into the window, outside the shadow and its budget. Which skills are resident: setting resident_skills.

A partial skill reads like the whole one: Codex keeps the head and the tail of a cut output, and a ranged read
(sed -n '1,220p') looks complete too. So a skill read that missed part of the file is flagged at once (partial), and
after a replacement the shadow never shows an excerpt or a summary of a skill, only that it must be read again in full.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import settings

OWN = Path(__file__).resolve().parents[2] / "skills"


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def locate(name: str, root: Path | None = None) -> Path | None:
    """The SKILL.md of a skill named `name` (as in its folder), searched where Codex keeps skills; the newest plugin
    version wins."""
    home = _codex_home()
    for path in (OWN / name / "SKILL.md", home / "skills" / name / "SKILL.md", home / "skills" / ".system" / name / "SKILL.md",
                 *([root / ".agents" / "skills" / name / "SKILL.md", root / ".codex" / "skills" / name / "SKILL.md"] if root else [])):
        if path.is_file():
            return path
    cached = [p for p in (home / "plugins" / "cache").glob(f"*/*/*/skills/{name}/SKILL.md") if p.is_file()]
    return max(cached, key=lambda p: p.stat().st_mtime) if cached else None


def _body(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def resident(root: Path | None = None) -> list[dict]:
    found = []
    for name in settings.load()["resident_skills"]:
        path = locate(name, root)
        try:
            text = _body(path.read_text(encoding="utf-8")) if path else ""
        except OSError:
            text = ""
        if text:
            found.append({"name": name, "path": str(path), "text": text})
    return found


def segment(root: Path | None = None) -> dict | None:
    """Every resident skill in full, most stable first in the window; never shortened (the shadow gives way instead)."""
    skills = resident(root)
    if not skills:
        return None
    text = "\n".join(
        f"[segment type=resident-skill name={s['name']} relevance=mandatory]\nThe resident skill {s['name']} ({s['path']}), "
        "word for word. It applies to all work in this session and stays in every context window, so do not read this "
        "SKILL.md again.\n" + s["text"] + "\n[/segment]" for s in skills)
    return {"text": text, "names": [s["name"] for s in skills]}


READ_VERB = re.compile(r"\b(?:cat|sed|head|tail|nl|less|more|bat|awk|Get-Content)\b")
SKILL_PATH = re.compile(r"""((?:[^\s'"`;|&<>(){},]*[/\\])?([\w.-]+)[/\\]SKILL\.md)(?![\w.])""")
SAMPLES = 12


def read(command: str | None) -> list[dict]:
    """The skills whose SKILL.md this tool command reads (name as its folder, path as written)."""
    if not command or not READ_VERB.search(command):
        return []
    found = {}
    for path, name in SKILL_PATH.findall(command):
        found.setdefault(name, path)
    return [{"name": name, "path": path} for name, path in found.items()]


def reads(command: str | None, names) -> bool:
    """Whether a recorded tool command read one of these skills' SKILL.md."""
    return any(skill["name"] in names for skill in read(command))


def _resolve(path: str, root: Path | None) -> Path:
    resolved = Path(os.path.expandvars(os.path.expanduser(path)))
    return resolved if resolved.is_absolute() or root is None else root / resolved


def partial(command: str, response, root: Path | None = None) -> list[dict]:
    """The skills this tool call read but did not get whole: lines spread over the file (the last one too) that the
    output lacks. Resident skills are left out: every window holds them in full."""
    output = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
    resident, missing = settings.load()["resident_skills"], []
    for skill in read(command):
        if skill["name"] in resident:
            continue
        try:
            text = _resolve(skill["path"], root).read_text(encoding="utf-8")
        except OSError:
            continue
        lines = [(n, line.strip()) for n, line in enumerate(text.splitlines(), 1) if len(line.strip()) >= 20]
        picked = lines[::max(1, len(lines) // SAMPLES)][:SAMPLES] + lines[-1:]
        absent = [n for n, line in picked if line not in output and json.dumps(line)[1:-1] not in output
                  and json.dumps(json.dumps(line)[1:-1])[1:-1] not in output]
        if absent:
            missing.append({**skill, "from_line": min(absent), "lines": len(text.splitlines())})
    return missing


def partial_note(missing: list[dict]) -> str:
    return "\n".join(
        f"Better Codex Context: that output does not hold the whole skill {m['name']} ({m['path']}): it lacks text from "
        f"about line {m['from_line']} of {m['lines']} (cut by the output limit or by the range read). A partial "
        "skill reads like the whole one; read the rest of the file before following this skill." for m in missing)


def shadow_note(ref: dict, resident) -> str | None:
    """What the shadow shows for an earlier read of a skill instead of an excerpt or a summary (None: not a skill read)."""
    skills = read(ref["command"])
    if not skills:
        return None
    others = [s for s in skills if s["name"] not in resident]
    if not others:
        return f"    ⟦{ref['ref']}⟧ read a resident skill; its whole text is in its own segment above"
    rest = f" The rest of this output: context_recall(ref=\"{ref['artifact']}\")." if ref["artifact"] else ""
    return (f"    ⟦{ref['ref']}⟧ read the skill " + ", ".join(f"{s['name']} ({s['path']})" for s in others) + ": its text is not "
            "kept in this window and whatever you remember of it is partial. Before using it again, read the whole "
            "SKILL.md." + rest)
