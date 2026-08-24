"""#332 — per-visitor + global rate limiting on the hosted demo.

WHY THIS EXISTS. `edition.py` has said for a while that "the per-visitor rate limit is
the remaining deploy-time piece", and the Caddyfile on the public box says the basic-auth
gate may only be lifted "alongside that rule". Neither was ever built for THIS app: the
abuse protection from #133 lives entirely in `site/app/api/demo/search/route.ts` (the
Next.js marketing site), while dbsearch.ai serves the FastAPI app, which had none. An
anonymous visitor drives Groq calls on a real API key, so an unmetered public endpoint is
a direct spend hole.

The properties that matter:

  1. The client IP must be the REAL visitor. The app sits behind Caddy AND Cloudflare, so
     every request arrives from 127.0.0.1 — a per-IP limit keyed on the socket peer would
     put the whole internet in one bucket and either throttle everyone at once or nobody.
  2. A per-IP cap stops one visitor looping the demo.
  3. A GLOBAL cap bounds total spend even when the per-IP cap is evaded. That matters
     because the origin is reachable directly by IP, so a caller who bypasses Cloudflare
     can forge CF-Connecting-IP and rotate identity at will. Per-IP alone is not a spend
     bound; the global cap is what actually caps the bill.
  4. Cheap paths (/health, /, /static) must NEVER be limited — uptime checks and the
     landing page are not spend.
  5. Oversized bodies are rejected BEFORE parsing.
  6. Every rejection carries Retry-After, so a well-behaved client can back off.

    PYTHONPATH=src python3 tests/selftest_rate_limit.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.rate_limit import (  # noqa: E402
    FixedWindowLimiter,
    RateLimitMiddleware,
    client_ip,
)


class FakeClock:
    """Hand-advanced clock. Real sleeps would make this suite slow and flaky."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRequest:
    """Minimal stand-in exposing only what client_ip reads."""

    def __init__(self, headers: dict | None = None, peer: str | None = "127.0.0.1"):
        self.headers = headers or {}

        class _C:
            host = peer

        self.client = _C() if peer is not None else None


# ─────────────────────────── client IP resolution ───────────────────────────

def test_client_ip_prefers_cf_connecting_ip():
    """Cloudflare sets CF-Connecting-IP to the true visitor. It is the most specific
    signal available and must win."""
    req = FakeRequest(headers={
        "cf-connecting-ip": "203.0.113.9",
        "x-forwarded-for": "198.51.100.1, 127.0.0.1",
    })
    assert client_ip(req) == "203.0.113.9", client_ip(req)


def test_client_ip_falls_back_to_first_forwarded_for():
    """Without Cloudflare, Caddy still sets X-Forwarded-For. The ORIGINAL client is the
    left-most entry; the rest are proxies."""
    req = FakeRequest(headers={"x-forwarded-for": "198.51.100.1, 127.0.0.1"})
    assert client_ip(req) == "198.51.100.1", client_ip(req)


def test_client_ip_falls_back_to_socket_peer():
    """No proxy headers at all (direct local dev) -> the socket peer."""
    req = FakeRequest(peer="192.0.2.7")
    assert client_ip(req) == "192.0.2.7", client_ip(req)


def test_client_ip_never_returns_empty():
    """A missing peer must not produce an empty key — that would merge unrelated callers
    into one bucket."""
    req = FakeRequest(headers={}, peer=None)
    assert client_ip(req), "client_ip returned a falsy key"


# ─────────────────────────────── the limiter ────────────────────────────────

def test_per_ip_limit_blocks_the_request_after_the_budget():
    clock = FakeClock()
    lim = FixedWindowLimiter(per_ip=3, global_limit=100, window=60.0, clock=clock)
    for i in range(3):
        assert lim.check("1.1.1.1") is None, f"request {i + 1} should be allowed"
    retry = lim.check("1.1.1.1")
    assert retry is not None, "4th request in the window should be limited"
    assert 0 < retry <= 60, f"Retry-After should be within the window, got {retry}"


