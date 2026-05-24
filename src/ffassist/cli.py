from __future__ import annotations

import argparse
import json
import sys

import time

from ffassist.config import settings
from ffassist.draft_state import (
    available_players,
    filter_by_position,
    my_picks,
    parse_picks,
    parse_players,
)
from ffassist.mfl.client import MFLClient
from ffassist.poller import find_my_franchise, poll_all
from ffassist.rankings import (
    best_available,
    diagnose_csv,
    get_filtered_adp,
    load_csv_override,
    merge_rankings,
    parse_mfl_adp,
)
from ffassist.sfb import monitor as sfb
from ffassist.slack_notify import SlackNotifier, sfb_event_message


def _cmd_sfb_scan(args: argparse.Namespace) -> int:
    results = sfb.scan(min_id=args.min, max_id=args.max)
    real = [e for e in results if e.is_real]
    if args.json:
        print(json.dumps([e.__dict__ for e in real], indent=2))
        return 0
    print(f"Found {len(real)} live eliminator(s) (scanned ids {args.min or settings.sfb_elim_min_id}..{args.max or settings.sfb_elim_max_id}):")
    for e in real:
        cap = f"{e.entrants}/{e.capacity}" if e.entrants is not None else "?"
        print(f"  id={e.id:>3}  {cap:>7}  {e.status or '':<18}  {e.name}")
    return 0


def _cmd_sfb_check(args: argparse.Namespace) -> int:
    from ffassist import state as state_mod
    from ffassist.slack_notify import SFB_KIND

    prev = sfb.load_state()
    current = sfb.scan(min_id=args.min, max_id=args.max)
    events = sfb.diff(prev, current)
    notifier = SlackNotifier() if not args.no_slack else SlackNotifier(token="")
    for kind, elim in events:
        line = f"[{kind}] id={elim.id} {elim.name} ({elim.entrants}/{elim.capacity})"
        print(line)
        if notifier.enabled and (args.notify_all or kind in {"new", "full"}):
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
    if not events:
        print("No changes.")
    if not args.dry_run:
        sfb.save_state(sfb.to_state(current))
    return 0


def _cmd_sfb_watch(args: argparse.Namespace) -> int:
    interval = args.interval or settings.sfb_poll_seconds
    print(f"Watching SFB ids {args.min or settings.sfb_elim_min_id}..{args.max or settings.sfb_elim_max_id} every {interval}s. Ctrl-C to stop.")
    while True:
        try:
            _cmd_sfb_check(args)
        except Exception as e:
            print(f"[error] {e!r}")
        time.sleep(interval)


def _cmd_mfl_login(args: argparse.Namespace) -> int:
    with MFLClient() as c:
        ok = c.login()
    print("Login OK." if ok else "Login FAILED (no MFL_USER_ID cookie returned).")
    return 0 if ok else 1


def _cmd_slack_test(args: argparse.Namespace) -> int:
    n = SlackNotifier()
    if not n.enabled:
        print("SLACK_BOT_TOKEN not set.")
        return 1
    target = "dm" if args.dm else "channel"
    msg = args.message or f":wave: ffassist test ({target})"
    result = (n.dm(msg) if args.dm else n.channel(msg))
    if result and result.get("ok"):
        print(f"Sent to {target}. ts={result.get('ts')}")
        return 0
    print(f"Slack error: {result}")
    return 1


def _cmd_mfl_league(args: argparse.Namespace) -> int:
    with MFLClient() as c:
        data = c.league(args.league_id)
    print(json.dumps(data, indent=2))
    return 0


def _cmd_mfl_draft(args: argparse.Namespace) -> int:
    with MFLClient() as c:
        data = c.draft_results(args.league_id)
    print(json.dumps(data, indent=2))
    return 0


