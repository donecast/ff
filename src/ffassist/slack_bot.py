from __future__ import annotations

import re
from typing import Any

from rapidfuzz import process
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from ffassist import auto_pick, state as state_mod
from ffassist.config import settings
from ffassist.draft_state import (
    Player,
    available_players,
    filter_by_position,
    my_picks,
    parse_picks,
    parse_players,
)
from ffassist.mfl.client import MFLClient
from ffassist.poller import find_my_franchise
from ffassist.rankings import (
    best_available,
    get_filtered_adp,
    load_csv_override,
    merge_rankings,
    parse_mfl_adp,
)

CONFIRM_WORDS = {"yes", "y", "confirm", "ok", "okay", "yep", "do it", "send it"}
CANCEL_WORDS = {"no", "n", "cancel", "stop", "abort"}
SFB_JOIN_WORDS = {"join", "signup", "sign up", "sign me up", "enter", "in"}

HELP_TEXT = """*Just talk to me in plain English.* I can:
  • Check draft status across your leagues
  • List available / best players (filter by position, flex, sflex)
  • Show your picks
  • Look up scoring rules; diff rules year-over-year
  • Propose a draft pick (`take Jefferson` etc.) — you confirm with `yes`
  • Set / change / disable auto-pick rules per league
    (e.g. "for 37714, if time's running low, prefer best WR or TE, never kicker")

*Fast-paths I keep instant (no AI round-trip):*
  • `yes` / `no` — confirm or cancel a pending pick or signup
  • `stop` / `wait` / `hold` — in a draft thread, cancel auto-pick for that pick
  • `help` — this message

*Auto-pick:* if a rule is set for a league, I warn in-thread when <30 min remain, then submit at 10 min unless you say `stop`.

*In #fantasyfootball threads:* reply `join` under an SFB announcement to sign up (you confirm with `yes`)."""


def build_app() -> App:
    app = App(token=settings.slack_bot_token)

    @app.event("message")
    def on_message(event: dict, say: Any, client: Any) -> None:
        # Debug: log every incoming event
        print(
            f"[bot-event] type={event.get('type')} subtype={event.get('subtype')} "
            f"ch={event.get('channel')} ch_type={event.get('channel_type')} "
            f"user={event.get('user')} bot_id={event.get('bot_id')} "
            f"thread_ts={event.get('thread_ts')} text={(event.get('text') or '')[:60]!r}",
            flush=True,
        )
        if event.get("subtype") in {"bot_message", "message_changed", "message_deleted", "channel_join"}:
            return
        if event.get("bot_id"):
            return
        thread_ts = event.get("thread_ts")
        text = (event.get("text") or "").strip()
        channel_type = event.get("channel_type")
        channel_id = event.get("channel")

        # DM behavior unchanged
        if channel_type == "im":
            if thread_ts:
                _handle_thread_message(thread_ts, text, say, client)
            else:
                _handle_toplevel(text, say, thread_ts=event.get("ts"))
            return

        # Only act on the configured #fantasyfootball channel — ignore everything else
        if channel_id != settings.slack_channel_id:
            return

        # Thread reply in our channel
        if thread_ts:
            state = state_mod.load()
            thread = state["threads"].get(thread_ts)
            if thread:
                if thread.get("kind") == "sfb_eliminator":
                    _handle_sfb_thread(thread_ts, thread, text, say)
                    return
                _handle_thread_message(thread_ts, text, say, client)
                return
            # Unknown thread — continued NLP conversation. Fetch thread history for memory.
            history = _fetch_thread_history(client, channel_id, thread_ts)
            _nlp_fallback(text, None, say, thread_ts, history=history)
            return

        # Top-level message in our channel — open a new thread and answer via NLP
        _handle_toplevel(text, say, thread_ts=event.get("ts"))

    @app.event("app_mention")
    def on_mention(event: dict, say: Any) -> None:
        text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text") or "").strip()
        _handle_toplevel(text, say, thread_ts=event.get("ts"))

    return app