def test_per_ip_budgets_are_independent():
    """One noisy visitor must not consume another visitor's budget."""
    clock = FakeClock()
    lim = FixedWindowLimiter(per_ip=2, global_limit=100, window=60.0, clock=clock)
    lim.check("1.1.1.1")
    lim.check("1.1.1.1")
    assert lim.check("1.1.1.1") is not None, "first IP should now be limited"
    assert lim.check("2.2.2.2") is None, "a different IP must still be allowed"


def test_window_rolls_over():
    clock = FakeClock()
    lim = FixedWindowLimiter(per_ip=2, global_limit=100, window=60.0, clock=clock)
    lim.check("1.1.1.1")
    lim.check("1.1.1.1")
    assert lim.check("1.1.1.1") is not None, "should be limited inside the window"
    clock.advance(61)
    assert lim.check("1.1.1.1") is None, "budget should reset after the window"


def test_global_cap_survives_ip_rotation():
    """THE SPEND BOUND. A caller reaching the origin directly can forge CF-Connecting-IP,
    so per-IP limits are evadable. The global cap is what actually bounds the Groq bill."""
    clock = FakeClock()
    lim = FixedWindowLimiter(per_ip=10, global_limit=5, window=60.0, clock=clock)
    for i in range(5):
        assert lim.check(f"10.0.0.{i}") is None, f"request {i + 1} under the global cap"
    retry = lim.check("10.0.0.99")
    assert retry is not None, "a brand-new IP must still be refused once global is spent"
    assert 0 < retry <= 60, retry


def test_global_window_rolls_over():
    clock = FakeClock()
    lim = FixedWindowLimiter(per_ip=10, global_limit=2, window=60.0, clock=clock)
    lim.check("1.1.1.1")
    lim.check("2.2.2.2")
    assert lim.check("3.3.3.3") is not None
    clock.advance(61)
    assert lim.check("3.3.3.3") is None, "global budget should reset after the window"


# ──────────────────────────────── middleware ────────────────────────────────

def _app(per_ip=2, global_limit=100, max_body=1024, clock=None, per_user=None,
         costly_only=False):
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/chat")
    def chat():
        return {"answer": "hi"}

    @app.post("/search")
    def search():
        return {"hits": []}

    @app.post("/ingest")
    def ingest():
        return {"ok": True}

    @app.get("/ingest/jobs/{job_id}")
    def ingest_job(job_id: str):
        return {"job_id": job_id, "status": "running"}

    # #904: the cheap router read the canvas makes once per store, and the expensive one.
    @app.post("/router/probe")
    def router_probe():
        return {"available": True}

    @app.post("/router/ask")
    def router_ask():
        return {"answer": "hi"}

    app.add_middleware(
        RateLimitMiddleware,
        limiter=FixedWindowLimiter(per_ip=per_ip, global_limit=global_limit,
                                   per_user=per_user, window=60.0,
                                   clock=clock or FakeClock()),
        max_body_bytes=max_body,
    )
    return app


def test_costly_path_is_limited_with_retry_after():
    client = TestClient(_app(per_ip=2))
    hdr = {"cf-connecting-ip": "203.0.113.5"}
    assert client.post("/chat", headers=hdr).status_code == 200
    assert client.post("/chat", headers=hdr).status_code == 200
    r = client.post("/chat", headers=hdr)
    assert r.status_code == 429, f"expected 429, got {r.status_code}"
    assert r.headers.get("retry-after"), "429 must carry Retry-After"
    assert int(r.headers["retry-after"]) > 0


def test_cheap_paths_are_never_limited():
    """Uptime checks and the landing page cost nothing and must never be throttled."""
    client = TestClient(_app(per_ip=1))
    hdr = {"cf-connecting-ip": "203.0.113.6"}
    for i in range(10):
        r = client.get("/health", headers=hdr)
        assert r.status_code == 200, f"/health limited on request {i + 1} — {r.status_code}"


