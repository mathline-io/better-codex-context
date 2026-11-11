"""Loop guard: repeated identical results and repeated identical failures are called out to the model."""
from __future__ import annotations
import _isolation  # noqa: F401


from _support import WorkspaceCase, hook, tool_payload
from better_codex_context.hooks import process

FAILURE = "collected 3 items\n\ntests/test_login.py F\nE   AssertionError: timeout 30 != 10\n1 failed in 0.12s\n"


class LoopGuardCase(WorkspaceCase):
    def setUp(self):
        super().setUp()
        hook(self.root, "UserPromptSubmit", turn_id="t1", prompt="Fix the login timeout.")
        self.calls = 0

    def run_tool(self, command: str, output: str, tool: str = "Bash") -> dict:
        self.calls += 1
        return process(tool_payload(self.root, call=f"c{self.calls}", tool_name=tool,
                                    tool_input={"command": command}, tool_response=output))

    def edit(self):
        patch_text = "*** Begin Patch\n*** Update File: src/login.py\n@@\n-x\n+y\n*** End Patch"
        return self.run_tool(patch_text, "Success", tool="apply_patch")

    def note(self, output: dict) -> str | None:
        return output.get("hookSpecificOutput", {}).get("additionalContext")


class StuckTests(LoopGuardCase):
    def test_same_failure_after_each_fix_attempt_rises_through_three_levels(self):
        self.assertIsNone(self.note(self.run_tool("pytest -q", FAILURE)))
        self.edit()
        warning = self.note(self.run_tool("pytest -q", FAILURE))
        self.assertIn("[loop guard] `pytest -q` failed again with the same error after your change (2 runs in a row)", warning)
        self.edit()
        note = self.note(self.run_tool("pytest -q", FAILURE))
        self.assertIn("[loop guard] `pytest -q` has now failed 3 times in a row with the same error", note)
        self.assertIn("AssertionError: timeout 30 != 10", note)
        self.assertIn("record the rejected approach as a lesson", note)
        self.edit()
        self.assertIsNone(self.note(self.run_tool("pytest -q", FAILURE)))  # no reminder at every run
        self.edit()
        escalate = self.note(self.run_tool("pytest -q", FAILURE))
        self.assertIn("No progress after 5 attempts", escalate)
        self.assertIn("tell the user what blocks you", escalate)


class RepeatTests(LoopGuardCase):
    def test_identical_result_three_times_since_the_last_message(self):
        for _ in range(2):
            self.assertIsNone(self.note(self.run_tool("cat src/login.py", "x = 1\n")))
        note = self.note(self.run_tool("cat src/login.py", "x = 1\n"))
        self.assertIn("returned the identical result 3 times since the user's last message", note)
        self.assertIn("Do not run it again", note)


