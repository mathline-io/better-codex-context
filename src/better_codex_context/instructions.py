"""Project instructions that Codex cannot load.

Codex concatenates the AGENTS.md files from the repository root down to the working directory
and keeps only the first `project_doc_max_bytes` (32 KiB by default). Everything after that
never reaches the model, often mid-sentence: in one measured project 51 of 60 sections were
never seen. Rules written there are silently ignored.

This module finds the cut part, archives every cut section so the model can read it in full with
context_recall, and builds a budgeted block with the hard rules found in it (lines with must,
never, always, 必须, 禁止 …) plus an index of the sections. The SessionStart hook injects that
block into every fresh context window, next to the task card. The files are never modified.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import tomllib

from .util import digest, token_measure

DEFAULT_PROJECT_DOC_BYTES = 32 * 1024
HEADING = re.compile(rb"^#{1,6}[ \t]+\S.*$", re.M)
RULE = re.compile(r"(\bmust\b|\bnever\b|\balways\b|\bdo not\b|\bdon't\b|\bnot allowed\b|\bforbidden\b|"
                  r"\bshall\b|必须|务必|一律|始终|永远|总是|只能|只允许|不要|不得|不能|不准|不许|不可|禁止|严禁|切勿|"
                  r"严格|绝不|千万|硬约束|红线)", re.I)
MAX_RULE_CHARS = 180
INDEX_SHARE = 0.45


def codex_config() -> dict:
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def doc_limit(config: dict | None = None) -> int:
    value = (codex_config() if config is None else config).get("project_doc_max_bytes")
    return value if isinstance(value, int) and value >= 0 else DEFAULT_PROJECT_DOC_BYTES


def instruction_files(root: Path) -> list[Path]:
    """AGENTS.override.md or AGENTS.md per directory, from the repository root down to `root`."""
    root = Path(root)
    top = next((parent for parent in [root, *root.parents] if (parent / ".git").exists()), root)
    chain = [top, *reversed([p for p in root.parents if p.is_relative_to(top) and p != top])]
    if root != top:
        chain.append(root)
    files = []
    for directory in dict.fromkeys(chain):
        for name in ("AGENTS.override.md", "AGENTS.md"):
            candidate = Path(directory) / name
            if candidate.is_file():
                files.append(candidate)
                break
    return files


def loaded_parts(root: Path, limit: int | None = None) -> list[dict]:
    """Every instruction file with how many of its bytes Codex actually loads."""
    limit = doc_limit() if limit is None else limit
    parts, used = [], 0
    for path in instruction_files(root):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        kept = max(0, min(len(data), limit - used))
        while 0 < kept < len(data) and data[kept] & 0xC0 == 0x80:
            kept += 1  # never split a UTF-8 character
        used += kept
        parts.append({"path": path, "data": data, "kept": kept})
    return parts


def sections(data: bytes, start: int) -> list[dict]:
    """The cut part of one file, split at Markdown headings. The first entry continues the
    section in which the cut happened."""
    heads = [(m.start(), m.group().decode("utf-8", "replace").lstrip("#").strip()) for m in HEADING.finditer(data)]
    current = next((title for offset, title in reversed(heads) if offset <= start), "beginning of file")
    bounds = [(start, f"{current} (continued)")] + [(offset, title) for offset, title in heads if offset > start]
    result = []
    for index, (offset, title) in enumerate(bounds):
        end = bounds[index + 1][0] if index + 1 < len(bounds) else len(data)
        text = data[offset:end].decode("utf-8", "replace")
        if text.strip():
            result.append({"title": title, "offset": offset, "text": text})
    return result


STRONG = re.compile(r"(\bmust\b|\bnever\b|\bdo not\b|\bdon't\b|必须|禁止|严禁|不许|不得|不准|务必|切勿|绝不)", re.I)


def rules(text: str) -> list[str]:
    """Rule-like lines of a section, strongest first: emphasized lines (**…**, ★), then explicit
    prohibitions and obligations, then the rest; ties keep document order."""
    found = []
    in_code = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not stripped or stripped.startswith("#") or stripped.startswith("|---"):
            continue
        stripped = re.sub(r"^(?:[-*+]|\d+[.)、])\s+", "", stripped).strip()
        if len(stripped) < 6 or not RULE.search(stripped) or stripped[0] in "（）)，,、；;。":
            continue  # a continuation fragment, not a rule
        found.append(stripped if len(stripped) <= MAX_RULE_CHARS else stripped[:MAX_RULE_CHARS - 1] + "…")
    weight = lambda line: (line.startswith(("**", "★")) or "★" in line[:4]) * 2 + bool(STRONG.search(line))
    return sorted(found, key=weight, reverse=True)


def overflow(store, root: Path | None = None, budget: int = 2000) -> dict | None:
    """Archive the sections Codex cannot load and build the block for a fresh window.

    Returns None when everything fits. Otherwise {"text", "files", "sections", "rules_shown",
    "rules_found", "tokens"}.
    """
    root = Path(root or store.root)
    limit = doc_limit()
    cut = [p for p in loaded_parts(root, limit) if p["kept"] < len(p["data"])]
    if not cut or budget <= 0:
        return None
    index, per_section, files = [], [], []
    for part in cut:
        try:
            relative = str(part["path"].relative_to(root))
        except ValueError:
            relative = str(part["path"])
        files.append({"path": relative, "bytes": len(part["data"]), "loaded_bytes": part["kept"]})
        version = digest(part["data"])[:12]
        label = "§" if len(cut) == 1 else f"{relative} §"
        for section in sections(part["data"], part["kept"]):
            ref = store.put(section["text"], "instructions", f"{relative} § {section['title']}"[:200],
                            {"path": relative, "file_version": version, "offset": section["offset"]})
            size = len(section["text"].encode())
            index.append(f"- {label} {section['title'][:80]} ({size / 1024:.1f} KB) → context_recall(ref=\"{ref}\")")
            per_section.append([f"- [{section['title'][:32]}] {rule}" for rule in dict.fromkeys(rules(section["text"]))])
    total = sum(len(p["data"]) for p in loaded_parts(root, limit))
    head = (f"[segment type=instructions-overflow relevance=standing-rules]\nCodex loads only the first {limit // 1024} KiB of "
            f"this project's instructions ({total / 1024:.0f} KiB in total), so the parts below never reached you. They still "
            "apply. Follow these rules, and read a section in full before working in its area.")
    tail = "[/segment]"
    used = token_measure(head + tail)["count"] + 30  # the two list captions
    index_lines = []
    index_budget = used + int(budget * INDEX_SHARE)
    for line in index:
        cost = token_measure(line)["count"] + 1
        if used + cost > index_budget:
            break
        index_lines.append(line)
        used += cost
    if len(index_lines) < len(index):
        index_lines.append(f"- … {len(index) - len(index_lines)} more sections: context_recall(query=\"§\")")
        used += 20
    # Round-robin over sections, so every cut section contributes its first rules before any
    # section contributes more; shown in document order.
    chosen, depth, full = set(), 0, False
    while not full and any(len(found) > depth for found in per_section):
        for number, found in enumerate(per_section):
            if len(found) > depth:
                cost = token_measure(found[depth])["count"] + 1
                if used + cost > budget:
                    full = True
                    break
                chosen.add((number, depth))
                used += cost
        depth += 1
    rule_lines = [found[d] for number, found in enumerate(per_section) for d in range(len(found)) if (number, d) in chosen]
    found = [line for section in per_section for line in section]
    unique = len(found)
    parts = [head]
    if rule_lines:
        parts += ["Rules in the unloaded part" + (f" ({len(rule_lines)} of {unique}; the rest are in the sections below)"
                                                   if len(rule_lines) < unique else "") + ":"] + rule_lines
    parts += ["Unloaded sections:"] + index_lines + [tail]
    text = "\n".join(parts)
    return {"text": text, "files": files, "sections": len(index), "rules_shown": len(rule_lines), "rules_found": unique,
            "tokens": token_measure(text)["count"], "limit": limit}
