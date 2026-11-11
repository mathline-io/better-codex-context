"""AGENTS.md content beyond Codex's project_doc_max_bytes: found, archived, restored as rules and an index."""
from __future__ import annotations
import _isolation  # noqa: F401


from _support import StoreCase
from better_codex_context import instructions
from better_codex_context.materials import recall


def agents_text(filler_sections: int = 6) -> str:
    """Synthetic AGENTS.md: a short head, filler that pushes later sections past a small limit,
    then sections with rules."""
    head = "# Project rules\n\nUse Python 3.11.\n\n"
    filler = "".join(f"## Background {n}\n\n" + "Plain description of the module layout and history.\n" * 12 + "\n"
                     for n in range(filler_sections))
    tail = ("## Deployment\n\n- Never deploy on Fridays.\n- The release script lives in tools/release.sh.\n"
            "- **Always tag the release before pushing.**\n\n"
            "## 数据库\n\n- 改线上函数必须以线上定义为底。\n- 迁移文件放在 db/migrations。\n\n"
            "```sh\n# never run this in production\nmake reset\n```\n")
    return head + filler + tail


class OverflowTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.codex_config("project_doc_max_bytes = 1024\n")
        (self.root / "AGENTS.md").write_text(agents_text())


    def test_rules_and_index_of_the_cut_part_with_recallable_sections(self):
        block = instructions.overflow(self.store, budget=2000)
        text = block["text"]
        self.assertIn("Codex loads only the first 1 KiB", text)
        self.assertIn("Never deploy on Fridays.", text)
        self.assertIn("**Always tag the release before pushing.**", text)
        self.assertIn("改线上函数必须以线上定义为底。", text)
        self.assertNotIn("Use Python 3.11.", text)  # loaded by Codex, not repeated
        self.assertNotIn("make reset", text)
        self.assertIn("§ Deployment", text)
        self.assertEqual(1, len(block["files"]))
        self.assertLessEqual(block["tokens"], 2000)
        ref = text.split("§ Deployment", 1)[1].split('ref="', 1)[1].split('"', 1)[0]
        section = recall(self.store, ref)
        self.assertTrue(section["text"].startswith("## Deployment"))
        self.assertIn("tools/release.sh", section["text"])


