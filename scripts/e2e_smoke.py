#!/usr/bin/env python3
"""End-to-end check against a real Codex binary, with no paid model requests.

Creates a throwaway CODEX_HOME whose model provider is a local synthetic Responses server,
installs this checkout exactly as `bcc install` does, and drives three turns through
`codex app-server`:

1. a large finished turn (250k tokens): a shell command, then an answer. Stop asks for a
   replacement; the model calls new_context; SessionStart restores the shadow context
   (the conversation so far) and the task card.
2. a small turn (40k tokens) that runs the same command three times: the loop guard tells the
   model, Stop asks for nothing, and the shadow body passes the (lowered) sidecar trigger, so a
   detached worker runs a real `codex exec` against the same synthetic server and commits a summary.
3. another large finished turn: the second replacement restores the sidecar summary.

The workspace AGENTS.md is larger than Codex's 32 KiB limit; a rule past the cut must reach the
model at startup and after the replacements. Your real ~/.codex, sessions and running Codex
processes are never touched.

    python3 scripts/e2e_smoke.py /path/to/codex [--keep]
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIRST = ("E2E_FIRST from now on always run the tests before committing\n\nnever edit notes/release.md without bumping "
         "the version\n\nlist the workspace files")
EDIT = "echo E2E_EDIT >> notes/release.md"
SECOND = "E2E_SECOND rename the output folder"
THIRD = "E2E_THIRD summarize the release notes"
FOURTH = "E2E_FOURTH export the report"
QUESTION = "E2E_QUESTION CSV or XLSX? Which format do you want? (recommended: CSV)"
REMINDER = "This turn is finished; the window holds about"  # the Stop reminder itself, not the skill naming it
NOTE = ("**BCC Change note** · T3 · 2026-11-09 15:20:33 UTC · `notes/release.md`\n- **Symbols:** `E2E_SYMBOL` (L3)\n"
        "- **Motive:** E2E_NOTE record the release\n- **Reason:** append only\n"
        "- **Risk:** none\n- **Tests:** none: notes only")
DIGEST = "E2E_DIGEST: the session listed the workspace files and renamed the output folder."
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Model(BaseHTTPRequestHandler):
    """Synthetic Responses API for the session and for the sidecar's codex exec."""
    requests: list[dict] = []
    sidecar_requests: list[dict] = []
    evidence_requests: list[dict] = []
    supervisor_requests: list[dict] = []
    usage = 250000

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = json.dumps(body.get("input", []), ensure_ascii=False)
        tools = _tools(body)
        done = {i.get("call_id") for i in body.get("input", []) if i.get("type", "").endswith("call_output")}
        shell = next((n for n in ("exec_command", "shell_command", "shell") if n in tools), None)
        if "BCC-EVIDENCE" in text:
            # Evidence summaries (evidence.py): one structured item per ref, quoting lines of the seq output.
            Model.evidence_requests.append(body)
            items = [{"ref": ref, "headline": "E2E_EVIDENCE 25000 lines counted", "findings": ["The output was truncated."],
                      "evidence": [{"label": "Line", "quote": "2\n3\n4"}], "next_step": "none"}
                     for ref in dict.fromkeys(re.findall(r"### (m\d+) ", text))]
            item = {"type": "message", "role": "assistant", "id": "mev", "content": [{"type": "output_text",
                                                                                      "text": json.dumps({"items": items})}]}
            return self._send(item, 3000)
        if "BCC-SUPERVISOR" in text:
            # The supervisor (auto): the screen passes the question, the judge answers it. The screen is slower than
            # the 5 s every other hook gets, so the check also shows that Codex waits for the longer Stop hook.
            Model.supervisor_requests.append(body)
            if "BCC-SUPERVISOR screen" in text:
                time.sleep(8)
                reply = {"problem": True, "kind": "needless_question", "confidence": 0.9, "evidence": ["Which format"],
                         "reason": "The request already names the format.", "lines": [{"from": 1, "to": 2}]}
            else:
                reply = {"verdict": "off_track", "kind": "needless_question", "user_request": FOURTH, "departure": [],
                         "simple_path": "Export as CSV.", "message_to_agent": "Export as CSV.", "answer": "CSV",
                         "basis": ["recommended: CSV"], "confidence": 0.9}
            item = {"type": "message", "role": "assistant", "id": "msv", "content": [{"type": "output_text",
                                                                                      "text": json.dumps(reply)}]}
            return self._send(item, 3000)
        if "BCC-SIDECAR" in text:
            Model.sidecar_requests.append(body)
            reply = json.dumps({"digest": DIGEST, "open_items": [], "kept_anchors": []})
            item = {"type": "message", "role": "assistant", "id": "msc", "content": [{"type": "output_text", "text": reply}]}
            return self._send(item, 3000)
        Model.requests.append(body)
        reset_call = f"c-new-{text.count('E2E_THIRD') and 3 or 1}"
        recent = json.dumps(body.get("input", [])[-3:], ensure_ascii=False)
        if "[change notes] This turn changes" in recent and "E2E_NOTE" not in text:
            # Reminded at the turn's first file change: the answer ends with the change note (the turn is never held).
            note = {"type": "message", "role": "assistant", "id": f"mn{len(Model.requests)}", "content": [
                {"type": "output_text", "text": NOTE}]}
            return self._send([note], Model.usage)
        if "without updating the task card" in recent and '"c-status' not in recent and _find_tool(body, "context_state"):
            # Asked with the next message for the statuses the last turn left unset: one card update, done with a note.
            listed = recent.split("The last turn worked on", 1)[1].split("without updating", 1)[0]
            ids = sorted(set(re.findall(r"\b(T\d+) \(", listed)))
            name, namespace = _find_tool(body, "context_state")
            item = {"type": "function_call", "name": name, "call_id": f"c-status-{len(Model.requests)}", "arguments": json.dumps(
                {"action": "card", "ops": [{"op": "set", "id": i, "status": "done", "note": "E2E verified"} for i in ids]})}
            if namespace:
                item["namespace"] = namespace
            return self._send(item, Model.usage)
        if REMINDER in json.dumps(body.get("input", [])[-3:], ensure_ascii=False) and "new_context" in tools and reset_call not in done:
            item = {"type": "function_call", "name": "new_context", "call_id": reset_call, "arguments": "{}"}
        elif "E2E_FIRST" in text and "E2E_SECOND" not in text and shell and "c-ls" not in done and "[segment type=shadow-context" not in text:
            item = {"type": "function_call", "name": shell, "call_id": "c-ls", "arguments": json.dumps(_ls(shell))}
        elif "E2E_FIRST" in text and "E2E_SECOND" not in text and shell and "c-arch" not in done and "[segment type=shadow-context" not in text:
            item = {"type": "function_call", "name": shell, "call_id": "c-arch",
                    "arguments": json.dumps(_ls(shell, "cat docs/architecture.md"))}
        elif "E2E_FIRST" in text and "E2E_SECOND" not in text and shell and "c-big" not in done and "[segment type=shadow-context" not in text:
            # A long output (Codex cuts it at about 10k tokens): summarized as evidence when the turn ends.
            item = {"type": "function_call", "name": shell, "call_id": "c-big", "arguments": json.dumps(_ls(shell, "seq 1 25000"))}
        elif "E2E_FIRST" in text and "E2E_SECOND" not in text and "c-card" not in done and "[segment type=shadow-context" not in text \
                and _find_tool(body, "context_state"):
            # The plugin's own MCP tool, called under approvalPolicy "never": it must run without asking.
            name, namespace = _find_tool(body, "context_state")
            item = {"type": "function_call", "name": name, "call_id": "c-card", "arguments": json.dumps(
                {"action": "card", "ops": [{"op": "checkpoint", "focus": "E2E_CARD checkpoint", "next_steps": ["finish"]}]})}
            if namespace:
                item["namespace"] = namespace
        elif "E2E_FOURTH" in text and "[BCC supervisor] The user wants" in recent:
            item = {"type": "message", "role": "assistant", "id": f"m{len(Model.requests)}",
                    "content": [{"type": "output_text", "text": "E2E_SUPERVISED exported as CSV"}]}
        elif "E2E_FOURTH" in text and "E2E_QUESTION" not in text:
            item = {"type": "message", "role": "assistant", "id": f"m{len(Model.requests)}",
                    "content": [{"type": "output_text", "text": QUESTION}]}
        elif "E2E_THIRD" in text and shell and "c-edit" not in done and "c-new-3" not in done:
            item = {"type": "function_call", "name": shell, "call_id": "c-edit", "arguments": json.dumps(_ls(shell, EDIT))}
        elif "E2E_SECOND" in text and "E2E_THIRD" not in text and shell and any(f"c-rep{n}" not in done for n in range(3)):
            repeat = next(n for n in range(3) if f"c-rep{n}" not in done)  # the same command three times: a loop
            item = {"type": "function_call", "name": shell, "call_id": f"c-rep{repeat}", "arguments": json.dumps(_ls(shell))}
        else:
            item = {"type": "message", "role": "assistant", "id": f"m{len(Model.requests)}",
                    "content": [{"type": "output_text", "text": "done"}]}
        self._send(item, Model.usage)

    def _send(self, item, usage):
        rid = f"r{len(Model.requests) + len(Model.sidecar_requests) + len(Model.evidence_requests)}"
        items = item if isinstance(item, list) else [item]
        events = [{"type": "response.created", "response": {"id": rid}},
                  *({"type": "response.output_item.done", "item": one} for one in items),
                  {"type": "response.completed", "response": {"id": rid, "usage": {
                      "input_tokens": usage, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 5,
                      "output_tokens_details": None, "total_tokens": usage + 5}}}]
        payload = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _ls(shell: str, command: str = "ls") -> dict:
    return {"exec_command": {"cmd": command}, "shell_command": {"command": command},
            "shell": {"command": ["bash", "-lc", command]}}[shell]


