from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from anthropic import Anthropic

from ffassist import state as state_mod
from ffassist.config import settings
from ffassist.draft_state import (
    Player,
    available_players,
    parse_picks,
    parse_players,
)
from ffassist.mfl.client import MFLClient
from ffassist.rankings import (
    best_available,
    get_filtered_adp,
    load_csv_override,
    merge_rankings,
    parse_mfl_adp,
)

logger = logging.getLogger(__name__)

WARN_THRESHOLD_SEC = 30 * 60
PICK_THRESHOLD_SEC = 10 * 60


def parse_hours(s: str) -> float:
    """Parse 'H:MM' to seconds."""
    if not s:
        return 24 * 3600.0
    h, _, m = s.partition(":")
    try:
        return float(int(h) * 3600 + int(m or "0") * 60)
    except ValueError:
        return 24 * 3600.0


def parse_susp(s: str) -> tuple[int, int] | None:
    """Parse 'HH HH' to (start_hour, end_hour) suspension window (24h, hours only)."""
    if not s:
        return None
    parts = s.strip().split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def time_until_deadline(last_pick_ts: float, league: dict, now_ts: float | None = None) -> float:
    """Return seconds until the current pick deadline.

    Approximate — counts suspension hours by assuming a full daily window per
    24h elapsed since last pick. Off by < 8h in pathological cases but fine for
    30/10-min thresholds.
    """
    now_ts = now_ts if now_ts is not None else time.time()
    limit_sec = parse_hours(league.get("draftLimitHours", "24:00"))
    deadline = last_pick_ts + limit_sec
    susp = parse_susp(league.get("draftTimerSusp", ""))
    if susp:
        start_h, end_h = susp
        hours_per_day_paused = (end_h - start_h) % 24
        if hours_per_day_paused > 0:
            days_elapsed = max(0.0, (deadline - last_pick_ts) / 86400.0)
            deadline += days_elapsed * hours_per_day_paused * 3600.0
    return deadline - now_ts


def last_pick_timestamp(picks_raw: dict) -> float | None:
    """Return Unix timestamp of the most recent completed pick, or None if draft hasn't started."""
    units = picks_raw.get("draftUnit")
    if units is None:
        return None
    if isinstance(units, dict):
        units = [units]
    latest: float | None = None
    for unit in units:
        raw = unit.get("draftPick", [])
        if isinstance(raw, dict):
            raw = [raw]
        for dp in raw:
            ts = dp.get("timestamp") or dp.get("time")
            if ts is None:
                continue
            try:
                v = float(ts)
            except (TypeError, ValueError):
                continue
            if v > 0 and (latest is None or v > latest):
                latest = v
    return latest


# ---------- rule storage ----------


@dataclass
class AutoRule:
    league_id: str
    rule: str
    enabled: bool
    set_at: str


def get_rule(league_id: str) -> AutoRule | None:
    state = state_mod.load()
    rules = state.get("auto_rules", {})
    entry = rules.get(league_id)
    if not entry:
        return None
    return AutoRule(
        league_id=league_id,
        rule=entry.get("rule", ""),
        enabled=bool(entry.get("enabled")),
        set_at=entry.get("set_at", ""),
    )


def set_rule(league_id: str, rule: str) -> None:
    state = state_mod.load()
    rules = state.setdefault("auto_rules", {})
    rules[league_id] = {
        "rule": rule,
        "enabled": True,
        "set_at": datetime.now(timezone.utc).isoformat(),
    }
    state_mod.save(state)


def disable(league_id: str) -> bool:
    state = state_mod.load()
    rules = state.get("auto_rules", {})
    if league_id in rules and rules[league_id].get("enabled"):
        rules[league_id]["enabled"] = False
        state_mod.save(state)
        return True
    return False


def list_rules() -> list[AutoRule]:
    state = state_mod.load()
    rules = state.get("auto_rules", {})
    return [
        AutoRule(
            league_id=lid,
            rule=v.get("rule", ""),
            enabled=bool(v.get("enabled")),
            set_at=v.get("set_at", ""),
        )
        for lid, v in rules.items()
    ]


# ---------- pick selection ----------


def select_auto_pick(league_id: str, rule: str, year: int | None = None) -> tuple[Player, str] | None:
    """Use Claude to pick a player given the rule + top-30 available. Returns (player, reasoning)."""
    if not settings.anthropic_api_key:
        return None
    with MFLClient(year=year) as mfl:
        players = parse_players(mfl.players())
        picks = parse_picks(mfl.draft_results(league_id))
        adp = get_filtered_adp(mfl.adp(), players)
    overrides = load_csv_override(league_id, players=players)
    rankings = merge_rankings(adp, overrides)
    avail = available_players(players, picks)
    candidates = best_available(avail, rankings, n=30)
    if not candidates:
        return None

    candidate_lines = []
    for i, (pl, rank) in enumerate(candidates):
        r = f"{rank:.1f}" if rank is not None else "—"
        candidate_lines.append(f"  {i + 1}. id={pl.id} {pl.display()} (ADP {r})")
    prompt = (
        "You are picking on behalf of Scott Gerhardt in his MFL eliminator slow draft "
        "(best-ball, 16-roster, weekly low-score elim; flex=RB/WR/TE, sflex=QB/RB/WR/TE).\n\n"
        f"RULE for this league: {rule}\n\n"
        "Top 30 available (by ADP):\n"
        + "\n".join(candidate_lines)
        + "\n\nPick one player. Respond with exactly two lines:\n"
        "PLAYER_ID: <id>\n"
        "REASON: <one short sentence>\n"
    )

    client = Anthropic(api_key=settings.anthropic_api_key)
    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.exception("auto-pick selection LLM error")
        # Fallback: top of rankings respecting position from rule is too hard here;
        # just return best available.
        pl, _ = candidates[0]
        return pl, f"(LLM error: {e!r}; fell back to best available)"

    text = "".join(b.text for b in response.content if b.type == "text")
    pid: str | None = None
    reason = ""
    for line in text.splitlines():
        s = line.strip()
        if s.upper().startswith("PLAYER_ID:"):
            pid = s.split(":", 1)[1].strip()
        elif s.upper().startswith("REASON:"):
            reason = s.split(":", 1)[1].strip()
    if pid:
        for pl, _ in candidates:
            if pl.id == pid:
                return pl, reason or "(no reason given)"
    # LLM didn't return a valid id — fall back
    pl, _ = candidates[0]
    return pl, "(LLM produced no valid id; fell back to best available)"


# ---------- phase tracking ----------


def thread_phase_keys(pick_number: int) -> dict[str, str]:
    return {
        "warned": f"auto_warned_p{pick_number}",
        "planned_id": f"auto_planned_p{pick_number}",
        "picked": f"auto_picked_p{pick_number}",
        "stopped": f"auto_stopped_p{pick_number}",
    }


def mark_stopped(thread_ts: str, pick_number: int) -> bool:
    """Called when user says 'stop' in a draft thread."""
    state = state_mod.load()
    thread = state["threads"].get(thread_ts)
    if not thread:
        return False
    keys = thread_phase_keys(pick_number)
    thread[keys["stopped"]] = True
    state["threads"][thread_ts] = thread
    state_mod.save(state)
    return True