def _cmd_draft_available(args: argparse.Namespace) -> int:
    with MFLClient() as c:
        players = parse_players(c.players())
        picks = parse_picks(c.draft_results(args.league_id))
        adp = get_filtered_adp(c.adp(), players)
    overrides = load_csv_override(args.league_id, players=players)
    rankings = merge_rankings(adp, overrides)
    avail = available_players(players, picks)
    if args.position:
        avail = filter_by_position(avail, args.position)
    top = best_available(avail, rankings, n=args.n)
    for pl, rank in top:
        r = f"{rank:6.1f}" if rank is not None else "   -- "
        print(f"  {r}  {pl.display()}")
    print(f"({len(avail)} total available)")
    return 0


def _cmd_draft_mypicks(args: argparse.Namespace) -> int:
    with MFLClient() as c:
        players = parse_players(c.players())
        league = c.league(args.league_id)
        picks = parse_picks(c.draft_results(args.league_id))
    my_id = find_my_franchise(league, settings.mfl_username)
    if not my_id:
        print("Could not identify your franchise in this league.")
        return 1
    mine = my_picks(picks, my_id)
    if not mine:
        print(f"No picks yet for franchise {my_id}.")
        return 0
    print(f"Franchise {my_id} picks:")
    for p in mine:
        disp = players[p.player_id].display() if p.player_id in players else p.player_id
        print(f"  R{p.round} #{p.pick:>3}: {disp}")
    return 0


def _parse_league_at_year(spec: str, default_year: int) -> tuple[str, int]:
    if ":" in spec:
        lid, _, y = spec.partition(":")
        return lid.strip(), int(y)
    if "@" in spec:
        lid, _, y = spec.partition("@")
        return lid.strip(), int(y)
    return spec.strip(), default_year


def _cmd_mfl_rules(args: argparse.Namespace) -> int:
    league_id, year = _parse_league_at_year(args.league_spec, settings.mfl_year)
    with MFLClient(year=year) as c:
        data = c.rules(league_id)
    print(json.dumps(data, indent=2))
    return 0


def _cmd_mfl_rules_diff(args: argparse.Namespace) -> int:
    from ffassist.rules_compare import diff_position_rules, format_diff

    a_id, a_year = _parse_league_at_year(args.a, settings.mfl_year)
    b_id, b_year = _parse_league_at_year(args.b, settings.mfl_year)
    with MFLClient(year=a_year) as ca:
        a = ca.rules(a_id)
    with MFLClient(year=b_year) as cb:
        b = cb.rules(b_id)
    print(format_diff(f"{a_id}@{a_year}", f"{b_id}@{b_year}", diff_position_rules(a, b)))
    return 0


def _cmd_sfb_login_test(args: argparse.Namespace) -> int:
    from ffassist.sfb.client import SfbClient

    with SfbClient() as c:
        ok = c.login()
    print("SFB login OK." if ok else "SFB login FAILED.")
    return 0 if ok else 1


def _cmd_sfb_signup(args: argparse.Namespace) -> int:
    from ffassist.sfb.client import SfbClient

    with SfbClient() as c:
        result = c.signup(args.eliminator_id, promo_code=args.promo or "")
    print(("OK: " if result.ok else "FAIL: ") + result.message)
    if result.final_url:
        print(f"final URL: {result.final_url}")
    return 0 if result.ok else 1


def _cmd_rankings_check(args: argparse.Namespace) -> int:
    with MFLClient() as c:
        players = parse_players(c.players())
    total, resolved, unresolved = diagnose_csv(args.league_id, players)
    print(f"data/rankings/{args.league_id}.csv: {resolved}/{total} resolved")
    if unresolved:
        print("Unresolved:")
        for name in unresolved[:50]:
            print(f"  - {name}")
        if len(unresolved) > 50:
            print(f"  ... and {len(unresolved) - 50} more")
    return 0


def _cmd_poll(args: argparse.Namespace) -> int:
    hits = poll_all(args.league_ids or None)
    if hits:
        for h in hits:
            print(f"alert sent: {h}")
    else:
        print("No on-the-clock alerts.")
    return 0