def test_polling_a_jobs_status_is_not_metered_as_an_ingest():
    """#880 aftermath, found on prod. GET /ingest/jobs/{id} is a status READ - one lookup, no
    retrieval, no embedding, no write - and it is the endpoint a client is TOLD to poll while a
    multi-minute crawl runs. It matched the "/ingest" costly prefix, so it shared a
    30-per-minute budget with the crawl submit and with every other costly route.

    The arithmetic makes the feature impossible, not merely slow: the whole per-IP window is 30
    requests, so a single watcher polling even every two seconds spends the entire application's
    allowance. It presented as a LIE, which is why it earns a guard rather than a tuning note -
    the progress modal ate its allowance in 18 seconds and then reported "That ingest did not
    finish" about a crawl that finished with 5 documents.

    The budget here is 2, and 12 polls is six times it."""
    client = TestClient(_app(per_ip=2))
    hdr = {"cf-connecting-ip": "203.0.113.9"}
    for i in range(12):
        r = client.get("/ingest/jobs/job-abc123", headers=hdr)
        assert r.status_code == 200, (
            f"a job-status poll was throttled on request {i + 1} ({r.status_code}) - a surface "
            "that cannot see its job reports a healthy crawl as a failure")


def test_submitting_an_ingest_is_still_metered():
    """The other half, and the one that makes the exemption a CORRECTION rather than a hole:
    POST /ingest is the expensive write and keeps its meter. Mutated separately from the test
    above - an exemption written as a broader prefix would pass that one and fail this."""
    client = TestClient(_app(per_ip=2))
    hdr = {"cf-connecting-ip": "203.0.113.10"}
    assert client.post("/ingest", headers=hdr).status_code == 200
    assert client.post("/ingest", headers=hdr).status_code == 200
    r = client.post("/ingest", headers=hdr)
    assert r.status_code == 429, \
        f"POST /ingest must stay rate-limited - it is the expensive one ({r.status_code})"


def test_probing_a_store_is_not_metered_as_an_ask():
    """#904, found on the owner's own prod canvas - and the SAME DEFECT as #880 above, which
    is why it sits next to it. /router/probe opens a connection and reads a schema: no
    retrieval, no embedding, no LLM, no write. It matched the "/router/" costly prefix and so
    shared a 30-per-minute budget with /router/ask.

    The arithmetic is what makes it a lie rather than a slowdown, and it is WORSE than #880's
    because the cost scales with the customer: the canvas probes ONCE PER STORE, so a 15-source
    fleet spends half the entire application allowance just rendering itself, and a re-check
    exceeds it. The refusals were painted as red "not connected" nodes over healthy stores.
    Measured on prod 260821: five stores that had probed available=true minutes earlier all
    returned "rate limit exceeded - this is a public demo" in one sweep, then all returned
    available=true again when probed slowly.

    The budget here is 2, and 12 probes is six times it."""
    client = TestClient(_app(per_ip=2))
    hdr = {"cf-connecting-ip": "203.0.113.20"}
    for i in range(12):
        r = client.post("/router/probe", headers=hdr, json={"entry": {"kind": "postgres"}})
        assert r.status_code != 429, (
            f"a store probe was throttled on request {i + 1} - a canvas that cannot probe its "
            "own stores paints healthy sources as disconnected")


def test_asking_is_still_metered():
    """The half that makes the exemption a CORRECTION rather than a hole: /router/ask drives an
    LLM and keeps its meter. Mutated separately from the test above - an exemption written as
    the broad "/router/" prefix would pass that test and fail this one."""
    client = TestClient(_app(per_ip=2))
    hdr = {"cf-connecting-ip": "203.0.113.21"}
    codes = [client.post("/router/ask", headers=hdr, json={}).status_code for _ in range(6)]
    assert 429 in codes, (
        "/router/ask must stay metered - it is the LLM path this whole file exists to bound")


