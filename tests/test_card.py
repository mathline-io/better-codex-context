"""Task card: classification of user messages, card operations and budgeted rendering."""
from __future__ import annotations
import _isolation  # noqa: F401

import unittest

from _support import StoreCase
from better_codex_context import card
from better_codex_context.requirements import capture
from better_codex_context.util import ContextError


class CardTestCase(StoreCase):
    def item(self, identifier):
        return next(i for i in card.items(self.store, include_merged=True) if i["id"] == identifier)


class CardIngestTests(CardTestCase):
    def test_messages_land_on_the_card_and_noise_stays_archived(self):
        capture(self.store, "迁移订单面板。\n\n- 不得修改下单行为\n- 以后每次修 bug 先复现", "u1")
        capture(self.store, "好的", "u2")
        capture(self.store, "【后台守卫 event=1】请读取交接文件", "u3")
        capture(self.store, "滚动回弹又出现了", "u4")
        rows = {r["id"]: (r["class"], r["item"]) for r in self.store.requirements()}
        self.assertEqual({"R0001": ("task", "T1"), "R0002": ("constraint", "C1"), "R0003": ("constraint", "C2"),
                          "R0004": ("ack", None), "R0005": ("automated", None), "R0006": ("correction", "T2")}, rows)
        self.assertIn("correction", self.item("T2")["tags"])
        text, info = card.render(self.store)
        self.assertEqual(0, info["level"])
        self.assertIn("C1 [active] 不得修改下单行为", text)
        self.assertNotIn("后台守卫", text)
        self.assertIn("R0001–R0006", text)  # every paragraph still accounted for
        self.assertEqual(text, card.render(self.store)[0])  # no timestamps or random ids: stable for prompt caching


class CardOperationTests(CardTestCase):
    def setUp(self):
        super().setUp()
        capture(self.store, "Build A", "u1")


    def test_failed_batch_changes_nothing(self):
        before = (self.store.row()["revision"], self.store.requirements())
        with self.assertRaises(ContextError):
            card.apply(self.store, [{"op": "set", "id": "T1", "status": "in_progress"}, {"op": "nope"}])
        self.assertEqual(before, (self.store.row()["revision"], self.store.requirements()))


class CardRenderBudgetTests(CardTestCase):
    def test_large_history_degrades_within_budget_and_keeps_every_id(self):
        for n in range(300):
            capture(self.store, f"Historical request number {n}: " + "details " * 20, f"u{n}")
        capture(self.store, "以后每次提交前都运行完整测试", "rule")
        text, info = card.render(self.store, 2000)
        self.assertLessEqual(info["tokens"], 2000)
        self.assertGreater(info["level"], 0)
        self.assertGreater(info["backlog_snippets"], 0)
        self.assertIn("R0001–R0301", text)
        self.assertIn("C1 [active]", text)  # standing rules survive degradation


if __name__ == "__main__":
    unittest.main()
