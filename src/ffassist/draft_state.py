from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

FLEX_POSITIONS = {"RB", "WR", "TE"}
SUPERFLEX_POSITIONS = {"QB", "RB", "WR", "TE"}


@dataclass(frozen=True)
class Player:
    id: str
    name: str
    team: str
    position: str
    is_rookie: bool = False
    draft_year: str = ""
    birthdate: str = ""  # UNIX timestamp string from MFL

    def display(self) -> str:
        tag = " R" if self.is_rookie else ""
        return f"{self.name} ({self.team} {self.position}{tag})"


@dataclass
class Pick:
    player_id: str
    franchise_id: str
    round: int
    pick: int


def parse_players(players_data: dict) -> dict[str, Player]:
    raw = players_data.get("player", [])
    if isinstance(raw, dict):
        raw = [raw]
    out: dict[str, Player] = {}
    for p in raw:
        pid = p.get("id")
        if not pid:
            continue
        out[pid] = Player(
            id=pid,
            name=_normalize_name(p.get("name", "")),
            team=p.get("team", ""),
            position=p.get("position", ""),
            is_rookie=(p.get("status") or "").upper() == "R",
            draft_year=p.get("draft_year", ""),
            birthdate=p.get("birthdate", ""),
        )
    return out


def _normalize_name(raw: str) -> str:
    # MFL returns "Last, First" — flip to "First Last"
    if "," in raw:
        last, _, first = raw.partition(",")
        return f"{first.strip()} {last.strip()}"
    return raw.strip()


def parse_picks(draft_results: dict) -> list[Pick]:
    if not draft_results:
        return []
    units = draft_results.get("draftUnit")
    if units is None:
        return []
    if isinstance(units, dict):
        units = [units]
    picks: list[Pick] = []
    for unit in units:
        raw = unit.get("draftPick", [])
        if isinstance(raw, dict):
            raw = [raw]
        for dp in raw:
            pid = dp.get("player", "")
            franchise = dp.get("franchise", "")
            try:
                rnd = int(dp.get("round", 0))
                pnum = int(dp.get("pick", 0))
            except (TypeError, ValueError):
                continue
            if not pid or pid == "0000":
                continue
            picks.append(Pick(player_id=pid, franchise_id=franchise, round=rnd, pick=pnum))
    picks.sort(key=lambda p: (p.round, p.pick))
    return picks


def drafted_ids(picks: Iterable[Pick]) -> set[str]:
    return {p.player_id for p in picks}


def available_players(players: dict[str, Player], picks: Iterable[Pick]) -> list[Player]:
    taken = drafted_ids(picks)
    return [p for p in players.values() if p.id not in taken]


def filter_by_position(players: list[Player], position: str) -> list[Player]:
    pos = position.upper()
    if pos == "FLEX":
        wanted = FLEX_POSITIONS
    elif pos in {"SFLEX", "SUPERFLEX", "SF"}:
        wanted = SUPERFLEX_POSITIONS
    else:
        wanted = {pos}
    return [p for p in players if p.position in wanted]


def my_picks(picks: Iterable[Pick], my_franchise_id: str) -> list[Pick]:
    return [p for p in picks if p.franchise_id == my_franchise_id]


def infer_round1_order(picks: list[Pick]) -> list[str]:
    return [p.franchise_id for p in picks if p.round == 1]


def extract_draft_order(draft_results: dict) -> list[str]:
    """Pull the canonical R1 draft order from MFL's draftResults response.

    MFL exposes `round1DraftOrder` as a comma-separated string of franchise IDs.
    This is the AUTHORITATIVE order — works even before round 1 is complete.
    Returns empty list if the field is missing.
    """
    if not draft_results:
        return []
    units = draft_results.get("draftUnit")
    if units is None:
        return []
    if isinstance(units, dict):
        units = [units]
    for unit in units:
        order_str = unit.get("round1DraftOrder") or ""
        if order_str:
            return [x for x in order_str.split(",") if x]
    return []


def _is_reversed_round(round_num: int, draft_type: str) -> bool:
    """Decide whether a given round runs in REVERSE order vs round 1.

    Standard snake: R1 forward, R2 reverse, R3 forward, R4 reverse, ...
                    (even rounds reversed)
    3RR (3rd-Round Reversal, MFL draftType "3RDROUNDFIRSTRANDOM"):
                    R1 forward, R2 reverse, R3 reverse (the reversal — R3
                    keeps R2's direction instead of flipping back), then
                    standard snake continues from R3:
                    R4 forward, R5 reverse, R6 forward, R7 reverse, ...
                    Verified against MFL draftResults for L=42033 (2026).
    Linear: never reversed.
    """
    if round_num == 1:
        return False
    t = (draft_type or "").upper()
    if t in {"LINEAR", "STRAIGHT"}:
        return False
    if t == "3RDROUNDFIRSTRANDOM" or t.startswith("3RDROUND"):
        if round_num == 2:
            return True
        # R3 onward: reverse iff odd (R3 rev, R4 fwd, R5 rev, R6 fwd, ...)
        return round_num % 2 == 1
    # Default: standard snake — even rounds reversed
    return round_num % 2 == 0


def snake_owner(
    pick_number: int,
    round1_order: list[str],
    draft_type: str = "",
) -> str | None:
    """Return franchise_id whose turn it is for the given absolute pick number (1-based).

    `draft_type` should come from MFL's draftResults `draftUnit.draftType` field.
    Defaults to standard snake when unspecified.
    """
    n = len(round1_order)
    if n == 0:
        return None
    idx_zero = (pick_number - 1) % n
    round_num = (pick_number - 1) // n + 1
    if _is_reversed_round(round_num, draft_type):
        idx_zero = n - 1 - idx_zero
    return round1_order[idx_zero]


def extract_draft_type(draft_results: dict) -> str:
    """Pull the draftType string from MFL's draftResults response, or empty string."""
    if not draft_results:
        return ""
    units = draft_results.get("draftUnit")
    if units is None:
        return ""
    if isinstance(units, dict):
        units = [units]
    for unit in units:
        dt = unit.get("draftType") or ""
        if dt:
            return dt
    return ""


def next_pick_number(picks: list[Pick], team_count: int | None = None) -> int:
    """Return the next OVERALL pick number (1-based).

    If team_count is supplied, computes overall from (round-1)*team_count + slot.
    Otherwise infers team_count from max(p.pick across all picks) — only safe once
    at least one round has had every team pick (any round 2+ does it).
    """
    if not picks:
        return 1
    tc = team_count or max(p.pick for p in picks)
    last_overall = max((p.round - 1) * tc + p.pick for p in picks)
    return last_overall + 1
