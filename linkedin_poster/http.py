"""The one place that talks to the network. Tests swap `send` for a fake, so nothing else imports urllib."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

TIMEOUT = 60


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # lower-case keys
    body: bytes = b""

    def json(self):
        return json.loads(self.body or b"{}")

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def _urllib_send(method: str, url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> Response:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:  # 4xx/5xx still carry a useful body
        return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b"")


send = _urllib_send


def request(method: str, url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> Response:
    """Look `send` up at call time so a test can replace it after import."""
    return send(method, url, headers, data)