def _handle_toplevel(text: str, say: Any, thread_ts: str | None) -> None:
    stripped = text.strip().lower()
    if stripped in {"help", "?"} or stripped == "":
        say(text=HELP_TEXT, thread_ts=thread_ts)
        return
    _nlp_fallback(text, None, say, thread_ts)


def _handle_automate(arg: str) -> str:
    arg = arg.strip()
    if not arg:
        rules = auto_pick.list_rules()
        if not rules:
            return "No auto-pick rules set. Set one with `automate <league_id> <rule text>`."
        lines = ["*Auto-pick rules:*"]
        for r in rules:
            status = "✓ enabled" if r.enabled else "✗ disabled"
            lines.append(f"  L{r.league_id} [{status}]: {r.rule}")
        return "\n".join(lines)
    parts = arg.split(maxsplit=1)
    if len(parts) < 2:
        # `automate <league_id>` shows current rule
        lid = parts[0]
        if not lid.isdigit():
            return "Usage: `automate <league_id> <rule text>` or `automate <league_id> off`."
        r = auto_pick.get_rule(lid)
        if not r:
            return f"No rule for L{lid}."
        status = "enabled" if r.enabled else "disabled"
        return f"L{lid} [{status}]: {r.rule}"
    lid, rest = parts[0], parts[1].strip()
    if not lid.isdigit():
        return "Usage: `automate <league_id> <rule text>` or `automate <league_id> off`."
    if rest.lower() in {"off", "disable", "stop", "none"}:
        ok = auto_pick.disable(lid)
        return f"Auto-pick disabled for L{lid}." if ok else f"No active rule for L{lid}."
    auto_pick.set_rule(lid, rest)
    return f":white_check_mark: Auto-pick enabled for L{lid}: _{rest}_"


def _nlp_fallback(
    text: str,
    thread_ctx: dict | None,
    say: Any,
    thread_ts: str | None,
    history: list[dict] | None = None,
) -> None:
    from ffassist.nlp import handle_message

    reply = handle_message(text, thread_ctx=thread_ctx, history=history)
    say(text=reply, thread_ts=thread_ts)


def _fetch_thread_history(client: Any, channel: str, thread_ts: str, limit: int = 20) -> list[dict]:
    """Fetch Slack thread replies, return as Claude-style alternating user/assistant messages.

    Drops the trailing user message (caller will pass it separately as the latest turn).
    """
    try:
        result = client.conversations_replies(channel=channel, ts=thread_ts, limit=limit)
        msgs = result.get("messages", []) or []
    except Exception:
        return []

    out: list[dict] = []
    for m in msgs:
        if m.get("subtype") in {"channel_join", "channel_leave", "message_changed", "message_deleted"}:
            continue
        text = (m.get("text") or "").strip()
        text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
        if not text:
            continue
        role = "assistant" if m.get("bot_id") else "user"
        out.append({"role": role, "content": text})

    # Merge consecutive same-role messages
    merged: list[dict] = []
    for m in out:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(m)
    # Ensure starts with user (Claude requirement) — drop any leading assistant
    while merged and merged[0]["role"] == "assistant":
        merged.pop(0)
    return merged


def _split_league(arg: str | None) -> tuple[str | None, str | None]:
    if not arg:
        return (None, None)
    parts = arg.split(maxsplit=1)
    return (parts[0], parts[1] if len(parts) > 1 else None)


