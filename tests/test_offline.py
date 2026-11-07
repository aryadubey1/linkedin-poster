"""Offline tests: the HTTP layer is faked, so no LinkedIn account or token is needed and nothing is ever posted.
Run: .venv/bin/python -m unittest discover -s tests -v"""
import json
import os
import stat
import struct
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CFG = tempfile.mkdtemp(prefix="li-poster-test-")
os.environ["LINKEDIN_POSTER_CONFIG"] = CFG
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linkedin_poster import api, core, http  # noqa: E402
from linkedin_poster.core import PosterError  # noqa: E402

REDIRECT = "https://example.com/oauth/linkedin"
SIXTY_DAYS = 5184000  # the expires_in LinkedIn documents for every access token


def png(path: Path, w: int = 1200, h: int = 627) -> Path:
    """A real, tiny PNG with the given header size (pixels need not be stored for our header check)."""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"\x00" * 10)) + chunk(b"IEND", b""))
    return path


class Fake:
    """Stands in for http.send. Routes by (method, url prefix); records every call."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers=None, data=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "data": data})
        for (m, prefix), resp in self.routes.items():
            if m == method and url.startswith(prefix):
                return resp(method, url, headers, data) if callable(resp) else resp
        raise AssertionError(f"unexpected {method} {url}")


def ok_json(obj, status=200, headers=None):
    return http.Response(status, headers or {}, json.dumps(obj).encode())


USERINFO = ok_json({"sub": "abc123XYZ", "name": "Test Member", "given_name": "Test"})


class Base(unittest.TestCase):
    def setUp(self):
        for p in Path(CFG).glob("**/*"):
            if p.is_file():
                p.unlink()
        core._write_private(core.CLIENT_FILE, json.dumps(
            {"client_id": "cid123", "client_secret": "s3cret", "redirect_uri": REDIRECT}))
        self._send = http.send
        self._sleep = api.time.sleep
        api.time.sleep = lambda s: None

    def tearDown(self):
        http.send = self._send
        api.time.sleep = self._sleep

    def connect(self, scope="email,openid,profile,w_member_social", extra=None):
        url = core.auth_start()
        state = parse_qs(urlparse(url).query)["state"][0]
        tok = {"access_token": "AQV-test-token", "expires_in": SIXTY_DAYS, "scope": scope, **(extra or {})}
        fake = Fake({("POST", core.TOKEN_URL): ok_json(tok), ("GET", core.USERINFO_URL): USERINFO})
        http.send = fake
        msg = core.auth_finish(f"{REDIRECT}?code=AUTHCODE&state={state}")
        return fake, msg, state


class AuthTests(Base):
    def test_auth_url(self):
        q = parse_qs(urlparse(core.auth_start()).query)
        self.assertEqual(q["response_type"], ["code"])
        self.assertEqual(q["client_id"], ["cid123"])
        self.assertEqual(q["redirect_uri"], [REDIRECT])
        self.assertEqual(q["scope"], ["openid profile w_member_social"])
        pending = core.PENDING_DIR / f"{q['state'][0]}.json"
        self.assertTrue(pending.exists())
        self.assertEqual(stat.S_IMODE(pending.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(core.PENDING_DIR.stat().st_mode), 0o700)

    def test_token_exchange(self):
        fake, msg, state = self.connect()
        form = parse_qs(fake.calls[0]["data"].decode())
        self.assertEqual(fake.calls[0]["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(form, {"grant_type": ["authorization_code"], "code": ["AUTHCODE"], "client_id": ["cid123"],
                                "client_secret": ["s3cret"], "redirect_uri": [REDIRECT]})
        self.assertEqual(fake.calls[1]["headers"]["Authorization"], "Bearer AQV-test-token")
        rec = json.loads(core.TOKEN_FILE.read_text())
        self.assertEqual(stat.S_IMODE(core.TOKEN_FILE.stat().st_mode), 0o600)
        self.assertAlmostEqual(rec["expires_at"], time.time() + SIXTY_DAYS, delta=5)
        self.assertNotIn("refresh_token", rec)
        self.assertEqual(core.author_urn(rec), "urn:li:person:abc123XYZ")
        self.assertIn("Test Member", msg)
        self.assertIn("60 days", msg)
        with self.assertRaises(PosterError):  # a link works once
            core.auth_finish(f"{REDIRECT}?code=AUTHCODE&state={state}")

    def test_phone_paste_without_scheme(self):
        self.assertEqual(core.parse_callback("example.com/oauth/linkedin?code=C&state=S"), ("C", "S"))

    def test_cancelled_consent(self):
        with self.assertRaisesRegex(PosterError, "user_cancelled_authorize"):
            core.parse_callback(f"{REDIRECT}?error=user_cancelled_authorize&error_description=x&state=S")

    def test_unknown_state(self):
        core.auth_start()
        with self.assertRaisesRegex(PosterError, "unknown"):
            core.auth_finish(f"{REDIRECT}?code=C&state=forged")

    def test_missing_post_permission(self):
        with self.assertRaisesRegex(PosterError, "w_member_social"):
            self.connect(scope="openid,profile")
        self.assertFalse(core.TOKEN_FILE.exists())

    def test_token_error(self):
        url = core.auth_start()
        state = parse_qs(urlparse(url).query)["state"][0]
        http.send = Fake({("POST", core.TOKEN_URL): ok_json(
            {"error": "invalid_request", "error_description": "authorization code expired"}, 400)})
        with self.assertRaisesRegex(PosterError, "code expired"):
            core.auth_finish(f"{REDIRECT}?code=C&state={state}")

    def test_http_redirect_rejected(self):
        core._write_private(core.CLIENT_FILE, json.dumps(
            {"client_id": "a", "client_secret": "b", "redirect_uri": "http://localhost:8790/"}))
        with self.assertRaisesRegex(PosterError, "https"):
            core.auth_start()


class StatusTests(Base):
    def test_not_set_up(self):
        core.CLIENT_FILE.unlink()
        self.assertFalse(core.status()["connected"])
        self.assertEqual(core.expiry_line(), "")  # silent until the tool is set up

    def test_not_connected(self):
        self.assertEqual(core.status()["next_step"], "auth_link")
        self.assertIn("not connected", core.expiry_line())

    def test_days_left_and_warning(self):
        self.connect()
        s = core.status()
        self.assertTrue(s["connected"])
        self.assertEqual(s["member_name"], "Test Member")
        self.assertIn(s["days_left"], (59, 60))
        self.assertEqual(core.expiry_line(10), "")
        later = time.time() + 55 * core.DAY
        self.assertIn("expires in 4 days", core.expiry_line(10, now=later))
        self.assertIn("expired", core.expiry_line(10, now=time.time() + 61 * core.DAY))

    def test_expired_token_refuses(self):
        self.connect()
        rec = json.loads(core.TOKEN_FILE.read_text())
        rec["expires_at"] = int(time.time()) - 10
        core._write_private(core.TOKEN_FILE, json.dumps(rec))
        with self.assertRaisesRegex(PosterError, "expired"):
            core.token()

    def test_refresh_when_partner_app(self):
        self.connect(extra={"refresh_token": "RT", "refresh_token_expires_in": 31536000})
        rec = json.loads(core.TOKEN_FILE.read_text())
        rec["expires_at"] = int(time.time()) + 3600
        core._write_private(core.TOKEN_FILE, json.dumps(rec))
        fake = Fake({("POST", core.TOKEN_URL): ok_json({"access_token": "NEW", "expires_in": SIXTY_DAYS})})
        http.send = fake
        self.assertEqual(core.token()["access_token"], "NEW")
        form = parse_qs(fake.calls[0]["data"].decode())
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["refresh_token"], ["RT"])


class TextTests(unittest.TestCase):
    def test_escape(self):
        self.assertEqual(api.escape_little("a_b (c) [d] *e* ~f~ {g} <h> |i| @j \\k"),
                         r"a\_b \(c\) \[d\] \*e\* \~f\~ \{g\} \<h\> \|i\| \@j \\k")

    def test_hashtags_stay(self):
        self.assertEqual(api.escape_little("Built with #Django and #nextjs. # alone, C#"),
                         r"Built with #Django and #nextjs. \# alone, C\#")

    def test_url_is_escaped_plain_text(self):
        self.assertEqual(api.escape_little("https://x.dev/a_b"), r"https://x.dev/a\_b")


class PlanTests(Base):
    def test_rules(self):
        self.assertTrue(api.plan("Hire me for your next app!")["blockers"])
        self.assertTrue(api.plan("I'm open to work")["blockers"])
        self.assertTrue(api.plan("Deployed to /var/www/site")["blockers"])
        self.assertIn("'freelancer': keep team size ambiguous", api.plan("As a freelancer I built x")["warnings"])
        self.assertTrue(api.plan("#a #b #c #d")["warnings"])
        self.assertEqual(api.plan("How we cut page weight by half with WebP")["blockers"], [])

    def test_private_blocklist(self):
        core.BLOCKLIST.write_text("# client names\nAcme Jewellers\n")
        self.assertTrue(api.plan("Billing app for acme jewellers")["blockers"])

    def test_too_long(self):
        with self.assertRaisesRegex(PosterError, "3000"):
            api.plan("x" * 3001)

    def test_image_checks(self):
        with self.assertRaisesRegex(PosterError, "secrets"):
            api.plan("t", image_path=str(Path.home() / ".ssh/id_ed25519"))
        bad = Path(CFG).parent / "li-poster-cover.webp"
        bad.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
        with self.assertRaisesRegex(PosterError, "JPG, PNG or GIF"):
            api.image_info(str(bad))
        huge = png(Path(CFG).parent / "li-poster-huge.png", 7000, 6000)
        with self.assertRaisesRegex(PosterError, "pixels"):
            api.image_info(str(huge))

    def test_payload_text_only(self):
        p = api.plan("Hello (world)")
        body = api.payload(p, "urn:li:person:X")
        self.assertEqual(body, {
            "author": "urn:li:person:X", "commentary": r"Hello \(world\)", "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False})

    def test_payload_link_card(self):
        p = api.plan("New post: caching\nmore", link="https://example.com/blog/caching", link_description="d")
        self.assertEqual(p["kind"], "article card")
        art = api.payload(p, "urn:li:person:X")["content"]["article"]
        self.assertEqual(art, {"source": "https://example.com/blog/caching", "title": "New post: caching",
                               "description": "d"})

    def test_image_plus_link_goes_inline(self):
        img = png(Path(CFG).parent / "li-poster-cover.png")
        p = api.plan("Shipped it", image_path=str(img), link="https://example.com/x", alt_text="A chart")
        self.assertEqual(p["kind"], "image + link in text")
        self.assertTrue(p["text"].endswith("\n\nhttps://example.com/x"))
        body = api.payload(p, "urn:li:person:X", "urn:li:image:IMG")
        self.assertEqual(body["content"], {"media": {"id": "urn:li:image:IMG", "altText": "A chart"}})

    def test_card_with_thumbnail(self):
        img = png(Path(CFG).parent / "li-poster-cover.png")
        p = api.plan("t", image_path=str(img), link="https://example.com/x", link_as_card=True)
        art = api.payload(p, "urn:li:person:X", "urn:li:image:IMG")["content"]["article"]
        self.assertEqual(art["thumbnail"], "urn:li:image:IMG")


class PublishTests(Base):
    def test_image_post_flow(self):
        self.connect()
        img = png(Path(CFG).parent / "li-poster-cover.png")
        upload_url = "https://www.linkedin.com/dms-uploads/IMG1/uploaded-image/0?sync=0"
        fake = Fake({
            ("POST", f"{api.API}/images?action=initializeUpload"): ok_json(
                {"value": {"uploadUrl": upload_url, "image": "urn:li:image:IMG1", "uploadUrlExpiresAt": 1}}),
            ("PUT", upload_url): http.Response(201),
            ("POST", f"{api.API}/posts"): http.Response(201, {"x-restli-id": "urn:li:share:777"}),
        })
        http.send = fake
        p = api.plan("Image post", image_path=str(img), alt_text="Cover")
        out = api.publish(p)
        self.assertEqual([c["method"] for c in fake.calls], ["POST", "PUT", "POST"])
        init, put, create = fake.calls
        for c in (init, create):
            self.assertEqual(c["headers"]["LinkedIn-Version"], api.VERSION)
            self.assertEqual(c["headers"]["X-Restli-Protocol-Version"], "2.0.0")
            self.assertEqual(c["headers"]["Authorization"], "Bearer AQV-test-token")
        self.assertEqual(json.loads(init["data"]), {"initializeUploadRequest": {"owner": "urn:li:person:abc123XYZ"}})
        self.assertEqual(put["headers"]["Authorization"], "Bearer AQV-test-token")  # images need the token
        self.assertEqual(put["data"], img.read_bytes())
        body = json.loads(create["data"])
        self.assertEqual(body["author"], "urn:li:person:abc123XYZ")
        self.assertEqual(body["content"], {"media": {"id": "urn:li:image:IMG1", "altText": "Cover"}})
        self.assertEqual(out["url"], "https://www.linkedin.com/feed/update/urn:li:share:777/")
        # same text again → blocked by the duplicate guard
        self.assertTrue(api.plan("Image post")["blockers"])

    def test_blocked_never_calls_api(self):
        self.connect()
        fake = Fake({})
        http.send = fake
        with self.assertRaisesRegex(PosterError, "Not posted"):
            api.publish(api.plan("hire me"))
        self.assertEqual(fake.calls, [])

    def test_api_error_is_readable(self):
        self.connect()
        http.send = Fake({("POST", f"{api.API}/posts"): ok_json({"message": "ACCESS_DENIED", "status": 403}, 403)})
        with self.assertRaisesRegex(PosterError, "Share on LinkedIn"):
            api.publish(api.plan("text post"))


if __name__ == "__main__":
    unittest.main()
