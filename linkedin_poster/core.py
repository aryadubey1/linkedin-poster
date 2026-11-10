"""Config, OAuth 2.0 (3-legged, paste-back finish) and the stored token for linkedin-poster.

LinkedIn facts this relies on (learn.microsoft.com/linkedin, checked 2026-11-07):
- Authorize: GET https://www.linkedin.com/oauth/v2/authorization (response_type, client_id, redirect_uri, state, scope).
- Token:     POST https://www.linkedin.com/oauth/v2/accessToken, form-encoded, with client_secret and the same redirect_uri.
- Codes live 30 minutes. Access tokens live 60 days (expires_in). Programmatic refresh tokens are only for approved
  Marketing Developer Platform partners, so a self-serve app re-runs the auth flow; while the member is still signed in
  and the old token is still valid, LinkedIn skips the consent screen.
- Redirect URLs must be absolute, are added in the app's Auth tab, and the docs ask for HTTPS.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from . import http

CONFIG_DIR = Path(os.environ.get("LINKEDIN_POSTER_CONFIG", Path.home() / ".config/linkedin-poster"))
CLIENT_FILE = CONFIG_DIR / "client.json"
TOKEN_FILE = CONFIG_DIR / "token.json"
PENDING_DIR = CONFIG_DIR / "pending"
POSTED_LOG = CONFIG_DIR / "posted.jsonl"
BLOCKLIST = CONFIG_DIR / "blocklist.txt"

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
SCOPES = ["openid", "profile", "w_member_social"]
PENDING_TTL = 30 * 60  # LinkedIn's authorization code lifespan
DAY = 86400


class PosterError(Exception):
    """An error message meant for the caller (shown as-is)."""


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def client() -> dict:
    if not CLIENT_FILE.exists():
        raise PosterError(f"Missing {CLIENT_FILE}. Copy client.example.json there, fill in the app's Client ID, "
                          f"Client Secret and redirect URL, then chmod 600.")
    c = json.loads(CLIENT_FILE.read_text())
    missing = [k for k in ("client_id", "client_secret", "redirect_uri") if not c.get(k) or "paste" in str(c.get(k))]
    if missing:
        raise PosterError(f"{CLIENT_FILE} is missing: {', '.join(missing)}.")
    if not c["redirect_uri"].startswith("https://"):
        raise PosterError("redirect_uri must be an absolute https:// URL (LinkedIn's docs ask for HTTPS).")
    return c


# ---------- OAuth: start + finish (paste-back) ----------

def auth_start() -> str:
    c = client()
    state = secrets.token_urlsafe(24)
    _write_private(PENDING_DIR / f"{state}.json", json.dumps({"created": time.time()}))
    for old in PENDING_DIR.glob("*.json"):  # tidy expired attempts
        try:
            if time.time() - json.loads(old.read_text())["created"] > PENDING_TTL:
                old.unlink()
        except Exception:
            old.unlink(missing_ok=True)
    return AUTH_URL + "?" + urlencode({
        "response_type": "code", "client_id": c["client_id"], "redirect_uri": c["redirect_uri"],
        "state": state, "scope": " ".join(SCOPES),
    })


def parse_callback(pasted: str) -> tuple[str, str]:
    """The pasted address-bar URL → (code, state). Raises with a plain reason on errors or a cancelled consent."""
    pasted = pasted.strip().strip("'\"<>")
    if "?" not in pasted:
        raise PosterError("Paste the WHOLE address-bar URL (it has ?code=...&state=... in it).")
    q = parse_qs(urlparse("https://x/?" + pasted.split("?", 1)[1]).query)
    if "error" in q:
        desc = q.get("error_description", [""])[0]
        raise PosterError(f"LinkedIn returned an error: {q['error'][0]} {desc}".strip())
    code, state = q.get("code", [None])[0], q.get("state", [None])[0]
    if not code or not state:
        raise PosterError("That URL has no code/state. Paste the WHOLE address-bar URL after clicking Allow.")
    return code, state


def _token_post(form: dict) -> dict:
    r = http.request("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                     urlencode(form).encode())
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status != 200 or "access_token" not in data:
        reason = data.get("error_description") or data.get("error") or r.text[:200]
        raise PosterError(f"LinkedIn refused the token request ({r.status}): {reason}")
    return data


def token_record(data: dict, member: dict, now: float | None = None) -> dict:
    """Token endpoint JSON + userinfo → what we store. Refresh fields only exist for partner apps."""
    now = time.time() if now is None else now
    rec = {
        "access_token": data["access_token"],
        "expires_at": int(now + int(data.get("expires_in", 60 * DAY))),
        "scope": data.get("scope", ""),
        "member": {"sub": member.get("sub", ""), "name": member.get("name", "")},
        "connected_at": int(now),
    }
    if data.get("refresh_token"):
        rec["refresh_token"] = data["refresh_token"]
        rec["refresh_expires_at"] = int(now + int(data.get("refresh_token_expires_in", 365 * DAY)))
    return rec


def userinfo(access_token: str) -> dict:
    r = http.request("GET", USERINFO_URL, {"Authorization": f"Bearer {access_token}"})
    if r.status != 200:
        raise PosterError(f"Could not read the LinkedIn profile ({r.status}): {r.text[:200]}")
    info = r.json()
    if not info.get("sub"):
        raise PosterError("LinkedIn's userinfo had no member id (sub). Is the OpenID Connect product added?")
    return info


def auth_finish(pasted: str) -> str:
    code, state = parse_callback(pasted)
    pfile = PENDING_DIR / f"{Path(state).name}.json"
    if not pfile.exists():
        raise PosterError("That link is unknown or already used. Start again with auth_link.")
    created = json.loads(pfile.read_text())["created"]
    pfile.unlink(missing_ok=True)  # one try per link
    if time.time() - created > PENDING_TTL:
        raise PosterError("That link expired (30 min). Start again with auth_link.")
    c = client()
    data = _token_post({"grant_type": "authorization_code", "code": code, "client_id": c["client_id"],
                        "client_secret": c["client_secret"], "redirect_uri": c["redirect_uri"]})
    granted = set(data.get("scope", "").replace(",", " ").split())
    if granted and "w_member_social" not in granted:
        raise PosterError("Connected, but without permission to post (w_member_social). "
                          "Add the 'Share on LinkedIn' product to the app and run auth again. Not saved.")
    member = userinfo(data["access_token"])
    rec = token_record(data, member)
    _write_private(TOKEN_FILE, json.dumps(rec, indent=1))
    days = (rec["expires_at"] - time.time()) / DAY
    return f"Connected as {rec['member']['name'] or 'your LinkedIn account'}. Token valid for {days:.0f} days " \
           f"(until {time.strftime('%Y-%m-%d', time.gmtime(rec['expires_at']))})."


# ---------- the stored token ----------

def _load() -> dict | None:
    return json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else None


def _refresh(rec: dict) -> dict:
    c = client()
    data = _token_post({"grant_type": "refresh_token", "refresh_token": rec["refresh_token"],
                        "client_id": c["client_id"], "client_secret": c["client_secret"]})
    new = token_record(data, rec["member"])
    new.setdefault("refresh_token", rec["refresh_token"])
    new.setdefault("refresh_expires_at", rec.get("refresh_expires_at"))
    _write_private(TOKEN_FILE, json.dumps(new, indent=1))
    return new


def token() -> dict:
    """A usable token record, refreshed when we can. Raises with the next step when we can't."""
    rec = _load()
    if not rec:
        raise PosterError("LinkedIn is not connected yet. Use auth_link (or `linkedin-poster auth`).")
    now = time.time()
    if rec["expires_at"] - now < DAY:
        if rec.get("refresh_token") and rec.get("refresh_expires_at", 0) > now:
            return _refresh(rec)
        if rec["expires_at"] <= now:
            raise PosterError("The LinkedIn token has expired (they last 60 days). Run auth_link again.")
    return rec