def _cmd_service(args: argparse.Namespace) -> int:
    from ffassist.service import main as svc_main
    svc_main(mfl_poll_interval=args.interval)
    return 0


def _cmd_fp_refresh(args: argparse.Namespace) -> int:
    from ffassist import fantasypros as fp

    if not settings.fp_api_key:
        print("FP_API_KEY not set in .env")
        return 1
    used_before, limit = fp.calls_today()
    print(f"FP calls today before refresh: {used_before}/{limit}")
    positions = ["QB", "RB", "WR", "TE", "K", "DST"]
    for pos in positions:
        try:
            data = fp.get_consensus(position=pos, type_=args.type, scoring=args.scoring, force=args.force)
            print(f"  consensus {pos:>4s} {args.type:>7s} {args.scoring}: {data.get('count')} players (updated {data.get('last_updated')})")
        except fp.FPError as e:
            print(f"  consensus {pos}: ERROR {e}")
    try:
        proj = fp.get_projections(position="ALL", scoring=args.scoring, force=args.force)
        print(f"  projections ALL {args.scoring}: {proj.get('count')} players")
    except fp.FPError as e:
        print(f"  projections: ERROR {e}")
    try:
        news = fp.get_news(force=args.force)
        print(f"  news: {news.get('count')} items")
    except fp.FPError as e:
        print(f"  news: ERROR {e}")
    print(f"Building fp↔mfl id map...")
    try:
        mapping = fp.build_fp_to_mfl_map(force=args.force)
        print(f"  mapped {len(mapping)} players ({fp.MAP_FILE})")
    except fp.FPError as e:
        print(f"  mapping: ERROR {e}")
    used_after, _ = fp.calls_today()
    print(f"FP calls today after refresh: {used_after}/{limit}  (used {used_after - used_before} this run)")
    return 0


def _cmd_fp_ecr(args: argparse.Namespace) -> int:
    from ffassist import fantasypros as fp

    if not settings.fp_api_key:
        print("FP_API_KEY not set in .env")
        return 1
    data = fp.get_consensus(position=args.position, type_=args.type, scoring=args.scoring)
    print(f"{args.position} {args.type} {args.scoring} — {data.get('count')} players, {data.get('total_experts')} experts, updated {data.get('last_updated')}")
    for pl in data.get("players", [])[: args.n]:
        ecr = pl.get("rank_ecr")
        tier = pl.get("tier")
        name = pl.get("player_name")
        team = pl.get("player_team_id")
        pos = pl.get("player_position_id")
        bye = pl.get("player_bye_week") or "-"
        rmin = pl.get("rank_min") or "-"
        rmax = pl.get("rank_max") or "-"
        rave = pl.get("rank_ave") or "-"
        print(f"  #{str(ecr):>3} T{tier}  {name:<26s} {team}/{pos}  bye={str(bye):<2}  min/max/ave={rmin}/{rmax}/{rave}")
    used, limit = fp.calls_today()
    print(f"(FP calls today: {used}/{limit})")
    return 0


def _cmd_fp_news(args: argparse.Namespace) -> int:
    from ffassist import fantasypros as fp

    if not settings.fp_api_key:
        print("FP_API_KEY not set in .env")
        return 1
    data = fp.get_news()
    items = data.get("items", [])
    if args.player:
        needle = args.player.lower()
        items = [
            i for i in items
            if needle in (i.get("title", "") or "").lower()
            or needle in (i.get("desc", "") or "").lower()
        ]
    for item in items[: args.n]:
        when = item.get("created_formated", "")
        impact = item.get("impact", "") or "—"
        title = item.get("title", "")
        print(f"  [{when}] ({impact}) {title}")
    used, limit = fp.calls_today()
    print(f"\n({len(items)} matching items; FP calls today: {used}/{limit})")
    return 0