def _toplevel_league_cmd(cmd: str, league_id: str, pos: str | None, say: Any, thread_ts: str | None) -> None:
    try:
        with MFLClient() as mfl:
            players_lookup = parse_players(mfl.players())
            league = mfl.league(league_id)
            picks = parse_picks(mfl.draft_results(league_id))
            adp = get_filtered_adp(mfl.adp(), players_lookup)
    except Exception as e:
        say(text=f":x: MFL error: `{e!r}`", thread_ts=thread_ts)
        return
    overrides = load_csv_override(league_id, players=players_lookup)
    rankings = merge_rankings(adp, overrides)
    avail = available_players(players_lookup, picks)

    if cmd == "available":
        pool = filter_by_position(avail, pos) if pos else avail
        top = best_available(pool, rankings, n=15)
        say(text=_format_list(top, f"L{league_id} available {pos or 'all'} (top 15)"), thread_ts=thread_ts)
        return
    if cmd == "best":
        pool = filter_by_position(avail, pos or "FLEX")
        top = best_available(pool, rankings, n=5)
        say(text=_format_list(top, f"L{league_id} best {pos or 'FLEX'}"), thread_ts=thread_ts)
        return
    if cmd == "mypicks":
        my_id = find_my_franchise(league, settings.mfl_username)
        if not my_id:
            say(text=f":x: Could not identify your franchise in {league_id}.", thread_ts=thread_ts)
            return
        mine = my_picks(picks, my_id)
        lines = [
            f"R{p.round} #{p.pick}: {players_lookup[p.player_id].display() if p.player_id in players_lookup else p.player_id}"
            for p in mine
        ]
        say(text=f"*L{league_id} my picks:*\n" + ("\n".join(lines) or "(none yet)"), thread_ts=thread_ts)


def _format_status() -> str:
    ids = settings.league_ids
    if not ids:
        return "No leagues tracked (set MFL_LEAGUE_IDS)."
    lines = ["*Status:*"]
    try:
        with MFLClient() as mfl:
            for lid in ids:
                try:
                    league = mfl.league(lid)
                    picks = parse_picks(mfl.draft_results(lid))
                    my_id = find_my_franchise(league, settings.mfl_username)
                    name = league.get("name", "?")
                    completed = len(picks)
                    mine = sum(1 for p in picks if p.franchise_id == my_id) if my_id else 0
                    lines.append(f"  L{lid} — {name}: {completed} picks made, you've drafted {mine}")
                except Exception as e:
                    lines.append(f"  L{lid} — error: {e!r}")
    except Exception as e:
        return f":x: MFL error: `{e!r}`"
    return "\n".join(lines)


def _handle_sfb_thread(thread_ts: str, thread: dict, text: str, say: Any) -> None:
    from ffassist.sfb.client import SfbClient

    lower = text.lower().strip()
    elim_id = thread["eliminator_id"]
    name = thread.get("name", f"id={elim_id}")

    if thread.get("pending"):
        if any(w in lower for w in CONFIRM_WORDS):
            say(text=f"Submitting signup for *{name}* (id {elim_id})…", thread_ts=thread_ts)
            try:
                with SfbClient() as sfb:
                    result = sfb.signup(elim_id)
            except Exception as e:
                say(text=f":x: SFB signup failed: `{e!r}`", thread_ts=thread_ts)
                return
            if result.ok:
                say(text=f":white_check_mark: Signed up for *{name}*. {result.message}", thread_ts=thread_ts)
            else:
                say(text=f":x: Signup failed: {result.message}", thread_ts=thread_ts)
            state = state_mod.load()
            t = state["threads"].get(thread_ts, thread)
            t["pending"] = None
            t["signed_up"] = result.ok
            state["threads"][thread_ts] = t
            state_mod.save(state)
            return
        if any(w in lower for w in CANCEL_WORDS):
            state = state_mod.load()
            t = state["threads"].get(thread_ts, thread)
            t["pending"] = None
            state["threads"][thread_ts] = t
            state_mod.save(state)
            say(text="Cancelled.", thread_ts=thread_ts)
            return

    if any(w in lower for w in SFB_JOIN_WORDS):
        if not settings.sfb_email or not settings.sfb_password:
            say(text=":x: SFB credentials not configured (`SFB_EMAIL`/`SFB_PASSWORD`).", thread_ts=thread_ts)
            return
        state = state_mod.load()
        t = state["threads"].get(thread_ts, thread)
        t["pending"] = {"action": "signup"}
        state["threads"][thread_ts] = t
        state_mod.save(state)
        say(
            text=f":warning: Confirm: sign up for *{name}* (id {elim_id})? Reply `yes` or `no`.",
            thread_ts=thread_ts,
        )


