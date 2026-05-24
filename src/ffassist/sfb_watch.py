"""SFB watch list: auto-trigger signup when an eliminator matching a pattern appears."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ffassist import state as state_mod


@dataclass
class Watch:
    pattern: str
    one_shot: bool
    active: bool
    created_at: str
    used_at: str | None = None
    used_eliminator_id: int | None = None
    used_eliminator_name: str | None = None


def add_watch(pattern: str, one_shot: bool = True) -> Watch:
    state = state_mod.load()
    watches = state.setdefault("sfb_watches", [])
    # If a watch with the same pattern + active exists, reactivate / refresh
    for w in watches:
        if w.get("pattern", "").lower() == pattern.lower() and w.get("active"):
            return Watch(**{k: w.get(k) for k in Watch.__dataclass_fields__})
    entry = {
        "pattern": pattern,
        "one_shot": one_shot,
        "active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "used_at": None,
        "used_eliminator_id": None,
        "used_eliminator_name": None,
    }
    watches.append(entry)
    state_mod.save(state)
    return Watch(**entry)


def list_watches() -> list[Watch]:
    state = state_mod.load()
    return [
        Watch(**{k: w.get(k) for k in Watch.__dataclass_fields__})
        for w in state.get("sfb_watches", [])
    ]


def remove_watch(pattern: str) -> bool:
    state = state_mod.load()
    before = len(state.get("sfb_watches", []))
    state["sfb_watches"] = [
        w for w in state.get("sfb_watches", []) if w.get("pattern", "").lower() != pattern.lower()
    ]
    state_mod.save(state)
    return len(state["sfb_watches"]) < before


def find_matching(eliminator_name: str) -> list[dict]:
    """Return raw watch dicts that match the eliminator name (active + substring)."""
    state = state_mod.load()
    n = eliminator_name.lower()
    return [
        w
        for w in state.get("sfb_watches", [])
        if w.get("active") and w.get("pattern", "").lower() in n
    ]


def mark_consumed(pattern: str, eliminator_id: int, eliminator_name: str) -> None:
    """Mark a one-shot watch as used."""
    state = state_mod.load()
    for w in state.get("sfb_watches", []):
        if w.get("pattern", "").lower() == pattern.lower() and w.get("active") and w.get("one_shot"):
            w["active"] = False
            w["used_at"] = datetime.now(timezone.utc).isoformat()
            w["used_eliminator_id"] = eliminator_id
            w["used_eliminator_name"] = eliminator_name
    state_mod.save(state)