def _cmd_fp_map(args: argparse.Namespace) -> int:
    from ffassist import fantasypros as fp

    mapping = fp.load_fp_to_mfl_map()
    if not mapping:
        print(f"No fp↔mfl map yet. Run: ffassist fp-refresh")
        return 1
    print(f"fp↔mfl map: {len(mapping)} entries at {fp.MAP_FILE}")
    if args.show:
        for fpid, mflid in list(mapping.items())[: args.show]:
            print(f"  fp={fpid} -> mfl={mflid}")
    return 0


def _cmd_fp_enrich(args: argparse.Namespace) -> int:
    """Demo: enrich a comma-separated list of MFL player ids."""
    from ffassist import fantasypros as fp
    from ffassist.draft_state import parse_players
    from ffassist.mfl.client import MFLClient

    mflids = [x.strip() for x in args.mflids.split(",") if x.strip()]
    enriched = fp.enrich_for_mflids(mflids, scoring=args.scoring, type_=args.type)

    # Resolve names for nicer output
    name_by_id: dict[str, str] = {}
    try:
        with MFLClient() as c:
            players = parse_players(c.players())
        name_by_id = {pid: pl.display() for pid, pl in players.items()}
    except Exception:
        pass

    for mflid in mflids:
        e = enriched[mflid]
        name = name_by_id.get(mflid, f"mfl:{mflid}")
        if e.fpid is None:
            print(f"  {name}: no FP mapping")
            continue
        flag = f" 📰{e.news_impact}" if e.news_impact else ""
        ppts = f"  pts_ppr={e.points_ppr}" if e.points_ppr is not None else ""
        print(f"  {name}: ECR #{e.ecr} T{e.tier} {e.pos_rank} bye={e.bye}{ppts}{flag}")
    used, limit = fp.calls_today()
    print(f"(FP calls today: {used}/{limit})")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ffassist")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sfb-scan", help="One-shot scan of SFB eliminator signup pages")
    sp.add_argument("--min", type=int, default=None)
    sp.add_argument("--max", type=int, default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_sfb_scan)

    sp = sub.add_parser("sfb-check", help="Scan SFB and emit change events vs saved state")
    sp.add_argument("--min", type=int, default=None)
    sp.add_argument("--max", type=int, default=None)
    sp.add_argument("--dry-run", action="store_true", help="Do not update saved state")
    sp.add_argument("--no-slack", action="store_true", help="Skip Slack notifications")
    sp.add_argument("--notify-all", action="store_true", help="Slack on filling events too")
    sp.set_defaults(func=_cmd_sfb_check)

    sp = sub.add_parser("sfb-watch", help="Continuously scan SFB at SFB_POLL_SECONDS interval")
    sp.add_argument("--min", type=int, default=None)
    sp.add_argument("--max", type=int, default=None)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-slack", action="store_true")
    sp.add_argument("--notify-all", action="store_true")
    sp.add_argument("--interval", type=int, default=None, help="Override SFB_POLL_SECONDS")
    sp.set_defaults(func=_cmd_sfb_watch)

    sp = sub.add_parser("mfl-login", help="Test MFL username/password login")
    sp.set_defaults(func=_cmd_mfl_login)

    sp = sub.add_parser("slack-test", help="Send a test Slack message")
    sp.add_argument("--dm", action="store_true", help="DM the user instead of posting to channel")
    sp.add_argument("--message", help="Custom message text")
    sp.set_defaults(func=_cmd_slack_test)

    sp = sub.add_parser("mfl-league", help="Print MFL league info")
    sp.add_argument("league_id")
    sp.set_defaults(func=_cmd_mfl_league)

    sp = sub.add_parser("mfl-draft", help="Print MFL draft results")
    sp.add_argument("league_id")
    sp.set_defaults(func=_cmd_mfl_draft)

    sp = sub.add_parser("draft-available", help="List best available players for a league")
    sp.add_argument("league_id")
    sp.add_argument("--position", "-p", help="QB/RB/WR/TE/FLEX/SFLEX")
    sp.add_argument("-n", type=int, default=20)
    sp.set_defaults(func=_cmd_draft_available)

    sp = sub.add_parser("draft-mypicks", help="List your picks in a league")
    sp.add_argument("league_id")
    sp.set_defaults(func=_cmd_draft_mypicks)

    sp = sub.add_parser("mfl-rules", help="Print MFL scoring rules. Accepts <id> or <id>:<year>.")
    sp.add_argument("league_spec")
    sp.set_defaults(func=_cmd_mfl_rules)

    sp = sub.add_parser("mfl-rules-diff", help="Diff scoring between two leagues. Each as <id> or <id>:<year>.")
    sp.add_argument("a")
    sp.add_argument("b")
    sp.set_defaults(func=_cmd_mfl_rules_diff)

    sp = sub.add_parser("sfb-login-test", help="Verify SFB credentials by logging in")
    sp.set_defaults(func=_cmd_sfb_login_test)

    sp = sub.add_parser("sfb-signup", help="Sign up for an SFB eliminator by id")
    sp.add_argument("eliminator_id", type=int)
    sp.add_argument("--promo", help="Optional promo code")
    sp.set_defaults(func=_cmd_sfb_signup)

    sp = sub.add_parser("rankings-check", help="Validate data/rankings/<L>.csv vs MFL player list")
    sp.add_argument("league_id")
    sp.set_defaults(func=_cmd_rankings_check)

    sp = sub.add_parser("poll", help="Run on-the-clock check once across configured leagues")
    sp.add_argument("league_ids", nargs="*", help="Override MFL_LEAGUE_IDS")
    sp.set_defaults(func=_cmd_poll)

    sp = sub.add_parser("service", help="Run the long-lived service (poller + Slack bot)")
    sp.add_argument("--interval", type=int, default=300, help="Poller interval seconds")
    sp.set_defaults(func=_cmd_service)

    sp = sub.add_parser("fp-refresh", help="Warm FantasyPros caches (rankings + projections + news + id map)")
    sp.add_argument("--scoring", default="PPR", choices=["PPR", "HALF", "STD"])
    sp.add_argument("--type", default="draft", choices=["draft", "weekly", "ros", "dynasty"])
    sp.add_argument("--force", action="store_true", help="Bypass cache, refetch live")
    sp.set_defaults(func=_cmd_fp_refresh)

    sp = sub.add_parser("fp-ecr", help="Show FantasyPros ECR for a position")
    sp.add_argument("position", help="QB/RB/WR/TE/K/DST/OP/ALL")
    sp.add_argument("--scoring", default="PPR", choices=["PPR", "HALF", "STD"])
    sp.add_argument("--type", default="draft", choices=["draft", "weekly", "ros", "dynasty"])
    sp.add_argument("-n", type=int, default=30)
    sp.set_defaults(func=_cmd_fp_ecr)

    sp = sub.add_parser("fp-news", help="Recent FantasyPros player news")
    sp.add_argument("--player", help="Filter by name substring (case-insensitive)")
    sp.add_argument("-n", type=int, default=20)
    sp.set_defaults(func=_cmd_fp_news)

    sp = sub.add_parser("fp-map", help="Show the persisted fp↔mfl id map")
    sp.add_argument("--show", type=int, default=0, help="Show first N entries")
    sp.set_defaults(func=_cmd_fp_map)

    sp = sub.add_parser("fp-enrich", help="Enrich a list of MFL player ids with FP ECR/tier/projections/news")
    sp.add_argument("mflids", help="Comma-separated MFL player ids")
    sp.add_argument("--scoring", default="PPR", choices=["PPR", "HALF", "STD"])
    sp.add_argument("--type", default="draft", choices=["draft", "weekly", "ros", "dynasty"])
    sp.set_defaults(func=_cmd_fp_enrich)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
