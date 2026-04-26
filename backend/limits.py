"""Body-size and rate-limiting middleware/deps.

Production-readiness item: without these, a single bad actor can flood the
backend with multi-megabyte JSON payloads or brute-force /auth/login.

Storage caveat: counters live in-process. Single-instance Railway deploy is
fine. If/when we scale out, swap _Counter for a Redis-backed implementation.
The dep surface stays the same.
"""
import os
import threading
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# Default 1 MB. The biggest legitimate payload is an /events row whose JSONB
# `payload` carries an LLM message history. 1 MB fits ~2,000 turns of typical
# chat — anything larger is almost certainly accidental or hostile.
MAX_BODY_BYTES = int(os.environ.get("LIGHTSEI_MAX_BODY_BYTES", str(1 * 1024 * 1024)))


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds MAX_BODY_BYTES.

    Honors the header when present (cheap, runs before any body is buffered).
    For chunked uploads without Content-Length we'd need streaming inspection,
    but our SDK and dashboard always send Content-Length.
    """

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES) -> None:
        super().__init__(app)
        self._max = max_bytes

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                n = int(cl)
            except ValueError:
                n = 0
            if n > self._max:
                return Response(
                    content=f'{{"detail":"payload too large; max {self._max} bytes"}}',
                    status_code=413,
                    media_type="application/json",
                )
        return await call_next(request)


class _Counter:
    """In-process sliding-window counter. Thread-safe.

    For each key we keep a deque of request timestamps within the active
    window. On every check we pop everything older than `window`. If the
    remaining length >= limit, deny.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_s: float) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds). retry_after is 0 when
        allowed."""
        now = time.monotonic()
        cutoff = now - window_s
        with self._lock:
            q = self._buckets[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                # Earliest hit in the window expires at q[0] + window_s.
                retry = max(0.0, (q[0] + window_s) - now)
                return False, retry
            q.append(now)
            return True, 0.0

    def reset(self) -> None:
        """Test helper — clear all counters."""
        with self._lock:
            self._buckets.clear()


_global_counter = _Counter()


def reset_counter_for_tests() -> None:
    _global_counter.reset()


def rate_limit(
    key: str,
    *,
    limit: int,
    window_s: float = 60.0,
) -> None:
    """Raise 429 if `key` has exceeded `limit` requests in the trailing
    `window_s` seconds. Otherwise records a hit and returns."""
    allowed, retry = _global_counter.hit(key, limit, window_s)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded; retry later",
            headers={"Retry-After": str(max(1, int(retry) + 1))},
        )


def client_ip(request: Request) -> str:
    """Best-effort caller IP. Trusts the leftmost X-Forwarded-For entry when
    Railway/Cloudflare/etc. is in front; otherwise falls back to the socket
    peer. Used for unauthenticated endpoints (login/signup brute force)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


# ---------- limit policies ---------- #
# Numbers picked to be loose enough not to bother a healthy bot, tight enough
# to make a brute-force or runaway loop visible. Tune as we see real traffic.

# Per IP, pre-auth: brute force protection.
LOGIN_LIMIT_PER_MIN = 10
SIGNUP_LIMIT_PER_MIN = 5

# Per credential (api_key.id or session.id), authenticated endpoints.
EVENTS_LIMIT_PER_MIN = 600   # 10/sec — generous for a single bot
DEFAULT_AUTHED_LIMIT_PER_MIN = 300


def limit_login_attempt(request: Request) -> None:
    rate_limit(f"login:{client_ip(request)}", limit=LOGIN_LIMIT_PER_MIN)


def limit_signup_attempt(request: Request) -> None:
    rate_limit(f"signup:{client_ip(request)}", limit=SIGNUP_LIMIT_PER_MIN)


def limit_events_per_credential(credential_id: str) -> None:
    rate_limit(f"events:{credential_id}", limit=EVENTS_LIMIT_PER_MIN)


def limit_authed_default(credential_id: str) -> None:
    rate_limit(f"authed:{credential_id}", limit=DEFAULT_AUTHED_LIMIT_PER_MIN)
