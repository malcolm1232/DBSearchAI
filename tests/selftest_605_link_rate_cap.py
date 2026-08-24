"""#605 / ADR 0021 consequences: "A per-link question rate cap bounds the cost of a forwarded
link." A named grantee's cost is bounded by how many people the owner is willing to name; a
link has none by construction - it is forwardable to anyone, and every question a visitor asks
is a real LLM call the OWNER pays for. `link_access._link_limiter` is that ceiling.

THE RIG: this file imports the fixtures `selftest_605_anonymous_link_access.py` already built
(the single-partition seed, `_ingest`, `_turn`, `_make_link`, `_visitor`, `_share_record`,
`_cleanup`) rather than re-deriving them, for the same reason that file gives for its own rig
choice - a per-account partition would make a same-partition property untestable, and here the
property under test (two DIFFERENT shares must not share a rate budget) is exactly that shape.

TESTABLE TIME AND A TESTABLE CAP, both without touching the real clock or the real environment
for the rest of the process:

  cap    `DBSEARCH_LINK_QUESTIONS_PER_HOUR` is read ONCE, at `link_access` import time, by
         module-level `_link_limiter`. This file needs a small cap to test the refusal without
         100 requests, so it sets the env var and `importlib.reload`s `link_access` - the
         routes already wired into `dbsearch.server.app` keep working because a route's global
         lookups resolve against its *defining* module's namespace at CALL time, not at define
         time; reload rewrites that namespace in place, `dbsearch.server.app` is never touched
         and never rebuilt.
  clock  the reloaded `_link_limiter` is built without a `clock=` kwarg (the brief's exact
         `_link_limiter` construction takes none), so this file overwrites its `_clock`
         attribute directly with a hand-advanced fake - the same monkeypatch shape
         `selftest_rate_limit.py`'s `FakeClock` already establishes for the sibling limiter.

    PYTHONPATH=src python3 tests/selftest_605_link_rate_cap.py
"""
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SELFHOST_BACKEND", "memory")
os.environ["DBSEARCH_RATE_LIMIT"] = "0"  # see selftest_605_anonymous_link_access.py: the
                                          # public demo's per-IP cap must not fire mid-suite.

import selftest_605_anonymous_link_access as base  # noqa: E402

from dbsearch.server import link_access  # noqa: E402


