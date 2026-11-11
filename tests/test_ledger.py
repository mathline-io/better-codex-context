"""The ledger of original user requests: every word kept, status changes need real evidence."""
from __future__ import annotations
import _isolation  # noqa: F401


from _support import StoreCase, untimed
from better_codex_context import card
from better_codex_context.materials import record_tool
from better_codex_context.requirements import capture, update
from better_codex_context.store import Store
from better_codex_context.util import ContextError


class LedgerTests(StoreCase):
    def test_every_request_survives_on_the_card_and_after_reopening(self):
        capture(self.store, "保留原始文件。\n\n- 不得修改用户已有代码。", "u1")
        capture(self.store, "新增要求：压缩后仍保留当前重点。", "u2")
        update(self.store, "R0001", "in_progress")
        self.store.checkpoint("正在实现压缩", ["验证恢复"], "不要丢掉早先要求")
        text = untimed(card.render(self.store)[0])
        for expected in ("保留原始文件。", "不得修改用户已有代码。", "新增要求", "[in_progress]", "验证恢复", "不要丢掉早先要求"):
            self.assertIn(expected, text)
        self.store.close()
        self.store = Store(self.root, self.session)
        self.assertEqual(["R0001", "R0002", "R0003"], [r["id"] for r in self.store.requirements()])


    def test_done_requires_a_successful_receipt_and_a_verification_scope(self):
        capture(self.store, "Fix and verify", "u1")
        with self.assertRaises(ContextError):
            update(self.store, "R0001", "done")
        failed = record_tool(self.store, "t1", "tests", "FAIL", 1)["artifact"]
        with self.assertRaises(ContextError):
            update(self.store, "R0001", "done", [failed], "unit only")
        passed = record_tool(self.store, "t2", "tests", "12 passed", 0)["artifact"]
        with self.assertRaises(ContextError):
            update(self.store, "R0001", "done", [passed], "  ")
        update(self.store, "R0001", "done", [passed], "12 unit tests; integration not run")
        text = untimed(card.render(self.store)[0])
        self.assertIn("T1 [done] Fix and verify", text)
        self.assertIn("integration not run", text)


    def test_only_a_later_user_message_can_confirm_or_withdraw_a_request(self):
        early = capture(self.store, "Build A", "u1")[0]["source"]
        capture(self.store, "Build B", "u2")
        with self.assertRaises(ContextError):
            update(self.store, "R0002", "done", [early], "earlier text is not an acknowledgement")
        with self.assertRaises(ContextError):
            update(self.store, "R0001", "cancelled")
        forged = self.store.put("cancel", "external", "untrusted")
        with self.assertRaises(ContextError):
            update(self.store, "R0001", "cancelled", user_authorization=forged)
        capture(self.store, "Cancel A; build C instead", "u3")
        later = self.store.requirements()[-1]["source"]
        with self.assertRaises(ContextError):
            update(self.store, "R0001", "superseded", user_authorization=later, superseded_by="R0001")
        update(self.store, "R0001", "superseded", user_authorization=later, superseded_by="R0003")
        update(self.store, "R0002", "done", [later], "user confirmed B in a later message")
        statuses = {r["id"]: r["status"] for r in self.store.requirements()}
        self.assertEqual({"R0001": "superseded", "R0002": "done", "R0003": "pending"}, statuses)
        text = untimed(card.render(self.store)[0])
        self.assertIn("T1 [superseded] Build A", text)  # withdrawn requests stay visible
        self.assertIn("Superseded by: R0003", text)