def _tools(body: dict) -> set[str]:
    found = set()

    def walk(tools):
        for tool in tools or []:
            if tool.get("tools"):
                walk(tool["tools"])
            else:
                found.add(tool.get("name", ""))
    walk(body.get("tools", []))
    for item in body.get("input", []):
        if item.get("type") == "additional_tools":
            walk(item.get("tools", []))
    return found


def _find_tool(body: dict, wanted: str) -> tuple[str, str | None] | None:
    """(name, namespace) of an offered tool whose name ends with `wanted` (MCP tools may sit in a
    namespace or carry an mcp__server__ prefix, depending on the Codex version)."""
    def walk(tools, namespace=None):
        for tool in tools or []:
            if tool.get("tools"):
                found = walk(tool["tools"], tool.get("name"))
                if found:
                    return found
            elif tool.get("name", "").endswith(wanted):
                return tool["name"], namespace
        return None
    found = walk(body.get("tools", []))
    for item in body.get("input", []):
        if not found and item.get("type") == "additional_tools":
            found = walk(item.get("tools", []))
    return found


def _call_output(call_id: str) -> str | None:
    """The output Codex sent back to the model for one synthetic tool call."""
    for request in Model.requests:
        for item in request.get("input", []):
            if item.get("call_id") == call_id and item.get("type", "").endswith("call_output"):
                return json.dumps(item.get("output"), ensure_ascii=False)
    return None


