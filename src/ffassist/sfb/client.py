from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from selectolax.parser import HTMLParser

from ffassist.config import settings

BASE = "https://scottfishbowl.com"
LOGIN_URL = f"{BASE}/SFB-login.php"
ELIM_URL = f"{BASE}/eliminator-signup.php"


@dataclass
class SignupResult:
    ok: bool
    message: str
    final_url: str | None = None


class SfbClient:
    def __init__(self, email: str | None = None, password: str | None = None):
        self.email = email or settings.sfb_email
        self.password = password or settings.sfb_password
        self._client = httpx.Client(
            headers={"User-Agent": settings.mfl_user_agent},
            follow_redirects=True,
            timeout=20.0,
        )
        self._logged_in = False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SfbClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _csrf(self, html: str) -> str | None:
        doc = HTMLParser(html)
        node = doc.css_first('input[name="csrf_token"]')
        if node is not None:
            return node.attributes.get("value")
        m = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)', html)
        return m.group(1) if m else None

    def login(self) -> bool:
        if not self.email or not self.password:
            raise RuntimeError("SFB_EMAIL and SFB_PASSWORD required")
        # GET to obtain csrf token + initial session cookie
        r = self._client.get(LOGIN_URL)
        r.raise_for_status()
        token = self._csrf(r.text)
        if not token:
            raise RuntimeError("Could not find csrf_token on SFB login page")
        post = self._client.post(
            LOGIN_URL,
            data={
                "csrf_token": token,
                "next": "/useraccount.php",
                "email": self.email,
                "password": self.password,
            },
        )
        post.raise_for_status()
        # Heuristic: login success redirects to /useraccount.php and shows logout link
        body = post.text.lower()
        self._logged_in = "logout" in body and "invalid" not in body and "incorrect" not in body
        return self._logged_in

    def ensure_login(self) -> None:
        if not self._logged_in:
            ok = self.login()
            if not ok:
                raise RuntimeError("SFB login failed (check credentials)")

    def signup(self, eliminator_id: int, *, promo_code: str = "") -> SignupResult:
        """Sign the logged-in user up for the given eliminator id.

        The form fields are scraped from the live page when logged in. We submit all
        hidden inputs verbatim plus any non-disabled visible inputs we recognize.
        """
        self.ensure_login()
        r = self._client.get(ELIM_URL, params={"id": eliminator_id})
        r.raise_for_status()
        doc = HTMLParser(r.text)

        # Quick sanity: ensure the page resolved to a real eliminator
        name_node = doc.css_first(".league-name")
        league_name = name_node.text(strip=True) if name_node else ""
        if not league_name or league_name == "No Eliminator Selected":
            return SignupResult(ok=False, message=f"No eliminator at id={eliminator_id}")

        # Find a signup form. SFB pages usually have one POST form per page.
        form = doc.css_first("form[method='post' i]") or doc.css_first("form")
        if form is None:
            return SignupResult(ok=False, message="No signup form found on page")

        action = form.attributes.get("action") or ""
        post_url = httpx.URL(str(r.url)).join(action or "")
        data: dict[str, str] = {}
        for inp in form.css("input"):
            name = inp.attributes.get("name")
            if not name:
                continue
            itype = (inp.attributes.get("type") or "text").lower()
            value = inp.attributes.get("value") or ""
            if itype in {"submit", "button", "image"}:
                # Include the submit value so the server knows we hit the button
                if "value" in inp.attributes:
                    data[name] = value
                continue
            if itype == "checkbox":
                if "checked" in inp.attributes:
                    data[name] = value or "on"
                continue
            if itype == "radio":
                if "checked" in inp.attributes:
                    data[name] = value
                continue
            data[name] = value
        for sel in form.css("select"):
            name = sel.attributes.get("name")
            if not name:
                continue
            chosen = sel.css_first("option[selected]") or sel.css_first("option")
            if chosen is not None:
                data[name] = chosen.attributes.get("value") or ""

        # Force the eliminator id; ensure promo code field reflects argument
        for k in ("id", "eliminator_id"):
            if k in data:
                data[k] = str(eliminator_id)
        for k in ("promo", "promo_code", "promocode"):
            if k in data:
                data[k] = promo_code

        sub = self._client.post(str(post_url), data=data)
        sub.raise_for_status()
        body = sub.text.lower()
        if "already signed up" in body or "you are signed up" in body or "signed up successfully" in body:
            return SignupResult(ok=True, message="Signed up.", final_url=str(sub.url))
        if "invalid" in body or "error" in body:
            snippet = _extract_alert(sub.text) or "Form returned an error."
            return SignupResult(ok=False, message=snippet, final_url=str(sub.url))
        # Best-effort success: the page rendered without alert markers
        snippet = _extract_alert(sub.text) or "Submitted (no confirmation text recognized)."
        return SignupResult(ok=True, message=snippet, final_url=str(sub.url))


def _extract_alert(html: str) -> str | None:
    doc = HTMLParser(html)
    for sel in (".alert", ".message", ".error", ".success", ".notice"):
        node = doc.css_first(sel)
        if node is not None:
            t = node.text(strip=True)
            if t:
                return t[:200]
    return None
