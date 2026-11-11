"""Deterministic signals for the loop guard (loops.py): which test command a shell command is, what
failure an output shows, and whether a test run improved on the previous one.

Everything here is conservative. A command is only normalized when it is a plain, known test
invocation; anything with pipes, redirections, variables or several commands keeps its literal
text. A failure fingerprint keeps what identifies the failure (exception type and message, failing
test ids, compiler error codes, asserted values) and drops what changes from run to run without
meaning anything (line numbers, timings, addresses, temporary paths, clock times). When nothing can
be parsed reliably the callers fall back to the original error-line signature, and when two runs
cannot be compared the answer is "unknown", never a guess.

Computed once when a tool result is archived (materials.record_tool) and kept in the event's
metadata, so the loop guard itself never re-reads large outputs.
"""
from __future__ import annotations

import re
import shlex

COMPLEX = re.compile(r"[|<>;&$`\n]|\(\(|\$\(")
PYTHON = re.compile(r"^python(?:3(?:\.\d+)?)?$")
NPM_LIKE = {"npm", "pnpm", "yarn", "bun"}


def command_key(command) -> str:
    """A key under which equivalent invocations of the same test command compare equal.

    `pytest -q`, `python -m pytest -q` and `python3 -m pytest -q` share `pytest -q`; `npm test`,
    `npm run test`, `pnpm test` and `yarn test` share `npm-test`. Arguments (test paths, flags) are
    kept in order. Everything else, and anything not plain, is its whitespace-normalized text."""
    text = " ".join(str(command or "").split())
    if not text or COMPLEX.search(text):
        return text
    try:
        words = shlex.split(text)
    except ValueError:
        return text
    if not words or "=" in words[0]:
        return text  # environment assignments change what runs
    head, rest = words[0].rsplit("/", 1)[-1], words[1:]
    if PYTHON.match(head) and rest[:2] == ["-m", "pytest"]:
        return " ".join(["pytest", *rest[2:]])
    if head in ("pytest", "py.test"):
        return " ".join(["pytest", *rest])
    if head in NPM_LIKE:
        if rest[:1] in (["test"], ["t"]):
            return " ".join(["npm-test", *rest[1:]])
        if rest[:2] == ["run", "test"]:
            return " ".join(["npm-test", *rest[2:]])
    if head == "npx" and rest[:1] in (["jest"], ["vitest"]):
        return " ".join(rest)
    return text


# ------------------------------------------------------------------ failure fingerprints

PYTEST_FAILED = re.compile(r"^(FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$", re.M)
JEST_FAILED = re.compile(r"^\s*(?:✕|×|✗)\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$", re.M)
VITEST_FILE = re.compile(r"^\s*FAIL\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)(?:\s+>\s+(.+?))?\s*$", re.M)
TSC = re.compile(r"^(\S+?\.[cm]?tsx?)(?:\(\d+,\d+\)|:\d+:\d+)\s*[-:]?\s*error\s+(TS\d+):\s*(.+)$", re.M)
EXCEPTION = re.compile(r"^(?:E\s+)?([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Failure)):\s*(.*)$", re.M)

TEMP = re.compile(r"(/tmp|/var/folders|/private/var)/[^\s:'\"]+|pytest-of-\w+/pytest-\d+[^\s:'\"]*|[A-Z]:\\\\Temp\\\\\S+")
LOCATION = re.compile(r"(\.[A-Za-z]{1,5}):\d+(?::\d+)?\b|\bline \d+\b")
VOLATILE = re.compile(r"\b0x[0-9a-fA-F]+\b|\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\b|"
                      r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z?\b|\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b")
LIMIT = 64 * 1024


def clean(text: str) -> str:
    """Drop what changes between runs without changing the failure; keep asserted values."""
    text = TEMP.sub("<tmp>", text)
    text = LOCATION.sub(lambda m: (m.group(1) + ":#") if m.group(1) else "line #", text)
    return " ".join(VOLATILE.sub("#", text).split())


def fingerprint(output: str) -> str | None:
    """A stable identity of the failure an output shows, or None when no supported format matched
    (callers then use the plain error-line signature)."""
    text = (output or "")[-LIMIT:]
    parts = []
    parts += [f"{kind} {test}" + (f" - {clean(message)[:160]}" if message else "") for kind, test, message in PYTEST_FAILED.findall(text)]
    parts += [f"tsc {path} {code}: {clean(message)[:160]}" for path, code, message in TSC.findall(text)]
    parts += [f"vitest {path}" + (f" > {clean(name)[:120]}" if name else "") for path, name in VITEST_FILE.findall(text)]
    parts += [f"jest {clean(name)[:120]}" for name in JEST_FAILED.findall(text)]
    if not parts:
        found = EXCEPTION.findall(text)
        if found:  # the last exception of a traceback names the failure
            kind, message = found[-1]
            parts.append(f"{kind}: {clean(message)[:200]}")
    if not parts:
        return None
    return " | ".join(sorted(set(parts)))[:600]


# ------------------------------------------------------------------ test results and progress

PYTEST_SUMMARY = re.compile(r"^=*\s*((?:\d+ (?:failed|passed|errors?|error|skipped|xfailed|xpassed|deselected|warnings?)"
                            r"(?:, )?)+)\s+in\s+[\d.]+s", re.M)
JEST_SUMMARY = re.compile(r"^Tests:\s+(.*\btotal)\s*$", re.M)
VITEST_SUMMARY = re.compile(r"^\s*Tests\s+((?:\d+ (?:failed|passed|skipped|todo)\s*\|?\s*)+)\(\d+\)", re.M)
COUNT = re.compile(r"(\d+) (failed|passed|errors?)")


def test_result(output: str) -> dict | None:
    """Counts and failing tests of a pytest, Jest or Vitest run, or None when no summary is found."""
    text = (output or "")[-LIMIT:]
    summary = None
    for pattern in (PYTEST_SUMMARY, JEST_SUMMARY, VITEST_SUMMARY):
        found = pattern.findall(text)
        if found:
            summary = found[-1] if isinstance(found[-1], str) else found[-1][0]
            break
    if summary is None:
        return None
    passed = failed = 0
    for number, word in COUNT.findall(summary):
        if word == "passed":
            passed += int(number)
        else:
            failed += int(number)
    failing = sorted({test for _, test, _ in PYTEST_FAILED.findall(text)}
                     | {f"{p} > {n}" if n else p for p, n in VITEST_FILE.findall(text)}
                     | {clean(n) for n in JEST_FAILED.findall(text)})
    return {"passed": passed, "failed": failed, "failing": failing[:50]}


def progress(older: dict | None, newer: dict | None) -> str:
    """Whether the newer of two runs of the same test command improved: "better", "same", "worse"
    or "unknown" (no comparable results)."""
    if not older or not newer:
        return "unknown"
    before, after = set(older.get("failing") or []), set(newer.get("failing") or [])
    if newer["failed"] < older["failed"] or (newer["passed"] > older["passed"] and newer["failed"] <= older["failed"]) \
            or (before and after < before):
        return "better"
    if newer["failed"] > older["failed"] or (after - before):
        return "worse"
    return "same"


def passing(result: dict | None) -> bool:
    return bool(result) and result["failed"] == 0 and result["passed"] > 0


# ------------------------------------------------------------------ code oscillation

def oscillating(versions: list[str]) -> bool:
    """A file that went back to a version it had before, with a different one in between (A → B → A)."""
    for index, version in enumerate(versions):
        if version in versions[:max(0, index - 1)] and versions[index - 1] != version:
            return True
    return False
