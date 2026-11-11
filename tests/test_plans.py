"""Plan mode: the <proposed_plan> becomes the plan of record (P#), restored word for word in every fresh
window; answers to plan questions (request_user_input) become standing decisions on the card. Changes of
direction: a newly approved plan replaces the earlier one, and a user redirect leaves what is open to decide."""
from __future__ import annotations
import _isolation  # noqa: F401

import os
from unittest.mock import patch

from _support import StoreCase, hook, tool_payload, untimed
from better_codex_context import card, changes, hooks, plans
from better_codex_context.classify import is_redirect
from better_codex_context.hooks import process
from better_codex_context.materials import recall
from better_codex_context.requirements import capture, record_answers
from better_codex_context.store import Store

PLAN = ("Here is the plan.\n\n<proposed_plan>\n# Add a --quiet flag\n\n## Summary\nSuppress the final JSON print.\n\n"
        "## Test Plan\n- `verify.py --quiet` prints nothing\n</proposed_plan>")


class PlanModeHookTests(StoreCase):
    def test_the_plan_at_the_end_of_a_turn_is_announced_and_restored_after_a_reset(self):
        hook(self.root, "UserPromptSubmit", turn_id="t1", prompt="Plan a --quiet flag for verify.py.")
        with patch.dict(os.environ, {"BCC_ACTIVITY_LOG": "normal"}):
            output = hook(self.root, "Stop", turn_id="t1", last_assistant_message=PLAN)
        self.assertIn("Plan recorded: P1 is the plan of record", output["systemMessage"])
        restored = hooks._session_parts(self.store, "compact", {})
        self.assertIn("[segment type=plan relevance=plan-of-record]", restored["text"])
        self.assertIn("Suppress the final JSON print.", restored["text"])


HANDOVER_PLAN = """# LoftySIP 全系统盘点

## 概要

先建立证据清单，再整合库存候选。

## 实施

- **盘点与基线**：记录事实所有者。
  - 子项：核对 5180 服务
- **补验工作单**：在隔离环境运行浏览器测试。
- **库存整合**：复核候选并合并。

## 验证与发布

- 运行完整工程检查。

## 假设

- 当前工作单已上线。"""
STEPS = ["**盘点与基线**：记录事实所有者。", "**补验工作单**：在隔离环境运行浏览器测试。", "**库存整合**：复核候选并合并。",
         "运行完整工程检查。"]


class PlanHandoverTests(StoreCase):
    """Codex starts an approved plan with a fixed message: the plan becomes P#, its task and one task per step."""

    def tasks(self):
        return {item["id"]: item for item in card.items(self.store) if item["kind"] == "task"}

    def test_a_plan_started_in_a_fresh_context(self):
        hook(self.root, "UserPromptSubmit", turn_id="t1", prompt=plans.HANDOVER + "\n\n" + HANDOVER_PLAN)
        tasks = self.tasks()
        self.assertEqual("Implement plan P1: LoftySIP 全系统盘点", tasks["T1"]["text"])
        self.assertEqual(STEPS, [tasks[f"T{n}"]["text"] for n in range(2, 6)])
        self.assertEqual(5, len(tasks))
        self.assertEqual(HANDOVER_PLAN, plans.current(self.store)["text"])  # restored in full in every fresh window
        rows = {r["text"].strip(): r["item"] for r in self.store.requirements()}
        self.assertEqual("T2", rows["- 子项：核对 5180 服务"])  # a sub-item stays with its step
        self.assertEqual("T1", rows["- 当前工作单已上线。"])  # assumptions describe; they are not steps
        self.assertEqual("T1", rows["## 实施"])
        # The work starts the plan's task; the steps wait until the model reaches them.
        process(tool_payload(self.root, turn_id="t1", tool_input={"command": "ls"}, tool_response="a.py"))
        tasks = self.tasks()
        self.assertEqual(("in_progress", "pending"), (tasks["T1"]["status"], tasks["T2"]["status"]))
        self.assertIn("T2 [pending] (step of P1) **盘点与基线**", untimed(card.render(self.store)[0]))

    def test_a_plan_implemented_in_the_same_thread(self):
        plans.capture(self.store, "<proposed_plan>\n" + HANDOVER_PLAN + "\n</proposed_plan>")
        hook(self.root, "UserPromptSubmit", turn_id="t1", prompt=plans.IMPLEMENT)
        tasks = self.tasks()
        self.assertEqual(["Implement plan P1: LoftySIP 全系统盘点", *STEPS], [tasks[f"T{n}"]["text"] for n in range(1, 6)])
        hook(self.root, "UserPromptSubmit", turn_id="t2", prompt=plans.IMPLEMENT)  # approved again: no second set
        self.assertEqual((5, 1), (len(self.tasks()), self.tasks()["T1"]["repeats"]))


