"""The shadow context: mirroring the session log, rendering it into a fresh window, and the edge cases
around replacements (resume, /clear, TokenBudget off, oversized shadows, ignored reminders, failures)."""
from __future__ import annotations
import _isolation  # noqa: F401

import json
import time
from unittest.mock import patch

from _support import StoreCase, WorkspaceCase, hook
from better_codex_context import shadow
from better_codex_context.materials import recall
from better_codex_context.util import ContextError


def line(kind: str, payload: dict | None = None, top: dict | None = None) -> str:
    """One rollout line in Codex's compact JSON form."""
    record = {"timestamp": "2026-11-08T00:00:00.000Z", "type": kind}
    if payload is not None:
        record["payload"] = payload
    record.update(top or {})
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def user(text):
    return line("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]})


def assistant(text):
    return line("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})


def developer(text):
    return line("response_item", {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]})


def call(call_id, command, name="exec_command"):
    return line("response_item", {"type": "function_call", "name": name, "arguments": json.dumps({"cmd": command}), "call_id": call_id})


def output(call_id, text):
    return line("response_item", {"type": "function_call_output", "call_id": call_id, "output": text})


def tokens(count, window=475000):
    return line("event_msg", {"type": "token_count", "info": {"last_token_usage": {"total_tokens": count}, "model_context_window": window}})


COMPACTED = line("compacted", {"message": "", "replacement_history": []})


class LogCase(StoreCase):
    def setUp(self):
        super().setUp()
        self.log = self.base / "rollout.jsonl"
        self.log.write_text(line("session_meta", {"cwd": str(self.root)}))

    def write(self, *lines):
        with self.log.open("a") as handle:
            handle.write("".join(lines))

    def sync(self, seconds=5.0):
        return shadow.sync(self.store, str(self.log), time.monotonic() + seconds)

    def entries(self):
        return [(r["kind"], r["text"]) for r in self.store.db.execute(
            "SELECT kind,text FROM shadow_entries WHERE scope=? ORDER BY id", (self.store.scope,))]


class SyncTests(LogCase):
    def test_conversation_is_mirrored_and_everything_else_left_out(self):
        self.write(developer("You are Codex …"), user("<environment_context>cwd</environment_context>"),
                   user("Fix the login timeout."), line("response_item", {"type": "reasoning", "encrypted_content": "x"}),
                   assistant("Running the tests first."), call("c1", "pytest -q"), output("c1", "E   AssertionError: 30 != 10\n1 failed"),
                   developer("Better Codex Context restored this task state …"), user("[task card] The user repeated …"),
                   line("response_item", {"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch\n…", "call_id": "c2"}),
                   line("response_item", {"type": "custom_tool_call_output", "call_id": "c2",
                                          "output": [{"type": "input_text", "text": "Success"}, {"type": "input_image", "image_url": "x"}]}),
                   line("response_item", {"type": "image_generation_call", "id": "ig1", "revised_prompt": "A dark architecture diagram",
                                          "result": "iVBOR" * 1000}))
        self.sync()
        self.assertEqual([("user", "Fix the login timeout."), ("assistant", "Running the tests first."),
                          ("tool", "exec_command: pytest -q ⟦m1⟧"), ("tool", "apply_patch: *** Begin Patch … ⟦m2⟧"),
                          ("tool", "image_generation ⟦m3⟧")], self.entries())
        refs = {r["ref"]: r for r in self.store.db.execute("SELECT * FROM shadow_refs WHERE scope=?", (self.store.scope,))}
        self.assertIn("1 failed", recall(self.store, refs["m1"]["artifact"])["text"])
        self.assertEqual("Success[image]", recall(self.store, refs["m2"]["artifact"])["text"])
        self.assertEqual("A dark architecture diagram", refs["m3"]["prompt"])
        # The anchors themselves are recall refs: the output may have been left out of the window to fit.
        self.assertIn("1 failed", recall(self.store, "m1")["text"])
        self.assertEqual("Success[image]", recall(self.store, "⟦M2⟧")["text"])
        self.assertEqual("A dark architecture diagram", recall(self.store, "m3")["prompt"])
        with self.assertRaisesRegex(ContextError, "anchor m9 does not exist"):
            recall(self.store, "m9")


    def test_a_message_without_a_clients_id_comes_from_another_task_not_the_user(self):
        """Codex logs the client's message id after the prompt hook: typed in a client it is a random UUID (version 4);
        from another Codex task it is the queue item's id or, steered into a running turn, none."""
        from better_codex_context import card

        def message(turn_id, text, client_id, trigger=None):
            hook(self.root, "UserPromptSubmit", turn_id=turn_id, prompt=text)  # filed as a request at first
            started = [line("event_msg", {"type": "task_started", "turn_id": turn_id,
                                          "turn_attribution": {"turn_trigger": trigger}})] if trigger else []
            item = {"type": "UserMessage", "id": "u-" + text[:4], "content": [{"type": "text", "text": text}]}
            item.update({"client_id": client_id} if client_id else {})  # Codex leaves an empty id out
            self.write(*started, user(text), line("event_msg", {"type": "item_completed", "turn_id": turn_id, "item": item}))

        message("t1", "Add the CSV export.", "3b056846-1ce8-4f84-9fe3-cb8faa26e689", "user")
        message("t1", "协调：通知复测需要约3分钟独占窗口，请确认。", None)  # steered into the running turn by another task
        message("t2", "采购候选已固定6550项，请继续串行验收。", "01a1bf47-214f-7102-ab40-f65f80e513f7", "queue")
        message("t3", "Summarize the report.", None, "exec")  # a codex exec prompt is the user's
        self.sync()
        tasks = {i["text"]: (i["status"], i["origin"]) for i in card.items(self.store) if i["kind"] == "task"}
        self.assertEqual({"Add the CSV export.": ("pending", "user"),
                          "协调：通知复测需要约3分钟独占窗口，请确认。": ("cancelled", "peer"),
                          "采购候选已固定6550项，请继续串行验收。": ("cancelled", "peer"),
                          "Summarize the report.": ("pending", "user")}, tasks)
        self.assertEqual(["user", "peer", "peer", "user"], [kind for kind, _ in self.entries()])
        self.assertEqual(["task", "peer", "peer", "task"], [r["class"] for r in self.store.requirements()])


class SessionStartTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.codex_config("model_auto_compact_token_limit = 440000\n[features.token_budget]\nenabled = true\n")
        self.log = self.base / "rollout.jsonl"
        self.log.write_text(line("session_meta", {"cwd": str(self.root)}) + user("Add an export button.")
                            + assistant("Added; tests pass.") + tokens(320000))
        hook(self.root, "UserPromptSubmit", turn_id="t1", prompt="Add an export button.")

    def start(self, source, **extra):
        output = hook(self.root, "SessionStart", source=source, transcript_path=str(self.log), **extra)
        return output.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_replacement_restores_the_conversation_before_the_card(self):
        with self.log.open("a") as handle:
            handle.write(COMPACTED)
        text = self.start("compact")
        self.assertIn("[segment type=shadow-context", text)
        self.assertIn("[You] Add an export button.", text)
        self.assertLess(text.index("[segment type=shadow-context"), text.index("task-card"))

    def test_resume_and_clear_do_not_duplicate_the_conversation(self):
        with self.log.open("a") as handle:
            handle.write(COMPACTED + developer("[segment type=shadow-context relevance=continuity]\n…"))
        self.assertNotIn("[segment type=shadow-context", self.start("resume"))
        self.assertNotIn("[segment type=shadow-context", self.start("clear"))


    def test_a_broken_shadow_falls_back_to_the_card(self):
        with patch.object(shadow, "render", side_effect=RuntimeError("boom")):
            text = self.start("compact")
        self.assertIn("task-card", text)
        self.assertNotIn("[segment type=shadow-context", text)
        log = self.base / "state" / "better-codex-context" / "hook-errors.log"
        self.assertIn("shadow-restore", log.read_text())


