"""Which standing constraints apply to a file the agent just changed.

Keeping a user's rules is not enough; they have to reach the model at the moment it touches the
code they are about. A constraint's scope is either explicit (`paths` on the card, glob patterns
the model sets when the user names a module) or implied by path-like words in the constraint's own
text ("src/trading", "order_service.py"). Matching is deterministic. The plugin reminds the model
right after the edit, once per constraint and file in a turn; it never blocks a tool call, because a
wrong block would stall the work and a plugin cannot judge semantics. Constraints without any path
apply everywhere and already head the task card.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re

from .util import ContextError

MAX_PATHS = 20
EXTENSIONS = ("py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|kts|rb|php|c|cc|cpp|cxx|h|hh|hpp|cs|swift|m|mm|scala|sql|sh|bash|"
              "zsh|ps1|yaml|yml|toml|json|ini|cfg|conf|md|rst|vue|svelte|css|scss|less|html|proto|graphql|tf|lua|dart|ex|exs")
# A directory path ("src/trading/", "app/orders") or a file name with a known extension ("scale.ts").
PATH_WORD = re.compile(r"(?<![\w./:-])((?:[\w.-]+/)+[\w.*-]*|[\w-]+(?:\.[\w-]+)*\.(?:" + EXTENSIONS + r"))(?![\w/])", re.I)


def clean(paths) -> list[str]:
    """Validated explicit scope patterns for a constraint (relative globs)."""
    if not isinstance(paths, list) or len(paths) > MAX_PATHS or not all(isinstance(p, str) for p in paths):
        raise ContextError(f"paths must be a list of at most {MAX_PATHS} relative path patterns")
    result = []
    for raw in paths:
        pattern = raw.strip().replace("\\", "/")
        while pattern.startswith("./"):
            pattern = pattern[2:]
        if not pattern or len(pattern) > 200 or pattern.startswith(("/", "~")) or ".." in pattern.split("/"):
            raise ContextError("each path must be a relative pattern inside the workspace, at most 200 characters")
        result.append(pattern)
    return list(dict.fromkeys(result))


def explicit(item) -> list[str]:
    return [tag[5:] for tag in item.get("tags") or [] if tag.startswith("path:")]


def implied(text: str) -> list[str]:
    found = []
    for match in PATH_WORD.findall(text or ""):
        word = match.strip(".").rstrip("/")
        if word and "://" not in word and not word.startswith(("http", "www.")) and len(word) >= 4:
            found.append(word)
    return list(dict.fromkeys(found))


def _matches_explicit(path: str, pattern: str) -> bool:
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path, pattern.rstrip("/") + "/*")
    pattern = pattern.rstrip("/")
    return path == pattern or path.startswith(pattern + "/")


def _matches_implied(path: str, word: str) -> bool:
    if "/" in word:
        return ("/" + word.strip("/") + "/") in ("/" + path + "/")
    return os.path.basename(path).lower() == word.lower()


def applies(item, path: str) -> str | None:
    """The pattern through which `item` applies to `path` (workspace-relative), or None."""
    path = path.replace(os.sep, "/")
    for pattern in explicit(item):
        if _matches_explicit(path, pattern):
            return pattern
    for word in implied(item.get("text", "")):
        if _matches_implied(path, word):
            return word
    return None


def applicable(constraints, paths) -> list[dict]:
    """[{item, path, pattern}] for every active constraint whose scope covers a changed path."""
    hits = []
    for path in paths:
        for item in constraints:
            pattern = applies(item, path)
            if pattern:
                hits.append({"item": item, "path": path, "pattern": pattern})
    return hits


def edited_paths(store, command: str, tool_name: str) -> list[str]:
    """Workspace-relative files this tool call changed, parsed the same way as the working set."""
    from .workset import _place, parse_command, parse_patch
    text = command or ""
    observed = parse_patch(text) if tool_name == "apply_patch" or text.lstrip().startswith("*** Begin Patch") else parse_command(text)
    paths = []
    for action, raw, _ in observed or []:
        if action not in ("edit", "delete") or not raw or any(c in raw for c in "*?$`{}"):
            continue
        display, inside = _place(store, raw)
        if inside and not display.startswith(".better-codex-context"):
            paths.append(display.replace(os.sep, "/"))
    return list(dict.fromkeys(paths))[:MAX_PATHS]


def check(store, command: str, tool_name: str, turn_id: str | None) -> list[dict]:
    """New constraint hits for this tool call; each (constraint, file) is reported once per turn."""
    paths = edited_paths(store, command, tool_name)
    if not paths:
        return []
    from .card import items
    constraints = [i for i in items(store) if i["kind"] == "constraint" and i["status"] == "active"]
    fresh = []
    for hit in applicable(constraints, paths):
        cursor = store.db.execute("INSERT OR IGNORE INTO constraint_checks(scope,turn_id,item,path) VALUES(?,?,?,?)",
                                  (store.scope, turn_id or "", hit["item"]["id"], hit["path"]))
        if cursor.rowcount:
            fresh.append(hit)
    return fresh


def note(hits) -> str:
    """What the model reads right after the edit."""
    by_path = {}
    for hit in hits:
        by_path.setdefault(hit["path"], []).append(hit["item"])
    lines = ["[constraint check] Standing user constraints apply to the files you just changed:"]
    for path, constraints in by_path.items():
        lines.append(f"{path}:")
        for item in constraints:
            refs = ", ".join(item.get("ledger") or [])
            lines.append(f"- {item['id']} {' '.join(item['text'].split())[:600]}" + (f" ⟨{refs}⟩" if refs else ""))
    lines.append("Check that this change complies with each of them before you finish. If it cannot comply, stop and tell "
                 "the user instead of working around the constraint.")
    return "\n".join(lines)


def tags_with(tags: list[str], paths: list[str]) -> str:
    return json.dumps([t for t in tags if not t.startswith("path:")] + ["path:" + p for p in paths], ensure_ascii=False)
