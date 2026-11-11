"""Evidence summaries: which mirrored tool outputs are summarized at the end of a turn, the `codex exec`
worker (a fake codex), the checked evidence quotes, and where the summaries are used."""
from __future__ import annotations
import _isolation  # noqa: F401

import base64
import json
import os
import time
from unittest.mock import patch

from _support import StoreCase, fake_executable, hook
from better_codex_context import evidence, shadow
from better_codex_context.materials import recall
from better_codex_context.util import token_measure

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
LOG = ("collected 12 items\n" + "tests/test_session.py::test_ttl_units FAILED\n" * 40
       + "E   AssertionError: expected 600000, received 600\n  File \"src/auth/session.py\", line 84, in is_expired\n"
       + "=== 3 failed, 9 passed in 2.31s ===\n")
FAKE_CODEX = r'''
import json, os, re, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.167.0"); sys.exit(0)
if args[:2] == ["features", "list"]:
    print("shell_tool  stable  true"); sys.exit(0)
material = sys.stdin.read()
images = [args[i + 1] for i, a in enumerate(args) if a == "-i"]
open(os.environ["FAKE_CODEX_LOG"], "a").write(json.dumps({"args": args, "images": [os.path.getsize(p) for p in images]}) + "\n")
if os.environ.get("FAKE_CODEX_MODE") == "fail":
    print("boom", file=sys.stderr); sys.exit(3)
items = []
for ref in re.findall(r"^### (m\d+(?:\.\.m\d+)?) ", material, re.M):
    if ".." in ref:
        quotes = [{"label": "Read", "quote": "def is_expired(self):"}]
    elif re.search(r"^### " + ref + r" image", material, re.M):
        quotes = [{"label": "Screen", "quote": "Login failed"}]
    else:
        quotes = [{"label": "Assertion", "quote": "AssertionError:   expected 600000,\nreceived 600"},  # re-wrapped by the model
                  {"label": "Stack trace", "quote": "src/auth/session.py\", line 84"},
                  {"label": "Invented", "quote": "this line is not in the output"}]
    items.append({"ref": ref, "headline": "3 tests failed", "findings": ["The TTL is compared in two units (unverified)."],
                  "evidence": quotes, "next_step": "Check the unit conversion; the root cause is not verified."})
open(args[args.index("-o") + 1], "w").write(json.dumps({"items": items}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 9000, "cached_input_tokens": 0, "output_tokens": 700}}))
'''


class EvidenceCase(StoreCase):
    def setUp(self):
        super().setUp()
        self.codex = fake_executable(self.base, "codex", FAKE_CODEX)
        self.codex_log = self.base / "codex-calls.jsonl"
        # The thresholds follow the fixtures as the plugin's tokenizer measures them: LOG is large, READ is
        # smaller, three READs make a step and two do not.
        log, read = token_measure(LOG)["count"], token_measure(READ)["count"]
        environment = patch.dict(os.environ, {"BCC_EVIDENCE": "auto", "BCC_SIDECAR_CODEX": self.codex,
                                              "BCC_EVIDENCE_MIN_TOKENS": str((log + read) // 2),
                                              "BCC_EVIDENCE_GROUP_TOKENS": str(read * 5 // 2),
                                              "FAKE_CODEX_LOG": str(self.codex_log)})
        environment.start()
        self.addCleanup(environment.stop)
        self.transcript = self.base / "rollout.jsonl"
        self.transcript.touch()  # the log exists before the calls, as in a session: later lines are new
        shadow.sync(self.store, str(self.transcript), time.monotonic() + 10)
        self.calls = 0

    def log(self, tool, command, output):
        """One tool call and its output, as Codex writes them to the session log."""
        self.calls += 1
        call = f"c{self.calls}"
        lines = [{"type": "response_item", "payload": {"type": "function_call", "name": tool, "call_id": call,
                                                       "arguments": json.dumps({"cmd": command})}},
                 {"type": "response_item", "payload": {"type": "function_call_output", "call_id": call, "output": output}}]
        with self.transcript.open("a") as handle:
            handle.writelines(json.dumps(line, separators=(",", ":")) + "\n" for line in lines)  # compact, as Codex writes it
        shadow.sync(self.store, str(self.transcript), time.monotonic() + 10)

    def say(self, text):
        """A message of the model between tool calls (it starts a new step)."""
        line = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                     "content": [{"type": "output_text", "text": text}]}}
        with self.transcript.open("a") as handle:
            handle.write(json.dumps(line, separators=(",", ":")) + "\n")
        shadow.sync(self.store, str(self.transcript), time.monotonic() + 10)

    def three_outputs(self):
        self.log("exec_command", "pytest -q", LOG)                     # m1: large
        self.log("exec_command", "ls", "README.md\n")                  # m2: small, never summarized
        image = "data:image/png;base64," + base64.b64encode(PNG).decode()
        self.log("view_image", "shot.png", [{"type": "input_text", "text": "Screenshot"}, {"type": "input_image", "image_url": image}])

    def run_once(self, mode="ok"):
        decision = evidence.decide(self.store)
        self.assertTrue(decision["start"], decision)
        run_id = self.store.db.execute("INSERT INTO evidence_runs(scope,status,refs,input_tokens,started,pid) VALUES(?,?,?,?,?,?)",
                                       (self.store.scope, "running", json.dumps(decision["refs"]), decision["tokens"], time.time(),
                                        os.getpid())).lastrowid
        with patch.dict(os.environ, {"FAKE_CODEX_MODE": mode}):
            return evidence.execute(self.store, run_id)


