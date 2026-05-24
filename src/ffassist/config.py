from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    mfl_year: int = 2026
    mfl_username: str = ""
    mfl_password: str = ""
    mfl_apikey: str = ""
    mfl_user_agent: str = "ffassist/0.1"
    mfl_league_ids: str = ""

    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_user_id: str = ""
    slack_channel_id: str = ""

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"

    fp_api_key: str = ""

    sfb_elim_min_id: int = 1
    sfb_elim_max_id: int = 200
    sfb_poll_seconds: int = 1800
    sfb_email: str = ""
    sfb_password: str = ""

    @property
    def league_ids(self) -> list[str]:
        env_ids = [x.strip() for x in self.mfl_league_ids.split(",") if x.strip()]
        # Merge with dynamically tracked leagues from state (managed via NLP tools)
        try:
            from ffassist import state as state_mod

            s = state_mod.load()
            dyn = s.get("tracked_leagues", [])
            return list(dict.fromkeys(env_ids + list(dyn)))
        except Exception:
            return env_ids


settings = Settings()
