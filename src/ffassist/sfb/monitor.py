from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
from selectolax.parser import HTMLParser

from ffassist.config import DATA_DIR, settings

BASE_URL = "https://scottfishbowl.com/eliminator-signup.php"
EMPTY_NAME = "No Eliminator Selected"
STATE_FILE = DATA_DIR / "sfb_state.json"


@dataclass
class Eliminator:
    id: int
    name: str
    entrants: int | None
    capacity: int | None
    status: str | None

    @property
    def is_real(self) -> bool:
        return bool(self.name) and self.name != EMPTY_NAME


def _parse(html: str, id_: int) -> Eliminator:
    doc = HTMLParser(html)
    name_node = doc.css_first(".league-name")
    name = name_node.text(strip=True) if name_node else ""

    entrants = capacity = None
    for pill in doc.css(".pill"):
        m = re.search(r"(\d+)\s*/\s*(\d+)\s*entrants", pill.text(strip=True), re.I)
        if m:
            entrants, capacity = int(m.group(1)), int(m.group(2))
            break

    status = None
    for pill in doc.css(".pill"):
        t = pill.text(strip=True)
        if re.search(r"Signups (Open|Closed)|Full|Available", t, re.I):
            status = t
            break

    return Eliminator(id=id_, name=name, entrants=entrants, capacity=capacity, status=status)


def scan(
    min_id: int | None = None,
    max_id: int | None = None,
    client: httpx.Client | None = None,
    consecutive_empty_stop: int = 15,
) -> list[Eliminator]:
    """Scan id=min..max_id. Stops early after `consecutive_empty_stop` empty IDs in a row.

    Treat max_id as a hard ceiling (default 200) rather than the expected count.
    """
    min_id = min_id or settings.sfb_elim_min_id
    max_id = max_id or settings.sfb_elim_max_id
    own_client = client is None
    if own_client:
        client = httpx.Client(
            headers={"User-Agent": settings.mfl_user_agent},
            follow_redirects=True,
            timeout=15.0,
        )
    try:
        results: list[Eliminator] = []
        empty_streak = 0
        for id_ in range(min_id, max_id + 1):
            r = client.get(BASE_URL, params={"id": id_})
            r.raise_for_status()
            elim = _parse(r.text, id_)
            results.append(elim)
            if elim.is_real:
                empty_streak = 0
            else:
                empty_streak += 1
                if empty_streak >= consecutive_empty_stop:
                    break
        return results
    finally:
        if own_client:
            client.close()


def load_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict[str, dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def diff(previous: dict[str, dict], current: list[Eliminator]) -> list[tuple[str, Eliminator]]:
    """Return (event_type, eliminator) for changes worth notifying.

    Event types: 'new', 'filling' (entrants increased), 'full', 'closed'.
    """
    events: list[tuple[str, Eliminator]] = []
    for elim in current:
        if not elim.is_real:
            continue
        prev = previous.get(str(elim.id))
        if prev is None:
            events.append(("new", elim))
            continue
        if not prev.get("is_real", False):
            events.append(("new", elim))
            continue
        prev_entrants = prev.get("entrants")
        if (
            elim.entrants is not None
            and prev_entrants is not None
            and elim.entrants > prev_entrants
        ):
            if elim.capacity and elim.entrants >= elim.capacity:
                events.append(("full", elim))
            else:
                events.append(("filling", elim))
    return events


def to_state(eliminators: list[Eliminator]) -> dict[str, dict]:
    return {str(e.id): {**asdict(e), "is_real": e.is_real} for e in eliminators}
