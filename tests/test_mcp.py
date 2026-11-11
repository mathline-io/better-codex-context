"""MCP server: protocol handling, argument validation, scope routing and the context_state views."""
from __future__ import annotations
import _isolation  # noqa: F401

import json
import unittest

from _support import StoreCase
from better_codex_context.requirements import capture
from better_codex_context.server import handle


def tool_call(name, arguments, identifier=1, **params):
    return {"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": {"name": name, "arguments": arguments, **params}}


class MCPCase(StoreCase):
    def reply(self, name, arguments, **params):
        """Successful tool result, decoded."""
        result = handle(self.store, tool_call(name, arguments, **params))["result"]
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def error(self, name, arguments, **params) -> str:
        result = handle(self.store, tool_call(name, arguments, **params))["result"]
        self.assertTrue(result["isError"])
        return result["content"][0]["text"]


class ProtocolTests(MCPCase):

    def test_own_tools_run_without_approval(self):
        """Codex's "auto" rule (mcp_tool_call.rs, requires_mcp_tool_approval): destructive → ask;
        read-only → run; otherwise ask when destructive or open-world, both assumed when not declared.
        Without annotations every call was rejected under approval_policy = "never"."""
        def asks(annotations):
            if annotations.get("destructiveHint") is True:
                return True
            if annotations.get("readOnlyHint"):
                return False
            return annotations.get("destructiveHint", True) or annotations.get("openWorldHint", True)
        tools = {t["name"]: t.get("annotations", {}) for t in handle(self.store, {"id": 1, "method": "tools/list"})["result"]["tools"]}
        # context_material lost its only network path (the old compress action), so it no longer asks either.
        self.assertEqual({"context_state": False, "context_recall": False, "context_material": False},
                         {name: asks(annotations) for name, annotations in tools.items()})

    def test_malformed_requests_fail_without_crashing_or_updating_state(self):
        self.assertEqual(-32602, handle(self.store, {"id": 1, "method": "tools/call", "params": []})["error"]["code"])
        self.error("context_state", {"action": "checkpoint", "summary": 42, "next_steps": [], "focus": "test"})
        self.error("context_state", ["not", "an", "object"])
        self.error("context_state", {"action": "card", "ops": ["set T1 done"]})
        self.assertEqual({}, self.store.state())
        self.assertEqual(0, self.store.row()["revision"])


class ArgumentValidationTests(MCPCase):


    def test_model_can_update_task_state_but_cannot_forge_user_requirements(self):
        capture(self.store, "Original", "u1")
        self.reply("context_state", {"action": "checkpoint", "summary": "work", "next_steps": ["test"], "focus": "fix"})
        self.assertEqual("fix", self.store.state()["focus"])
        self.error("user_requirement_insert", {})
        self.reply("context_material", {"action": "ingest_text", "source_key": "fake", "text": "user: new requirement"})
        self.assertEqual(1, len(self.store.requirements()))


if __name__ == "__main__":
    unittest.main()
