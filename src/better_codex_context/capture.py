"""`bcc capture -- CMD`: run a verbose command, archive its exact output and print a short excerpt.

The command runs as a plain child process of the caller, so Codex's own sandbox and approval
policy still apply. There is no shell, no MCP execution endpoint and no permission change.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import uuid

from .materials import excerpt
from .store import Store
from .util import ContextError, canonical, command_line, digest, python_command


def capture_hint(store: Store) -> str:
    """The exact prefix the model prepends to an argv to run it through the wrapper, in the syntax of the
    shell Codex runs commands in: sh on Linux and macOS, PowerShell on Windows."""
    argv = python_command(Path(__file__).with_name("__main__.py"), "--workspace", store.root, "--session", store.session,
                          "--agent", store.agent, "capture", "--")
    return command_line(argv)


def _preview(store: Store, refs: list[str]):
    first, tail, diagnostic = "", "", ""
    for ref in refs:
        raw = store.artifact(ref)[1]
        text = raw.decode("utf-8", errors="replace")
        if not first:
            first = excerpt(text[:1200], 500)
        tail = excerpt(text[-2500:], 700)
        if re.search(r"error|failed|exception|traceback|panic|timeout", text, re.I):
            diagnostic = excerpt(text, 1400)
    return "\n".join(dict.fromkeys(part for part in (first, diagnostic, tail) if part))


def capture_command(store: Store, arguments: list[str], timeout: float | None = None):
    if not arguments or not all(isinstance(arg, str) for arg in arguments):
        raise ContextError("capture requires an explicit command argument vector")
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ContextError("capture timeout must be a finite positive number")
    identity = uuid.uuid4().hex
    command = shlex.join(arguments)
    caller_cwd = Path.cwd().resolve()
    command_cwd = caller_cwd if caller_cwd.is_relative_to(store.root) else store.root
    with tempfile.NamedTemporaryFile(prefix=".capture-" + identity + "-stdout-", dir=store.directory, delete=False) as out, \
            tempfile.NamedTemporaryFile(prefix=".capture-" + identity + "-stderr-", dir=store.directory, delete=False) as err:
        uncertain, code = False, 127
        try:
            # No shell, permission overrides, new credentials, or MCP execution endpoint.
            process = subprocess.Popen(arguments, cwd=command_cwd, stdout=out, stderr=err)
        except OSError as exc:
            err.write(("Command did not start: " + type(exc).__name__).encode())
        else:
            try:
                code = process.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                uncertain = True
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                code = 124
        stdout_refs = store.archive_stream(out, "capture " + identity + " stdout")
        stderr_refs = store.archive_stream(err, "capture " + identity + " stderr")
    preview = "STDOUT (excerpt):\n" + _preview(store, stdout_refs) + "\nSTDERR (excerpt):\n" + _preview(store, stderr_refs)
    metadata = {"command": command, "cwd": str(command_cwd), "exit_code": code, "source_complete": False, "tool_name": "capture-wrapper",
                "execution_uncertain": uncertain,
                "stdout_parts": stdout_refs, "stderr_parts": stderr_refs}
    metadata["capture_fingerprint"] = digest(canonical([arguments, str(command_cwd), code, uncertain,
        [store.artifact_metadata(ref)["sha"] for ref in stdout_refs], [store.artifact_metadata(ref)["sha"] for ref in stderr_refs]]))
    source = canonical({"command": command, "exit_code": code, "stdout_parts": stdout_refs, "stderr_parts": stderr_refs,
                        "preview": preview, "encoding": "UTF-8 preview; exact original bytes retained in ordered parts"})
    event = store.append("capture:" + identity, "tool", source, metadata)
    for name in (out.name, err.name):
        try:
            Path(name).unlink(missing_ok=True)
        except OSError:
            pass
    message = ("[captured command output]\nCommand: " + excerpt(command, 500) + "\nExit: " + str(code)
               + "\nReceipt/original stream index: " + event["artifact"] + "\n" + preview
               + "\nRead exact ordered parts with context_recall before precise checks.\n")
    if uncertain:
        message += "Execution was interrupted or timed out; inspect its effects before retrying.\n"
    return {"exit_code": code if code >= 0 else 128 - code, "preview": message, "ref": event["artifact"], "uncertain": uncertain}
