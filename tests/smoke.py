"""Smoke test: start the MCP server over stdio, list tools, call the ones that need no LinkedIn token.
Uses a throwaway config dir with a fake client.json. Never posts.
Run: .venv/bin/python tests/smoke.py"""
import asyncio
import json
import os
import tempfile

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def main():
    cfg = tempfile.mkdtemp(prefix="li-poster-smoke-")
    with open(os.path.join(cfg, "client.json"), "w") as f:
        json.dump({"client_id": "cid", "client_secret": "sec", "redirect_uri": "https://example.com/cb"}, f)
    env = dict(os.environ, LINKEDIN_POSTER_CONFIG=cfg)
    params = StdioServerParameters(command=os.path.join(ROOT, "bin/linkedin-poster"), args=["serve"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            names = sorted(t.name for t in tools)
            print(len(tools), "tools:", ", ".join(names))
            assert names == ["auth_finish", "auth_link", "post", "preview", "status"], names
            post = next(t for t in tools if t.name == "post")
            assert "confirm=true" in post.description, post.description
            checks = [
                ("status", {}, False),
                ("auth_link", {}, False),
                ("preview", {"text": "How we cut a page's weight in half #webperf", "link": "https://example.com/b"}, False),
                ("post", {"text": "Dry run only"}, False),                    # no confirm → preview, nothing sent
                ("post", {"text": "hire me", "confirm": True}, True),           # blocked by the rules
                ("post", {"text": "Not connected", "confirm": True}, True),     # no token → readable error
                ("auth_finish", {"url": "https://example.com/cb?code=x&state=nope"}, True),
            ]
            for name, args, want_error in checks:
                res = await s.call_tool(name, args)
                text = res.content[0].text
                print(f"- {name}: error={res.is_error} {text[:150]!r}")
                assert res.is_error == want_error, (name, text)
                if name == "post" and not want_error:
                    assert "dry_run" in text, text
    print("smoke OK")


asyncio.run(main())