def _handle_thread_message(thread_ts: str, text: str, say: Any, client: Any) -> None:
    state = state_mod.load()
    thread = state["threads"].get(thread_ts)
    if not thread:
        say(
            text="I don't have context for this thread. Try `help` at top level.",
            thread_ts=thread_ts,
        )
        return
    if thread.get("kind") == "sfb_eliminator":
        _handle_sfb_thread(thread_ts, thread, text, say)
        return

    league_id = thread["league_id"]
    year = thread["year"]
    my_franchise = thread["my_franchise_id"]
    lower = text.lower()

    # Stop auto-pick for this pick if user says stop / wait / hold
    if any(w in lower for w in {"stop", "wait", "hold", "cancel auto"}) and not thread.get("pending"):
        pick_number = thread.get("pick_number")
        if pick_number is not None:
            auto_pick.mark_stopped(thread_ts, pick_number)
            say(
                text=":octagonal_sign: Auto-pick cancelled for this pick. You're still on the clock — make a manual pick.",
                thread_ts=thread_ts,
            )
            return

    if thread.get("pending"):
        if any(w in lower for w in CONFIRM_WORDS):
            _submit_pending(thread_ts, thread, say, year)
            return
        if any(w in lower for w in CANCEL_WORDS):
            thread["pending"] = None
            state["threads"][thread_ts] = thread
            state_mod.save(state)
            say(text="Cancelled.", thread_ts=thread_ts)
            return

    # Reply-with-number to draft: "1", "12", etc. → look up last_top20 entry
    stripped = lower.strip()
    if stripped.isdigit():
        rank = int(stripped)
        last_top = thread.get("last_top20") or []
        match = next((e for e in last_top if e["rank"] == rank), None)
        if match:
            thread["pending"] = {
                "player_id": match["player_id"],
                "name": match["player_name"],
                "team": match["team"],
                "position": match["position"],
            }
            state["threads"][thread_ts] = thread
            state_mod.save(state)
            fp_suffix = f"  _[{match['fp']}]_" if match.get("fp") else ""
            say(
                text=f":warning: Pick #{rank}: *{match['player_name']}* ({match['position']}, {match['team']}).{fp_suffix} Reply `yes` to draft, `no` to cancel.",
                thread_ts=thread_ts,
            )
            return
        else:
            say(
                text=f":x: No rank #{rank} in the last top-20. Ask for the top 20 first.",
                thread_ts=thread_ts,
            )
            return

    if lower.strip() in {"help", "?"}:
        say(text=HELP_TEXT, thread_ts=thread_ts)
        return

    thread_ctx_for_nlp = {
        "kind": "mfl_draft",
        "thread_ts": thread_ts,
        "league_id": league_id,
        "year": year,
        "pick_number": thread.get("pick_number"),
        "round": thread.get("round"),
    }
    # Fetch thread history for conversation memory
    history = _fetch_thread_history(client, thread.get("channel"), thread_ts) if client else None
    _nlp_fallback(text, thread_ctx_for_nlp, say, thread_ts, history=history)


def _parse_cmd(text: str) -> tuple[str, str | None]:
    s = text.strip()
    if not s:
        return ("", None)
    parts = s.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else None
    m = re.search(r"best\s+(\w+)\s+available", s, re.I)
    if m:
        return ("best", m.group(1))
    if cmd in {"available", "avail"}:
        return ("available", arg)
    if cmd in {"best", "boa"}:
        return ("best", arg)
    if cmd in {"mypicks", "mine", "my"}:
        return ("mypicks", arg)
    if cmd in {"draft", "pick", "take"}:
        return ("draft", arg)
    if cmd == "leagues":
        return ("leagues", None)
    if cmd == "status":
        return ("status", None)
    if cmd == "automate":
        return ("automate", arg)
    if cmd in {"help", "?"}:
        return ("help", None)
    return (cmd, arg)


