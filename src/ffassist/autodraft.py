"""Per-league auto-draft list — the ordered fallback Scott wants used when
MFL has flipped him to auto-draft mode (no clock; MFL picks instantly when
his slot comes up). Stored in state["leagues"][lid]:
  - autodraft_list: list[str] of player_ids, in priority order
  - mfl_auto_mode: bool — when True, the poller submits from this list the
    moment Scott is on the clock, racing MFL's own auto-pick.
  - picks_away_alerted_for_pick: int — last pick# we sent a "4 away" alert
    for, to dedupe the proactive nudge.
"""

from __future__ import annotations

from ffassist import state as state_mod


def get_list(league_id: str) -> list[str]:
    state = state_mod.load()
    return list(state.get("leagues", {}).get(league_id, {}).get("autodraft_list") or [])


def set_list(league_id: str, player_ids: list[str]) -> None:
    state = state_mod.load()
    ls = state.setdefault("leagues", {}).setdefault(league_id, {})
    # Dedupe while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for pid in player_ids:
        if pid and pid not in seen:
            seen.add(pid)
            deduped.append(pid)
    ls["autodraft_list"] = deduped
    state_mod.save(state)


def add(league_id: str, player_id: str, index: int | None = None) -> bool:
    """Append or insert. Returns False if already in the list."""
    lst = get_list(league_id)
    if player_id in lst:
        return False
    if index is None or index >= len(lst):
        lst.append(player_id)
    else:
        lst.insert(max(index, 0), player_id)
    set_list(league_id, lst)
    return True


def remove(league_id: str, player_id: str) -> bool:
    lst = get_list(league_id)
    if player_id not in lst:
        return False
    lst.remove(player_id)
    set_list(league_id, lst)
    return True


def next_available(league_id: str, drafted_ids: set[str]) -> str | None:
    """First player_id from the auto-draft list not already drafted in-league."""
    for pid in get_list(league_id):
        if pid not in drafted_ids:
            return pid
    return None


def get_auto_mode(league_id: str) -> bool:
    state = state_mod.load()
    return bool(state.get("leagues", {}).get(league_id, {}).get("mfl_auto_mode"))


def set_auto_mode(league_id: str, on: bool) -> None:
    state = state_mod.load()
    ls = state.setdefault("leagues", {}).setdefault(league_id, {})
    ls["mfl_auto_mode"] = bool(on)
    state_mod.save(state)
