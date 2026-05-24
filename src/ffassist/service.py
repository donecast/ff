from __future__ import annotations

import threading
import time

from ffassist import account_monitor, auto_pick, sfb_watch, state as state_mod
from ffassist.config import settings
from ffassist.poller import poll_all
from ffassist.sfb import monitor as sfb
from ffassist.slack_bot import run as run_bot
from ffassist.slack_notify import SFB_KIND, SlackNotifier, sfb_event_message

FAST_POLL_SEC = 120  # 2 min when any auto-pick rule is enabled
ACCOUNT_POLL_SEC = 1800  # 30 min — myleagues check for new leagues


def _any_auto_rule_enabled() -> bool:
    return any(r.enabled for r in auto_pick.list_rules())


def _mfl_poller_loop(default_interval: int) -> None:
    while True:
        interval = FAST_POLL_SEC if _any_auto_rule_enabled() else default_interval
        try:
            hits = poll_all()
            if hits:
                print(f"[mfl-poll] events: {hits}", flush=True)
        except Exception as e:
            print(f"[mfl-poll-error] {e!r}", flush=True)
        time.sleep(interval)


def _account_monitor_loop(interval: int) -> None:
    # Seed on first run (no DM); after that, alert on new leagues.
    state = state_mod.load()
    seeded = bool(state.get("known_leagues", {}).get(str(settings.mfl_year)))
    if not seeded:
        try:
            n = account_monitor.seed_known_leagues(settings.mfl_year)
            print(f"[account-monitor] seeded {n} leagues for {settings.mfl_year}", flush=True)
        except Exception as e:
            print(f"[account-monitor-seed-error] {e!r}", flush=True)
    while True:
        time.sleep(interval)
        try:
            n = account_monitor.notify_new_leagues(settings.mfl_year)
            if n:
                print(f"[account-monitor] alerted on {n} new league(s)", flush=True)
        except Exception as e:
            print(f"[account-monitor-error] {e!r}", flush=True)


def _sfb_poller_loop(interval: int) -> None:
    notifier = SlackNotifier()
    while True:
        try:
            prev = sfb.load_state()
            current = sfb.scan()
            events = sfb.diff(prev, current)
            for kind, elim in events:
                if kind not in {"new", "full"}:
                    continue
                msg = sfb_event_message(kind, elim.name, elim.id, elim.entrants, elim.capacity)
                result = notifier.channel(msg)
                if result and result.get("ok") and kind == "new":
                    bot_state = state_mod.load()
                    bot_state["threads"][result["ts"]] = {
                        "kind": SFB_KIND,
                        "channel": result["channel"],
                        "eliminator_id": elim.id,
                        "name": elim.name,
                    }
                    state_mod.save(bot_state)
                    print(f"[sfb-poll] new: {elim.id} {elim.name}", flush=True)

                    # Check watch list for auto-signup
                    matches = sfb_watch.find_matching(elim.name)
                    if matches and settings.sfb_email and settings.sfb_password:
                        from ffassist.sfb.client import SfbClient

                        try:
                            with SfbClient() as sclient:
                                signup_result = sclient.signup(elim.id)
                            if signup_result.ok:
                                for w in matches:
                                    if w.get("one_shot"):
                                        sfb_watch.mark_consumed(
                                            w["pattern"], elim.id, elim.name
                                        )
                                notifier.channel(
                                    f":robot_face: *Auto-signed up* for *{elim.name}* "
                                    f"(matched watch '{matches[0]['pattern']}'). "
                                    f"{signup_result.message}"
                                )
                                print(
                                    f"[sfb-poll] AUTO-SIGNUP success: {elim.id} {elim.name}",
                                    flush=True,
                                )
                            else:
                                notifier.channel(
                                    f":x: Auto-signup *failed* for *{elim.name}*: {signup_result.message}. "
                                    "Watch left active — try manually."
                                )
                                print(
                                    f"[sfb-poll] AUTO-SIGNUP failed: {signup_result.message}",
                                    flush=True,
                                )
                        except Exception as e:
                            notifier.channel(
                                f":x: Auto-signup ERROR for *{elim.name}*: `{e!r}`. Watch left active."
                            )
                            print(f"[sfb-poll] AUTO-SIGNUP error: {e!r}", flush=True)
                    elif matches and not (settings.sfb_email and settings.sfb_password):
                        notifier.channel(
                            f":warning: *{elim.name}* matched watch '{matches[0]['pattern']}' "
                            "but SFB credentials aren't configured — signing up manually."
                        )
            sfb.save_state(sfb.to_state(current))
        except Exception as e:
            print(f"[sfb-poll-error] {e!r}", flush=True)
        time.sleep(interval)


def main(mfl_poll_interval: int = 300, sfb_poll_interval: int | None = None) -> None:
    sfb_interval = sfb_poll_interval or settings.sfb_poll_seconds
    if not settings.league_ids:
        print("[warn] MFL_LEAGUE_IDS not set — MFL poller will idle.", flush=True)
    threading.Thread(target=_mfl_poller_loop, args=(mfl_poll_interval,), daemon=True).start()
    threading.Thread(target=_sfb_poller_loop, args=(sfb_interval,), daemon=True).start()
    threading.Thread(target=_account_monitor_loop, args=(ACCOUNT_POLL_SEC,), daemon=True).start()
    print(
        f"[service] MFL poll every {mfl_poll_interval}s (or {FAST_POLL_SEC}s when an auto rule is on); "
        f"SFB poll every {sfb_interval}s; "
        f"account-monitor every {ACCOUNT_POLL_SEC}s; "
        f"starting Slack bot…",
        flush=True,
    )
    run_bot()


if __name__ == "__main__":
    main()
