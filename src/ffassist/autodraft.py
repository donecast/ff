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


def _push_to_mfl(league_id: str, player_ids: list[str]) -> tuple[bool, str]:
    """Best-effort push to MFL's native myDraftList. Returns (ok, message).

    Failures are non-fatal — local state still updates. Caller may display message.
    """
    try:
        from ffassist.mfl.client import MFLClient

        with MFLClient() as mfl:
            resp = mfl.set_draft_list(league_id, player_ids)
        raw = resp.get("raw_body", "") if isinstance(resp, dict) else ""
        if "<status>OK" in raw or resp.get("status") == "OK":
            return True, f"MFL updated ({len(player_ids)} entries)."
        return False, f"MFL response: {resp!r}"
    except Exception as e:
        return False, f"MFL push raised: {e!r}"


def get_list(league_id: str) -> list[str]:
    state = state_mod.load()
    return list(state.get("leagues", {}).get(league_id, {}).get("autodraft_list") or [])


def set_list(league_id: str, player_ids: list[str], push_to_mfl: bool = True) -> tuple[bool, str]:
    """Replace the list. Pushes to MFL by default. Returns (mfl_ok, mfl_message)."""
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
    if push_to_mfl:
        return _push_to_mfl(league_id, deduped)
    return True, "MFL push skipped."


def add(league_id: str, player_id: str, index: int | None = None) -> tuple[bool, tuple[bool, str]]:
    """Append or insert. Returns (added, (mfl_ok, mfl_msg)).
    `added` is False if already in the list.
    """
    lst = get_list(league_id)
    if player_id in lst:
        return False, (True, "no-op")
    if index is None or index >= len(lst):
        lst.append(player_id)
    else:
        lst.insert(max(index, 0), player_id)
    mfl_result = set_list(league_id, lst)
    return True, mfl_result


def remove(league_id: str, player_id: str) -> tuple[bool, tuple[bool, str]]:
    """Returns (removed, (mfl_ok, mfl_msg))."""
    lst = get_list(league_id)
    if player_id not in lst:
        return False, (True, "no-op")
    lst.remove(player_id)
    mfl_result = set_list(league_id, lst)
    return True, mfl_result


def fetch_from_mfl(league_id: str) -> list[str]:
    """Read MFL's current native myDraftList for the authenticated franchise."""
    from ffassist.mfl.client import MFLClient

    with MFLClient() as mfl:
        return mfl.get_draft_list(league_id)


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
