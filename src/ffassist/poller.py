from __future__ import annotations

import re
from dataclasses import dataclass

from ffassist import auto_pick, state as state_mod
from ffassist.config import settings
from ffassist.draft_state import (
    Player,
    available_players,
    extract_draft_order,
    extract_draft_type,
    filter_by_position,
    infer_round1_order,
    my_picks,
    next_pick_number,
    parse_picks,
    parse_players,
    snake_owner,
)
from ffassist.mfl.client import MFLClient
from ffassist.rankings import (
    best_available,
    get_filtered_adp,
    load_csv_override,
    merge_rankings,
    parse_mfl_adp,
)
from ffassist.slack_notify import SlackNotifier


@dataclass
class LeagueConfig:
    league_id: str
    my_franchise_id: str
    year: int
    round1_order_override: list[str] | None = None
    display_name: str = ""


_HOST_RE = re.compile(
    r"^zzz\s*#?FCEliminator\s+\d{4}\s+(.+?)(?:\s+\d+)?\s*$", re.IGNORECASE
)


def extract_host(league_name: str) -> str:
    """Strip `zzz #FCEliminator YYYY ... <n>` to just the host's name."""
    if not league_name:
        return ""
    m = _HOST_RE.match(league_name)
    return m.group(1).strip() if m else league_name.strip()


def league_label(league_id: str, state: dict | None = None) -> str:
    """Read the cached host name; fall back to bare id."""
    s = state if state is not None else state_mod.load()
    ls = s.get("leagues", {}).get(league_id, {})
    return ls.get("display_name") or f"L{league_id}"


def find_my_franchise(league: dict, username: str) -> str | None:
    franchises = league.get("franchises", {}).get("franchise", [])
    if isinstance(franchises, dict):
        franchises = [franchises]
    # MFL doesn't expose username on franchise; match by name as best-effort.
    # User's franchise.name often matches their MFL display name.
    candidates = {username.lower(), "scott gerhardt"}
    for f in franchises:
        if (f.get("name") or "").lower() in candidates:
            return f.get("id")
    return None


