"""Bot-level settings stored in state (toggleable via NLP).

Distinct from config.Settings (which reads .env at startup). These are runtime
toggles the bot itself can flip.
"""

from __future__ import annotations

from ffassist import state as state_mod

DEFAULTS: dict = {
    "exclude_rookies_from_adp": True,
    "eliminator_only_adp": True,
    "blend_weight_ppg": 0.6,
    "blend_weight_adp": 0.4,
}


def get(key: str):
    state = state_mod.load()
    settings = state.get("bot_settings", {})
    return settings.get(key, DEFAULTS.get(key))


def set_value(key: str, value) -> None:
    state = state_mod.load()
    settings = state.setdefault("bot_settings", {})
    settings[key] = value
    state_mod.save(state)


def all_settings() -> dict:
    state = state_mod.load()
    saved = state.get("bot_settings", {})
    return {k: saved.get(k, v) for k, v in DEFAULTS.items()}
