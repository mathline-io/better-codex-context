"""Native hook handlers: root resolution, failure reporting, restoration after resets, idle resets,
steering messages and tool-result archiving."""
from __future__ import annotations
import _isolation  # noqa: F401

import io
import os
import json
import unittest
from unittest.mock import patch

from _support import StoreCase, WorkspaceCase, hook, tool_payload
from better_codex_context import card, hookrun
from better_codex_context.hooks import in_progress_request, process
from better_codex_context.store import Store


class HookCase(WorkspaceCase):
    def open_store(self, session="s1"):
        store = Store(self.root, session)
        self.addCleanup(store.close)
        return store

    def prompt(self, text, turn="t1", **extra):
        return hook(self.root, "UserPromptSubmit", turn_id=turn, prompt=text, **extra)

    def restored(self, source="compact"):
        output = hook(self.root, "SessionStart", source=source).get("hookSpecificOutput") or {}
        return output.get("additionalContext", "")


class HookRootTests(HookCase):
    def setUp(self):
        super().setUp()
        self.other = self.base / "other"
        self.other.mkdir()

    def test_tool_events_from_another_directory_land_in_the_session_root(self):
        self.prompt("实现导出")
        process(tool_payload(self.other))
        self.assertFalse((self.other / ".better-codex-context").exists())
        store = self.open_store()
        self.assertEqual(["tool:c1"], [e["source_key"] for e in store.events() if e["kind"] == "tool"])
        self.assertEqual(1, store.db.execute("SELECT worked FROM hook_turns WHERE turn_id='t1'").fetchone()[0])


class HookFailureReportingTests(HookCase):
    def run_hook(self, payload) -> dict:
        return hookrun.run(io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))

    def test_failure_returns_a_system_message_and_logs_the_traceback(self):
        with patch("better_codex_context.hooks.record_tool", side_effect=RuntimeError("disk full")):
            message = self.run_hook(tool_payload(self.root))["systemMessage"]
        self.assertIn("PostToolUse hook failed: RuntimeError: disk full", message)
        self.assertIn("hooks.py:", message)
        self.assertIn(str(hookrun.log_path()), message)
        self.assertTrue(str(hookrun.log_path()).startswith(str(self.base)))
        record = json.loads(hookrun.log_path().read_text().splitlines()[-1])
        self.assertEqual("RuntimeError", record["error_type"])
        self.assertIn("Traceback", record["traceback"])
        self.assertEqual(str(self.root), record["resolved_root"])
        self.assertEqual("ls", record["tool_input_preview"])
        health = hookrun.recent()
        self.assertEqual(1, health["errors_last_7_days"])
        self.assertEqual("disk full", health["recent"][-1]["error"])


class SessionRestoreTests(HookCase):

    def test_mid_turn_reset_restores_the_request_being_handled(self):
        self.prompt("实现导出按钮，导出 CSV 和 XLSX")
        self.assertNotIn("type=current-request", self.restored())  # reset before any work keeps the native message
        process(tool_payload(self.root))
        restored = self.restored()
        self.assertIn("type=current-request", restored)
        self.assertIn("导出 CSV 和 XLSX", restored)
        self.assertIn("⟨R0001⟩", restored)
        self.assertNotIn("type=current-request", self.restored("startup"))
        hook(self.root, "Stop", turn_id="t1")
        self.assertNotIn("type=current-request", self.restored())  # finished turns live on the card


class IdleResetTests(HookCase):
    """Two thresholds: native resets at the in-task limit, Stop resets a large window when a turn ends."""

    def setUp(self):
        super().setUp()
        self.codex_config("model_auto_compact_token_limit = 440000\n[features.token_budget]\nenabled = true\n")
        self.transcript = self.base / "rollout.jsonl"

    def usage(self, tokens):
        rows = [{"type": "session_meta", "payload": {"cwd": str(self.root)}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {
                    "input_tokens": tokens - 100, "output_tokens": 100, "total_tokens": tokens}, "model_context_window": 475000}}}]
        self.transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def stop(self, turn="t1", active=False, **extra):
        self.prompt("实现导出按钮", turn)
        process(tool_payload(self.root, turn_id=turn, call="c-" + turn))
        return hook(self.root, "Stop", turn_id=turn, transcript_path=str(self.transcript), stop_hook_active=active, **extra)

    def test_large_window_after_an_answer_asks_for_one_reset(self):
        self.usage(350000)
        result = self.stop()
        self.assertEqual("block", result["decision"])
        self.assertIn("about 350k tokens", result["reason"])
        self.assertIn("call new_context", result["reason"])
        text = self.restored()
        self.assertIn("previous turn was finished before this reset", text)
        self.assertIn("reply with exactly this one line, in English whatever language the conversation uses: Context refreshed.", text)
        self.assertIn("Handle the next user message normally", text)
        self.assertNotIn("type=current-request", text)  # the answered request must not be redone
        self.assertEqual({}, self.stop("t2", active=True))  # Codex's loop guard on the following Stop
        self.assertEqual({}, self.stop("t3", agent_id="sub-agent"))  # a sub-agent never resets its parent's window