def poll_once(
    mfl: MFLClient,
    notifier: SlackNotifier,
    league_cfg: LeagueConfig,
    players_lookup: dict[str, Player],
    rankings_global: dict[str, float],
) -> dict | None:
    """Check one league. If on-the-clock state changed to my franchise, post a Slack DM.

    Returns the notification dict (channel + ts + pick_number) when a new on-the-clock alert is sent.
    """
    league_id = league_cfg.league_id
    state = state_mod.load()
    league_state = state["leagues"].setdefault(league_id, {})

    draft = mfl.draft_results(league_id)
    picks = parse_picks(draft)

    # Authoritative draft order + draft type from the API response.
    api_order = extract_draft_order(draft)
    draft_type = extract_draft_type(draft)
    stored_order = league_state.get("draft_order")
    if api_order:
        round1 = api_order
        if stored_order != api_order:
            league_state["draft_order"] = api_order
    elif stored_order:
        round1 = list(stored_order)
    else:
        inferred = league_cfg.round1_order_override or infer_round1_order(picks)
        round1 = inferred or None
    if draft_type:
        league_state["draft_type"] = draft_type

    team_count = len(round1) if round1 else None
    next_pick = next_pick_number(picks, team_count=team_count)
    on_clock = snake_owner(next_pick, round1, draft_type=draft_type) if round1 else None

    last_alerted_pick = league_state.get("last_alerted_pick")
    if on_clock != league_cfg.my_franchise_id:
        league_state["on_clock"] = on_clock
        league_state["next_pick"] = next_pick
        state_mod.save(state)
        return None

    if last_alerted_pick == next_pick:
        return None

    # We're on the clock for a pick we haven't alerted on yet.
    overrides = load_csv_override(league_id, players=players_lookup)
    rankings = merge_rankings(rankings_global, overrides)
    avail = available_players(players_lookup, picks)
    flex_top = best_available(filter_by_position(avail, "FLEX"), rankings, n=3)
    mine = my_picks(picks, league_cfg.my_franchise_id)

    mine_lines = (
        "\n".join(
            f"  R{p.round} #{p.pick}: {players_lookup[p.player_id].display() if p.player_id in players_lookup else p.player_id}"
            for p in mine
        )
        or "  (none yet)"
    )

    # FantasyPros tier overlay for the brief flex_top preview (additive; silent on failure)
    fp_tag_by_id: dict[str, str] = {}
    if settings.fp_api_key:
        try:
            from ffassist import fantasypros as _fp
            mflids = [pl.id for pl, _ in flex_top]
            enriched = _fp.enrich_for_mflids(mflids, scoring="PPR", type_="draft")
            for pid, en in enriched.items():
                parts = []
                if en.tier is not None:
                    parts.append(f"T{en.tier}")
                if en.pos_rank:
                    parts.append(en.pos_rank)
                if en.news_impact:
                    parts.append("📰")
                if parts:
                    fp_tag_by_id[pid] = " ".join(parts)
        except Exception:
            pass

    def _fmt_top(pl, rank) -> str:
        adp_s = f"(ADP {rank:.1f})" if rank is not None else ""
        tag = fp_tag_by_id.get(pl.id)
        tag_s = f" [{tag}]" if tag else ""
        return f"  {pl.display()} {adp_s}{tag_s}".rstrip()

    top_lines = "\n".join(_fmt_top(pl, rank) for pl, rank in flex_top)

    round_num = (next_pick - 1) // max(len(round1), 1) + 1 if round1 else 1
    label = league_cfg.display_name or league_label(league_id, state)
    text = (
        f":alarm_clock: *On the clock* in *{label}*'s league — pick *#{next_pick}* (round {round_num})\n"
        f"*Your picks so far:*\n{mine_lines}\n"
        f"*Top flex available:*\n{top_lines}\n"
        f"_Reply in this thread to act._"
    )

    # Post to channel (user prefers it + DM replies blocked by workspace policy)
    result = notifier.channel(text)
    if not result or not result.get("ok"):
        return None

    league_state["last_alerted_pick"] = next_pick
    league_state["on_clock"] = on_clock
    league_state["next_pick"] = next_pick
    league_state["active_thread_ts"] = result["ts"]
    league_state["active_thread_channel"] = result["channel"]

    # ---- Top-20 build with 30-min dedupe ----
    import time as _time
    from ffassist import rankings_v2 as _r2

    now = _time.time()
    last_top20_at = league_state.get("last_top20_at", 0)
    top20_text = league_state.get("last_top20_text") or ""
    top20_list = league_state.get("last_top20") or []
    use_cached = top20_text and (now - last_top20_at) < 1800  # 30 min

    if not use_cached:
        try:
            top20_text, top20_list = _r2.build_league_top_20(league_id, mfl, settings)
            league_state["last_top20_text"] = top20_text
            league_state["last_top20"] = top20_list
            league_state["last_top20_at"] = now
            cached_label = "(fresh)"
        except Exception as e:
            top20_text = f":x: Top-20 build failed: {e!r}"
            top20_list = []
            cached_label = "(error)"
    else:
        age_min = int((now - last_top20_at) / 60)
        cached_label = f"(reusing list cached {age_min}m ago — within 30-min window)"

    state["threads"][result["ts"]] = {
        "league_id": league_id,
        "channel": result["channel"],
        "pick_number": next_pick,
        "round": round_num,
        "year": league_cfg.year,
        "my_franchise_id": league_cfg.my_franchise_id,
        "kind": "mfl_draft",
        "pending": None,
        "last_top20": top20_list,
    }
    state_mod.save(state)

    # Post the top-20 as a thread reply
    if top20_text:
        fp_legend = "  · `[ECR/PosRk]` = FantasyPros ECR / position rank" if settings.fp_api_key else ""
        notifier.post(
            result["channel"],
            f"*Top 20 in {label}'s league* {cached_label}\n"
            "Format: rank. Name (Pos, Team) (PPG_rank/ADP/Raw_rank-fce) {dupes/bye_twins}\n"
            "  · `fce` = # of tracked FCE drafts this player has been selected in\n"
            f"{fp_legend}\n"
            "Reply with a number 1-20 to draft that player.\n\n"
            f"{top20_text}",
            thread_ts=result["ts"],
        )

    return {"ts": result["ts"], "channel": result["channel"], "pick": next_pick}


