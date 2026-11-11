"""Protected reference documents: automatic and explicit protection, the restored segment with live
version states (never the content), and re-read reminders on change or mention."""
from __future__ import annotations
import _isolation  # noqa: F401

from _support import StoreCase, hook, tool_payload
from better_codex_context import card, hooks, protected
from better_codex_context.hooks import process


class ProtectedTests(StoreCase):
    def setUp(self):
        super().setUp()
        hook(self.root, "UserPromptSubmit", turn_id="t1", prompt="Move the order panel to the new chart.")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "architecture.md").write_text("# Architecture\nOrders go through the risk gate.\n")
        (self.root / "docs" / "notes.md").write_text("misc\n")
        self.calls = 0

    def tool(self, command, output, tool="Bash"):
        self.calls += 1
        return process(tool_payload(self.root, call=f"c{self.calls}", tool_name=tool, tool_input={"command": command},
                                    tool_response=output))

    def paths(self):
        return [d["path"] for d in protected.status(self.store)]


    def test_a_fresh_window_lists_them_with_their_state_but_not_their_content(self):
        self.tool("cat docs/architecture.md", "# Architecture\nOrders go through the risk gate.\n")
        card.apply(self.store, [{"op": "protect", "paths": ["docs/notes.md"], "note": "risk limits"}])
        (self.root / "docs" / "architecture.md").write_text("# Architecture\nOrders now skip the risk gate.\n")
        parts = hooks._session_parts(self.store, "compact", {})
        text = parts["text"]
        self.assertIn("[segment type=protected-references", text)
        self.assertIn("docs/architecture.md — CHANGED since you last read it", text)
        self.assertIn("docs/notes.md — not read in this session", text)
        self.assertIn("(risk limits)", text)
        self.assertNotIn("risk gate", text.split("[segment type=protected-references")[1].split("[/segment]")[0])
        self.assertEqual({"changed", "unread"}, {d["state"] for d in parts["protected"]})


