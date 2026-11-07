# linkedin-poster

Post to your own LinkedIn profile from Claude (MCP) or the command line, through LinkedIn's official API.
Text, an optional image and an optional link, always behind a dry-run preview.

```
Claude Code ──stdio──► bin/linkedin-poster serve ──► api.linkedin.com/rest  (Posts + Images, LinkedIn-Version 202610)
      CLI ──────────►        │
                             └── ~/.config/linkedin-poster/   chmod 700, never in git
                                   client.json    client_id, client_secret, redirect_uri
                                   token.json     access token (60 days) + member name/id
                                   pending/       auth attempts in flight (30 min)
                                   posted.jsonl   what was posted (duplicate guard)
                                   blocklist.txt  names that must never appear in a post
```

## Tools (MCP and CLI)
| Tool | What it does |
|---|---|
| `status` | connected?, member name, token expiry date and days left, next step |
| `auth_link` | consent link (scopes `openid profile w_member_social`) |
| `auth_finish` | finish with the redirect URL pasted from the browser's address bar |
| `preview` | exactly what would be posted, plus rule warnings and blockers. Never posts |
| `post` | publishes PUBLIC. **Returns a dry-run preview unless `confirm=true`** |

How `post` shapes a post:

| Input | Becomes |
|---|---|
| text | text post |
| text + image | image post (JPG/PNG/GIF, < 36,152,320 px) |
| text + link | article card (title = `link_title` or the first line; the API doesn't scrape URLs) |
| text + image + link | image post with the link at the end of the text, or a card with the image as thumbnail (`link_as_card=true`) |

Safety: blockers stop a real post (asking for work, names in `blocklist.txt`, server paths, IPs, tokens, the same text
within 7 days); image paths in secret folders are refused; commentary is escaped for LinkedIn's "little" text format so
`( ) _ *` etc. stay plain text and `#hashtags` still work.

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/linkedin-poster && chmod 700 ~/.config/linkedin-poster
cp client.example.json ~/.config/linkedin-poster/client.json && chmod 600 ~/.config/linkedin-poster/client.json
# fill it in, then:
claude mcp add -s user linkedin-poster -- "$PWD/bin/linkedin-poster" serve
bin/linkedin-poster auth
```
LinkedIn app: create one at developer.linkedin.com (it is linked to a LinkedIn Page), add the products
**Share on LinkedIn** and **Sign In with LinkedIn using OpenID Connect**, and add your https redirect URL in the Auth tab.
`deploy/nginx-oauth-callback.conf` is an optional tidy landing page for that URL.

## CLI
```
linkedin-poster status | auth | finish '<url>' | preview ... | post ... [--yes] | expiry [days] | serve
linkedin-poster preview --text-file post.txt --image cover.png --link https://example.com/post
bin/linkedin-expiry-check 10     # prints one line when the token needs attention, else nothing (for a daily brief)
```

## Tests (no LinkedIn account needed, nothing is sent)
```bash
.venv/bin/python -m unittest discover -s tests -v   # HTTP faked: auth URL, token exchange, refresh, image upload + post payloads
.venv/bin/python tests/smoke.py                     # starts the MCP server over stdio and calls every tool without a token
```

## LinkedIn API facts used (learn.microsoft.com/linkedin, checked 2026-11-07)
| Fact | Source |
|---|---|
| Authorize `www.linkedin.com/oauth/v2/authorization`, token `.../oauth/v2/accessToken` (form, with client_secret + same redirect_uri). Codes live 30 min | shared/authentication/authorization-code-flow |
| Redirect URLs: absolute, added in the app's Auth tab, docs ask for HTTPS; query params ignored, no `#` | same page |
| Access tokens: 60 days. Programmatic refresh tokens only for approved Marketing Developer Platform partners | same page; shared/authentication/programmatic-refresh-tokens |
| Re-auth while signed in and the token is still valid skips the consent screen | authorization-code-flow, Step 5 |
| `profile` / `openid` via "Sign In with LinkedIn using OpenID Connect"; `w_member_social` via "Share on LinkedIn" (open, self-serve) | shared/authentication/getting-access |
| Member id = `sub` from `GET api.linkedin.com/v2/userinfo` → `urn:li:person:{sub}` | consumer/.../sign-in-with-linkedin-v2, share-on-linkedin |
| `POST /rest/posts`, headers `LinkedIn-Version: YYYYMM` + `X-Restli-Protocol-Version: 2.0.0`; 201 + `x-restli-id` | marketing/community-management/shares/posts-api |
| Images: `initializeUpload` (owner = person URN) → `uploadUrl` + `urn:li:image:…`; PUT bytes with the token; JPG/GIF/PNG, < 36,152,320 px; no sync upload; w_member_social can't GET image status | shares/images-api, shares/vector-asset-api |
| Article posts: no URL scraping; set source, title, description, thumbnail | posts-api |
| "little" text: escape `\| { } @ [ ] ( ) < > # \ * _ ~` | shares/little-text-format |
| Versions monthly, each supported ≥ 1 year; latest 202610 (override with `LINKEDIN_VERSION`) | marketing/versioning |
