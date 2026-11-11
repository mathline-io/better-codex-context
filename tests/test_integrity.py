"""Integrity: standing constraints always reach a fresh window in words, `bcc verify` invariants,
signs that Codex's interfaces changed, and ranked search over the ledger and the card."""
from __future__ import annotations
import _isolation  # noqa: F401

import json

from _support import StoreCase
from better_codex_context import card, verify
from better_codex_context.requirements import hook_prompt


def line(kind, payload):
    return json.dumps({"timestamp": "2026-11-08T00:00:00Z", "type": kind, "payload": payload}, separators=(",", ":")) + "\n"


def user(text):
    return line("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]})


class ConstraintGuardTests(StoreCase):
    def setUp(self):
        super().setUp()


    def test_the_guard_restores_constraints_an_id_only_card_drops(self):
        card.apply(self.store, [{"op": "add", "kind": "constraint", "text": "Order placement must not change."}])
        segment, ids = card.constraint_guard(self.store, "Standing constraints (always apply):\n  C1 — read them before acting")
        self.assertEqual(["C1"], ids)
        self.assertIn("C1 [active] Order placement must not change.", segment)


class VerifyTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.log = self.base / "rollout-2026-11-08T00-00-00-s1.jsonl"
        self.log.write_text(line("session_meta", {"cli_version": "0.167.0"}) + user("before the plugin")
                            + user("<environment_context>cwd</environment_context>"))
        for n, text in enumerate(("Fix the login timeout.", "From now on, always run the tests first.")):
            hook_prompt(self.store, text, f"user:t{n}")
            with self.log.open("a") as handle:
                handle.write(user(text))

    def result(self):
        report = verify.run(self.store, "s1", str(self.log))
        return report["result"], {c["name"]: c for c in report["checks"]}


    def test_a_message_the_hook_missed_fails_the_ledger_check(self):
        with self.log.open("a") as handle:
            handle.write(user("Also export to CSV."))
        result, checks = self.result()
        self.assertEqual("fail", result)
        self.assertEqual(["Also export to CSV."], checks["ledger"]["missing"])