def test_a_signed_in_caller_gets_their_own_budget():
    """#904, second half, DRIVEN THROUGH THE MIDDLEWARE and not through the limiter.

    An earlier version of this test called FixedWindowLimiter directly and PASSED while the
    middleware still charged signed-in callers to the shared anonymous bucket - it could not
    reach the code the fix actually changed. Caught by mutation, and the reason this version
    goes through a real TestClient with a real signed cookie: the assertion has to fail if the
    ROUTING is reverted, not merely if the counter is.

    Before this fix every caller shared ONE per-IP bucket sized for an anonymous demo visitor,
    so a signed-in owner walking his own canvas was throttled by a rule written for abuse
    prevention - and told "this is a public demo" while it happened.

    Both callers below come from the SAME IP, so the only thing that can separate them is the
    verified session."""
    from dbsearch.server import user_auth
    app = _app(per_ip=2, costly_only=True)
    client = TestClient(app)
    hdr = {"cf-connecting-ip": "203.0.113.30"}

    token = user_auth.sign_session({"oid": "oid-abc", "exp": int(time.time()) + 3600})
    signed = dict(hdr, cookie=f"{user_auth.COOKIE}={token}")

    # The anonymous cap is 2. A signed-in caller must sail past it on the same IP.
    for i in range(8):
        r = client.post("/chat", headers=signed)
        assert r.status_code != 429, (
            f"signed-in request {i + 1} was throttled by the ANONYMOUS budget of 2 - the "
            "middleware is still charging a signed-in caller to the shared per-IP bucket")

    # ...while an anonymous caller from that very same IP still gets the old, small cap.
    codes = [client.post("/chat", headers=hdr).status_code for _ in range(6)]
    assert 429 in codes, "the anonymous per-IP cap must be unchanged by this fix"


def test_the_signed_in_budget_is_larger_but_still_bounded():
    """Being signed in buys a bigger budget, never an unmetered one."""
    lim = FixedWindowLimiter(per_ip=2, per_user=5, global_limit=1000, window=60,
                             clock=FakeClock())
    for i in range(5):
        assert lim.check("u:oid-123", limit=lim.per_user) is None, f"request {i + 1}"
    assert lim.check("u:oid-123", limit=lim.per_user) is not None, (
        "the signed-in budget must still be BOUNDED")


def test_the_global_cap_still_binds_a_signed_in_caller():
    """The control that stops the fix becoming a spend hole. The file's own docstring says the
    global cap "must never be removed as redundant", so a signed-in identity must NOT escape
    it - being logged in buys a bigger personal budget, never an unmetered one."""
    lim = FixedWindowLimiter(per_ip=2, per_user=1000, global_limit=3, window=60, clock=FakeClock())
    for _ in range(3):
        assert lim.check("u:oid-123", limit=lim.per_user) is None
    assert lim.check("u:oid-123", limit=lim.per_user) is not None, (
        "the GLOBAL cap must bind a signed-in caller too - otherwise a session is a spend hole")


def test_a_forged_session_cookie_does_not_buy_the_bigger_budget():
    """The security control. The bigger budget is granted on a VERIFIED session, so garbage or
    a tampered cookie must fall back to the anonymous per-IP budget rather than being trusted."""
    from starlette.datastructures import Headers
    from dbsearch.server.rate_limit import _session_oid_or_none
    assert _session_oid_or_none(Headers({})) is None, "no cookie -> anonymous"
    assert _session_oid_or_none(Headers({"cookie": "dbs_session=not-a-real-token"})) is None, (
        "an unsigned/forged cookie must NOT be accepted as an identity")
    assert _session_oid_or_none(Headers({"cookie": "dbs_session=abc.deadbeef"})) is None, (
        "a cookie with a bad HMAC must NOT be accepted as an identity")


def test_limit_is_keyed_on_the_real_visitor_not_the_proxy():
    """The regression that makes a per-IP limit worthless behind Caddy+Cloudflare: if the
    key were the socket peer, these two DIFFERENT visitors would share one bucket."""
    client = TestClient(_app(per_ip=1))
    assert client.post("/chat", headers={"cf-connecting-ip": "203.0.113.1"}).status_code == 200
    r = client.post("/chat", headers={"cf-connecting-ip": "203.0.113.2"})
    assert r.status_code == 200, (
        "a second, distinct visitor was throttled by the first visitor's budget — the "
        f"limiter is keyed on the proxy, not the client (got {r.status_code})")


def test_oversized_body_is_rejected_before_parsing():
    client = TestClient(_app(max_body=100))
    r = client.post("/search", headers={"cf-connecting-ip": "203.0.113.7",
                                        "content-length": "999999"},
                    content=b"x" * 200)
    assert r.status_code == 413, f"expected 413, got {r.status_code}"


