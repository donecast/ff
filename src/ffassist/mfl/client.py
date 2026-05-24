from __future__ import annotations

import time
from typing import Any

import httpx

from ffassist.config import settings
from ffassist.mfl import cache

DEFAULT_MIN_INTERVAL = 2.5
CACHE_TTLS = {
    "league": 3600.0,
    "rules": 3600.0,
    "players": 86400.0,
    "rosters": 600.0,
    "freeAgents": 600.0,
    "draftResults": 60.0,
    "adp": 6 * 3600.0,
    "playerRanks": 6 * 3600.0,
    "myleagues": 600.0,
}


class MFLClient:
    def __init__(
        self,
        year: int | None = None,
        apikey: str | None = None,
        user_agent: str | None = None,
        min_interval: float = DEFAULT_MIN_INTERVAL,
    ):
        self.year = year or settings.mfl_year
        self.apikey = apikey if apikey is not None else settings.mfl_apikey
        self._client = httpx.Client(
            headers={"User-Agent": user_agent or settings.mfl_user_agent},
            follow_redirects=True,
            timeout=15.0,
        )
        if min_interval < 2.0:
            min_interval = 2.0
        self._min_interval = min_interval
        self._last_call = 0.0
        self._host_for_league: dict[str, str] = {}
        self._logged_in = False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MFLClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _base(self, league_id: str | None) -> str:
        if league_id and league_id in self._host_for_league:
            return self._host_for_league[league_id]
        # Try to read host from persisted state (avoid api.* rate limit)
        if league_id:
            try:
                from ffassist import state as state_mod

                host = state_mod.load().get("league_hosts", {}).get(league_id)
                if host:
                    self._host_for_league[league_id] = host
                    return host
            except Exception:
                pass
        return f"https://api.myfantasyleague.com/{self.year}"

    def _do_request(
        self, method: str, url: str, params: dict, _retry: bool = True
    ) -> httpx.Response:
        self._throttle()
        if method == "GET":
            r = self._client.get(url, params=params)
        else:
            r = self._client.post(url, params=params)
        if r.status_code == 429 and _retry:
            # Short backoff (was 60s, but that hangs interactive paths).
            # Background jobs that NEED to wait should use their own retry loop.
            print("[mfl] 429 Too Many Requests — short 3s backoff + one retry.")
            time.sleep(3)
            return self._do_request(method, url, params, _retry=False)
        if r.status_code == 429:
            # Second 429 — bubble up so callers can show a friendly error
            raise httpx.HTTPStatusError(
                "MFL rate-limited (429) — try again in a minute",
                request=r.request,
                response=r,
            )
        return r

    def _export(
        self,
        type_: str,
        league_id: str | None = None,
        *,
        ttl: float | None = None,
        force: bool = False,
        **params: Any,
    ) -> dict:
        cache_params = {**params, "L": league_id}
        effective_ttl = ttl if ttl is not None else CACHE_TTLS.get(type_, 0.0)
        if not force:
            hit = cache.get(self.year, type_, cache_params, effective_ttl)
            if hit is not None:
                return hit
        q = {"TYPE": type_, "JSON": 1, **params}
        if league_id:
            q["L"] = league_id
        if self.apikey:
            q["APIKEY"] = self.apikey
        url = f"{self._base(league_id)}/export"
        r = self._do_request("GET", url, q)
        if league_id:
            actual_host = f"{r.url.scheme}://{r.url.host}/{self.year}"
            if actual_host != self._base(league_id):
                self._host_for_league[league_id] = actual_host
                # Persist so future MFLClient instances skip api.*
                try:
                    from ffassist import state as state_mod

                    state = state_mod.load()
                    state.setdefault("league_hosts", {})[league_id] = actual_host
                    state_mod.save(state)
                except Exception:
                    pass
        r.raise_for_status()
        data = r.json()
        if effective_ttl > 0:
            cache.put(self.year, type_, cache_params, data)
        return data

    def login(self, username: str | None = None, password: str | None = None) -> bool:
        username = username or settings.mfl_username
        password = password or settings.mfl_password
        if not username or not password:
            raise RuntimeError("MFL username/password not set")
        self._throttle()
        url = f"https://api.myfantasyleague.com/{self.year}/login"
        r = self._client.post(
            url,
            data={"USERNAME": username, "PASSWORD": password, "XML": 1},
        )
        r.raise_for_status()
        ok = "MFL_USER_ID" in self._client.cookies
        if ok:
            self._logged_in = True
        return ok

    def ensure_auth(self) -> str:
        if self.apikey:
            return "apikey"
        if self._logged_in:
            return "session"
        if settings.mfl_username and settings.mfl_password:
            if self.login():
                return "session"
        raise RuntimeError("No MFL auth available: set MFL_APIKEY or MFL_USERNAME+MFL_PASSWORD")

    def league(self, league_id: str, *, force: bool = False) -> dict:
        return self._export("league", league_id, force=force)["league"]

    def rules(self, league_id: str, *, force: bool = False) -> dict:
        return self._export("rules", league_id, force=force)["rules"]

    def myleagues(self, year: int | None = None, *, force: bool = False) -> dict:
        """List all leagues for the authenticated user. Requires login or APIKEY."""
        self.ensure_auth()
        params: dict[str, Any] = {}
        if year is not None:
            params["YEAR"] = year
        return self._export("myleagues", force=force, **params)["leagues"]

    def players(self, since: int | None = None, details: bool = True, *, force: bool = False) -> dict:
        params: dict[str, Any] = {"DETAILS": 1 if details else 0}
        if since is not None:
            params["SINCE"] = since
        return self._export("players", force=force, **params)["players"]

    def rosters(self, league_id: str, *, force: bool = False) -> dict:
        return self._export("rosters", league_id, force=force)["rosters"]

    def free_agents(self, league_id: str, position: str | None = None, *, force: bool = False) -> dict:
        params: dict[str, Any] = {}
        if position:
            params["POSITION"] = position
        return self._export("freeAgents", league_id, force=force, **params)["freeAgents"]

    def draft_results(self, league_id: str, *, force: bool = False) -> dict:
        return self._export("draftResults", league_id, force=force)["draftResults"]

    def adp(self, period_days: int | None = None, *, force: bool = False) -> dict:
        params: dict[str, Any] = {}
        if period_days is not None:
            params["PERIOD"] = period_days
        return self._export("adp", force=force, **params)["adp"]

    def player_ranks(self, position: str | None = None, *, force: bool = False) -> dict:
        params: dict[str, Any] = {}
        if position:
            params["POSITION"] = position
        return self._export("playerRanks", force=force, **params)["playerRanks"]

    def submit_live_draft_pick(
        self, league_id: str, player_id: str, round_: int, pick: int
    ) -> dict:
        """Submit a draft pick. `pick` is the WITHIN-ROUND slot (1..team_count), not overall.

        MFL's live draft endpoint is /{year}/live_draft (NOT /import), with param
        PLAYER_PICK (NOT PLAYER). Authenticated session covers the submitter's franchise.
        """
        self.ensure_auth()
        url = f"{self._base(league_id)}/live_draft"
        params: dict[str, Any] = {
            "L": league_id,
            "CMD": "DRAFT",
            "PLAYER_PICK": player_id,
            "ROUND": round_,
            "PICK": pick,
            "JSON": 1,
        }
        if self.apikey:
            params["APIKEY"] = self.apikey
        r = self._do_request("POST", url, params)
        r.raise_for_status()
        body = (r.text or "").strip()
        if not body:
            # MFL returns empty body on some success and some error paths.
            return {"raw_status": r.status_code, "raw_body": "", "note": "empty response — verify via draftResults"}
        try:
            return r.json()
        except Exception:
            return {"raw_status": r.status_code, "raw_body": body[:500], "note": "non-JSON response"}
