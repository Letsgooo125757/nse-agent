"""Login, CSRF and HTTP hardening for the web app.

The app holds one person's finances, so it's built to be safe on the public
internet:
  * one password, stored only as a salted scrypt hash (APP_PASSWORD_HASH)
  * signed, HttpOnly, SameSite=Lax session cookies (Secure over HTTPS)
  * a CSRF token on every state-changing request
  * login throttling per client IP
  * strict security headers; the CSP allows no third-party scripts,
    styles or fonts (everything is served from /static)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ---------------------------------------------------------------- passwords

_N, _R, _P = 2**14, 8, 1  # scrypt cost: ~16 MB, a few tens of ms


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Use at least 10 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        got = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p),
                             dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


# ---------------------------------------------------------------- throttling


class LoginThrottle:
    """At most `max_failures` failed logins per IP within `window` seconds."""

    def __init__(self, max_failures: int = 5, window: float = 900, clock: Callable[[], float] = time.monotonic):
        self.max_failures, self.window, self.clock = max_failures, window, clock
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, ip: str) -> deque[float]:
        q, now = self._fails[ip], self.clock()
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, ip: str) -> int:
        """Seconds until the IP may try again (0 = allowed)."""
        q = self._prune(ip)
        if len(q) < self.max_failures:
            return 0
        return max(1, int(self.window - (self.clock() - q[0])))

    def fail(self, ip: str) -> None:
        self._prune(ip).append(self.clock())

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)


# ---------------------------------------------------------------- CSRF


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


def csrf_ok(request: Request, submitted: str | None) -> bool:
    expected = request.session.get("csrf")
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))


# ---------------------------------------------------------------- headers

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'none'")


class SecurityHeaders(BaseHTTPMiddleware):
    def __init__(self, app, *, hsts: bool):
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next) -> Response:
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if self.hsts:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if not request.url.path.startswith("/static/"):
            h.setdefault("Cache-Control", "no-store")  # personal data: never cache pages
        return resp