def author_urn(rec: dict) -> str:
    # The OpenID Connect `sub` is the member id used in Person URNs (Share on LinkedIn docs point to OIDC for it).
    return f"urn:li:person:{rec['member']['sub']}"


def status(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    out: dict = {"client_configured": False, "connected": False}
    try:
        client()
        out["client_configured"] = True
    except (PosterError, ValueError) as e:
        out["client_problem"] = str(e)
    rec = _load()
    if not rec:
        out["next_step"] = "auth_link" if out["client_configured"] else f"fill in {CLIENT_FILE} (see the playbook)"
        return out
    days = (rec["expires_at"] - now) / DAY
    out.update({
        "connected": days > 0,
        "member_name": rec["member"].get("name", ""),
        "token_expires": time.strftime("%Y-%m-%d", time.gmtime(rec["expires_at"])),
        "days_left": max(0, int(days)),
        "can_post": "w_member_social" in rec.get("scope", "").replace(",", " ").split() or not rec.get("scope"),
        "auto_refresh": bool(rec.get("refresh_token")),
    })
    if days <= 0:
        out["next_step"] = "token expired: run auth_link"
    elif days < 10 and not rec.get("refresh_token"):
        out["next_step"] = "re-auth soon: run auth_link (no consent screen while still signed in)"
    return out


def expiry_line(warn_days: int = 10, now: float | None = None) -> str:
    """One line for a daily brief, or '' when all is fine (or the tool isn't set up at all)."""
    s = status(now)
    if not s["client_configured"]:
        return ""
    if "days_left" not in s:
        return "LinkedIn poster: not connected yet (run `linkedin-poster auth`)."
    if not s["connected"]:
        return f"LinkedIn poster: token expired on {s['token_expires']}. Re-auth: `linkedin-poster auth`."
    if s["days_left"] <= warn_days and not s["auto_refresh"]:
        return f"LinkedIn poster: token expires in {s['days_left']} days ({s['token_expires']}). " \
               f"Re-auth: `linkedin-poster auth`."
    return ""