def auto_pick_check(
    mfl: MFLClient,
    notifier: SlackNotifier,
    league_cfg: LeagueConfig,
) -> dict | None:
    """If a rule is enabled and we're on the clock, handle warn (30m) / submit (10m) phases."""
    league_id = league_cfg.league_id
    rule = auto_pick.get_rule(league_id)
    if not rule or not rule.enabled:
        return None

    state = state_mod.load()
    league_state = state["leagues"].get(league_id, {})
    thread_ts = league_state.get("active_thread_ts")
    thread_channel = league_state.get("active_thread_channel")
    next_pick = league_state.get("next_pick")
    if not thread_ts or not next_pick:
        return None
    if league_state.get("on_clock") != league_cfg.my_franchise_id:
        return None

    thread = state["threads"].get(thread_ts, {})
    keys = auto_pick.thread_phase_keys(next_pick)
    if thread.get(keys["stopped"]):
        return None

    draft_raw = mfl.draft_results(league_id)
    last_ts = auto_pick.last_pick_timestamp(draft_raw)
    if last_ts is None:
        return None  # draft hasn't started; no deadline yet
    league_info = mfl.league(league_id)
    remaining = auto_pick.time_until_deadline(last_ts, league_info)

    # Phase 1: warn at 30 min
    if remaining < auto_pick.WARN_THRESHOLD_SEC and not thread.get(keys["warned"]):
        sel = auto_pick.select_auto_pick(league_id, rule.rule, year=league_cfg.year)
        if sel is None:
            return None
        planned, reason = sel
        mins_left = max(0, int(remaining / 60))
        warn_text = (
            f":warning: *Time remaining ~{mins_left}min* on pick #{next_pick}.\n"
            f"Per rule: _{rule.rule}_\n"
            f"I plan to submit *{planned.display()}* at the 10-min mark.\n"
            f"Reasoning: {reason}\n"
            "Reply *stop* in this thread to cancel auto-pick, or make a manual pick."
        )
        notifier.post(thread_channel, warn_text, thread_ts=thread_ts)
        thread[keys["warned"]] = True
        thread[keys["planned_id"]] = planned.id
        state["threads"][thread_ts] = thread
        state_mod.save(state)
        return {"phase": "warn", "league": league_id, "player_id": planned.id}

    # Phase 2: submit at 10 min
    if remaining < auto_pick.PICK_THRESHOLD_SEC and not thread.get(keys["picked"]):
        sel = auto_pick.select_auto_pick(league_id, rule.rule, year=league_cfg.year)
        if sel is None:
            return None
        chosen, reason = sel
        round_num = thread.get("round", 1)
        # MFL's live_draft expects PICK as within-round slot, not overall.
        order = league_state.get("draft_order") or []
        tc = len(order)
        slot = next_pick - (round_num - 1) * tc if tc else next_pick
        try:
            resp = mfl.submit_live_draft_pick(
                league_id=league_id,
                player_id=chosen.id,
                round_=round_num,
                pick=slot,
            )
        except Exception as e:
            err_text = f":x: Auto-pick FAILED to submit *{chosen.display()}*: `{e!r}`. You're still on the clock."
            notifier.post(thread_channel, err_text, thread_ts=thread_ts)
            return {"phase": "pick_error", "league": league_id, "error": repr(e)}

        thread[keys["picked"]] = True
        state["threads"][thread_ts] = thread
        state_mod.save(state)
        ok_text = (
            f":robot_face: *Auto-picked* {chosen.display()} (pick #{next_pick}).\n"
            f"Per rule: _{rule.rule}_\n"
            f"Reasoning: {reason}"
        )
        notifier.post(thread_channel, ok_text, thread_ts=thread_ts)
        return {"phase": "picked", "league": league_id, "player_id": chosen.id, "response": resp}

    return None


# Escalating reminder ladder: (seconds_elapsed_on_clock, level_key, message_template)
# Scott timed out on Joey Wright (2026-05-15) — these progressively-aggressive nudges
# exist so a single missed alert doesn't cost a pick. Each level fires at most once
# per pick and only while Scott is still on the clock for that exact pick.
REMINDER_LEVELS: list[tuple[float, str, str, str]] = [
    (2 * 3600,           "2h",    ":alarm_clock:",              "*2h on the clock*"),
    (4 * 3600,           "4h",    ":alarm_clock::alarm_clock:", "*4h on the clock* — 2h left on a 6h clock"),
    (5 * 3600,           "5h",    ":warning:",                  "*5h on the clock* — 1h to go"),
    (5 * 3600 + 30 * 60, "5h30",  ":rotating_light:",           "*5:30 on the clock* — *30 MIN LEFT*"),
    (5 * 3600 + 50 * 60, "5h50",  ":rotating_light::rotating_light:", "*5:50 on the clock* — *10 MIN LEFT*"),
    (5 * 3600 + 55 * 60, "5h55",  ":rotating_light::rotating_light::rotating_light:", "*5:55 — 5 MINUTES LEFT*. LAST CHANCE."),
]


