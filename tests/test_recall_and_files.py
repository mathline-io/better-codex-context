"""Exact recall of archived originals and captured workspace files with their diffs."""
from __future__ import annotations
import _isolation  # noqa: F401

import unittest

from _support import StoreCase
from better_codex_context.materials import recall
from better_codex_context.requirements import capture
from better_codex_context.util import ContextError


class RecallReferenceTests(StoreCase):
    def test_requirement_and_card_ids_open_the_original_message(self):
        capture(self.store, "以后每次提交前都跑测试\n\n修复导出按钮的回弹问题", "u1")
        rows = self.store.db.execute("SELECT id,item FROM requirements WHERE scope=? ORDER BY id", (self.store.scope,)).fetchall()
        requirement, item = rows[-1]["id"], rows[-1]["item"]
        for ref in (requirement, requirement.lower(), "R" + str(int(requirement[1:]))):
            result = recall(self.store, ref)
            self.assertEqual(requirement, result["resolved_from"])
            self.assertIn("修复导出按钮的回弹问题", result["text"])
            self.assertIn("以后每次提交前都跑测试", result["text"])
        by_item = recall(self.store, item)
        self.assertEqual(item, by_item["card_item"]["id"])
        self.assertIn("修复导出按钮", by_item["text"])
        self.assertEqual("u1", recall(self.store, "u1")["resolved_from"])


class RecallReadingTests(StoreCase):

    def test_progressive_reads_are_exact_and_bounded(self):
        text = "".join(f"line {n}\n" for n in range(2000))
        ref = self.store.put(text, "tool", "long log")
        first = recall(self.store, ref, limit=100)
        second = recall(self.store, ref, start=first["end_chars"], limit=100)
        self.assertEqual(text[:200], first["text"] + second["text"])
        self.assertTrue(second["has_more"])
        self.assertEqual(len(text), second["total_chars"])
        found = recall(self.store, ref, query="line 1500\n")
        self.assertIn("line 1500\n", found["text"])
        self.assertEqual({"matches": [], "complete": True}, {k: v for k, v in recall(self.store, ref, query="absent").items()
                                                              if k in ("matches", "complete")})
        for bounds in ({"start": -1}, {"limit": 0}, {"limit": 16001}):
            with self.assertRaises(ContextError):
                recall(self.store, ref, **bounds)


if __name__ == "__main__":
    unittest.main()
