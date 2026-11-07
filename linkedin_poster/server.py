"""linkedin-poster MCP server: post to the connected member's own LinkedIn profile, with a dry-run gate."""
from __future__ import annotations

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import api, core
from .core import PosterError

EXPECTED = (PosterError, OSError, ValueError)
GATE = (" Without confirm=true this only returns a dry-run preview; show it to the account owner and get a clear yes "
        "before calling again with confirm=true.")
RULES = (" Public posts are work and education only: never ask for work ('hire me', 'open to work'), no client names, "
         "no server paths or internal tooling details.")

mcp = MCPServer(
    "linkedin-poster",
    instructions=(
        "Posts to the connected member's own LinkedIn profile via LinkedIn's official API. "
        "`post` needs confirm=true and returns a preview otherwise: show it and only confirm after an explicit yes."
        + RULES
    ),
)


def tool(*extras: str):
    """mcp.tool() that turns expected errors into readable tool errors and appends shared notes."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except EXPECTED as e:
                raise ToolError(f"{type(e).__name__}: {e}") from e
        wrapper.__doc__ = (fn.__doc__ or "").strip() + "".join(extras)
        return mcp.tool()(wrapper)
    return deco


@tool()
def status() -> dict:
    """Is LinkedIn connected? Member name, token expiry date, days left, and the next step if something is due.
    Tokens last 60 days; self-serve apps get no refresh token, so re-run auth_link before they run out."""
    return core.status()


@tool()
def auth_link() -> dict:
    """Start connecting (or re-connecting) LinkedIn. Returns a consent link to open on any device.
    After 'Allow', the browser lands on the redirect page: copy the WHOLE address-bar URL (it has ?code=...&state=...)
    and pass it to auth_finish. The link works once, within 30 minutes."""
    return {"link": core.auth_start(), "next": "open it, click Allow, then send back the whole address-bar URL"}


@tool()
def auth_finish(url: str) -> str:
    """Finish connecting with the full redirect URL from the browser's address bar (contains code and state)."""
    return core.auth_finish(url)


def _preview(p: dict) -> dict:
    out = {k: p[k] for k in ("kind", "chars", "text", "link", "card", "alt_text", "warnings", "blockers")}
    out["image"] = p["image"]
    st = core.status()
    out["posting_as"] = st.get("member_name") if st.get("connected") else "(LinkedIn not connected)"
    out["api_version"] = api.VERSION
    return out


@tool(RULES)
def preview(text: str, image_path: str = "", link: str = "", link_title: str = "", link_description: str = "",
            alt_text: str = "", link_as_card: bool | None = None) -> dict:
    """Show exactly what `post` would publish, with content-rule warnings and blockers. Never posts.
    image_path: local JPG/PNG/GIF. link: added as an article card when there's no image (link_title defaults to the
    first line), or appended to the text when there is one; link_as_card=true makes the image the card's thumbnail."""
    return _preview(api.plan(text, image_path, link, link_title, link_description, alt_text, link_as_card))


@tool(GATE, RULES)
def post(text: str, image_path: str = "", link: str = "", link_title: str = "", link_description: str = "",
         alt_text: str = "", link_as_card: bool | None = None, confirm: bool = False) -> dict:
    """Publish a post (text, optional image file, optional link) to the connected member's LinkedIn profile, PUBLIC.
    Same arguments as preview. Refuses when the preview lists blockers, or the same text was posted in the last 7 days."""
    p = api.plan(text, image_path, link, link_title, link_description, alt_text, link_as_card)
    if not confirm:
        return {"dry_run": True, "would_post": _preview(p),
                "to_publish": "show this to the owner; after a clear yes call post again with confirm=true"}
    return api.publish(p)


def main() -> None:
    mcp.run()