def escalating_reminders(
    mfl: MFLClient,
    notifier: SlackNotifier,
    league_cfg: LeagueConfig,
) -> dict | None:
    """Post progressively-aggressive reminders while Scott is on the clock.

    Fires at 2h, 4h, 5h, 5:30, 5:50, 5:55 of elapsed clock time. Each level fires
    once per pick. Skipped entirely when Scott is no longer on the clock or has
    already submitted the pick — checked on every invocation, so a successful pick
    silences the remaining ladder.
    """
    league_id = league_cfg.league_id
    state = state_mod.load()
    ls = state.get("leagues", {}).get(league_id, {})

    if ls.get("on_clock") != league_cfg.my_franchise_id:
        return None

    thread_ts = ls.get("active_thread_ts")
    thread_channel = ls.get("active_thread_channel")
    next_pick = ls.get("next_pick")
    if not thread_ts or not thread_channel or not next_pick:
        return None

    thread = state.get("threads", {}).get(thread_ts, {})
    if thread.get("pick_number") != next_pick:
        return None  # stale thread
    if thread.get("submitted"):
        return None  # already picked

    try:
        draft_raw = mfl.draft_results(league_id)
    except Exception:
        return None
    last_ts = auto_pick.last_pick_timestamp(draft_raw)
    if last_ts is None:
        return None  # pick #1 / draft not started

    import time as _time
    elapsed = _time.time() - last_ts
    fired: list[str] = list(thread.get("reminder_levels_fired") or [])

    label = league_cfg.display_name or league_label(league_id, state)
    posted: list[str] = []
    for threshold, key, icon, headline in REMINDER_LEVELS:
        if elapsed < threshold or key in fired:
            continue
        # Re-verify on-clock + not-submitted before each post (cheap; state already loaded)
        if ls.get("on_clock") != league_cfg.my_franchise_id:
            break
        if thread.get("submitted"):
            break
        hours = int(elapsed // 3600)
        mins = int((elapsed % 3600) // 60)
        text = (
            f"{icon} {headline}\n"
            f"Pick *#{next_pick}* in *{label}*'s league — elapsed *{hours}h{mins:02d}m*.\n"
            f"Reply with a top-20 number in this thread, or make a manual pick on MFL."
        )
        notifier.post(thread_channel, text, thread_ts=thread_ts)
        fired.append(key)
        posted.append(key)

    if posted:
        thread["reminder_levels_fired"] = fired
        state["threads"][thread_ts] = thread
        state_mod.save(state)
        return {"phase": "reminder", "league": league_id, "levels": posted}
    return None


DEFAULT_TOP1_THRESHOLD_SEC = 15 * 60


def default_top1_auto_pick(
    mfl: MFLClient,
    notifier: SlackNotifier,
    league_cfg: LeagueConfig,
) -> dict | None:
    """Universal fallback: when Scott is on the clock and <15 min remain, auto-take #1
    from the cached top-20 for that league. Runs regardless of per-league rules.

    Only fires once per pick (tracked via thread state). Verifies the submission via a
    fresh draftResults re-read before claiming success.
    """
    league_id = league_cfg.league_id
    state = state_mod.load()
    ls = state.get("leagues", {}).get(league_id, {})
    thread_ts = ls.get("active_thread_ts")
    thread_channel = ls.get("active_thread_channel")
    next_pick = ls.get("next_pick")
    if not thread_ts or not next_pick:
        return None
    if ls.get("on_clock") != league_cfg.my_franchise_id:
        return None
    thread = state["threads"].get(thread_ts, {})
    if thread.get("pick_number") != next_pick:
        return None  # Stale thread reference
    if thread.get("default_top1_done"):
        return None  # Already fired for this pick
    if thread.get("submitted"):
        return None  # Manually submitted already

    # Time check
    try:
        draft_raw = mfl.draft_results(league_id)
    except Exception:
        return None
    last_ts = auto_pick.last_pick_timestamp(draft_raw)
    if last_ts is None:
        return None
    try:
        league_info = mfl.league(league_id)
    except Exception:
        return None
    remaining = auto_pick.time_until_deadline(last_ts, league_info)
    if remaining > DEFAULT_TOP1_THRESHOLD_SEC:
        return None

    top20 = thread.get("last_top20") or ls.get("last_top20") or []
    if not top20:
        notifier.post(
            thread_channel,
            ":warning: 15 min left — wanted to auto-take #1 but no cached top-20 exists. Pick manually.",
            thread_ts=thread_ts,
        )
        thread["default_top1_done"] = True
        state["threads"][thread_ts] = thread
        state_mod.save(state)
        return None

    chosen = top20[0]
    round_num = int(thread.get("round", 1))
    order = ls.get("draft_order") or []
    tc = len(order)
    slot = next_pick - (round_num - 1) * tc if tc else next_pick

    mins_left = max(0, int(remaining / 60))
    label = league_cfg.display_name or league_label(league_id, state)
    notifier.post(
        thread_channel,
        (
            f":robot_face: *Auto-pick at {mins_left}min remaining*\n"
            f"Taking #1 from cached top-20: *{chosen['player_name']}* "
            f"({chosen['position']} {chosen['team']}) — L{league_id} ({label}), R{round_num}.{slot:02d}"
        ),
        thread_ts=thread_ts,
    )

    try:
        resp = mfl.submit_live_draft_pick(
            league_id=league_id,
            player_id=chosen["player_id"],
            round_=round_num,
            pick=slot,
        )
    except Exception as e:
        notifier.post(
            thread_channel,
            f":x: Auto-pick submit raised: `{e!r}`. You may still be on the clock — check MFL.",
            thread_ts=thread_ts,
        )
        thread["default_top1_done"] = True
        state["threads"][thread_ts] = thread
        state_mod.save(state)
        return {"phase": "default_top1_error", "league": league_id, "error": repr(e)}

    # Verify
    try:
        dr2 = mfl._export("draftResults", league_id, force=True)["draftResults"]
        from ffassist.draft_state import parse_picks as _pp
        picks_now = _pp(dr2)
        made = any(
            p.player_id == chosen["player_id"]
            and p.round == round_num
            and p.pick == slot
            for p in picks_now
        )
    except Exception:
        made = None

    if made is True:
        notifier.post(
            thread_channel,
            f":white_check_mark: Auto-picked *{chosen['player_name']}* — verified at R{round_num}.{slot:02d}.",
            thread_ts=thread_ts,
        )
        thread["submitted"] = {
            "player_id": chosen["player_id"],
            "name": chosen["player_name"],
            "team": chosen["team"],
            "position": chosen["position"],
            "auto": True,
        }
    elif made is False:
        notifier.post(
            thread_channel,
            f":x: Auto-submit didn't land. MFL response: ```{resp}```\nYou're still on the clock. Check MFL.",
            thread_ts=thread_ts,
        )
    else:
        notifier.post(
            thread_channel,
            f":warning: Submitted but verification failed. MFL response: ```{resp}```\nCheck MFL to confirm.",
            thread_ts=thread_ts,
        )
    thread["default_top1_done"] = True
    state["threads"][thread_ts] = thread
    state_mod.save(state)
    return {"phase": "default_top1", "league": league_id, "player_id": chosen["player_id"], "made": made}


def poll_all(league_ids: list[str] | None = None) -> list[dict]:
    league_ids = league_ids or settings.league_ids
    if not league_ids:
        return []
    notifier = SlackNotifier()
    results = []
    with MFLClient() as mfl:
        players_lookup = parse_players(mfl.players())
        adp = get_filtered_adp(mfl.adp(), players_lookup)
        for lid in league_ids:
            league = mfl.league(lid)
            my_id = find_my_franchise(league, settings.mfl_username)
            if not my_id:
                continue
            host = extract_host(league.get("name", "") or "")
            if host:
                s = state_mod.load()
                ls = s.setdefault("leagues", {}).setdefault(lid, {})
                if ls.get("display_name") != host or ls.get("league_name") != league.get("name"):
                    ls["display_name"] = host
                    ls["league_name"] = league.get("name", "")
                    state_mod.save(s)
            cfg = LeagueConfig(
                league_id=lid, my_franchise_id=my_id, year=mfl.year, display_name=host
            )
            try:
                hit = poll_once(mfl, notifier, cfg, players_lookup, adp)
                if hit:
                    results.append({"league": lid, **hit})
            except Exception as e:
                print(f"[poll-error] league={lid} {e!r}")
            try:
                auto_hit = auto_pick_check(mfl, notifier, cfg)
                if auto_hit:
                    results.append({"league": lid, **auto_hit})
            except Exception as e:
                print(f"[auto-pick-error] league={lid} {e!r}")
            try:
                rem_hit = escalating_reminders(mfl, notifier, cfg)
                if rem_hit:
                    results.append({"league": lid, **rem_hit})
            except Exception as e:
                print(f"[reminder-error] league={lid} {e!r}")
    return results