class SteeringMessageTests(HookCase):
    def test_messages_sent_during_a_running_turn_are_all_kept_and_restored(self):
        self.prompt("实现导出按钮")
        self.prompt("另外导出要支持 CSV")  # same turn_id: raised "source key reused" before 0.9.0
        self.prompt("实现导出按钮")  # an exact repeat is the same event
        process(tool_payload(self.root))
        store = self.open_store()
        keys = [e["source_key"] for e in store.events() if e["kind"] == "user"]
        self.assertEqual(2, len(keys))
        self.assertEqual("user:t1", keys[0])
        self.assertTrue(keys[1].startswith("user:t1:"))
        restored = in_progress_request(store)
        self.assertIn("实现导出按钮", restored)
        self.assertIn("[later message in the same request]\n另外导出要支持 CSV", restored)


class ResidentSkillTests(StoreCase):
    def test_a_resident_skill_stays_whole_in_every_new_window(self):
        from better_codex_context import hooks as hook_module
        card.apply(self.store, [{"op": "goal", "text": "Ship the export."}])
        for source in ("startup", "clear", "compact"):
            text = hook_module._session_parts(self.store, source, {})["text"]
            self.assertTrue(text.startswith("[segment type=resident-skill name=context-discipline"), source)  # most stable first
            for rule in ("# BCC — Engineering Discipline", "### 16. Debug the Frontend in Chrome Through CDP First",
                         "**The supervisor (offer it once)**"):
                self.assertIn(rule, text)
            self.assertNotIn("\nname: context-discipline", text)  # the front matter is not part of the rules
        self.assertNotIn("resident-skill", hook_module._session_parts(self.store, "resume", {})["text"])  # Codex's log has it
        with patch.dict(os.environ, {"BCC_RESIDENT_SKILLS": "[]"}):  # a skill used once is not resident
            self.assertNotIn("resident-skill", hook_module._session_parts(self.store, "compact", {})["text"])

    def test_the_shadow_gives_way_and_never_shortens_a_resident_skill(self):
        from better_codex_context import shadow, skills
        command = 'text(await tools.exec_command({cmd:"cat /x/skills/context-discipline/SKILL.md","max_output_tokens":5000}))'
        self.assertTrue(skills.reads(command, ["context-discipline"]))
        self.assertFalse(skills.reads(command.replace("context-discipline", "presentations"), ["context-discipline"]))
        self.store.db.execute("INSERT INTO shadow_entries(scope,kind,text,ref,tokens) VALUES(?,?,?,?,?)",
                              (self.store.scope, "tool", "exec: " + command + " ⟦m1⟧", "m1", 40))
        artifact = self.store.put("# BCC — Engineering Discipline\n" + "rule\n" * 4000, "shadow-output", "m1")
        self.store.db.execute("INSERT INTO shadow_refs(scope,ref,call_id,kind,tool,command,entry,artifact,tokens) "
                              "VALUES(?,?,?,?,?,?,?,?,?)", (self.store.scope, "m1", "c1", "tool", "exec", command, 1, artifact, 8000))
        with_skill = shadow.render(self.store, resident=["context-discipline"])["text"]
        self.assertIn("read a resident skill; its whole text is in its own segment above", with_skill)
        self.assertNotIn("rule\nrule", with_skill)  # no excerpt of it
        once = shadow.render(self.store)["text"]  # a skill used once: no excerpt either, only that it must be read again
        self.assertIn("its text is not kept in this window and whatever you remember of it is partial", once)
        self.assertNotIn("rule\nrule", once)
        self.assertEqual(shadow.render(self.store, 10**6, 20000, reserve=20000)["entries"], 0)  # the shadow gives way

    def test_a_skill_read_in_part_is_flagged_at_once(self):
        from better_codex_context import skills
        folder = self.root / "deck"
        folder.mkdir()
        lines = [f"Rule {n}: build every slide from the outline step number {n}." for n in range(1, 61)]
        (folder / "SKILL.md").write_text("---\nname: deck\n---\n" + "\n".join(lines) + "\n", encoding="utf-8")
        whole = "\n".join(lines)
        cut = "\n".join(lines[:15]) + "\nWarning: truncated output (original token count: 9000)\n" + "\n".join(lines[-10:])
        ranged = "\n".join(lines[:30])
        self.assertEqual(["deck"], [s["name"] for s in skills.read("cat deck/SKILL.md")])  # a relative path too
        self.assertEqual(skills.partial("cat deck/SKILL.md", whole, self.root), [])
        self.assertEqual(skills.partial("cat deck/SKILL.md", {"output": whole}, self.root), [])  # JSON-escaped output
        missing = skills.partial('text(await tools.exec_command({cmd:"cat deck/SKILL.md","max_output_tokens":2500}))',
                                 {"output": cut}, self.root)
        self.assertEqual(["deck"], [m["name"] for m in missing])
        self.assertIn("read the rest of the file before following this skill", skills.partial_note(missing))
        self.assertEqual(["deck"], [m["name"] for m in skills.partial("sed -n '1,30p' deck/SKILL.md", ranged, self.root)])
        self.assertEqual(skills.partial("sha256sum deck/SKILL.md", "abc  deck/SKILL.md", self.root), [])  # not a read



if __name__ == "__main__":
    unittest.main()
