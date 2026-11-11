"""`bcc` command line: installation, settings, diagnostics and read access to the task state."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .util import ContextError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bcc", description="Better Codex Context: shadow context replacement for "
                                     "Codex CLI (task card, working set, exact recall, zero-model resets).")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--workspace", default=None, help="Project root (default: $BCC_WORKSPACE, then the current directory)")
    parser.add_argument("--session", default="default", help="Codex session/thread id whose state to read")
    parser.add_argument("--agent", default="main", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    # The same options are accepted after the command too (`bcc card --session ID`).
    scoped = argparse.ArgumentParser(add_help=False)
    scoped.add_argument("--workspace", default=argparse.SUPPRESS, help="Project root (default: current directory)")
    scoped.add_argument("--session", default=argparse.SUPPRESS, help="Codex session id")
    scoped.add_argument("--agent", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    for name, text in (("install", "Package this checkout, install it into every Codex on this machine (CLI and VS Code "
                                    "extension), trust its hooks, apply settings and reload the running Codex daemon"),
                       ("upgrade", "Same as install; use after pulling a new version")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--codex", default=None, help="Install for this Codex executable only (default: every "
                                                             "Codex on this machine: the CLI on PATH and the one bundled "
                                                             "with each VS Code install)")
        command.add_argument("--dist-root", help="Where versioned packages are kept (default: <checkout>/dist)")
    activation = sub.add_parser("activate", help="Show (or --apply) the native TokenBudget settings this plugin needs")
    activation.add_argument("--codex", default="codex")
    activation.add_argument("--apply", action="store_true", help="Write the settings through Codex's config API")
    sub.add_parser("reload", help="Ask the running Codex daemon to reload ~/.codex/config.toml and plugins")
    diagnostic = sub.add_parser("doctor", help="Read-only health report: settings, hooks, AGENTS.md truncation, state",
                                parents=[scoped])
    diagnostic.add_argument("--codex", default="codex")
    card = sub.add_parser("card", help="Print the task card and working set of a session", parents=[scoped])
    card.add_argument("--budget", type=int, default=6000, help="Token budget for the card (default: 6000)")
    sub.add_parser("status", help="Print context_state(action=status) as JSON", parents=[scoped])
    read = sub.add_parser("recall", help="Print archived text: an archive ref, R0009, T3 or an event key", parents=[scoped])
    read.add_argument("ref")
    read.add_argument("--start", type=int, default=0)
    read.add_argument("--limit", type=int, default=4000)
    invoke = sub.add_parser("call", help="Call one MCP tool directly, e.g. call context_state '{\"action\":\"status\"}'", parents=[scoped])
    invoke.add_argument("tool")
    invoke.add_argument("arguments", help="JSON object")
    history = sub.add_parser("import-history", help="Import user messages from a workspace JSON/JSONL export", parents=[scoped])
    history.add_argument("path")
    wrapper = sub.add_parser("capture", help="Run a command, archive its full output, print a short excerpt", parents=[scoped])
    wrapper.add_argument("--timeout", type=float, help="Seconds before the command is stopped")
    wrapper.add_argument("arguments", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...]")
    package = sub.add_parser("package", help="Build a self-contained plugin directory (install does this for you)")
    package.add_argument("--output", required=True)
    check = sub.add_parser("verify", help="Check the state of a session: ledger vs. session log, card links, "
                           "constraints, archive, hook gaps", parents=[scoped])
    check.add_argument("--transcript", default=None, help="Session log (default: found by --session under ~/.codex)")
    view = sub.add_parser("shadow", help="Print the shadow context a replacement would restore now", parents=[scoped])
    view.add_argument("--transcript", help="Session log to sync first (default: the last one synced)")
    worker = sub.add_parser("sidecar-run", parents=[scoped])  # internal: the detached sidecar compression worker
    worker.add_argument("--run", type=int, required=True)
    summarizer = sub.add_parser("evidence-run", parents=[scoped])  # internal: the detached evidence-summary worker
    reviewer = sub.add_parser("supervisor-run", parents=[scoped])  # internal: the detached supervisor review worker
    reviewer.add_argument("--run", type=int, required=True)
    summarizer.add_argument("--run", type=int, required=True)
    sub.add_parser("hook")  # internal: run by Codex for every hook event
    sub.add_parser("serve")  # internal: the MCP server Codex starts
    return parser


def main(argv=None):
    if os.name == "nt" and not sys.flags.utf8_mode:
        # Windows Python reads and writes text in the locale code page; every plugin process runs in
        # UTF-8 mode (python_command passes -X utf8; this covers the pip-installed `bcc` command).
        import subprocess
        source = str(Path(__file__).resolve().parents[1])  # also when run from a plugin folder via launch.py
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in (source, os.environ.get("PYTHONPATH")) if p)}
        return subprocess.call([sys.executable, "-X", "utf8", "-m", "better_codex_context",
                                *(sys.argv[1:] if argv is None else argv)], env=env)
    args = _parser().parse_args(argv)
    if args.command == "hook":
        # Hook payloads carry their own cwd, and the process cwd may already be deleted.
        from .hookrun import run as run_hook
        print(json.dumps(run_hook(), ensure_ascii=False), flush=True)
        return 0
    workspace = args.workspace or os.environ.get("BCC_WORKSPACE") or os.getcwd()
    if getattr(args, "codex", None):
        from .util import find_codex
        args.codex = find_codex(args.codex)  # VS Code only: the extension's bundled Codex
    store = None
    try:
        if args.command in ("install", "upgrade"):
            from .upgrade import codex_targets, install
            return _print(install([args.codex] if args.codex else codex_targets(),
                                  dist_root=Path(args.dist_root) if args.dist_root else None))
        if args.command == "package":
            from .packaging import native_package
            return _print(native_package(Path(args.output)))
        if args.command == "activate":
            from . import native_budget
            if args.apply:
                return _print(native_budget.apply(args.codex, workspace))
            return _print({"plan": native_budget.plan(native_budget.read_config()), "apply_with": "bcc activate --apply"})
        if args.command == "reload":
            from .native_budget import codex_home
            from .upgrade import reload_running
            return _print(reload_running(codex_home()))
        if args.command == "doctor":
            from .diagnostics import doctor
            return _print(doctor(workspace, args.codex))
        from .store import Store
        store = Store(workspace, args.session, args.agent)
        if args.command == "serve":
            from .server import serve
            serve(store)
            return 0
        if args.command == "capture":
            from .capture import capture_command
            command = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            result = capture_command(store, command, args.timeout)
            sys.stdout.write(result["preview"])
            return result["exit_code"]
        if args.command == "sidecar-run":
            from .sidecar import execute
            return _print(execute(store, args.run))
        if args.command == "evidence-run":
            from .evidence import execute as summarize
            return _print(summarize(store, args.run))
        if args.command == "supervisor-run":
            from .supervisor import execute as supervise
            return _print(supervise(store, args.run))
        if args.command == "verify":
            from .verify import run as verify_run
            report = verify_run(store, args.session, args.transcript)
            _print(report)
            return 1 if report["result"] == "fail" else 0
        if args.command == "shadow":
            import time
            from . import shadow
            row = shadow.sync_state(store)
            transcript = args.transcript or (row["path"] if row else None)
            if transcript:
                shadow.sync(store, transcript, time.monotonic() + 30)
            block = shadow.render(store)
            print(block["text"] if block else "(no shadow context yet for this session)")
            if block:
                print(json.dumps({k: v for k, v in block.items() if k != "text"}), file=sys.stderr)
            return 0
        if args.command == "card":
            from . import card, workset
            text, info = card.render(store, args.budget)
            work, _ = workset.render(store, 1500)
            print(text + ("\n" + work if work else ""))
            print(json.dumps(info), file=sys.stderr)
            return 0
        from .service import call
        if args.command == "status":
            return _print(call(store, "context_state", {"action": "status"}))
        if args.command == "recall":
            from .materials import recall
            return _print(recall(store, args.ref, args.start, args.limit))
        if args.command == "call":
            return _print(call(store, args.tool, json.loads(args.arguments)))
        if args.command == "import-history":
            from .requirements import import_history
            return _print(import_history(store, args.path))
        raise ContextError("unknown command")
    except (ContextError, KeyError, ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()


def _print(value) -> int:
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
