from __future__ import annotations

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from ffassist.config import settings

SFB_BASE = "https://scottfishbowl.com/eliminator-signup.php?id="
SFB_KIND = "sfb_eliminator"


class SlackNotifier:
    def __init__(self, token: str | None = None):
        self.token = token or settings.slack_bot_token
        self._client = WebClient(token=self.token) if self.token else None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def post(
        self,
        channel: str,
        text: str,
        blocks: list[dict] | None = None,
        thread_ts: str | None = None,
    ) -> dict | None:
        if not self._client:
            return None
        try:
            kwargs: dict = {
                "channel": channel,
                "text": text,
                "blocks": blocks,
                "unfurl_links": False,
            }
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            return self._client.chat_postMessage(**kwargs).data
        except SlackApiError as e:
            return {"error": e.response.get("error", str(e))}

    def dm(self, text: str, blocks: list[dict] | None = None) -> dict | None:
        return self.post(settings.slack_user_id, text, blocks)

    def channel(self, text: str, blocks: list[dict] | None = None) -> dict | None:
        return self.post(settings.slack_channel_id, text, blocks)


def sfb_event_message(kind: str, name: str, id_: int, entrants: int | None, capacity: int | None) -> str:
    url = f"{SFB_BASE}{id_}"
    cap = f"{entrants}/{capacity}" if entrants is not None else "?"
    if kind == "new":
        return f":rotating_light: New SFB eliminator open: *{name}* ({cap}) — {url}"
    if kind == "filling":
        return f":hourglass: SFB *{name}* now {cap} — {url}"
    if kind == "full":
        return f":no_entry: SFB *{name}* is FULL ({cap}) — {url}"
    return f"SFB *{name}* ({cap}) [{kind}] — {url}"
