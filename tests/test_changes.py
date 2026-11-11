"""Change notes: the markdown change note in Codex's answer (or the older <bcc-change> tag) is captured
as K#, restored in every fresh window, linked from the working set, recallable, and required at Stop
when files changed without one."""
from __future__ import annotations
import _isolation  # noqa: F401

import os
from unittest.mock import patch

from _support import StoreCase, hook, tool_payload
from better_codex_context import changes, hooks
from better_codex_context.hooks import process
from better_codex_context.materials import recall

NOTE = ("Done.\n\n**BCC Change note** · T1 · `src/auth/session.py`, `src/auth/token.py`\n"
        "- **Motive:** sessions expired after 30 s instead of 10 min\n- **Reason:** the timeout was read in seconds but stored in ms;\n"
        "  converting at the boundary keeps the API unchanged\n- **Risk:** tokens issued before the fix keep the old expiry\n"
        "- **Tests:** needed: an expiry test across a restart")
TAGGED = ('Done.\n\n<bcc-change files="src/auth/session.py, src/auth/token.py" task="T1">\n'
          "motive: sessions expired after 30 s instead of 10 min\nreason: the timeout was read in seconds but stored in ms;\n"
          "  converting at the boundary keeps the API unchanged\nrisk: tokens issued before the fix keep the old expiry\n"
          "tests: needed: an expiry test across a restart\n</bcc-change>")
PATCH = "*** Begin Patch\n*** Update File: {path}\n@@\n-a\n+b\n*** End Patch\n"


class ParseTests(StoreCase):
    def test_fields_files_and_continuation_lines(self):
        [note] = changes.parse(NOTE)
        self.assertEqual(["src/auth/session.py", "src/auth/token.py"], note["files"])
        self.assertEqual("T1", note["task"])
        self.assertIn("converting at the boundary keeps the API unchanged", note["reason"])
        self.assertEqual("needed: an expiry test across a restart", note["tests"])
        self.assertEqual([], changes.parse("no tag here"))
        self.assertEqual(changes.parse(NOTE), changes.parse(TAGGED))  # the older tag form reads the same


    def test_symbols_are_kept_and_line_numbers_dropped(self):
        text = ("**BCC Change note** · T1 · `src/a.py:42`\n- **Symbols:** add `Store.flush` (L120-L140); edit "
                "`user_directory:88`, lines 10-20 `TTL`; del `legacy_ttl`\n- **Motive:** m")
        [note] = changes.parse(text)
        self.assertEqual((["src/a.py"], "add `Store.flush`; edit `user_directory`, `TTL`; del `legacy_ttl`"),
                         (note["files"], note["symbols"]))
        self.assertEqual("add `a`; edit `b`; del `c`, `d`; `e`",  # +/~/- marks and Chinese keywords; unmarked stays as is
                         changes.parse("**BCC Change note** · `x.py`\n- **Symbols:** +`a`, ~`b`；删除 `c`, -`d`; `e`\n- **Motive:** m")
                         [0]["symbols"])
        # A description is not a symbol (a real session wrote Chinese sentences there); names without backticks still count.
        self.assertNotIn("symbols", changes.parse("**BCC Change note** · `x.py`\n- **Symbols:** 新增隔离回退源码目录及临时目录登记。"
                                                  "\n- **Motive:** m")[0])
        self.assertEqual("add `Store.flush`; `SIP_AP_PG_ROOT`", changes.parse(
            "**BCC Change note** · `x.py`\n- **Symbols:** add Store.flush; SIP_AP_PG_ROOT 保留 服务日志\n- **Motive:** m")[0]["symbols"])
        self.assertEqual("add `foo`", changes.parse("**BCC Change note** · `x.py`\n- **Symbols:** 新增`foo`\n- **Motive:** m")
                         [0]["symbols"])
        self.assertNotIn("symbols", changes.parse(NOTE)[0])  # a note without them keeps the identity it had before
        [stored] = changes.capture(self.store, text)
        self.assertIn("  symbols: add `Store.flush`; edit `user_directory`, `TTL`; del `legacy_ttl`",
                      changes.render(self.store, 2000)[0])
        self.assertEqual(note["symbols"], recall(self.store, stored["id"])["change_note"]["symbols"])
        found = recall(self.store, query="legacy_ttl")["change_note_matches"]  # a deleted function is found in its note
        self.assertEqual([stored["id"]], [match["id"] for match in found])

    def test_the_title_carries_the_utc_time(self):
        [note] = changes.parse(NOTE.replace("· T1 ·", "· T1 · 2026-11-09 15:20:33 UTC ·"))
        self.assertEqual(("2026-11-09 15:20:33", "T1"), (note["time"], note["task"]))
        self.assertEqual(["src/auth/session.py", "src/auth/token.py"], note["files"])
        # ISO and untagged forms read the same, and the time is never taken for a file or a task.
        [iso] = changes.parse("**BCC Change note** · 2026-11-09T15:20:33.5Z · src/a.py\n- **Motive:** m")
        self.assertEqual(("2026-11-09 15:20:33", ["src/a.py"], ""), (iso["time"], iso["files"], iso["task"]))
        [stored] = changes.capture(self.store, NOTE.replace("· T1 ·", "· T1 · 2026-11-09 15:20:33 UTC ·"))
        self.assertEqual("2026-11-09 15:20:33", stored["time"])
        self.assertEqual("2026-11-09 15:20:33", recall(self.store, stored["id"])["change_note"]["time"])


