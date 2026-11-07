"""Build, check and publish a member post through LinkedIn's versioned REST API.

LinkedIn facts this relies on (learn.microsoft.com/linkedin, checked 2026-11-07):
- POST https://api.linkedin.com/rest/posts with headers `LinkedIn-Version: YYYYMM` and `X-Restli-Protocol-Version: 2.0.0`.
  201 Created; the new post's URN is in the `x-restli-id` response header. w_member_social is enough for a member post.
- Images: POST /rest/images?action=initializeUpload {"initializeUploadRequest": {"owner": <person urn>}} → value.uploadUrl
  + value.image (urn:li:image:...). Then PUT the bytes to uploadUrl WITH the OAuth token. JPG, GIF, PNG; fewer than
  36,152,320 pixels. No synchronous upload, and a w_member_social-only token can't GET /rest/images to poll status.
- Article posts: the API does not scrape the URL; the caller sets content.article {source, title, description, thumbnail}.
- commentary is in "little" text format: | { } @ [ ] ( ) < > # \\ * _ ~ must be backslash-escaped to stay plain text.
- Versions are monthly and supported for at least a year; an unversioned or sunset version is an error.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import time
from pathlib import Path
from urllib.parse import urlparse

from . import core, http
from .core import PosterError

API = "https://api.linkedin.com/rest"
# Latest version on 2026-11-07 was 202610 (supported until at least 2027-10). Override without a code change:
VERSION = os.environ.get("LINKEDIN_VERSION", "202610")
MAX_CHARS = 3000          # LinkedIn's post length limit; the API answers FIELD_LENGTH_TOO_LONG past it
MAX_PIXELS = 36_152_320   # Images API: "less than 36,152,320 pixels"
MAX_BYTES = 20 * 1024 * 1024  # our own sanity cap for a feed image
DUPLICATE_DAYS = 7

RESERVED = set("|{}@[]()<>#\\*_~")
BLOCKED_DIRS = [Path.home() / d for d in (".ssh", ".config", ".claude", ".secrets")] + [Path("/etc"), Path("/var/www")]


# ---------- text ----------

def escape_little(text: str) -> str:
    """Plain text → little format. Every reserved char is escaped, except '#' that starts a hashtag (#word)."""
    out = []
    for i, ch in enumerate(text):
        if ch == "#" and i + 1 < len(text) and (text[i + 1].isalnum() or text[i + 1] == "_") \
                and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            out.append(ch)  # a real hashtag
        elif ch in RESERVED:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


# Content rules: public posts are work + education only and never ask for work. Private names (clients, employer,
# people) live in the local blocklist.txt, not here.
ASKS_FOR_WORK = ["hire me", "open to work", "#opentowork", "looking for work", "looking for a job", "looking for clients",
                 "available for hire", "available for freelance", "seeking opportunities", "dm me for", "book a call"]
TEAM_SIZE = ["freelancer", "solo developer", "one-person", "one person team", "by myself"]
SECRET_PATTERNS = [
    (re.compile(r"(/var/www|/home/\w+|~/\.?\w+/)"), "a server path"),
    (re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b"), "an IP address"),
    (re.compile(r"\b(sk-|ghp_|AQ[A-Za-z0-9_-]{40,})"), "something that looks like a token"),
]


def check_content(text: str) -> tuple[list[str], list[str]]:
    """→ (blockers, warnings). Blockers stop a real post; warnings are shown in the preview."""
    low = text.lower()
    blockers, warnings = [], []
    for p in ASKS_FOR_WORK:
        if p in low:
            blockers.append(f"asks for work ('{p}'): public posts are work + education only")
    if core.BLOCKLIST.exists():  # private names (clients etc.), one per line, kept out of git
        for name in (ln.strip() for ln in core.BLOCKLIST.read_text().splitlines()):
            if name and not name.startswith("#") and name.lower() in low:
                blockers.append(f"contains a blocked name ('{name}')")
    for p in TEAM_SIZE:
        if p in low:
            warnings.append(f"'{p}': keep team size ambiguous")
    for rx, what in SECRET_PATTERNS:
        if rx.search(text):
            blockers.append(f"contains {what}")
    if re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
        warnings.append("contains an email address")
    if len(re.findall(r"(?<!\w)#\w+", text)) > 3:
        warnings.append("more than 3 hashtags")
    return blockers, warnings


# ---------- image ----------

def image_info(path: str) -> dict:
    """Check a local image is safe to read and one LinkedIn accepts. → {path, format, width, height, bytes}."""
    p = Path(os.path.expanduser(path)).resolve()
    if any(p == d or d in p.parents for d in BLOCKED_DIRS) or p.name.startswith(".env") or p.suffix in (".pem", ".key"):
        raise PosterError(f"Refusing to upload from {p.parent}: that folder holds secrets.")
    if not p.is_file():
        raise PosterError(f"No image at {p}.")
    size = p.stat().st_size
    if size > MAX_BYTES:
        raise PosterError(f"Image is {size / 1e6:.1f} MB; keep it under {MAX_BYTES // 1_000_000} MB.")
    with open(p, "rb") as f:
        head = f.read(64 * 1024)
    fmt, w, h = _dimensions(head, p)
    if w * h >= MAX_PIXELS:
        raise PosterError(f"Image is {w}x{h} = {w * h:,} pixels; LinkedIn takes fewer than {MAX_PIXELS:,}.")
    return {"path": str(p), "format": fmt, "width": w, "height": h, "bytes": size}


def _dimensions(head: bytes, p: Path) -> tuple[str, int, int]:
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", head[16:24])
        return "png", w, h
    if head[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", head[6:10])
        return "gif", w, h
    if head[:2] == b"\xff\xd8":
        data = head if len(head) < 64 * 1024 else p.read_bytes()
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "jpeg", w, h
            i += 2 + seg
        raise PosterError("Could not read the JPEG's size; re-save it and retry.")
    raise PosterError("Only JPG, PNG or GIF images (LinkedIn's Images API formats). Convert WebP covers first, "
                      "e.g. `ffmpeg -i cover.webp cover.png`.")


# ---------- the post ----------

def plan(text: str, image_path: str = "", link: str = "", link_title: str = "", link_description: str = "",
         alt_text: str = "", link_as_card: bool | None = None) -> dict:
    """Everything a post would do, without touching the network. Shared by preview and post."""
    text = (text or "").strip()
    if not text:
        raise PosterError("The post text is empty.")
    link = (link or "").strip()
    if link and urlparse(link).scheme not in ("http", "https"):
        raise PosterError("link must start with http:// or https://")
    image = image_info(image_path) if image_path else None
    card = bool(link) and (link_as_card if link_as_card is not None else image is None)
    body = text
    if link and not card and link not in text:
        body = f"{text}\n\n{link}"  # image post: the link goes in the text (a post has one content block)
    if card and not (link_title or "").strip():
        link_title = text.splitlines()[0][:200]
    if len(body) > MAX_CHARS:
        raise PosterError(f"Post is {len(body)} characters; LinkedIn's limit is {MAX_CHARS}.")
    blockers, warnings = check_content(body + "\n" + (link_title or "") + "\n" + (link_description or ""))
    if _recent_duplicate(body):
        blockers.append(f"the same text was already posted in the last {DUPLICATE_DAYS} days")
    kind = "article card" + (" with image thumbnail" if image else "") if card else ("image" if image else "text")
    if link and not card:
        kind += " + link in text"
    return {"kind": kind, "text": body, "chars": len(body), "image": image, "link": link or None,
            "card": {"source": link, "title": link_title.strip(), "description": (link_description or "").strip()}
            if card else None,
            "alt_text": (alt_text or "").strip(), "blockers": blockers, "warnings": warnings}


def payload(p: dict, author: str, image_urn: str | None = None) -> dict:
    """The JSON body for POST /rest/posts (shape from the Posts API docs)."""
    body = {
        "author": author,
        "commentary": escape_little(p["text"]),
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if p["card"]:
        art = {"source": p["card"]["source"], "title": p["card"]["title"]}
        if p["card"]["description"]:
            art["description"] = p["card"]["description"]
        if image_urn:
            art["thumbnail"] = image_urn
        body["content"] = {"article": art}
    elif image_urn:
        media = {"id": image_urn}
        if p["alt_text"]:
            media["altText"] = p["alt_text"][:4086]
        body["content"] = {"media": media}
    return body


def _headers(tok: dict, json_body: bool = True) -> dict:
    h = {"Authorization": f"Bearer {tok['access_token']}", "LinkedIn-Version": VERSION,
         "X-Restli-Protocol-Version": "2.0.0"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _fail(what: str, r: http.Response) -> PosterError:
    try:
        msg = r.json().get("message") or r.text[:300]
    except ValueError:
        msg = r.text[:300]
    hint = {401: " Token rejected: run auth_link again.",
            403: " Missing permission: is 'Share on LinkedIn' added to the app?",
            426: f" LinkedIn-Version {VERSION} may be sunset: set LINKEDIN_VERSION to a newer YYYYMM.",
            429: " Rate limited: try again later."}.get(r.status, "")
    return PosterError(f"{what} failed ({r.status}): {msg}.{hint}")


def upload_image(tok: dict, author: str, path: str) -> str:
    r = http.request("POST", f"{API}/images?action=initializeUpload", _headers(tok),
                     json.dumps({"initializeUploadRequest": {"owner": author}}).encode())
    if r.status not in (200, 201):
        raise _fail("Image upload setup", r)
    v = r.json().get("value", {})
    if not v.get("uploadUrl") or not v.get("image"):
        raise PosterError(f"Image upload setup returned no uploadUrl/image: {r.text[:200]}")
    put = http.request("PUT", v["uploadUrl"], {"Authorization": f"Bearer {tok['access_token']}",
                                                "Content-Type": "application/octet-stream"},
                       Path(path).read_bytes())
    if put.status not in (200, 201):
        raise _fail("Image upload", put)
    return v["image"]


def publish(p: dict) -> dict:
    """Really post. Callers must have shown the preview and got a yes."""
    if p["blockers"]:
        raise PosterError("Not posted: " + "; ".join(p["blockers"]))
    tok = core.token()
    author = core.author_urn(tok)
    image_urn = upload_image(tok, author, p["image"]["path"]) if p["image"] else None
    if image_urn:
        time.sleep(2)  # no way to poll image status with w_member_social; give processing a moment
    r = http.request("POST", f"{API}/posts", _headers(tok), json.dumps(payload(p, author, image_urn)).encode())
    if r.status != 201:
        raise _fail("Creating the post", r)
    urn = r.headers.get("x-restli-id", "")
    _log_posted(p["text"], urn)
    return {"posted": True, "post_urn": urn, "url": f"https://www.linkedin.com/feed/update/{urn}/" if urn else None,
            "image": image_urn}


# ---------- posted log (duplicate guard) ----------

def _digest(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def _recent_duplicate(text: str) -> bool:
    if not core.POSTED_LOG.exists():
        return False
    cutoff, d = time.time() - DUPLICATE_DAYS * core.DAY, _digest(text)
    for line in core.POSTED_LOG.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("sha256") == d and e.get("at", 0) > cutoff:
            return True
    return False


def _log_posted(text: str, urn: str) -> None:
    core.POSTED_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": int(time.time()), "urn": urn, "sha256": _digest(text), "start": text[:60]})
    fd = os.open(core.POSTED_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line + "\n")
