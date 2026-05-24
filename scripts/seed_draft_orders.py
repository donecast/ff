"""One-time: seed state['leagues'][L]['draft_order'] from known Round 1 orders.

Run with: uv run python scripts/seed_draft_orders.py
"""

from __future__ import annotations

import sys

from ffassist import state as state_mod
from ffassist.mfl.client import MFLClient

# Round 1 orders parsed from MFL "Draft Order Generated" emails.
DRAFT_ORDERS: dict[str, tuple[int, list[str]]] = {
    "37714": (
        2026,
        [
            "Steven Reaney",
            "Matt Harmon 1",
            "Eric Fernandez",
            "Sam Schneider",
            "Kevin Duff",
            "Frank Roth",
            "Sarah Payne-Poff",
            "Jennifer Smith",
            "Austin Zhu",
            "Scott Gerhardt",
            "Bill Schuler",
            "Noel Gray",
            "Brian Wright",
            "Gary Kilgore",
            "Kevin Payne",
            "Miguel Madrid",
            "GREGORY ADAS",
            "Logan Reinig",
        ],
    ),
    "54069": (
        2026,
        [
            "brad levondosky",
            "Michael Steinberg",
            "Daniel Schlissel",
            "Kevin Loewe",
            "Allan Weinkauf",
            "Anne Dunn",
            "Skinny McKinney",
            "Scott Gerhardt",
            "Scott Frankel",
            "Michael Thompson",
            "Ryan Hallam",
            "Angie Hatfield",
            "Hugo Lopez",
            "Jen Piacenti 1",
            "Paul Fitzsimmons",
            "Patrick Mohan",
            "Jana Kimmel",
            "Sam Schneider",
        ],
    ),
    "42033": (
        2026,
        [
            "Andrew Rosien",
            "Peter Harriott",
            "Jason Smith",
            "Scott Frankel",
            "Anne Dunn",
            "Joey Wright 1",
            "Kevin Williamson",
            "Adam Rosenbaum",
            "Scott Gerhardt",
            "Frank Neill",
            "Andrew Lupole",
            "Greg Eckert",
            "Brian Wright",
            "Ian Tanner",
            "Sharlene Ericson",
            "Will Thompson",
            "brad levondosky",
            "Joey Bankert",
        ],
    ),
}


def names_to_ids(league: dict, names: list[str], my_username: str = "Scott Gerhardt") -> list[str]:
    from rapidfuzz import fuzz, process

    franchises = league.get("franchises", {}).get("franchise", [])
    if isinstance(franchises, dict):
        franchises = [franchises]
    fr_by_id = {f.get("id"): (f.get("name") or "").strip() for f in franchises}
    name_to_id = {v: k for k, v in fr_by_id.items()}
    lower_map = {k.lower(): v for k, v in name_to_id.items()}
    fr_names = list(name_to_id.keys())
    used_ids: set[str] = set()
    result: list[str] = []
    unresolved: list[str] = []
    for n in names:
        n_stripped = n.strip()
        fid = name_to_id.get(n_stripped) or lower_map.get(n_stripped.lower())
        if not fid:
            # Fuzzy match (token-based, e.g. "Brian Wright" -> "Brian W")
            candidate = process.extractOne(
                n_stripped, fr_names, scorer=fuzz.token_sort_ratio, score_cutoff=65
            )
            if candidate:
                fid = name_to_id[candidate[0]]
        if fid and fid not in used_ids:
            used_ids.add(fid)
            result.append(fid)
        else:
            unresolved.append(n_stripped)
            result.append("")
    if unresolved:
        print(f"  ! Unresolved names (left blank): {unresolved}")
        # Try to fill blanks with unused franchise IDs (preserving Scott's position)
        unused = [fid for fid in fr_by_id if fid not in used_ids]
        for i, val in enumerate(result):
            if not val and unused:
                fid = unused.pop(0)
                result[i] = fid
                print(f"    -> position {i+1} filled with franchise {fid} ({fr_by_id[fid]!r})")
    return result


def main() -> int:
    state = state_mod.load()
    for lid, (year, names) in DRAFT_ORDERS.items():
        print(f"L{lid} ({year}): mapping {len(names)} names...")
        with MFLClient(year=year) as mfl:
            league = mfl.league(lid)
        order = names_to_ids(league, names)
        if not all(order):
            print(f"  ! L{lid} STILL has empty positions; skipping save")
            continue
        # Confirm Scott is somewhere in the order
        franchises = league.get("franchises", {}).get("franchise", [])
        if isinstance(franchises, dict):
            franchises = [franchises]
        scott_id = next(
            (f.get("id") for f in franchises if (f.get("name") or "").lower() == "scott gerhardt"),
            None,
        )
        if scott_id and scott_id in order:
            print(f"  ✓ L{lid}: Scott at draft position {order.index(scott_id) + 1} (franchise {scott_id})")
        else:
            print(f"  ! L{lid}: Scott not found in order — check franchise name")
        league_state = state["leagues"].setdefault(lid, {})
        league_state["draft_order"] = order
        print(f"  ✓ L{lid} draft_order: {order}")
    state_mod.save(state)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
