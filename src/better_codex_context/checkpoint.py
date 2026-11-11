"""Full task-state snapshots and source-backed deltas for context_state(view="delta")."""
from __future__ import annotations

CARD_FIELDS = ("id", "kind", "status", "text", "note", "repeats", "tags", "ledger")


def snapshot(store) -> dict:
    from .card import items
    card = [{key: item[key] for key in CARD_FIELDS} for item in items(store)]
    return {"requirements": store.requirements(), "card": card, "task": store.state()}


def delta(previous: dict, current: dict) -> dict:
    old = {r["id"]: r for r in previous["requirements"]}
    changed = []
    for requirement in current["requirements"]:
        before = old.get(requirement["id"])
        if before == requirement:
            continue
        # New rows carry their words; existing rows only report what moved.
        keys = ("id", "status", "note", "evidence", "superseded_by", "item")
        entry = {key: requirement.get(key) for key in keys}
        if before is None:
            entry.update(text=requirement["text"], **{"class": requirement.get("class")})
        changed.append(entry)
    value = {}
    if changed:
        value["changed_original_requirements"] = changed
    previous_card = {item["id"]: item for item in previous.get("card", [])}
    card = [item for item in current.get("card", []) if previous_card.get(item["id"]) != item]
    if card:
        value["changed_task_card_items"] = card
    removed = sorted(set(previous_card) - {item["id"] for item in current.get("card", [])})
    if removed:
        value["merged_or_moved_card_items"] = removed
    if previous["task"] != current["task"]:
        value["task_summary_next_work_current_focus"] = current["task"]
    return value