class AppServer:
    def __init__(self, codex: str, cwd: Path, env: dict, client: str = "bcc-e2e"):
        self.proc = subprocess.Popen([codex, "app-server"], cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.lines, self.replies, self.notes, self.sequence = queue.Queue(), {}, [], 0
        # Windows selectors cannot wait on subprocess pipes; a reader thread works on every platform.
        def read_lines():
            for line in self.proc.stdout:
                self.lines.put(line)
            self.lines.put(None)
        threading.Thread(target=read_lines, daemon=True).start()
        # clientInfo.name becomes the session's originator; "codex_vscode" is what the VS Code extension sends.
        self.call("initialize", {"clientInfo": {"name": client, "version": "1"}, "capabilities": {"experimentalApi": True}})
        self.proc.stdin.write(b'{"method":"initialized"}\n')
        self.proc.stdin.flush()

    def _pump(self, deadline: float):
        if time.monotonic() > deadline:
            raise TimeoutError
        try:
            line = self.lines.get(timeout=0.05)
        except queue.Empty:
            return
        if line is None:
            raise RuntimeError("codex app-server exited")
        value = json.loads(line)
        if "id" in value and "method" not in value:
            self.replies[value["id"]] = value
        else:
            self.notes.append(value)

    def call(self, method: str, params: dict):
        self.sequence += 1
        identifier = self.sequence
        self.proc.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}).encode() + b"\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + 30
        while identifier not in self.replies:
            self._pump(deadline)
        reply = self.replies.pop(identifier)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply["result"]

    def wait_turns(self, count: int, timeout: float = 90):
        deadline = time.monotonic() + timeout
        while sum(n.get("method") == "turn/completed" for n in self.notes) < count:
            self._pump(deadline)

    def close(self):
        self.proc.stdin.close()
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def typed(thread: str, text: str) -> dict:
    """A turn/start as a Codex client sends it: the message carries the client's random id (UUID version 4), which
    is how the plugin tells the user's words from another Codex task's message (shadow._ingest)."""
    return {"threadId": thread, "input": [{"type": "text", "text": text}], "clientUserMessageId": str(uuid.uuid4())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("codex", help="Codex executable to test")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory for inspection")
    parser.add_argument("--client", default="bcc-e2e", help="Client name sent to app-server; codex_vscode starts and "
                        "names the session as the VS Code extension does, and BCC's lines must then be plain text")
    args = parser.parse_args()
    # The CPU plan of every test run (resources.py); the Codex processes it starts inherit the binding.
    from better_codex_context.resources import bind_for_tests, describe
    planned = bind_for_tests()
    if planned:
        print(describe(planned), file=sys.stderr)
    codex = shutil.which(args.codex) or args.codex
    version = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    temp = Path(tempfile.mkdtemp(prefix="bcc-e2e-"))
    home, workspace, config_home = temp / "codex-home", temp / "workspace", temp / "config"
    for directory in (home, workspace, config_home / "better-codex-context"):
        directory.mkdir(parents=True)
    (workspace / "README.txt").write_text("synthetic workspace\n")
    (workspace / "notes").mkdir()
    (workspace / "notes" / "release.md").write_text("# Release notes\n")
    (workspace / "docs").mkdir()
    (workspace / "docs" / "architecture.md").write_text("# Architecture\nE2E_ARCH every order passes the risk gate.\n")
    # An AGENTS.md larger than Codex's 32 KiB project_doc_max_bytes, with a rule past the cut.
    (workspace / "AGENTS.md").write_text("# Synthetic project\n\n" + "".join(
        f"## Background {n}\n\n" + "Plain description of the module layout and history.\n" * 40 for n in range(20))
        + "## Vault\n\n- E2E_RULE: never write to the vault directory.\n")
    provider = (f'{{name="synthetic", base_url="http://127.0.0.1:{server.server_port}/v1", wire_api="responses", '
                'requires_openai_auth=false, supports_websockets=false}')
    (home / "config.toml").write_text(
        'model = "e2e-synthetic"\nmodel_provider = "synthetic"\nmodel_context_window = 475000\n'
        '[features]\nhooks = true\n[model_providers.synthetic]\nname = "synthetic"\nwire_api = "responses"\n'
        f'base_url = "http://127.0.0.1:{server.server_port}/v1"\nrequires_openai_auth = false\nsupports_websockets = false\n')
    # A low sidecar trigger so the short synthetic conversation is compressed, by the codex under test.
    (config_home / "better-codex-context" / "config.toml").write_text(
        f"sidecar_first_tokens = 20\nsidecar_min_trigger_tokens = 20\nevidence_min_tokens = 2000\nsidecar_codex = {json.dumps(codex)}\n"
        f"sidecar_extra_args = {json.dumps(['-c', 'model_provider=\"synthetic\"', '-c', 'model_providers.synthetic=' + provider])}\n")
    # Everything below, including `bcc install`, sees only the throwaway home.
    os.environ.update(CODEX_HOME=str(home), XDG_STATE_HOME=str(temp / "state"), XDG_CONFIG_HOME=str(config_home))
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "HOME", "USER", "SHELL", "LANG", "CODEX_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
                    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE"}}
    from better_codex_context.upgrade import upgrade
    checks, app = {}, None
    state = workspace / ".better-codex-context" / "state.sqlite3"
    try:
        installed = upgrade(codex, repo=ROOT, dist_root=temp / "dist", cwd=str(workspace))
        checks["install: 4 hooks trusted"] = installed["hooks_trusted"] == ["trusted"]
        import tomllib
        checks["install: under-development warning silenced"] = (
            tomllib.loads((home / "config.toml").read_text(encoding="utf-8")).get("suppress_unstable_features_warning") is True)
        if args.client == "codex_vscode":  # what the VS Code extension sets when it starts its Codex
            env["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"] = "codex_vscode"
        app = AppServer(codex, workspace, env, args.client)
        thread = app.call("thread/start", {"cwd": str(workspace), "approvalPolicy": "never", "sandbox": "read-only"})["thread"]["id"]
        app.call("turn/start", typed(thread, FIRST))
        app.wait_turns(1)
        Model.usage = 40000
        app.call("turn/start", typed(thread, SECOND))
        app.wait_turns(2)
        deadline = time.monotonic() + 90  # the sidecar and evidence workers run in the background
        while time.monotonic() < deadline and not (_digest(state) and _evidence(state)):
            time.sleep(0.5)
        Model.usage = 250000
        app.call("turn/start", typed(thread, THIRD))
        app.wait_turns(3)
        # The supervisor, fully automatic: the turn ends with a question it can answer, and goes on with the answer.
        with (config_home / "better-codex-context" / "config.toml").open("a") as settings_file:
            settings_file.write('supervisor = "auto"\n')
        Model.usage = 30000
        app.call("turn/start", typed(thread, FOURTH))
        app.wait_turns(4, timeout=240)
    except (TimeoutError, RuntimeError) as exc:
        checks[f"run without error ({type(exc).__name__}: {exc})"] = False
    finally:
        if app:
            app.close()
    notes = app.notes if app else []
    runs = [n["params"]["run"] for n in notes if n.get("method") == "hook/completed"]
    events = {run["eventName"] for run in runs}
    # A Stop run is "blocked" exactly when the plugin asks for the end-of-turn reset. Activity
    # lines ("BCC · …", coloured) arrive as systemMessage entries; a failure says "hook failed".
    failed = [run for run in runs if run.get("status") not in ("completed", "blocked") or any(
        entry.get("kind") == "error" or "hook failed" in entry.get("text", "") or "could not start" in entry.get("text", "")
        for entry in run.get("entries", []))]
    # One hook run can carry several activity lines (for example a finished background run
    # reported together with the current action), so look at every line.
    activity = [line for run in runs for entry in run.get("entries", []) for line in ANSI.sub("", entry.get("text", "")).splitlines()
                if line.startswith("BCC · ")]
    coloured = any("\x1b[" in entry.get("text", "") for run in runs for entry in run.get("entries", []))
    restores = [json.dumps(r, ensure_ascii=False) for r in Model.requests
                if "restored this task state after a context reset" in json.dumps(r, ensure_ascii=False)]
    items = [n["params"]["item"] for n in notes if n.get("method") == "item/completed"]
    checks["hooks ran: SessionStart, UserPromptSubmit, PostToolUse, Stop"] = \
        {"sessionStart", "userPromptSubmit", "postToolUse", "stop"} <= {e[0].lower() + e[1:] for e in events}
    checks["no hook failed"] = not failed
    gui = args.client == "codex_vscode"  # started as the extension does: its Hook stats dialog prints ANSI codes verbatim
    checks["activity lines for every action, in colour (plain text for VS Code)"] = coloured != gui and all(
        any(line.startswith("BCC · " + prefix) for line in activity)
        for prefix in ("Project instructions", "Organized your message", "Turn finished with about", "Context optimized",
                       "Loop guard", "Shadow context at", "Shadow context optimized in the background"))
    checks["activity wording never says compress"] = not any("compress" in line.lower() or "compact" in line.lower()
                                                             for line in activity)
    checks["large finished turns: the model called new_context twice"] = sum(
        "c-new" in json.dumps(i) or i.get("type") == "contextCompaction" for i in items) >= 2
    first_request = json.dumps(Model.requests[0], ensure_ascii=False) if Model.requests else ""
    checks["AGENTS.md rule beyond Codex's 32 KiB limit reached the model"] = "E2E_RULE" in first_request
    checks["and came back after each replacement"] = len(restores) >= 2 and all("E2E_RULE" in r for r in restores[:1] + restores[-1:])
    resident = "[segment type=resident-skill name=context-discipline"
    checks["resident skill (coding discipline) whole at the start and after every replacement"] = bool(restores) and all(
        resident in r and "### 16. Debug the Frontend in Chrome Through CDP First" in r for r in [first_request, *restores])
    checks["first replacement restored the conversation (shadow context)"] = bool(restores) and \
        "[segment type=shadow-context" in restores[0] and "E2E_FIRST" in restores[0] and "⟦m" in restores[0]
    checks["sidecar compression ran through codex exec"] = bool(Model.sidecar_requests) and bool(_digest(state))
    checks["second replacement restored the sidecar summary"] = len(restores) >= 2 and DIGEST.split(":")[0] in restores[-1]
    checks["evidence summary of the long output ran through codex exec"] = bool(Model.evidence_requests) and _evidence(state)
    checks["second replacement restored it with checked evidence"] = len(restores) >= 2 and "E2E_EVIDENCE" in restores[-1] \
        and "E1 · Line" in restores[-1] and "context_recall(ref=" in restores[-1]
    checks["protected reference listed after a replacement, not its content"] = len(restores) >= 1 and \
        "docs/architecture.md" in restores[-1] and "protected-references" in restores[-1] and "E2E_ARCH" not in \
        restores[-1].split("protected-references", 1)[1]
    checks["change note reminded while working, captured and restored after the replacement"] = len(restores) >= 2 and \
        "change-notes" in restores[-1] and "E2E_NOTE" in restores[-1] and "symbols: `E2E_SYMBOL`" in restores[-1] and \
        "(L3)" not in restores[-1] and any("Change notes: reminded the model" in a for a in activity)
    card_output = _call_output("c-card") or ""
    checks["MCP task card call runs under approval policy never"] = "changed" in card_output and \
        "approval" not in card_output and any(a.startswith("BCC · Task card organized") for a in activity)
    status_outputs = [_call_output(c) or "" for c in sorted({i.get("call_id", "") for r in Model.requests for i in r.get("input", [])
                                                          if str(i.get("call_id", "")).startswith("c-status")})]
    checks["task statuses: started at the first tool call, left unset ones asked for with the next message"] = \
        any(a.startswith("BCC · Task card: ") and a.endswith("in progress (work started)") for a in activity) and \
        any(a.startswith("BCC · Task card: asked the model to set") for a in activity) and \
        any("changed" in output for output in status_outputs)
    checks["supervisor (auto) answered a question at the turn end and the turn went on with it"] = \
        len(Model.supervisor_requests) == 2 and any(  # the title, then the eye with the answer beside it
            re.search(r"BCC · Supervisor · auto mode · answered for you\n\n.*▄▄▄.*  answer: CSV\n", ANSI.sub("", entry.get("text", "")))
            for run in runs for entry in run.get("entries", [])) and \
        any("E2E_SUPERVISED" in json.dumps(r.get("input", [])) or "[BCC supervisor] The user wants" in json.dumps(r.get("input", [])[-3:])
            for r in Model.requests[-2:])
    checks["task card restored after each replacement"] = len(restores) >= 2 and all("task-card" in r for r in restores)
    if state.exists():
        with sqlite3.connect(state) as db:
            rows = db.execute("SELECT id,class FROM requirements ORDER BY id").fetchall()
            tools = db.execute("SELECT count(*) FROM events WHERE kind='tool'").fetchone()[0]
        checks["all user messages are in the ledger"] = len(rows) >= 4
        checks["standing rule recognized"] = any(c == "constraint" for _, c in rows)
        checks["shell result archived"] = tools >= 1
    else:
        checks["state database created"] = False
    reminders = sum(REMINDER in json.dumps(r.get("input", [])[-3:], ensure_ascii=False) for r in Model.requests)
    checks["only the two large turns were asked to replace the window"] = reminders == 2
    checks["constraint check: editing notes/release.md reminded the model of its rule"] = any(
        "[constraint check]" in json.dumps(r.get("input", []), ensure_ascii=False)
        and "notes/release.md" in json.dumps(r.get("input", []), ensure_ascii=False) for r in Model.requests)
    checks["loop guard: the model saw a note after the third identical run"] = any(
        "[loop guard]" in json.dumps(r.get("input", []), ensure_ascii=False) for r in Model.requests)
    hook_log = temp / "state" / "better-codex-context" / "hook-errors.log"
    checks["hook error log empty"] = not hook_log.exists() or not hook_log.read_text(encoding="utf-8").strip()
    print(json.dumps({"codex": version, "model_requests": len(Model.requests), "sidecar_requests": len(Model.sidecar_requests), "evidence_requests": len(Model.evidence_requests),
                      "hook_events": sorted(events), "checks": checks,
                      "problem_hook_runs": [{k: r.get(k) for k in ("eventName", "status", "entries")} for r in failed],
                      "activity_lines": [line.split("\n")[0] for line in activity],
                      "hook_errors": hook_log.read_text(encoding="utf-8")[-1500:] if hook_log.exists() else None,
                      "temp": str(temp) if args.keep else None}, ensure_ascii=False, indent=2))
    server.shutdown()
    if not args.keep:
        shutil.rmtree(temp, ignore_errors=True)
    return 0 if all(checks.values()) else 1


def _evidence(state: Path) -> bool:
    try:
        with sqlite3.connect(state) as db:
            return bool(db.execute("SELECT 1 FROM evidence_summaries LIMIT 1").fetchone())
    except sqlite3.Error:
        return False


def _digest(state: Path) -> bool:
    if not state.exists():
        return False
    try:
        with sqlite3.connect(state) as db:
            return bool(db.execute("SELECT 1 FROM shadow_digests LIMIT 1").fetchone())
    except sqlite3.Error:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