class FakeClock:
    """Hand-advanced clock, same shape as selftest_rate_limit.py's - a test that sleeps for an
    hour to prove a window rolls over is not a test."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _reload_with_cap(per_hour: int) -> "tuple[link_access, FakeClock]":
    """Set the env var THIS ONCE-READ-AT-IMPORT limiter reads, reload the module so a fresh
    `_link_limiter` picks it up, then hand it a clock this file controls. Returns the module
    and the clock so a caller can advance time without reaching back into module internals."""
    os.environ["DBSEARCH_LINK_QUESTIONS_PER_HOUR"] = str(per_hour)
    importlib.reload(link_access)
    clock = FakeClock()
    link_access._link_limiter._clock = clock
    return link_access, clock


def _restore_default_cap() -> None:
    """Undo the reload so later test files (or a re-run of this one) see the documented
    default rather than whatever cap the last test here set."""
    os.environ.pop("DBSEARCH_LINK_QUESTIONS_PER_HOUR", None)
    importlib.reload(link_access)


# ---- the whole point: a forwarded link cannot run up the owner's bill ---------------------

def test_the_cap_refuses_the_next_question_with_429_and_retry_after():
    """Ask up to a cap of 3, then prove the 4th is refused - HTTP 429, a Retry-After header,
    and no answer text on the wire (a refusal that still answered would be no cap at all)."""
    base._seed_one_partition()
    _reload_with_cap(3)
    try:
        conv = "c-605-rate-cap"
        doc = base._ingest("doc-605-rate-cap", base.A_TEXT)
        base._turn(conv, "how much leave carries over?",
                  f"It carries over {base.A_MARKER}.", [doc])
        made = base._make_link(conv)
        url = made["url"]
        anon = base._visitor()
        assert anon.get(url).status_code == 200

        for i in range(3):
            r = anon.post(url + "/chat", json={"question": f"q{i} about leave"})
            assert r.status_code == 200, f"question {i + 1} of 3 should be within cap: {r.text}"

        refused = anon.post(url + "/chat", json={"question": "q4 about leave"})
        assert refused.status_code == 429, (
            f"a 4th question inside the hour was answered past a cap of 3: "
            f"{refused.status_code} {refused.text[:200]}")
        assert refused.headers.get("retry-after"), (
            f"a 429 without Retry-After gives a caller nothing to back off on: "
            f"{dict(refused.headers)}")
        assert int(refused.headers["retry-after"]) > 0
        assert base.A_MARKER not in refused.text, (
            f"a refused question still leaked the answer: {refused.text[:200]}")
        try:
            body = refused.json()
            assert "answer" not in body, f"a 429 body carried an answer: {body}"
        except ValueError:
            pass  # a plain-text/JSON-detail error body is fine; the assertion above covers it
    finally:
        base._cleanup(conv, link_access.link_principal(base._share_record(made["share_id"])))
        _restore_default_cap()


def test_the_cap_is_keyed_per_link_not_globally_and_not_per_visitor():
    """THE PROPERTY THE WHOLE TASK EXISTS FOR. Keyed on `share.share_id`:

      not per-visitor  a second visitor to the SAME link, with a fresh fork-key cookie (or
                       none at all), is capped exactly as the first one is - clearing a
                       cookie must not buy a new budget.
      not global       a DIFFERENT share, for a DIFFERENT conversation, answers normally while
                       the first is fully spent - one busy link must never silence another
                       owner's link on the same box."""
    base._seed_one_partition()
    _reload_with_cap(2)
    made_capped = made_other = None
    conv_capped = conv_other = None
    try:
        conv_capped = "c-605-rate-capped-link"
        doc_capped = base._ingest("doc-605-rate-capped", base.A_TEXT)
        base._turn(conv_capped, "how much leave carries over?",
                  f"It carries over {base.A_MARKER}.", [doc_capped])
        made_capped = base._make_link(conv_capped)
        url_capped = made_capped["url"]

        conv_other = "c-605-rate-other-link"
        doc_other = base._ingest("doc-605-rate-other", base.B_TEXT)
        base._turn(conv_other, "what happened to the Krakow headcount?",
                  f"It was cut by {base.B_MARKER}.", [doc_other])
        made_other = base._make_link(conv_other)
        url_other = made_other["url"]

        first = base._visitor()
        assert first.get(url_capped).status_code == 200
        for i in range(2):
            r = first.post(url_capped + "/chat", json={"question": f"q{i} about leave"})
            assert r.status_code == 200, r.text[:200]
        exhausted = first.post(url_capped + "/chat", json={"question": "q2 about leave"})
        assert exhausted.status_code == 429, (
            f"the cap did not fire once this link's own budget was spent: "
            f"{exhausted.status_code}")

        # A SECOND visitor to the SAME link, no cookie shared with the first: still capped.
        second = base._visitor()
        assert second.get(url_capped).status_code == 200
        assert second.cookies.get(link_access.VISITOR_COOKIE) != \
            first.cookies.get(link_access.VISITOR_COOKIE), (
            "rig: the two visitors must actually be different forks, or this proves nothing")
        still_capped = second.post(url_capped + "/chat",
                                   json={"question": "a fresh visitor's question"})
        assert still_capped.status_code == 429, (
            "a fresh visitor with a NEW fork-key cookie bought a new budget on the SAME "
            f"link - the cap is keyed per visitor, not per link: {still_capped.status_code}")

        # The OTHER owner's link, unrelated share_id, answers normally throughout.
        other = base._visitor()
        r = other.post(url_other + "/chat", json={"question": "what happened to headcount?"})
        assert r.status_code == 200, (
            f"a busy link silenced a DIFFERENT owner's link on the same box: "
            f"{r.status_code} {r.text[:200]}")
        assert base.B_MARKER in r.json()["answer"], r.json()
    finally:
        if made_capped:
            base._cleanup(conv_capped,
                          link_access.link_principal(base._share_record(made_capped["share_id"])))
        if made_other:
            base._cleanup(conv_other,
                          link_access.link_principal(base._share_record(made_other["share_id"])))
        _restore_default_cap()