def _match_player(query: str, pool: list[Player]) -> Player | None:
    names = {p.name: p for p in pool}
    if not names:
        return None
    match = process.extractOne(query, list(names.keys()), score_cutoff=70)
    if not match:
        return None
    return names[match[0]]


def _format_list(ranked: list, header: str) -> str:
    lines = [f"*{header}:*"]
    for pl, rank in ranked:
        r = f"ADP {rank:.1f}" if rank is not None else "—"
        lines.append(f"  {pl.display()} ({r})")
    return "\n".join(lines)


def _submit_pending(thread_ts: str, thread: dict, say: Any, year: int) -> None:
    pending = thread["pending"]
    league_id = thread["league_id"]
    overall_pick = int(thread["pick_number"])
    round_num = int(thread["round"])

    # MFL's live_draft import expects PICK as the within-round slot (1..team_count),
    # not the overall pick number. Derive slot from state-stored draft_order length.
    state = state_mod.load()
    ls = state.get("leagues", {}).get(league_id, {})
    order = ls.get("draft_order") or []
    team_count = len(order) if order else 0
    if team_count > 0:
        slot = overall_pick - (round_num - 1) * team_count
    else:
        # Fall back to overall — round 1 picks are slot=overall anyway
        slot = overall_pick

    say(
        text=(
            f"Submitting *{pending['name']}* — {pending['team']} {pending['position']} "
            f"(L{league_id}, R{round_num}.{slot:02d}, overall #{overall_pick})…"
        ),
        thread_ts=thread_ts,
    )
    try:
        with MFLClient(year=year) as mfl:
            resp = mfl.submit_live_draft_pick(
                league_id=league_id,
                player_id=pending["player_id"],
                round_=round_num,
                pick=slot,
            )
    except Exception as e:
        say(text=f":x: Submit failed: `{e!r}`", thread_ts=thread_ts)
        return

    # Verify by re-reading draftResults — don't trust the response body alone
    try:
        with MFLClient(year=year) as mfl2:
            dr = mfl2._export("draftResults", league_id, force=True)["draftResults"]
        from ffassist.draft_state import parse_picks
        picks_now = parse_picks(dr)
        made = any(
            p.player_id == pending["player_id"]
            and p.round == round_num
            and p.pick == slot
            for p in picks_now
        )
    except Exception as e:
        made = None
        verify_err = repr(e)
    else:
        verify_err = None

    if made is True:
        say(
            text=f":white_check_mark: Submitted and verified: *{pending['name']}* at R{round_num}.{slot:02d}.",
            thread_ts=thread_ts,
        )
    elif made is False:
        say(
            text=(
                f":x: MFL did NOT record the pick. Response: ```{resp}```\n"
                "You are likely still on the clock. Submit on MFL directly."
            ),
            thread_ts=thread_ts,
        )
        return
    else:
        say(
            text=(
                f":warning: Submitted but verification call failed ({verify_err}). "
                f"MFL response: ```{resp}```\nCheck MFL directly to confirm."
            ),
            thread_ts=thread_ts,
        )
    state = state_mod.load()
    t = state["threads"].get(thread_ts, thread)
    t["pending"] = None
    t["submitted"] = pending
    state["threads"][thread_ts] = t
    state_mod.save(state)


def run() -> None:
    if not settings.slack_bot_token or not settings.slack_app_token:
        raise RuntimeError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN required")
    app = build_app()
    handler = SocketModeHandler(app, settings.slack_app_token)
    handler.start()
