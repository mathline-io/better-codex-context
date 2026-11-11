"""Minimal stdio MCP server (JSON-RPC 2.0, one request per line)."""
from __future__ import annotations

import json
import sqlite3
import sys

from . import __version__
from .service import TOOLS, call
from .store import Store
from .util import ContextError


def handle(store: Store, request: dict):
    identifier = request.get("id")
    method = request.get("method")
    if identifier is None:
        return None
    if not isinstance(request.get("params", {}), dict):
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32602, "message": "params must be an object"}}
    if method == "initialize":
        result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "better-codex-context", "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params", {})
        routed = None
        try:
            metadata = params.get("_meta", {})
            if isinstance(metadata, dict) and isinstance(metadata.get("threadId"), str):
                # Codex 0.166 overwrites these host metadata fields; they are not model arguments.
                native_thread = metadata["threadId"]
                # Public mcpServer/tool/call supplies threadId only; model tool calls
                # also supply sessionId. An absent session never falls into default.
                native_session = metadata.get("sessionId", native_thread)
                if not isinstance(native_session, str):
                    raise ContextError("native session metadata must be a string")
                agent = "main" if native_thread == native_session else native_thread
                routed = Store(store.root, native_session, agent)
            args = params.get("arguments", {})
            if not isinstance(args, dict):
                raise ContextError("arguments must be an object")
            value = call(routed or store, params.get("name", ""), args)
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
        except (ContextError, KeyError, TypeError, UnicodeError) as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except (OSError, sqlite3.Error):
            result = {"content": [{"type": "text", "text": "Context storage unavailable; inspect local state before retrying uncertain writes."}], "isError": True}
        finally:
            if routed:
                routed.close()
    else:
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def serve(store: Store):
    maximum = 2 * 1024 * 1024
    while line := sys.stdin.buffer.readline(maximum + 1):
        if len(line) > maximum:
            while not line.endswith(b"\n"):
                line = sys.stdin.buffer.readline(maximum + 1)
                if not line:
                    break
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "request too large"}}
        else:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("object required")
                response = handle(store, request)
            except (ValueError, ContextError):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "invalid request"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