V1 = "# Export v1\n\n## Steps\n\n- Add the CSV writer.\n- Add the export button.\n- Write the export tests."
V2 = "# Export v2\n\n## Steps\n\n- Add the CSV writer.\n- Stream rows instead of buffering.\n- Write the export tests."
QUESTION = {"questions": [{"id": "q1", "question": "Which format?"}]}
REDIRECTS = ["停下。方向完全跑偏了，之前的方案全部作废，重新设计导出模块的架构。", "先停一下，整个思路有问题，推倒重来", "暂停，换个思路",
             "Stop. This has drifted from what I asked; scrap the current approach and restructure the export module.",
             "We went off track. Start over with a streaming design.", "back to the drawing board"]
NOT_REDIRECTS = ["重构这个函数", "这个按钮跑偏了，修一下", "要不要推倒重来？", "不用推倒重来，改一下按钮就行", "停下服务再重启",
                 "don't start over, just fix the test", "Write a JSON parser from scratch", "stop the dev server and restart it",
                 "> start over\n\nignore the quote above, fix the build", "```\nscrap it\n```\n\nrun the tests"]


class ChangeOfDirectionTests(StoreCase):
    def tasks(self, merged=False):
        return {i["id"]: i for i in card.items(self.store, include_merged=merged) if i["kind"] == "task"}

    def approve(self, turn, body=None):
        if body:
            plans.capture(self.store, "<proposed_plan>\n" + body + "\n</proposed_plan>")
        with patch.dict(os.environ, {"BCC_ACTIVITY_LOG": "normal"}):
            return hook(self.root, "UserPromptSubmit", turn_id=turn, prompt=plans.IMPLEMENT)

    def start_v1(self):
        """P1 approved in this thread (T1, steps T2-T4), its first step done and the second under way."""
        self.approve("t1", V1)
        process(tool_payload(self.root, turn_id="t1"))
        card.apply(self.store, [{"op": "set", "id": "T2", "status": "done", "note": "writer added, its tests pass"},
                                {"op": "set", "id": "T3", "status": "in_progress"}])
        hook(self.root, "Stop", turn_id="t1", last_assistant_message="The writer is done.")

    def test_a_new_plan_approved_in_the_same_thread_replaces_the_earlier_one(self):
        record_answers(self.store, "c0", QUESTION, {"answers": {"q1": {"answers": ["CSV"]}}})  # C1
        self.start_v1()
        # /plan again: the request, the question answered differently, the new proposal, its approval.
        hook(self.root, "UserPromptSubmit", turn_id="t2", prompt="Plan streaming instead of buffering.")  # T5
        record_answers(self.store, "c1", QUESTION, {"answers": {"q1": {"answers": ["JSON lines"]}}})  # C2
        hook(self.root, "Stop", turn_id="t2", last_assistant_message="<proposed_plan>\n" + V2 + "\n</proposed_plan>")
        shown = self.approve("t3")["systemMessage"]
        tasks = self.tasks(merged=True)
        self.assertEqual({"T1": "superseded", "T2": "done", "T3": "superseded", "T4": "superseded", "T5": "merged",
                          "T6": "pending", "T7": "done", "T8": "pending", "T9": "pending"},
                         {k: v["status"] for k, v in tasks.items()})
        self.assertTrue(tasks["T7"]["note"].startswith("Done earlier as T2 (step of P1): writer added"))  # not redone
        self.assertEqual("T6", tasks["T5"]["merged_into"])  # the request that asked for P2 is now P2's task
        self.assertEqual(["superseded", "active"], [i["status"] for i in card.items(self.store) if i["kind"] == "constraint"])
        self.assertIn("Plan P2 approved: replaces P1", shown)
        self.assertIn("T7 already done earlier; your request T5 joined T6", shown)
        restored = hooks._session_parts(self.store, "compact", {})["text"]
        self.assertIn("(P2, the latest plan proposed in this session; approved at", restored)
        self.assertIn("Earlier plans, not in force (context_recall reads them): P1 superseded.", restored)
        self.assertIn("no longer in force", recall(self.store, "P1")["status"])

    def test_a_proposal_awaiting_approval_and_an_older_plan_approved_again(self):
        self.start_v1()
        plans.capture(self.store, "<proposed_plan>\n" + V2 + "\n</proposed_plan>")
        segment = plans.render(self.store, 4000)[0]
        self.assertIn("P2, the latest plan proposed in this session; proposed, not approved", segment)
        self.assertIn("P1, approved earlier, is still open", segment)
        self.approve("t2")  # P2 approved: P1 is replaced
        self.approve("t3", V1)  # the user went back to P1: proposed again, approved again
        tasks = self.tasks()
        plan_tasks = [(i["text"].split(":")[0], i["status"]) for i in tasks.values() if "plan-step" not in i["tags"]]
        self.assertEqual([("Implement plan P1", "superseded"), ("Implement plan P2", "superseded"),
                          ("Implement plan P1", "pending")], plan_tasks)
        writer = [i for i in tasks.values() if i["text"] == "Add the CSV writer."]
        self.assertEqual(["done", "done", "done"], [i["status"] for i in writer])  # done once, never asked again
        self.assertIn("approved at", plans.render(self.store, 4000)[0])

    def test_a_redirect_leaves_what_is_open_to_decide_and_holds_once(self):
        self.assertEqual([], [t for t in REDIRECTS if not is_redirect(t)] + [t for t in NOT_REDIRECTS if is_redirect(t)])
        self.start_v1()
        card.apply(self.store, [{"op": "checkpoint", "focus": "the export button", "next_steps": ["wire the button"]}])
        changes.capture(self.store, "**BCC Change note** · T3 · src/export.py\n- **Motive:** the export button")
        note = hook(self.root, "UserPromptSubmit", turn_id="t2", prompt=REDIRECTS[0])["hookSpecificOutput"]["additionalContext"]
        source = card.direction(self.store)["source"]
        self.assertIn("The user stopped the current direction (T5): do not continue the earlier work.", note)
        self.assertIn(f"T1 (in progress), T3 (in progress), T4 (pending). Set each to cancelled, or to superseded with "
                      f"superseded_by (T5 or the new plan's task), both with user_authorization=\"{source}\"", note)
        restored = hooks._session_parts(self.store, "compact", {})["text"]
        self.assertIn("Change of direction: the user stopped the earlier work at", restored)
        self.assertIn("The checkpoint below predates the user's redirect", restored)
        self.assertIn("The user redirected the work at", restored)
        self.assertIn("The notes below predate the user's redirect", restored)
        process(tool_payload(self.root, turn_id="t2"))
        holding = patch.dict(os.environ, {"BCC_CARD_UPDATES": "require"})
        holding.start()
        self.addCleanup(holding.stop)
        self.assertNotIn("decision", hook(self.root, "Stop", turn_id="t2", last_assistant_message="Stopped."))  # never held
        reminded = hook(self.root, "UserPromptSubmit", turn_id="t3", prompt="go on")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Still open since the user's redirect (T5): T1 (in progress), T3 (in progress), T4 (pending)", reminded)
        later = hook(self.root, "UserPromptSubmit", turn_id="t4", prompt="and then?")
        self.assertNotIn("Still open since", later.get("hookSpecificOutput", {}).get("additionalContext", ""))  # said once
        card.apply(self.store, [{"op": "set", "id": i, "status": "cancelled", "user_authorization": source} for i in ("T1", "T3")]
                   + [{"op": "set", "id": "T4", "status": "pending", "note": "kept: the export still needs its tests"}])
        self.assertEqual([], card.undecided(self.store))
        self.assertNotIn("Change of direction", card.render(self.store)[0])
        self.assertIn("is no longer in force: its task T1 was cancelled", plans.render(self.store, 4000)[0])
        # A sub-agent's prompt comes from the main agent, not the user: it never redirects.
        hook(self.root, "UserPromptSubmit", turn_id="a1", agent_id="worker", prompt="推倒重来")
        worker = Store(self.root, self.session, "worker")
        self.addCleanup(worker.close)
        self.assertIsNone(card.direction(worker))

    def test_feedback_that_shows_drift_asks_to_stop_find_the_root_cause_and_re_plan(self):
        drifting = [["加一个导出按钮", "说了多少遍了，端口是8080！！"], ["修一下登录", "还是不行", "怎么又报错了"],
                    ["fix the flaky test", "still failing", "it is still failing on CI"],
                    ["导出按钮不对", "导出按钮点了没反应", "导出按钮点了没下载"]]
        calm = [["加一个导出按钮", "再加个导入页面", "好的", "把颜色改成蓝色"], ["接入支付SDK", "这个第三方库的文档写得真垃圾"],
                ["fix the parser", "the log:\n```\nWTF parse error!!!\nhow many times retry\n```"]]
        for n, messages in enumerate(drifting + calm):
            store = Store(self.root, f"probe{n}")
            self.addCleanup(store.close)
            for m, text in enumerate(messages):
                capture(store, text, f"user:p{m}")  # the path every user message takes
            self.assertEqual(n < len(drifting), card.drift(store) is not None, messages)
        self.start_v1()
        hook(self.root, "UserPromptSubmit", turn_id="t2", prompt="导出按钮还是不行")
        with patch.dict(os.environ, {"BCC_ACTIVITY_LOG": "normal", "BCC_CARD_UPDATES": "require"}):
            output = hook(self.root, "UserPromptSubmit", turn_id="t3", prompt="怎么又不行了？导出按钮点了没反应")
        note = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("The user's feedback signals that the work has drifted from what they want: the same problem raised 2 "
                      "times", note)
        self.assertIn("Record the root cause as a lesson", note)
        self.assertNotIn("This is a user correction", note)  # drift, not one more fix
        self.assertIn("Feedback: the same problem raised 2 times — asked Codex to stop", output["systemMessage"])
        self.assertIn("Feedback signal at", card.render(self.store)[0])
        self.assertIn("signals the work drifted: find the root cause and re-plan", plans.render(self.store, 4000)[0])
        with patch.dict(os.environ, {"BCC_CARD_UPDATES": "require"}):
            later = hook(self.root, "UserPromptSubmit", turn_id="t4", prompt="好的")
        self.assertIn("No root cause recorded yet", later["hookSpecificOutput"]["additionalContext"])  # said once more
        card.apply(self.store, [{"op": "add", "kind": "lesson", "text": "The button handler was patched, not the export route."}])
        self.assertIsNone(card.drift(self.store))
        self.assertNotIn("Feedback signal", card.render(self.store)[0])
