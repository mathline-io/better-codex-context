"""Task statuses: a turn's first real tool call moves its pending tasks to in_progress, and a turn that
did work without one task-card update is held once at Stop and asked for the statuses."""
from __future__ import annotations
import _isolation  # noqa: F401

import json
import os
from unittest.mock import patch

from _support import StoreCase, hook, tool_payload
from better_codex_context import card
from better_codex_context.materials import recall

STAMP = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"  # every BCC time, UTC
from better_codex_context.hooks import process
from better_codex_context.service import call


class CardStatusTests(StoreCase):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {"BCC_CARD_UPDATES": "require", "BCC_ACTIVITY_LOG": "normal"})
        environment.start()
        self.addCleanup(environment.stop)
        self.calls = 0

    def status(self, identifier):
        return next(i["status"] for i in card.items(self.store) if i["id"] == identifier)

    def tool(self, turn="t1", name="Bash", command="ls", response="a.py"):
        self.calls += 1
        return process(tool_payload(self.root, turn_id=turn, call=f"c{self.calls}", tool_name=name,
                                    tool_input={"command": command}, tool_response=response))

    def card_update(self, turn, ops):
        reply = call(self.store, "context_state", {"action": "card", "ops": ops})
        self.calls += 1
        process(tool_payload(self.root, turn_id=turn, call=f"c{self.calls}", tool_name="mcp__better_context__context_state",
                             tool_input={"action": "card", "ops": ops},
                             tool_response={"content": [{"type": "text", "text": json.dumps(reply)}]}))

    def stop(self, turn="t1", **extra):
        return hook(self.root, "Stop", turn_id=turn, last_assistant_message="Done.", **extra)


    def test_statuses_a_turn_left_unset_are_asked_for_with_the_next_message_never_by_a_hold(self):
        hook(self.root, "UserPromptSubmit", turn_id="t1", prompt="Fix the export button.")
        self.tool()
        self.assertNotIn("decision", self.stop())  # the finished turn is never held
        output = hook(self.root, "UserPromptSubmit", turn_id="t2", prompt="Now the import page.")
        note = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("The last turn worked on T1 (in progress) without updating the task card", note)
        self.assertIn("Task card: asked the model to set T1 in this turn (left open by the last one)", output["systemMessage"])
        self.tool(turn="t2")
        self.card_update("t2", [{"op": "set", "id": "T1", "status": "done", "note": "clicked it in the browser"}])
        self.stop(turn="t2")
        again = hook(self.root, "UserPromptSubmit", turn_id="t3", prompt="And the export page.")
        self.assertNotIn("The last turn worked", again.get("hookSpecificOutput", {}).get("additionalContext", ""))


    def test_every_status_change_is_stamped_and_kept(self):
        hook(self.root, "UserPromptSubmit", turn_id="t1", prompt="Fix the export button.")
        self.tool()
        self.card_update("t1", [{"op": "set", "id": "T1", "status": "done", "note": "clicked it in the browser"}])
        changes = card.history(self.store, "T1")
        self.assertEqual([("", "pending"), ("pending", "in_progress"), ("in_progress", "done")],
                         [(c["old"], c["new"]) for c in changes])
        self.assertTrue(all(__import__("re").match(STAMP, c["at"]) for c in changes))
        item = next(i for i in card.items(self.store) if i["id"] == "T1")
        self.assertEqual((changes[0]["at"], changes[-1]["at"]), (item["created"], item["status_at"]))
        self.assertIn(f"T1 [done · {changes[-1]['at']} UTC]", card.render(self.store)[0])
        recalled = recall(self.store, "T1")
        self.assertEqual(("done", 3), (recalled["card_item"]["status"], len(recalled["status_history"])))
        self.assertRegex(self.store.events()[0]["at"], STAMP)  # the user message itself
        at = self.store.db.execute("SELECT at FROM requirement_changes WHERE scope=?", (self.store.scope,)).fetchall()
        self.assertTrue(at and all(__import__("re").match(STAMP, row["at"]) for row in at))


