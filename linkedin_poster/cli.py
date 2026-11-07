"""linkedin-poster CLI. Run with no arguments for help."""
from __future__ import annotations

import argparse
import json
import sys

from . import api, core

EPILOG = """examples:
  linkedin-poster status
  linkedin-poster auth                      # prints a link, then asks for the redirect URL
  linkedin-poster preview --text-file post.txt --image cover.png --link https://example.com/blog/x
  linkedin-poster post    --text-file post.txt --image cover.png --yes
  linkedin-poster expiry 10                 # one line if the token needs attention, else nothing
"""


def _post_args(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--text-file", help="read the post text from a file ('-' = stdin)")
    p.add_argument("--image", default="")
    p.add_argument("--link", default="")
    p.add_argument("--link-title", default="")
    p.add_argument("--link-description", default="")
    p.add_argument("--alt", default="", help="image alt text")
    p.add_argument("--card", action="store_true", help="show the link as an article card even with an image")


def _plan(a) -> dict:
    text = a.text if a.text is not None else (sys.stdin.read() if a.text_file == "-" else open(a.text_file).read())
    return api.plan(text, a.image, a.link, a.link_title, a.link_description, a.alt, True if a.card else None)


def _show(p: dict) -> None:
    st = core.status()
    print(f"as:    {st.get('member_name') if st.get('connected') else '(not connected)'}")
    print(f"kind:  {p['kind']}  ·  {p['chars']} chars  ·  LinkedIn-Version {api.VERSION}")
    if p["image"]:
        i = p["image"]
        print(f"image: {i['path']} ({i['format']} {i['width']}x{i['height']}, {i['bytes'] // 1024} KB)")
    if p["card"]:
        print(f"card:  {p['card']['title']} → {p['card']['source']}")
    print("-" * 60 + f"\n{p['text']}\n" + "-" * 60)
    for w in p["warnings"]:
        print(f"warning: {w}")
    for b in p["blockers"]:
        print(f"BLOCKED: {b}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="linkedin-poster", epilog=EPILOG, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server (stdio)")
    sub.add_parser("status", help="connected? member name, token expiry")
    sub.add_parser("auth", help="print a consent link, then finish with the pasted redirect URL")
    f = sub.add_parser("finish", help="finish auth with the redirect URL")
    f.add_argument("url")
    _post_args(sub.add_parser("preview", help="show what would be posted (never posts)"))
    pp = sub.add_parser("post", help="publish (needs --yes)")
    _post_args(pp)
    pp.add_argument("--yes", action="store_true", help="really post; without it this is a preview")
    e = sub.add_parser("expiry", help="one line when the token needs attention (for a daily brief)")
    e.add_argument("warn_days", nargs="?", type=int, default=10)
    a = ap.parse_args(argv)

    try:
        if a.cmd == "serve":
            from .server import main as serve
            serve()
        elif a.cmd == "status":
            print(json.dumps(core.status(), indent=1))
        elif a.cmd == "auth":
            print(f"\n1. Open this link, sign in to LinkedIn and click Allow:\n\n{core.auth_start()}\n")
            print("2. Copy the WHOLE address-bar URL of the page you land on (it has ?code=...&state=...).")
            if sys.stdin.isatty():
                print(core.auth_finish(input("\nPaste it here: ")))
            else:
                print("\nThen run: linkedin-poster finish '<that url>'")
        elif a.cmd == "finish":
            print(core.auth_finish(a.url))
        elif a.cmd in ("preview", "post"):
            p = _plan(a)
            _show(p)
            if a.cmd == "post":
                if not a.yes:
                    print("\nDry run. Add --yes to publish.")
                else:
                    print(json.dumps(api.publish(p), indent=1))
        elif a.cmd == "expiry":
            line = core.expiry_line(a.warn_days)
            if line:
                print(line)
        else:
            ap.print_help()
    except core.PosterError as err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)
