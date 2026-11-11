"""The supervisor: cheap triggers pick a moment, Luna screens it, Sol judges an excerpt; auto mode answers a question
the supervisor knows and continues the turn, advise mode delivers a background finding at the next tool call."""
from __future__ import annotations
import _isolation  # noqa: F401

import json
import os
from unittest.mock import patch

from _support import StoreCase, fake_executable, hook, tool_payload
from better_codex_context import activity, card, shadow, supervisor
from better_codex_context.hooks import process

# Answers by prompt: the screen passes the moment and marks lines; the judge answers or finds the work off track.
FAKE_CODEX = r'''
import json, os, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.167.0"); sys.exit(0)
if args[:2] == ["features", "list"]:
    print("shell_tool  stable  true"); sys.exit(0)
material, prompt = sys.stdin.read(), args[-1]
schema = json.load(open(args[args.index("--output-schema") + 1]))
open(os.environ["FAKE_CODEX_LOG"], "a").write(json.dumps({"prompt": prompt, "material": material,
                                                          "schema": sorted(schema["properties"])}) + "\n")
if "BCC-SUPERVISOR screen" in prompt:
    answer = {"problem": True, "kind": "needless_question", "confidence": 0.8, "evidence": ["Which format"], "reason": "r",
              "lines": [{"from": 1, "to": 1}]}
else:
    answer = json.loads(os.environ["FAKE_JUDGE"])
open(args[args.index("-o") + 1], "w").write(json.dumps(answer))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 4000, "output_tokens": 500}}))
'''
LOG = "\n".join(json.dumps({"timestamp": "2026-11-09T00:00:00.000Z", "type": "response_item", "payload": p}, ensure_ascii=False,
                           separators=(",", ":"))  # Codex's compact form, which the shadow reads
                for p in ({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Export the report as CSV."}]},
                          {"type": "message", "role": "assistant", "content": [{"type": "output_text",
                           "text": "CSV or XLSX? Which format do you want? (recommended: CSV)"}]},
                          {"type": "function_call_output", "call_id": "x", "output":
                           "<codex_delegation>\n  <source_thread_id>01a1bccc-0103-7ca2-aea9-afef047a5653</source_thread_id>\n"
                           "  <input>My tests are running.</input>\n</codex_delegation>"})) + "\n"
JUDGE_ANSWER = {"verdict": "off_track", "kind": "needless_question", "user_request": "Export the report as CSV.", "departure": [],
                "simple_path": "Export as CSV.", "message_to_agent": "The user asked for CSV.", "answer": "CSV",
                "basis": ["Export the report as CSV."], "confidence": 0.9}
JUDGE_STUCK = {"verdict": "off_track", "kind": "stuck", "user_request": "Fix the build.", "departure": ["make again"],
               "simple_path": "Read the error.", "message_to_agent": "Stop rerunning make; read the first error and fix it.",
               "answer": "", "basis": [], "confidence": 0.9}

JUDGE_RESTART = {"verdict": "off_track", "kind": "stopped_early", "user_request": "Add the CSV and the XLSX export.",
                 "departure": ["CSV export added."], "simple_path": "Add the XLSX export.", "message_to_agent": "", "answer": "",
                 "basis": [], "confidence": 0.9, "unfinished": [{"id": "T2", "left": "the XLSX export is not written"}],
                 "restart_steps": ["Add export_xlsx() next to export_csv()", "Run tests/test_export.py"],
                 "restart_prompt": "Finish T2 now: write the XLSX export and do not end the turn before its test passes."}


class SupervisorCase(StoreCase):
    def setUp(self):
        super().setUp()
        self.log_path = self.base / "rollout.jsonl"
        self.log_path.write_text(LOG)
        self.calls = self.base / "codex-calls.jsonl"
        environment = patch.dict(os.environ, {"BCC_SIDECAR_CODEX": fake_executable(self.base, "codex", FAKE_CODEX),
                                              "FAKE_CODEX_LOG": str(self.calls), "BCC_ACTIVITY_LOG": "normal"})
        environment.start()
        self.addCleanup(environment.stop)

    def runs(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []


class SupervisorTests(SupervisorCase):
    def test_auto_answers_a_question_it_knows_and_the_turn_goes_on(self):
        with patch.dict(os.environ, {"BCC_SUPERVISOR": "auto", "FAKE_JUDGE": json.dumps(JUDGE_ANSWER)}):
            hook(self.root, "UserPromptSubmit", turn_id="t1", prompt="Export the report as CSV.")
            held = hook(self.root, "Stop", turn_id="t1", transcript_path=str(self.log_path),
                        last_assistant_message="CSV or XLSX? Which format do you want? (recommended: CSV)")
            again = hook(self.root, "Stop", turn_id="t1", stop_hook_active=True, last_assistant_message="Exported as CSV.")
        self.assertEqual("block", held["decision"])
        self.assertIn("the supervisor answers your question for them: CSV (from “Export the report as CSV.”)", held["reason"])
        self.assertRegex(activity.plain(held["systemMessage"]), r"BCC · Supervisor · auto mode · answered for you\n\n.*▄▄▄.*  answer: CSV\n")
        self.assertNotIn("decision", again)  # one continuation per turn end
        screen, judge = self.runs()
        # The request is an open task, so the open-task review runs and checks the question within it.
        self.assertIn("still lists open tasks", screen["prompt"])
        self.assertIn("a question or a choice for the user, check too whether the answer is already known", screen["prompt"])
        self.assertNotIn("needless_question", judge["prompt"])  # the judge never sees the screen's conclusion
        self.assertIn("[You] Export the report as CSV.", judge["material"])  # every user message is in the excerpt
        decisions = [i["text"] for i in card.items(self.store) if i["kind"] == "constraint"]
        self.assertEqual(["Supervisor decision (for the user): CSV or XLSX? Which format do you want? (recommended: CSV) → CSV"],
                         decisions)
        self.assertIn("[other Codex task] (from Codex task …afef047a5653) My tests are running.", shadow.plain_body(self.store))

    def test_advise_reviews_in_the_background_and_tells_codex_at_the_next_tool_call(self):
        launched = []

        def detached(store, command, run_id):  # the worker runs between tool calls, as a detached process would
            launched.append(run_id)
            return 0
        with patch.dict(os.environ, {"BCC_SUPERVISOR": "advise", "FAKE_JUDGE": json.dumps(JUDGE_STUCK)}), \
                patch("better_codex_context.sidecar.launch", detached):
            hook(self.root, "UserPromptSubmit", turn_id="t1", prompt="Fix the build.")
            for n in range(3):  # the same command three times: a moment worth a look
                started = process(tool_payload(self.root, turn_id="t1", call=f"c{n}", tool_input={"command": "make"},
                                               tool_response="error 2"))
            self.assertEqual("done", supervisor.execute(self.store, launched[0])["status"])
            later = process(tool_payload(self.root, turn_id="t1", call="c9", tool_input={"command": "ls"}, tool_response="x"))
            quiet = process(tool_payload(self.root, turn_id="t1", call="c10", tool_input={"command": "ls -la"}, tool_response="x"))
        self.assertRegex(activity.plain(started["systemMessage"]),
                         r"BCC · Supervisor · advise mode · reviewing\n\n.*▄▄▄.*  moment: the same command again\n")
        self.assertIn("[BCC supervisor] Stop rerunning make; read the first error and fix it.",
                      later["hookSpecificOutput"]["additionalContext"])
        self.assertRegex(activity.plain(later["systemMessage"]),
                         r"BCC · Supervisor · advise mode · off track · stuck\n\n.*▄▄▄.*  Stop rerunning make")
        self.assertNotIn("[BCC supervisor]", quiet.get("hookSpecificOutput", {}).get("additionalContext", ""))  # told once
        self.assertEqual(1, supervisor.usage(self.store)["runs"])

    def test_off_by_default_and_turning_it_on_says_so_in_one_line(self):
        hook(self.root, "UserPromptSubmit", turn_id="t1", prompt="Export the report as CSV.")
        self.assertNotIn("decision", hook(self.root, "Stop", turn_id="t1", transcript_path=str(self.log_path),
                                          last_assistant_message="Which format do you want?"))
        self.assertEqual([], self.runs())
        reply = {"content": [{"type": "text", "text": json.dumps({"settings_changed": {"supervisor": ["off", "auto"]},
                                                                  "file": "config.toml", "applies": "now"})}]}
        shown = activity.plain(activity.card_update(self.store, reply, "settings")).splitlines()
        self.assertIn("Plugin settings changed: supervisor: off → auto", shown[0])
        self.assertEqual(["BCC · Supervisor · auto mode · enabled, watching", ""], shown[-5:-3])  # a blank line, the eye
        self.assertEqual(["watching this session for drift", "models: GPT-6 Luna screens · GPT-6.1 Sol judges",
                          "speaks with quotes, never edits; Codex must follow it"],
                         [line.split("  ")[-1].strip() for line in shown[-3:]])  # beside the eye

    def test_offered_once_on_an_engineering_project_or_a_complex_request(self):
        def note(turn, prompt, session="s1"):
            output = hook(self.root, "UserPromptSubmit", session=session, turn_id=turn, prompt=prompt)
            return output.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertNotIn("[BCC supervisor offer]", note("t1", "Rename the export button."))  # short, no project
        self.assertIn("[BCC supervisor offer] This is a complex request.", note("t2", "Do these:\n- a\n- b\n- c"))
        self.assertNotIn("[BCC supervisor offer]", note("t3", "Do these:\n- d\n- e\n- f"))  # once per session
        (self.root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        offer = note("u1", "Fix the export.", session="s2")
        self.assertIn("This is an engineering project.", offer)
        self.assertIn("and in which mode: advise", offer)
        with patch.dict(os.environ, {"BCC_SUPERVISOR_OFFER": "off"}):
            self.assertNotIn("[BCC supervisor offer]", note("v1", "Fix the export.", session="s3"))
        with patch.dict(os.environ, {"BCC_SUPERVISOR": "advise"}):
            self.assertNotIn("[BCC supervisor offer]", note("w1", "Fix the export.", session="s4"))

    def test_open_tasks_at_a_turn_end_restart_the_work_through_codex_queue(self):
        launched, woken = [], []

        def detached(store, command, run_id):
            launched.append(run_id)
            return 0
        # A judge schema left by an earlier build under the same name, without the restart fields, must not be used.
        stale = self.store.directory / "sidecar" / f"schema-{supervisor.VERSION}-judge.json"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(json.dumps({"type": "object", "properties": {"verdict": {"type": "string"}}}))
        with patch.dict(os.environ, {"BCC_SUPERVISOR": "auto", "FAKE_JUDGE": json.dumps(JUDGE_RESTART)}), \
                patch("better_codex_context.sidecar.launch", detached), \
                patch.object(supervisor, "can_wake", lambda store: True), \
                patch.object(supervisor, "wake", lambda store, text, config: woken.append(text) or True):
            hook(self.root, "UserPromptSubmit", turn_id="t1", prompt="1. Add the CSV export.\n2. Add the XLSX export.")
            ended = hook(self.root, "Stop", turn_id="t1", transcript_path=str(self.log_path),
                         last_assistant_message="CSV export added.")
            self.assertTrue(supervisor.execute(self.store, launched[0])["woke"])
            woke = hook(self.root, "UserPromptSubmit", turn_id="t2", prompt=woken[0])  # codex queue delivers it as a prompt
        self.assertNotIn("decision", ended)  # the turn ends at once: the review runs in the background
        self.assertIn("in the background; Codex is woken if it must go on", activity.plain(ended["systemMessage"]))
        self.assertIn("still lists open tasks", self.runs()[0]["prompt"])  # Luna checks the open tasks first
        self.assertIn("restart_prompt", self.runs()[1]["schema"])  # Sol answers with this build's schema
        self.assertTrue(woken[0].startswith("[BCC supervisor] The task card still has unfinished work that nothing blocks "
                                            "(T2: the XLSX export is not written). Finish T2 now"))
        self.assertIn("Steps: 1. Add export_xlsx() next to export_csv() 2. Run tests/test_export.py", woken[0])
        shown = activity.plain(woke["systemMessage"])
        self.assertIn("restarting unfinished work", shown)
        self.assertIn("woke Codex (codex queue) to go on", shown)
        self.assertNotIn("[BCC supervisor]", woke.get("hookSpecificOutput", {}).get("additionalContext", ""))  # sent once
        latest = self.store.requirements()[-1]
        self.assertEqual("automated", latest["class"])  # the supervisor's message is not the user's request
        # The same open tasks are reviewed at most twice; then they need the user, not another restart.
        open_ids = sorted(i["id"] for i in card.items(self.store) if i["kind"] == "task" and i["status"] in supervisor.OPEN)
        self.assertTrue(open_ids)
        self.assertEqual("open_tasks", supervisor.turn_end(self.store, "t3", "已完成，测试通过。"))  # before a claim of success
        self.assertIsNone(supervisor.turn_end(self.store, "t4", "Done for now."))
        # A statement that only looks like a question ("whether it requires …") is no question for the user.
        self.assertIsNone(supervisor.ASKS_USER.search("是否要求事件仍为当前版本，需由库存 owner 明确定义。"))
        self.assertFalse(supervisor.can_wake(self.store))  # no daemon here: the Stop hook holds the turn instead

