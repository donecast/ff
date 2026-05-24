from __future__ import annotations

from ffassist import state as state_mod
from ffassist.config import settings
from ffassist.mfl.client import MFLClient
from ffassist.slack_notify import SlackNotifier


def _myleagues_list(data: dict) -> list[dict]:
    if isinstance(data, dict):
        raw = data.get("league", [])
    else:
        raw = data
    if isinstance(raw, dict):
        raw = [raw]
    return raw or []


def scan_and_diff(year: int) -> list[dict]:
    """Pull current myleagues, return newly-discovered leagues, update state."""
    with MFLClient(year=year) as mfl:
        data = mfl.myleagues(year=year)
    leagues = _myleagues_list(data)
    current: dict[str, dict] = {}
    for lg in leagues:
        lid = lg.get("league_id") or lg.get("id")
        if not lid:
            continue
        current[str(lid)] = lg

    state = state_mod.load()
    known = set(state.get("known_leagues", {}).get(str(year), []))
    new_ids = sorted(set(current.keys()) - known)
    new_leagues = [current[lid] for lid in new_ids]

    state.setdefault("known_leagues", {})[str(year)] = sorted(current.keys())
    state_mod.save(state)

    return new_leagues


def notify_new_leagues(year: int) -> int:
    new = scan_and_diff(year)
    if not new:
        return 0
    notifier = SlackNotifier()
    if not notifier.enabled:
        return 0
    lines = [f":new: *New league(s) on your MFL account ({year}):*"]
    for lg in new:
        lid = lg.get("league_id") or lg.get("id") or "?"
        name = lg.get("name", "?")
        lines.append(f"  • L{lid}: {name}")
    lines.append(
        "\nReply `add <id> to tracking` to enable on-the-clock alerts, or just `track all of them`."
    )
    notifier.dm("\n".join(lines))
    return len(new)


def seed_known_leagues(year: int) -> int:
    """Seed known_leagues without notifying. Returns count of leagues found."""
    with MFLClient(year=year) as mfl:
        data = mfl.myleagues(year=year)
    leagues = _myleagues_list(data)
    ids = sorted(
        {str(lg.get("league_id") or lg.get("id")) for lg in leagues if (lg.get("league_id") or lg.get("id"))}
    )
    state = state_mod.load()
    state.setdefault("known_leagues", {})[str(year)] = ids
    state_mod.save(state)
    return len(ids)
