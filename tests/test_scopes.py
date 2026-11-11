"""Scoped standing constraints: explicit paths and path words in the text, the reminder right after an
edit (once per constraint and file in a turn), and the card line that shows the scope."""
from __future__ import annotations
import _isolation  # noqa: F401

from unittest.mock import patch
import os

from _support import StoreCase, hook, tool_payload
from better_codex_context.hooks import process
from better_codex_context import activity

PATCH = "*** Begin Patch\n*** Update File: {path}\n@@\n-a\n+b\n*** End Patch\n"


class EditReminderTests(StoreCase):
    def setUp(self):
        super().setUp()
        hook(self.root, "UserPromptSubmit", turn_id="t1", prompt="From now on, never change src/trading/ without a test.")
        (self.root / "src" / "trading").mkdir(parents=True)
        (self.root / "src" / "trading" / "place.py").write_text("a\n")

    def edit(self, path, call, turn="t1"):
        return process(tool_payload(self.root, turn_id=turn, call=call, tool_name="apply_patch",
                                    tool_input={"command": PATCH.format(path=path)}, tool_response="Success"))

    def test_editing_a_scoped_file_reminds_the_model_once_per_turn(self):
        with patch.dict(os.environ, {"BCC_ACTIVITY_LOG": "normal"}):
            output = self.edit("src/trading/place.py", "c1")
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[constraint check]", context)
        self.assertIn("src/trading/place.py:", context)
        self.assertIn("C1 From now on, never change src/trading/ without a test.", context)
        self.assertIn("Constraint check: `src/trading/place.py` falls under C1", activity.plain(output["systemMessage"]))
        self.assertEqual({}, {k: v for k, v in self.edit("src/trading/place.py", "c2").items() if k == "hookSpecificOutput"})
        self.assertIn("hookSpecificOutput", self.edit("src/trading/place.py", "c3", turn="t2"))  # a new turn reminds again

