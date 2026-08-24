"""#332 — per-visitor + global rate limiting for the hosted demo.

`edition.py` has long noted that "the per-visitor rate limit is the remaining deploy-time
piece", and the public box's Caddyfile says its basic-auth gate may only be lifted
"alongside that rule". This is that rule, built where it actually belongs: in the app.

The abuse protection from #133 lives in `site/app/api/demo/search/route.ts` and guards the
Next.js marketing site only. dbsearch.ai serves THIS app, which had none — so an anonymous
visitor could loop the demo and drive unbounded Groq calls on a real API key.

Two caps, doing different jobs:

  per-IP    stops one visitor looping the demo. Evadable in principle: the origin is
            reachable directly by IP, so a caller who bypasses Cloudflare can forge
            CF-Connecting-IP and rotate identity at will.
  global    the actual spend bound. It holds no matter how the per-IP key is forged,
            which is why it exists and why it must never be removed as "redundant".

Deliberately in-process and dependency-free: one box, one uvicorn. If this ever runs
multi-replica the counters need to move to Redis, and the global cap becomes per-replica
until they do.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse

# Paths worth money: they drive retrieval, embeddings, or an LLM. Everything else
# (/health, /, /static, /config, /version, /auth/*) is cheap and stays unthrottled — an
# uptime check or a landing-page hit is not spend, and throttling those turns a busy
# moment into a false outage.
COSTLY_PREFIXES = (
    "/chat",
    "/draft",
    "/search",
    "/ingest",
    "/router/",
    "/graphql",
    "/admin/upload",
    "/admin/resync",
    "/api/demo",
    # /secrets (#319, ADR 0010 s3): not expensive - no retrieval, embedding, or LLM call - but
    # it is a credential-write surface (POST /secrets stores a plaintext password/key) and a
    # handle-guessing surface (GET/DELETE /secrets/{handle}), so it must not be brute-forceable
    # even though it costs nothing to serve.
    "/secrets",
)

# Uploads are legitimately large; they get the rate limit but not the body cap.
BODY_CAP_EXEMPT = ("/admin/upload",)

# A prefix match is a blunt instrument, and "/ingest" caught more than it meant to.
#
# GET /ingest/jobs/{job_id} is a STATUS READ - one lookup in the job store, no retrieval, no
# embedding, no LLM call, no write. It is the endpoint a client is supposed to POLL while a
# multi-minute crawl runs, and it was sharing the 30-requests-per-minute budget with the
# expensive POST /ingest that submits documents. Any poll interval at all starves it: the
# whole per-IP window is 30 requests, so even one watcher polling every 2 seconds consumes
# 100% of the budget for the entire application.
#
# Found on prod, not in review, and it presented as a lie rather than an error: #880's new
# progress modal polled this route, ate its allowance in 18 seconds, and then told the owner
# "That ingest did not finish" about a crawl that was finishing correctly and did (5 documents,
# last_sync 06:59:18Z). That is the exact failure #880 exists to remove - a surface asserting
# something about a job it had merely stopped being able to see.
#
# The expensive half keeps its meter: POST /ingest is still costly, because "/ingest/jobs" is
# checked as a NARROWER prefix, not because the check was loosened.
# #904 is the SAME DEFECT AS #880 ABOVE, on a different route, and the paragraph above
# describes it exactly: a cheap read sharing the budget of an expensive write, presenting
# as a LIE rather than an error. /router/probe opens a connection and reads a schema - no
# retrieval, no embedding, no LLM, no write - and the canvas necessarily calls it ONCE PER
# STORE. With 15 sources on the owner's canvas, a single walk plus one re-check exceeds the
# 30-per-minute allowance, and the refusals were rendered as red "not connected" nodes over
# stores that were healthy. Measured on prod 260821: five stores that had probed
# available=true minutes earlier all returned "rate limit exceeded - this is a public demo"
# in one sweep, then all returned available=true again when probed slowly.
#
# The property that makes it urgent: the cost scales with how many sources a customer has,
# so the product gets LESS trustworthy exactly as someone adopts it more.
#
# The expensive half keeps its meter. /router/ask (LLM) and /router/compose (makes stores
# live, can ingest) are NOT listed here and remain costly, because these are checked as
# NARROWER prefixes rather than by loosening the check.
METER_EXEMPT = (
    "/ingest/jobs",
    "/router/probe",
    "/router/manifest",
    "/router/kinds",
    "/router/catalog",
)


def client_ip(request) -> str:
    """The REAL visitor's IP.

    Behind Caddy (and Cloudflare in front of it) every request arrives from 127.0.0.1, so
    keying on the socket peer would put the entire internet in one bucket — throttling
    everyone at once or nobody. Preference order is most-specific-first:

      CF-Connecting-IP  set by Cloudflare, overwriting any client-supplied value
      X-Forwarded-For   set by Caddy; the ORIGINAL client is the left-most entry
      socket peer       direct local dev, no proxy in front

    Forgeable by anyone reaching the origin directly, which is precisely why the global
    cap exists alongside the per-IP one.
    """
    headers = getattr(request, "headers", {}) or {}
    cf = headers.get("cf-connecting-ip")
    if cf and cf.strip():
        return cf.strip()

    xff = headers.get("x-forwarded-for")
    if xff and xff.strip():
        first = xff.split(",")[0].strip()
        if first:
            return first

    peer = getattr(request, "client", None)
    host = getattr(peer, "host", None) if peer else None
    # Never return empty: a falsy key would merge unrelated callers into one bucket.
    return host or "unknown"


def _session_oid_or_none(headers: Headers) -> str | None:
    """The VERIFIED oid of a signed-in caller, or None (#904).

    Imported lazily and wrapped, because this middleware must never be the reason a
    request fails: if identity is not configured on this deployment, or anything at all
    goes wrong reading the cookie, the caller simply falls back to the anonymous per-IP
    budget - the behaviour that existed before this change. Failing OPEN into a smaller
    budget is safe; failing closed would turn a cookie problem into an outage.
    """
    raw = headers.get("cookie") or ""
    if not raw:
        return None
    try:
        from dbsearch.server import user_auth
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == user_auth.COOKIE:
                sess = user_auth.read_session(value)
                # read_session verifies the HMAC and the expiry, so a forged or stale
                # cookie returns None here and lands on the anonymous budget.
                return (sess or {}).get("oid") or None
    except Exception:
        return None
    return None


class FixedWindowLimiter:
    """Fixed-window counters, per-IP and global.

    Fixed rather than sliding on purpose: one integer per key, no per-request timestamp
    list to grow or trim. The known trade-off is that a visitor can spend two budgets
    across a window boundary; for a spend guard sized in tens per minute that burst is
    irrelevant, and the global cap bounds it anyway.
    """

    def __init__(self, per_ip: int, global_limit: int, window: float = 60.0, clock=None,
                 per_user: int | None = None):
        self.per_ip = per_ip
        # #904: a signed-in caller's own budget. Defaults generously relative to per_ip
        # because the canvas legitimately makes one request PER STORE, so the floor has to
        # clear a large fleet re-checking itself twice, not a human clicking.
        self.per_user = per_ip * 10 if per_user is None else per_user
        self.global_limit = global_limit
        self.window = float(window)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._ip_counts: dict[str, int] = defaultdict(int)
        self._ip_window_start = 0.0
        self._global_count = 0
        self._global_window_start = 0.0
        self._started = False

    def check(self, key: str, limit: int | None = None):
        """Record a request. Returns None when allowed, else Retry-After in seconds.

        `limit` overrides the per-key cap (#904). A signed-in caller is charged against
        their own identity with their own, larger budget; the GLOBAL cap below is
        deliberately unchanged and still applies to them, because that is the actual
        spend bound and the docstring at the top of this file says it must never be
        weakened as "redundant".
        """
        with self._lock:
            now = self._clock()
            if not self._started:
                self._ip_window_start = now
                self._global_window_start = now
                self._started = True

            if now - self._ip_window_start >= self.window:
                self._ip_counts.clear()
                self._ip_window_start = now
            if now - self._global_window_start >= self.window:
                self._global_count = 0
                self._global_window_start = now

            # Global first: once total spend is exhausted the answer is the same for
            # everyone, and a refused request must not consume a per-IP budget.
            if self._global_count >= self.global_limit:
                return self._retry_after(now, self._global_window_start)
            if self._ip_counts[key] >= (self.per_ip if limit is None else limit):
                return self._retry_after(now, self._ip_window_start)

            self._ip_counts[key] += 1
            self._global_count += 1
            return None

    def _retry_after(self, now: float, window_start: float) -> int:
        remaining = self.window - (now - window_start)
        return max(1, int(remaining) + (1 if remaining % 1 else 0))


class RateLimitMiddleware:
    """Pure-ASGI so the body cap can run before the app parses anything.

    Starlette's BaseHTTPMiddleware would force the request body through a parse cycle
    first, which is exactly what a size cap is meant to prevent.
    """

    def __init__(self, app, limiter: FixedWindowLimiter, max_body_bytes: int = 65_536,
                 costly_prefixes=COSTLY_PREFIXES):
        self.app = app
        self.limiter = limiter
        self.max_body_bytes = max_body_bytes
        self.costly_prefixes = tuple(costly_prefixes)

    def _is_costly(self, path: str) -> bool:
        if any(path.startswith(p) for p in METER_EXEMPT):
            return False
        return any(path.startswith(p) for p in self.costly_prefixes)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self._is_costly(scope.get("path", "")):
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        headers = Headers(scope=scope)
        cap_body = not any(path.startswith(p) for p in BODY_CAP_EXEMPT)

        if cap_body:
            declared = headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_body_bytes:
                return await self._reject(send, 413, "request body too large")

        # #904: WHOSE budget this request spends. Before this, every caller shared one
        # per-IP bucket sized for an anonymous demo visitor (30/min), so a SIGNED-IN owner
        # walking his own canvas exhausted it: 15 stores is one page load plus a re-check.
        # The product then got LESS reliable the more sources a customer added, and the
        # refusal was rendered as "not connected" on healthy stores while telling a paying
        # user "this is a public demo".
        #
        # read_session is a pure HMAC verification - no I/O, and unforgeable without the
        # signing key - so it is safe to do in middleware and cannot be spoofed into a
        # bigger budget. A signed-in caller is charged against their own oid; anonymous
        # traffic keeps exactly the old per-IP cap. The GLOBAL cap still binds both.
        oid = _session_oid_or_none(headers)
        if oid:
            retry = self.limiter.check(f"u:{oid}", limit=self.limiter.per_user)
            message = ("rate limit exceeded — too many requests in a short window, "
                       "please retry shortly")
        else:
            retry = self.limiter.check(f"ip:{client_ip(Request(scope))}")
            message = "rate limit exceeded — this is a public demo"
        if retry is not None:
            return await self._reject(
                send, 429, message,
                extra_headers=[(b"retry-after", str(retry).encode())])

        if not cap_body:
            return await self.app(scope, receive, send)

        # A chunked upload declares no Content-Length, so the cap has to be enforced on
        # the stream. Buffer it (bounded by the cap itself, so this cannot be used to
        # exhaust memory) and replay it to the app.
        body, too_large = b"", False
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self.max_body_bytes:
                too_large = True
                break

        if too_large:
            return await self._reject(send, 413, "request body too large")

        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        return await self.app(scope, replay, send)

    async def _reject(self, send, status: int, detail: str, extra_headers=None):
        response = JSONResponse({"detail": detail}, status_code=status)
        for key, value in (extra_headers or []):
            response.raw_headers.append((key, value))
        await response({"type": "http"}, None, send)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def install(app) -> None:
    """Wire the limiter from the environment.

    ON by default. `DBSEARCH_DEV_AUTH` is a standing lesson here (#183): a protection that
    defaults to off is a protection that is off in production, because the one box nobody
    remembers to configure is the public one. Opting OUT is explicit.
    """
    if os.environ.get("DBSEARCH_RATE_LIMIT", "1").strip() == "0":
        return
    app.add_middleware(
        RateLimitMiddleware,
        limiter=FixedWindowLimiter(
            per_ip=_env_int("DBSEARCH_RATE_LIMIT_PER_IP", 30),
            # #904: signed-in callers get their own budget. 300/min clears a large canvas
            # re-checking itself several times over; the global cap is what actually bounds
            # spend, and it still applies to them.
            per_user=_env_int("DBSEARCH_RATE_LIMIT_PER_USER", 300),
            global_limit=_env_int("DBSEARCH_RATE_LIMIT_GLOBAL", 300),
            window=_env_int("DBSEARCH_RATE_LIMIT_WINDOW", 60),
        ),
        max_body_bytes=_env_int("DBSEARCH_MAX_BODY_BYTES", 65_536),
    )