class WorkerTests(EvidenceCase):
    def test_one_run_summarizes_with_checked_quotes_and_the_screenshot_attached(self):
        self.three_outputs()
        self.assertEqual({"status": "done", "summaries": 2, "images": 1}, self.run_once())
        call = json.loads(self.codex_log.read_text().splitlines()[-1])
        self.assertEqual([len(PNG)], call["images"])  # the screenshot, read back from the session log
        self.assertIn("gpt-6-luna", call["args"])
        original = self.store.artifact(self.store.db.execute("SELECT artifact FROM shadow_refs WHERE ref='m1'").fetchone()[0])[1].decode()
        text = evidence.summary(self.store, "m1")["summary"]
        self.assertTrue(text.startswith("3 tests failed\nFindings:\n- The TTL is compared in two units (unverified)."), text)
        # Found despite the model's re-wrapping, shown as the original has it; its position opens the original there.
        start = original.find("AssertionError: expected 600000")
        self.assertIn(f'- E1 · Assertion: "AssertionError: expected 600000, received 600" → context_recall(ref="m1", start={start})', text)
        self.assertIn(f'- E2 · Stack trace: "src/auth/session.py", line 84" → context_recall(ref="m1", start={original.find("src/auth")})',
                      text)
        self.assertIn("(1 quoted line not found in the original, left out)", text)
        self.assertNotIn("this line is not in the output", text)
        self.assertIn('E1 · Screen: "Login failed" (read from the screenshot)', evidence.summary(self.store, "m3")["summary"])
        self.assertEqual([], list((self.store.directory / "sidecar").glob("evidence-*/*")))  # images removed after the run


class HookTests(EvidenceCase):
    def test_the_end_of_a_turn_starts_one_run_in_the_background(self):
        hook(self.root, "UserPromptSubmit", turn_id="t1", prompt="Fix the session timeout.")
        self.three_outputs()
        with patch("better_codex_context.evidence.launch", return_value=os.getpid()) as launched, \
                patch.dict(os.environ, {"BCC_ACTIVITY_LOG": "normal", "BCC_ACTIVITY_COLOR": "off"}):
            output = hook(self.root, "Stop", turn_id="t1", last_assistant_message="Done.", transcript_path=str(self.transcript))
        launched.assert_called_once()
        self.assertEqual([["m1"], ["m3"]], json.loads(self.store.db.execute("SELECT refs FROM evidence_runs").fetchone()[0]))
        self.assertIn("BCC · Evidence: summarizing 1 large tool output and 1 screenshot of this session in the background",
                      output["systemMessage"])


class ShadowTests(EvidenceCase):
    """The summaries are part of the shadow: the compressor sees them, restores keep them, late ones arrive."""


    def test_a_summary_that_missed_the_restore_is_added_once(self):
        self.three_outputs()
        evidence.mark_restore(self.store)  # the window was replaced before the run finished
        self.assertEqual(("", 0), evidence.late(self.store, 8000))
        self.run_once()
        text, count = evidence.late(self.store, 8000)
        self.assertEqual(2, count)
        self.assertTrue(text.startswith("[segment type=late-evidence]"))
        self.assertIn("⟦m1⟧ exec_command: pytest -q\n    3 tests failed", text)
        self.assertEqual(("", 0), evidence.late(self.store, 8000))  # once
        evidence.mark_restore(self.store)
        self.log("exec_command", "pytest -q -x", LOG + "x")  # after the restore: its original is in the window itself
        self.run_once()
        self.assertEqual(("", 0), evidence.late(self.store, 8000))


READ = "class Session:\n" + "    # session state\n" * 25 + "    def is_expired(self):\n        return self.ttl < now()\n"


class StepTests(EvidenceCase):
    """Smaller consecutive outputs of one step are summarized together once they add up."""

    def step(self):
        self.say("Reading the session code.")
        for name in ("session.py", "token.py", "config.py"):  # m1–m3: together a step
            self.log("exec_command", f"sed -n 1,80p src/auth/{name}", READ)


    def test_one_summary_for_the_step_shown_once_with_pointers_into_its_outputs(self):
        self.step()
        self.assertEqual({"status": "done", "summaries": 1, "images": 0}, self.run_once())
        found = evidence.summary(self.store, "m2")
        self.assertEqual(("m3", ["m1", "m2", "m3"]), (found["owner"], found["members"]))
        self.assertIn('- E1 · Read: "def is_expired(self):" → context_recall(ref="m1", start=', found["summary"])
        block = shadow.render(self.store)["text"]
        self.assertEqual(1, block.count("3 tests failed"))  # once, after the step's last output
        self.assertIn("⟦m1⟧–⟦m3⟧ 3 outputs of this step, summarized (original ≈", block)
        self.assertLess(block.index("sed -n 1,80p src/auth/config.py ⟦m3⟧"), block.index("⟦m1⟧–⟦m3⟧ 3 outputs"))
        self.assertEqual(["m1", "m2", "m3"], recall(self.store, "m1")["evidence_summary"]["covers"])
        self.assertIn("evidence summary of ⟦m1⟧–⟦m3⟧:", shadow.plain_body(self.store))
        [report] = evidence.unreported(self.store)
        self.assertEqual((1, 3), (report["summaries"], report["outputs"]))