def test_advancing_the_clock_past_the_window_restores_service():
    """The window is an HOUR, driven entirely through the injected clock - no sleep, real or
    simulated, appears anywhere in this test."""
    base._seed_one_partition()
    module, clock = _reload_with_cap(1)
    try:
        conv = "c-605-rate-window"
        doc = base._ingest("doc-605-rate-window", base.A_TEXT)
        base._turn(conv, "how much leave carries over?",
                  f"It carries over {base.A_MARKER}.", [doc])
        made = base._make_link(conv)
        url = made["url"]
        anon = base._visitor()
        assert anon.get(url).status_code == 200

        first = anon.post(url + "/chat", json={"question": "q0 about leave"})
        assert first.status_code == 200, first.text[:200]

        refused = anon.post(url + "/chat", json={"question": "q1 about leave"})
        assert refused.status_code == 429, (
            f"a cap of 1 did not refuse the 2nd question: {refused.status_code}")

        clock.advance(3599)
        still_refused = anon.post(url + "/chat", json={"question": "q2 about leave"})
        assert still_refused.status_code == 429, (
            "the window rolled over one second early - advanced 3599s of a 3600s window")

        clock.advance(2)
        restored = anon.post(url + "/chat", json={"question": "q3 about leave"})
        assert restored.status_code == 200, (
            f"the link stayed capped after the hour window fully elapsed: "
            f"{restored.status_code} {restored.text[:200]}")
        assert base.A_MARKER in restored.json()["answer"], restored.json()
    finally:
        base._cleanup(conv, link_access.link_principal(base._share_record(made["share_id"])))
        _restore_default_cap()


def test_the_stream_route_holds_the_same_cap_as_the_json_one():
    """Two endpoints onto one capability (see `link_access._check_link_rate`'s own docstring):
    a cap enforced on only one of `/chat` and `/chat/stream` is a cap a caller picks its way
    around by choosing the other. Spend the budget entirely through `/chat/stream` and prove
    IT refuses on its own - a suite that only ever asked through `/chat` would stay green if
    this route's check were ever deleted."""
    base._seed_one_partition()
    _reload_with_cap(2)
    try:
        conv = "c-605-rate-stream"
        doc = base._ingest("doc-605-rate-stream", base.A_TEXT)
        base._turn(conv, "how much leave carries over?",
                  f"It carries over {base.A_MARKER}.", [doc])
        made = base._make_link(conv)
        url = made["url"]
        anon = base._visitor()
        assert anon.get(url).status_code == 200

        for i in range(2):
            r = anon.post(url + "/chat/stream", json={"question": f"q{i} about leave"})
            assert r.status_code == 200, f"stream question {i + 1} of 2 should be allowed"

        refused = anon.post(url + "/chat/stream", json={"question": "q2 about leave"})
        assert refused.status_code == 429, (
            f"the stream route did not honour the same cap the json route enforces: "
            f"{refused.status_code} {refused.text[:200]}")
        assert refused.headers.get("retry-after"), dict(refused.headers)
    finally:
        base._cleanup(conv, link_access.link_principal(base._share_record(made["share_id"])))
        _restore_default_cap()


def test_the_page_load_and_transcript_read_are_never_capped():
    """ADR 0021's cost argument is specifically about QUESTIONS: reading costs the owner
    nothing (no retrieval, no LLM call), so a cap that also throttled reads would refuse a
    visitor re-reading a page they already loaded, for no matching saving on the bill this
    cap exists to bound. Spend the ENTIRE question budget first, then hammer both read routes
    well past that count and prove neither ever returns 429."""
    base._seed_one_partition()
    _reload_with_cap(1)
    try:
        conv = "c-605-rate-reads-free"
        doc = base._ingest("doc-605-rate-reads-free", base.A_TEXT)
        base._turn(conv, "how much leave carries over?",
                  f"It carries over {base.A_MARKER}.", [doc])
        made = base._make_link(conv)
        url = made["url"]
        anon = base._visitor()
        assert anon.get(url).status_code == 200

        spent = anon.post(url + "/chat", json={"question": "q0 about leave"})
        assert spent.status_code == 200, spent.text[:200]
        capped = anon.post(url + "/chat", json={"question": "q1 about leave"})
        assert capped.status_code == 429, (
            "rig: the question budget must actually be spent, or this test proves nothing")

        for i in range(10):
            page = anon.get(url)
            assert page.status_code == 200, (
                f"the page load was capped on request {i + 1} after the question budget was "
                f"spent: {page.status_code}")
            transcript = anon.get(url + "/transcript")
            assert transcript.status_code == 200, (
                f"the transcript read was capped on request {i + 1} after the question "
                f"budget was spent: {transcript.status_code}")
    finally:
        base._cleanup(conv, link_access.link_principal(base._share_record(made["share_id"])))
        _restore_default_cap()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
