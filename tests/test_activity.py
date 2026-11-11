"""Activity lines: the short `systemMessage` each hook shows in the Codex UI after the plugin acts."""
from __future__ import annotations
import _isolation  # noqa: F401

import json
import os
import unittest
from unittest.mock import patch

from _support import WorkspaceCase, hook, tool_payload
from better_codex_context import activity
from better_codex_context.hooks import process
from better_codex_context.store import Store


class ActivityCase(WorkspaceCase):
    level = "normal"

    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {"BCC_ACTIVITY_LOG": self.level})
        environment.start()
        self.addCleanup(environment.stop)

    def activity(self, output: dict) -> str:
        """The activity line of a hook result; checks the wording rule on every line it returns."""
        line = output.get("systemMessage")
        if line is not None:
            self.assertTrue(line.startswith("BCC · "), line)
            self.assertNotIn("compress", line.lower())  # the plugin organizes and optimizes; nothing is compressed away
            self.assertNotIn("compaction", line.lower())
        return line

    def prompt(self, text, turn="t1"):
        return hook(self.root, "UserPromptSubmit", turn_id=turn, prompt=text)

    def tool(self, **payload):
        return process(tool_payload(self.root, **payload))

    def with_store(self, action):
        store = Store(self.root, "s1")
        try:
            return action(store)
        finally:
            store.close()


class PromptActivityTests(ActivityCase):


    def test_activity_failures_never_break_the_hook(self):
        with patch("better_codex_context.activity.prompt", side_effect=RuntimeError("synthetic")):
            output = self.prompt("实现导出按钮")
        self.assertNotIn("systemMessage", output)
        self.assertEqual(1, len(self.with_store(lambda store: store.requirements())))


class SessionTokenTests(ActivityCase):
    """The end of a turn shows the session's token totals: input (orange) with its cached share (green),
    output (pink) and the window, read from Codex's own token_count events."""

    def log(self):
        path = self.root / "rollout.jsonl"
        path.write_text(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 170727863, "cached_input_tokens": 167497984, "output_tokens": 931643,
                                  "reasoning_output_tokens": 431660},
            "last_token_usage": {"input_tokens": 290983, "total_tokens": 291327}, "model_context_window": 475000}}}) + "\n")
        return str(path)

    def test_a_finished_turn_reports_the_session_tokens_in_colour(self):
        self.prompt("Explain the export.")
        with patch.dict(os.environ, {"BCC_ACTIVITY_COLOR": "on"}):
            line = hook(self.root, "Stop", turn_id="t1", last_assistant_message="It writes CSV.",
                        transcript_path=self.log())["systemMessage"]
        self.assertIn("BCC · Session tokens: 170.7M in (98.1% cached, 3.2M uncached) · 931k out (431k reasoning) · "
                      "context 291k of 475k (61%)", activity.plain(line))
        for colour, text in ((activity.IN_ORANGE, "170.7M in"), (activity.CACHED_GREEN, "98.1% cached"), (activity.OUT_PINK, "931k out")):
            self.assertIn(colour + text + activity.RESET, line)


if __name__ == "__main__":
    unittest.main()