def test_oversized_body_rejected_even_without_content_length():
    """A chunked upload has no Content-Length; the cap must still hold."""
    client = TestClient(_app(max_body=100))

    def gen():
        yield b"x" * 500

    r = client.post("/search", headers={"cf-connecting-ip": "203.0.113.8"}, content=gen())
    assert r.status_code == 413, f"expected 413, got {r.status_code}"


# ────────────────────────── the real app is wired ───────────────────────────

def test_real_app_has_the_middleware_installed():
    """A limiter nobody installed protects nothing."""
    from dbsearch.server.app import app as real_app

    names = [m.cls.__name__ for m in real_app.user_middleware]
    assert "RateLimitMiddleware" in names, (
        f"RateLimitMiddleware is not installed on the real app — {names}")


def test_real_app_health_is_not_limited():
    from dbsearch.server.app import app as real_app

    client = TestClient(real_app)
    for i in range(30):
        r = client.get("/health")
        assert r.status_code == 200, f"/health returned {r.status_code} on request {i + 1}"


def main():
    test_client_ip_prefers_cf_connecting_ip()
    print("  PASS  client IP prefers CF-Connecting-IP (the true visitor behind Cloudflare)")
    test_client_ip_falls_back_to_first_forwarded_for()
    print("  PASS  falls back to the left-most X-Forwarded-For entry (the original client)")
    test_client_ip_falls_back_to_socket_peer()
    print("  PASS  falls back to the socket peer when no proxy headers are present")
    test_client_ip_never_returns_empty()
    print("  PASS  never returns an empty key (would merge unrelated callers into one bucket)")

    test_per_ip_limit_blocks_the_request_after_the_budget()
    print("  PASS  per-IP budget blocks the request past the cap, with Retry-After")
    test_per_ip_budgets_are_independent()
    print("  PASS  per-IP budgets are independent between visitors")
    test_window_rolls_over()
    print("  PASS  per-IP budget resets when the window rolls")
    test_global_cap_survives_ip_rotation()
    print("  PASS  GLOBAL cap holds under IP rotation — the actual spend bound")
    test_global_window_rolls_over()
    print("  PASS  global budget resets when the window rolls")

    test_costly_path_is_limited_with_retry_after()
    print("  PASS  costly path (/chat) returns 429 + Retry-After past the cap")
    test_cheap_paths_are_never_limited()
    print("  PASS  cheap paths (/health) are never limited")
    test_polling_a_jobs_status_is_not_metered_as_an_ingest()
    test_submitting_an_ingest_is_still_metered()
    print("  PASS  #880: polling a job's STATUS is not metered as an ingest, while POST "
          "/ingest still is")
    test_probing_a_store_is_not_metered_as_an_ask()
    test_asking_is_still_metered()
    print("  PASS  #904: probing a store is not metered as an ask, while /router/ask still is")
    test_a_signed_in_caller_gets_their_own_budget()
    print("  PASS  #904: a signed-in caller gets their own budget, anonymous cap unchanged")
    test_the_signed_in_budget_is_larger_but_still_bounded()
    print("  PASS  #904: the signed-in budget is larger but still bounded")
    test_the_global_cap_still_binds_a_signed_in_caller()
    print("  PASS  #904: the GLOBAL cap still binds a signed-in caller (not a spend hole)")
    test_a_forged_session_cookie_does_not_buy_the_bigger_budget()
    print("  PASS  #904: a forged/garbage session cookie falls back to the anonymous budget")
    test_limit_is_keyed_on_the_real_visitor_not_the_proxy()
    print("  PASS  limit keys on the real visitor, not the Caddy/Cloudflare hop")
    test_oversized_body_is_rejected_before_parsing()
    print("  PASS  oversized body rejected with 413 before parsing")
    test_oversized_body_rejected_even_without_content_length()
    print("  PASS  oversized body rejected even with no Content-Length (chunked)")

    test_real_app_has_the_middleware_installed()
    print("  PASS  the real app actually has the middleware installed")
    test_real_app_health_is_not_limited()
    print("  PASS  the real app's /health is exempt")

    print("\n#332 RATE-LIMIT SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
