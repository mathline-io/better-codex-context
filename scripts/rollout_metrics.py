#!/usr/bin/env python3
"""Read-only context metrics from native Codex rollout JSONL files.

Reproduces the measurements in docs/design.md §1: context size per
request, latency by context size, compaction cadence and floor, retained images and
user messages, file re-reads after compaction and host truncation. Prints aggregates
only; never prints message text. Usage:

    python3 scripts/rollout_metrics.py [ROLLOUT_OR_DIR ...] [--json] [--min-requests N]
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from better_codex_context.workset import TRUNCATED, parse_command, parse_patch  # noqa: E402

CMD = re.compile(r'cmd\s*:\s*"((?:[^"\\]|\\.)*)"|cmd\s*:\s*`((?:[^`\\]|\\.)*)`')
CORRECTION = re.compile(r"不是|我说的|之前说|忘了|又|还是没|没有按|跑偏|不对|别再|怎么又|仍然|依然|搞错|still (broken|failing)|that'?s wrong", re.I)


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def _text(output):
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "".join(part.get("text", "") for part in output if isinstance(part, dict))
    return json.dumps(output, ensure_ascii=False) if isinstance(output, dict) else ""


def _commands(name, payload):
    raw = payload.get("input") or payload.get("arguments") or ""
    if name == "apply_patch" or "*** Begin Patch" in raw:
        return [], parse_patch(raw)
    commands = []
    if payload.get("type") == "function_call" and name in ("shell", "exec_command"):
        try:
            args = json.loads(raw)
            command = args.get("cmd") or args.get("command")
            commands = [" ".join(command) if isinstance(command, list) else command] if command else []
        except ValueError:
            pass
    for first, second in CMD.findall(raw):
        try:
            commands.append(json.loads('"' + (first or second) + '"'))
        except ValueError:
            commands.append(first or second)
    observed = []
    for command in commands:
        observed.extend(parse_command(command))
    return commands, observed


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else None


def analyze(path: Path) -> dict:
    window, requests, compactions, users, turns = None, [], [], [], []
    calls, events, truncations, lost = {}, [], 0, 0
    last_input = None
    with path.open("rb") as stream:
        for raw in stream:
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            kind, payload = record.get("type"), record.get("payload") or {}
            subtype = payload.get("type") if isinstance(payload, dict) else None
            when = _time(record.get("timestamp"))
            if kind == "token_usage_record":
                usage = payload.get("usage") or {}
                requests.append({"t": when, "input": usage.get("input_tokens", 0), "cached": usage.get("cached_input_tokens", 0),
                                 "output": usage.get("output_tokens", 0),
                                 "gap": when - last_input if when and last_input else None})
                events.append(("request", when, None))
            elif kind == "event_msg" and subtype == "token_count":
                window = (payload.get("info") or {}).get("model_context_window") or window
            elif kind == "event_msg" and subtype == "task_started":
                last_input = when
            elif kind == "event_msg" and subtype == "task_complete":
                turns.append(payload.get("duration_ms"))
            elif kind == "event_msg" and subtype == "item_completed" and (payload.get("item") or {}).get("type") == "UserMessage":
                text = "".join(c.get("text", "") for c in payload["item"].get("content", []) if isinstance(c, dict))
                users.append(bool(CORRECTION.search(text)) and len(text) < 400)
                last_input = when
            elif kind == "response_item" and subtype in ("custom_tool_call", "function_call"):
                commands, observed = _commands(payload.get("name", ""), payload)
                calls[payload.get("call_id")] = commands
                for action, target, _ in observed:
                    events.append((action, when, target))
            elif kind == "response_item" and subtype in ("custom_tool_call_output", "function_call_output"):
                for match in TRUNCATED.finditer(_text(payload.get("output"))):
                    truncations += 1
                    lost += int(match[1])
                last_input = when
            elif kind == "compacted":
                history = payload.get("replacement_history") or []
                texts, images = [], 0
                for item in history:
                    content = item.get("content") if isinstance(item.get("content"), list) else []
                    images += sum(1 for c in content if isinstance(c, dict) and "image" in str(c.get("type")))
                    if item.get("type") == "message" and item.get("role") == "user":
                        text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                        if not text.startswith("<environment_context>"):
                            texts.append(text)
                compactions.append({"t": when, "before": requests[-1]["input"] if requests else None, "user_messages": len(texts),
                                    "duplicates": len(texts) - len(set(texts)), "short": sum(len(t.strip()) < 15 for t in texts),
                                    "images": images})
                events.append(("compact", when, None))
    # floor after each compaction = first request that follows it
    index = 0
    for compaction in compactions:
        while index < len(requests) and (requests[index]["t"] or 0) <= (compaction["t"] or 0):
            index += 1
        compaction["after"] = requests[index]["input"] if index < len(requests) else None
    # re-reads after a compaction, before the next edit or user turn
    seen, after_reads, after_rereads, reads = set(), 0, 0, Counter()
    ordered = sorted(events, key=lambda e: e[1] or 0)
    for position, (action, _, target) in enumerate(ordered):
        if action == "read":
            reads[target] += 1
            seen.add(target)
        if action == "compact":
            before = set(seen)
            for later_action, _, later_target in ordered[position + 1:]:
                if later_action in ("edit", "delete", "compact"):
                    break
                if later_action == "read":
                    after_reads += 1
                    after_rereads += later_target in before
    inputs = [r["input"] for r in requests]
    latency = defaultdict(list)
    for request in requests:
        if request["gap"] is not None and 0 < request["gap"] < 900 and request["output"] < 2000:
            latency[min(request["input"] // 50000, 8) * 50].append(request["gap"])
    total_reads = sum(reads.values())
    return {
        "file": path.name, "window": window, "requests": len(requests), "user_messages": len(users), "turns": len(turns),
        "compactions": len(compactions),
        "context_p50": pct(inputs, .5), "context_p90": pct(inputs, .9),
        "share_over_half_window": round(sum(x > window / 2 for x in inputs) / len(inputs), 3) if window and inputs else None,
        "cached_ratio": round(sum(r["cached"] for r in requests) / max(1, sum(inputs)), 3),
        "latency_s_by_context_k": {k: round(statistics.median(v), 1) for k, v in sorted(latency.items()) if len(v) >= 5},
        "compaction_before_p50": pct([c["before"] for c in compactions if c["before"]], .5),
        "compaction_floor_series_k": [round((c["after"] or 0) / 1000) for c in compactions],
        "retained_last": {k: compactions[-1][k] for k in ("user_messages", "duplicates", "short", "images")} if compactions else None,
        "file_reads": total_reads, "reread_ratio": round(1 - len(reads) / total_reads, 3) if total_reads else None,
        "post_compaction_reads": after_reads, "post_compaction_rereads": after_rereads,
        "host_truncations": truncations, "host_truncated_tokens": lost,
        "correction_like_user_share": round(sum(users) / len(users), 3) if users else None,
        "turn_minutes_p50": round(pct([t for t in turns if t], .5) / 60000, 1) if any(turns) else None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*", default=[os.path.expanduser("~/.codex/sessions")])
    parser.add_argument("--json", action="store_true", help="one JSON object per session")
    parser.add_argument("--min-requests", type=int, default=0)
    args = parser.parse_args(argv)
    files = []
    for name in args.paths:
        path = Path(name)
        files.extend(sorted(path.rglob("*.jsonl")) if path.is_dir() else [path])
    rows = [row for row in (analyze(f) for f in files) if row["requests"] >= args.min_requests]
    if args.json:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        return 0
    print(f"{'session':<34}{'req':>7}{'comp':>6}{'p50k':>7}{'>½win':>7}{'floor k first→last':>20}{'reread':>8}{'post-comp reread':>18}")
    for row in rows:
        floors = row["compaction_floor_series_k"]
        floor = f"{floors[0]}→{floors[-1]}" if floors else "-"
        post = f"{row['post_compaction_rereads']}/{row['post_compaction_reads']}" if row["post_compaction_reads"] else "-"
        print(f"{row['file'][8:42]:<34}{row['requests']:>7}{row['compactions']:>6}{(row['context_p50'] or 0) // 1000:>7}"
              f"{row['share_over_half_window'] if row['share_over_half_window'] is not None else '-':>7}{floor:>20}"
              f"{row['reread_ratio'] if row['reread_ratio'] is not None else '-':>8}{post:>18}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
