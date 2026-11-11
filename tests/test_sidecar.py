"""Sidecar compression: the dynamic trigger, when a run may start, the `codex exec` worker (with a fake
codex), how results are committed and reported, and the detached worker end to end."""
from __future__ import annotations
import _isolation  # noqa: F401

import json
import os
import time
from unittest.mock import patch

from _support import StoreCase, fake_executable
from better_codex_context import shadow, sidecar

FAKE_CODEX = r'''
import json, os, sys, time
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.167.0"); sys.exit(0)
if args[:2] == ["features", "list"]:
    print("shell_tool  stable  true\nhooks  stable  true\nplugins  stable  true"); sys.exit(0)
assert args[0] == "exec", args
open(os.environ["FAKE_CODEX_LOG"], "a").write(json.dumps(args) + "\n")
material = sys.stdin.read()
mode = os.environ.get("FAKE_CODEX_MODE", "ok")
out = args[args.index("-o") + 1]
if mode == "sleep":
    time.sleep(30)
if mode == "fail":
    print("boom", file=sys.stderr); sys.exit(3)
digest = {"ok": "DIGEST: login timeout fixed ⟦m1⟧", "long": material, "bad": None}[mode]
open(out, "w").write("not json" if digest is None else json.dumps({"digest": digest, "open_items": ["deploy"], "kept_anchors": ["m1"]}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1234, "cached_input_tokens": 0, "output_tokens": 56}}))
'''


class SidecarCase(StoreCase):
    def setUp(self):
        super().setUp()
        self.codex = fake_executable(self.base, "codex", FAKE_CODEX)
        self.codex_log = self.base / "codex-calls.jsonl"
        environment = patch.dict(os.environ, {"BCC_SIDECAR": "auto", "BCC_SIDECAR_CODEX": self.codex,
                                              "BCC_SIDECAR_FIRST_TOKENS": "30", "BCC_SIDECAR_MIN_TRIGGER_TOKENS": "30",
                                              "FAKE_CODEX_LOG": str(self.codex_log)})
        environment.start()
        self.addCleanup(environment.stop)
        self.codex_config("model_auto_compact_token_limit = 440000\n[features.token_budget]\nenabled = true\n")
        for n in range(6):
            self.store.db.execute("INSERT INTO shadow_entries(scope,kind,text,tokens) VALUES(?,?,?,?)",
                                  (self.store.scope, "user" if n % 2 == 0 else "assistant", f"message {n} " + "word " * 40, 45))

    def run_once(self, mode="ok"):
        decision = sidecar.decide(self.store, 50000, 300000)
        self.assertTrue(decision["start"], decision)
        run_id = self.store.db.execute("INSERT INTO sidecar_runs(scope,status,upto,body_tokens,started,pid) VALUES(?,?,?,?,?,?)",
                                       (self.store.scope, "running", decision["upto"], decision["body"], time.time(), os.getpid())).lastrowid
        with patch.dict(os.environ, {"FAKE_CODEX_MODE": mode}):
            return sidecar.execute(self.store, run_id)


class ExecuteTests(SidecarCase):
    def test_success_commits_the_digest_and_moves_the_trigger(self):
        result = self.run_once()
        self.assertEqual("done", result["status"], result)
        digest = shadow.latest_digest(self.store)
        self.assertIn("DIGEST: login timeout fixed", digest["text"])
        self.assertIn("Still open:\n- deploy", digest["text"])
        self.assertEqual(sidecar.next_trigger(digest["tokens"]), digest["next_trigger"])
        self.assertEqual(1234, json.loads(digest["usage"])["input_tokens"])
        self.assertEqual(digest["tokens"], shadow.body(self.store)["tokens"])  # nothing new after the snapshot
        args = json.loads(self.codex_log.read_text().splitlines()[0])
        for expected in ("--ignore-user-config", "--ephemeral", "--output-schema", "gpt-6.1-terra", 'model_reasoning_effort="medium"',
                         "include_environment_context=false"):
            self.assertIn(expected, args)
        self.assertIn("shell_tool", args)  # known to this fake codex: disabled
        self.assertNotIn("code_mode_host", args)  # unknown to this fake codex: not passed
        self.assertTrue(args[-1].startswith("BCC-SIDECAR"))


    def test_failures_keep_the_old_state(self):
        for mode, error in (("fail", "exited 3"), ("bad", "no valid result"), ("long", "no real reduction")):
            result = self.run_once(mode)
            self.assertEqual("failed", result["status"])
            self.assertIn(error, result["error"])
        self.assertIsNone(shadow.latest_digest(self.store))