class HookTests(StoreCase):
    def setUp(self):
        super().setUp()
        hook(self.root, "UserPromptSubmit", turn_id="t1", prompt="Fix the session timeout.")
        (self.root / "src" / "auth").mkdir(parents=True)
        (self.root / "src" / "auth" / "session.py").write_text("a\n")

    def edit(self, path="src/auth/session.py"):
        process(tool_payload(self.root, turn_id="t1", call="c1", tool_name="apply_patch",
                             tool_input={"command": PATCH.format(path=path)}, tool_response="Success"))

    def stop(self, answer, **extra):
        return hook(self.root, "Stop", turn_id="t1", last_assistant_message=answer, **extra)

    def test_the_first_edit_of_a_turn_asks_for_the_note_and_comments_the_finished_turn_is_never_held(self):
        code = "*** Begin Patch\n*** Add File: src/auth/token.py\n" + "".join(f"+value_{n} = {n}\n" for n in range(12)) + "*** End Patch\n"
        with patch.dict(os.environ, {"BCC_ACTIVITY_LOG": "normal"}):
            first = process(tool_payload(self.root, turn_id="t1", call="c1", tool_name="apply_patch",
                                         tool_input={"command": code}, tool_response="Success"))
            second = process(tool_payload(self.root, turn_id="t1", call="c2", tool_name="apply_patch",
                                          tool_input={"command": PATCH.format(path="src/auth/session.py")}, tool_response="Success"))
            finished = self.stop("Fixed it.")
        context = first["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[change notes] This turn changes src/auth/token.py. Right after each coherent change, write its change "
                      "note in your next message", context)
        self.assertIn("- **Symbols:** (the names of the classes, methods, functions, variables or keys as they are in the code, "
                      "in backticks, marked add, edit or del in English, e.g. add `a`; edit `b`; del `c`; never a description, "
                      "never line numbers)", context)
        self.assertIn("[code comments] No comment yet in the code this turn adds to src/auth/token.py (+12 lines)", context)
        self.assertIn("Change notes: reminded the model to write a change note for", first["systemMessage"])
        self.assertNotIn("[change notes]", second.get("hookSpecificOutput", {}).get("additionalContext", ""))  # once a turn
        self.assertNotIn("decision", finished)


    def test_only_a_replaced_window_ends_with_the_root_cause_reminder(self):
        self.edit()
        self.stop(NOTE)
        lines = hooks._session_parts(self.store, "compact", {})["text"].splitlines()
        self.assertTrue(lines[-1].startswith("ATTENTION! ATTENTION! ATTENTION! Before writing any code"), lines[-2:])
        self.assertIn("lean, efficient algorithms, frameworks and solutions", lines[-1])
        self.assertIn("Replace rather than append", lines[-1])
        self.assertIn("do not keep growing the line count", lines[-1])
        self.assertIn("plug into it instead of building a separate branch path", lines[-1])
        self.assertIn("do not write branches for cases that can never occur", lines[-1])
        self.assertIn("First things first", lines[-1])
        self.assertIn("test the real user interface after the backend work in Chrome through CDP (install the latest Chrome "
                      "if it is missing; Playwright only when CDP cannot be used)", lines[-1])
        self.assertIn("together with a sub-agent when you can start one and yourself otherwise", lines[-1])
        self.assertIn("bind tests to one thread per physical core (never both hyper-threads of a core), at most 60%", lines[-1])
        self.assertIn("below 5% free disk clean up first (tmp-registry.jsonl, then what is safe to delete)", lines[-1])
        self.assertIn("add one JSON line for it to tmp-registry.jsonl", lines[-1])
        self.assertIn("look for the best known one that cuts CPU and memory", lines[-1])
        self.assertIn("While a test, download, build or long computation runs, do not wait idle", lines[-1])
        # Only the line that says what to do now comes after it (here: reply "Context refreshed.").
        with patch("better_codex_context.hooks.answered_before_reset", return_value=True):
            lines = hooks._session_parts(self.store, "compact", {})["text"].splitlines()
        self.assertTrue(lines[-2].startswith("ATTENTION! ATTENTION! ATTENTION!"), lines[-3:])
        self.assertIn(hooks.REFRESHED, lines[-1])
        # Only a replaced window ends with it (the skill restored in every window mentions it, so look at the end).
        self.assertFalse(hooks._session_parts(self.store, "startup", {})["text"].splitlines()[-1].startswith("ATTENTION!"))


