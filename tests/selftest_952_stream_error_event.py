"""#952 - an exception inside the /chat/stream SSE must become a TERMINAL EVENT, not a wedge.

FOUND ON PROD 260824, in the owner's own session: Groq answered a synthesis call with a 429
(`Rate limit reached ... on tokens per minute (TPM): Limit 8000 ...`). The exception raised
inside the streaming body AFTER the 200 headers were already on the wire, so uvicorn logged
"Exception in ASGI application" and the connection just died - no token, no done, no error.
The Ask box showed typing dots forever; the owner: "suddenly the chat cant type cos i think
its processing". The panic-navigation that followed is what drove #951's data loss, so this
wedge is not cosmetic - it is the first domino.

Two halves, both pinned here:
  SERVER  the sse() generator may never let an exception escape. It emits ONE terminal
          {"type": "error"} event - with a sentence a person can act on, and NEVER the raw
          exception text: the real 429 carried the Groq org id and an upsell URL, and other
          provider errors can quote api keys (LAW 1). A RateLimitError maps to a
          wait-and-retry sentence; anything else to a generic one. If `done` already went out,
          nothing more is emitted - the reader has their answer; the failure is logged.
  CLIENT  chatStream() must SETTLE on every stream ending: reject on an error event, reject
          when the stream ends with no done (the abrupt-death case), resolve on done. The
          wedge was chatStream resolving/hanging with onDone never called, which left the
          typing dots forever (and, if read() hung, `busy` stuck and the input dead).

    PYTHONPATH=src python3 tests/selftest_952_stream_error_event.py
"""
import json
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
os.environ["DBSEARCH_RATE_LIMIT"] = "0"
os.environ.pop("USERS_FILE", None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import app as app_mod  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
ALICE = {"X-DBSearch-User": "alice"}

#: The shape of the real leak: the prod 429's message carried the Groq org id and a billing
#: URL. Any of these substrings reaching the wire is the LAW 1 failure this test exists for.
_POISON = "org_01SECRETLEAK api_key=sk-poison https://console.groq.com/settings/billing"


class _FakeRateLimitError(Exception):
    """Same __name__ as openai.RateLimitError - the mapping must key on the class name, not
    on an import of the provider SDK (the local edition does not ship it)."""


_FakeRateLimitError.__name__ = "RateLimitError"


def _stream(question, headers=ALICE):
    events, raw = [], []
    with client.stream("POST", "/chat/stream", headers=headers,
                       json={"conv_id": "cv-952", "question": question}) as r:
        assert r.status_code == 200, r.status_code
        for line in r.iter_lines():
            line = line.strip()
            raw.append(line)
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events, "\n".join(raw)


def _with_producer(gen_fn):
    """Swap the conversation service's ask_stream for one turn - the exact seam sse() iterates."""
    svc = app_mod._edition.conversation_service
    orig = svc.ask_stream
    svc.ask_stream = lambda *a, **k: gen_fn()
    return lambda: setattr(svc, "ask_stream", orig)


def test_an_exception_mid_stream_becomes_a_terminal_error_event():
    def gen():
        yield {"type": "token", "text": "Hel"}
        raise RuntimeError(f"boom {_POISON}")
    restore = _with_producer(gen)
    try:
        events, raw = _stream("anything")
    finally:
        restore()
    kinds = [e.get("type") for e in events]
    assert kinds[-1] == "error", (
        f"the stream died without a terminal event - the client waits forever: {kinds}")
    assert kinds.count("error") == 1, kinds
    msg = events[-1].get("message", "")
    assert msg, "an error event with no message tells the reader nothing"
    for poison in ("org_01SECRETLEAK", "sk-poison", "console.groq.com"):
        assert poison not in raw, (
            f"the provider's raw error text reached the wire ({poison!r}) - LAW 1: a provider "
            "message can carry org ids, api keys and billing URLs")
    print("  PASS  a mid-stream exception ends the stream with one sanitized error event")


def test_a_pre_token_exception_still_emits_the_error_event():
    """The prod case exactly: the 429 was raised by the CREATE call, before any token."""
    def gen():
        raise RuntimeError(f"refused {_POISON}")
        yield  # pragma: no cover - makes this a generator
    restore = _with_producer(gen)
    try:
        events, raw = _stream("anything")
    finally:
        restore()
    assert [e.get("type") for e in events] == ["error"], events
    assert "org_01SECRETLEAK" not in raw
    print("  PASS  an exception before the first token still yields the error event")


def test_a_rate_limit_maps_to_a_wait_and_retry_sentence():
    def gen():
        raise _FakeRateLimitError(f"Rate limit reached ... {_POISON}")
        yield  # pragma: no cover
    restore = _with_producer(gen)
    try:
        events, raw = _stream("anything")
    finally:
        restore()
    msg = events[-1].get("message", "").lower()
    assert "rate" in msg or "busy" in msg, (
        f"a rate limit is a WAIT state, and the message must say so: {events[-1]}")
    assert "again" in msg or "moment" in msg or "seconds" in msg, (
        f"the reader's next action is 'wait and re-ask' - the message must carry it: {msg!r}")
    assert "org_01SECRETLEAK" not in raw and "console.groq.com" not in raw
    print("  PASS  a rate limit maps to a wait-and-retry sentence, provider text withheld")


def test_a_normal_stream_is_untouched():
    """The control: no error event rides on a healthy answer, and done still arrives."""
    client.post("/ingest", headers=ALICE,
                json={"external_id": "pto-952", "title": "PTO", "acl": ["all-staff"],
                      "text": "All staff receive twenty five days of paid leave."})
    events, _ = _stream("how many days of paid leave")
    kinds = [e.get("type") for e in events]
    assert "done" in kinds, kinds
    assert "error" not in kinds, f"an error event rode along on a healthy stream: {kinds}"
    print("  PASS  a healthy stream still ends in done, with no error event")


def test_the_draft_stream_has_the_same_terminal_event():
    """/draft/stream is the same shape with a longer generation - the strong model runs for
    every section, so the 429 is if anything MORE likely there. draft.js already renders an
    error event's message (#61 protocol); the server just never emitted one for a raise."""
    svc = app_mod._edition.draft_session
    orig = svc.confirm_stream

    def gen(*a, **k):
        yield {"type": "plan", "sections": []}
        raise RuntimeError(f"boom {_POISON}")
    svc.confirm_stream = gen
    try:
        events, raw = [], []
        with client.stream("POST", "/draft/stream", headers=ALICE,
                           json={"conv_id": "cv-952d", "intent": "confirm"}) as r:
            assert r.status_code == 200, r.status_code
            for line in r.iter_lines():
                line = line.strip()
                raw.append(line)
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
    finally:
        svc.confirm_stream = orig
    kinds = [e.get("type") for e in events]
    assert kinds and kinds[-1] == "error", (
        f"the draft stream died without a terminal event: {kinds}")
    assert "org_01SECRETLEAK" not in "\n".join(raw)
    print("  PASS  the draft stream ends a failure with the same sanitized error event")


def test_the_client_settles_on_every_stream_ending():
    """The CLIENT half, driven through the REAL chatStream in api.js (node, fetch stubbed):
    error event -> rejects with the message; stream ends with no done -> rejects; healthy
    stream -> resolves with onDone fired. The wedge was the middle case."""
    import _domgate
    if not _domgate.gate("the #952 chatStream settlement check"):
        return
    r = _domgate.run_node(
        ["node", str(ROOT / "tests/chat_stream_error_probe.mjs"),
         str(ROOT / "src/dbsearch/server/static/js/api.js")],
        "chatStream settlement")
    r = _domgate.resolve(r)
    assert r["errorEventRejects"], f"an error event did not reject chatStream: {r}"
    assert "rate" in (r["errorMessage"] or "").lower() or (r["errorMessage"] or ""), r
    assert r["abruptEndRejects"], (
        f"a stream that ended with no done resolved silently - the typing-dots wedge: {r}")
    assert r["healthyResolves"] and r["healthyDoneFired"], f"the healthy control broke: {r}"
    print("  PASS  chatStream settles on error, abrupt end, and done alike")


if __name__ == "__main__":
    failures = []
    for name in ["test_an_exception_mid_stream_becomes_a_terminal_error_event",
                 "test_a_pre_token_exception_still_emits_the_error_event",
                 "test_a_rate_limit_maps_to_a_wait_and_retry_sentence",
                 "test_a_normal_stream_is_untouched",
                 "test_the_draft_stream_has_the_same_terminal_event",
                 "test_the_client_settles_on_every_stream_ending"]:
        try:
            globals()[name]()
        except AssertionError as e:
            failures.append(name); print(f"FAIL  {name}\n      {e}")
        except Exception as e:
            failures.append(name); print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
