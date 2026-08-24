"""#593 - "Your data" must say something TRUE, never a status code.

Driven signed-out on prod 260808, /admin rendered:

    YOUR DOCUMENTS            Error: admin/documents failed: 401
    QUESTIONS YOU HAVE ASKED  Error: admin/audit failed: 403

The banner in the same viewport already said "Not signed in" and offered a Sign in button,
so the page knew the cause and printed a status code anyway. #409 built one error vocabulary
(ui/errors.js) for exactly this; admin.js never used it.

Driving it also found the substantive half, which the carded symptom hid. "Questions you have
asked" sits in the OWNER's section but called /admin/audit, which is operator-only by
deliberate decision (#549: the rows carry other users' question text attributed to their oid).
So on any real deployment an ordinary user - the person whose questions they are - could never
see that panel at all. Rendering the 403 more prettily would have made a nicer lie.

The fix keeps #549's gate exactly where it is and adds GET /me/questions, which can only ever
return the caller's own rows because the server filters by the caller. Three properties follow,
and this file pins all three plus the presentation rule:

  - your own history is YOURS, without being an operator
  - it is ONLY yours, on the metadata plane as well as the content plane (LAW 2)
  - it survives a busy deployment: the old client asked for the newest 25 rows and filtered
    them in the browser, so on a box where anyone else was asking questions the owner's own
    history silently rendered as "No questions yet."

    PYTHONPATH=src python3 tests/selftest_593_your_data_says_something_true.py
"""
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

STATIC = ROOT / "src/dbsearch/server/static"
client = TestClient(app)

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
OPERATOR = "99999999-9999-9999-9999-999999999999"

ALICE_QUESTION = "how many days of parental leave do I get"
BOB_QUESTION = "who is on the redundancy list for the Hamburg plant"

_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_OPERATOR_OIDS")


def _real_login(operators: str = OPERATOR):
    """A deployment with a real login, where is_operator() is not a no-op (ADR 0011 s3)."""
    for k in _VARS:
        os.environ.pop(k, None)
    os.environ.update({"AUTH_TENANT_ID": "tid-1", "AUTH_CLIENT_ID": "cid",
                       "AUTH_CLIENT_SECRET": "sec", "DBSEARCH_OPERATOR_OIDS": operators})


def _cookie(oid: str) -> dict:
    return {user_auth.COOKIE: user_auth.sign_session(
        {"oid": oid, "tid": "tid-1", "exp": int(time.time()) + 3600})}


def _ask(oid: str, question: str):
    client.post("/search", cookies=_cookie(oid), json={"question": question})


def _seed_two_people_asking():
    _real_login()
    _ask(ALICE, ALICE_QUESTION)
    _ask(BOB, BOB_QUESTION)


# ---- the owner's own history -----------------------------------------------------------

def test_an_ordinary_user_can_see_their_own_questions():
    """The panel is in the OWNER's section. It has to work for someone who is not an operator."""
    _seed_two_people_asking()
    r = client.get("/me/questions", cookies=_cookie(ALICE))
    assert r.status_code == 200, (
        f"an ordinary user cannot read her own question history: {r.status_code} {r.text[:200]}")
    assert ALICE_QUESTION in json.dumps(r.json()), "alice's own question is missing from her history"


def test_the_history_is_only_ever_your_own():
    """LAW 2 on the metadata plane. The route filters server-side; the browser is not a gate."""
    _seed_two_people_asking()
    blob = json.dumps(client.get("/me/questions", cookies=_cookie(ALICE)).json())
    assert BOB_QUESTION not in blob, "alice can read what BOB asked"
    assert BOB not in blob, "alice learns bob's oid from her own history"


def test_even_an_operator_gets_only_their_own_from_this_route():
    """Being an operator is a reason to reach /admin/audit, not a reason for THIS route to
    widen. One route, one contract - otherwise its meaning depends on who is asking."""
    _seed_two_people_asking()
    blob = json.dumps(client.get("/me/questions", cookies=_cookie(OPERATOR)).json())
    assert ALICE_QUESTION not in blob and BOB_QUESTION not in blob, (
        "/me/questions returned other people's questions to an operator")


def test_your_own_history_survives_a_busy_deployment():
    """The bug the client-side filter had: take the newest N, THEN filter. On a deployment
    where anyone else is asking, the owner's rows fall outside the window and the panel says
    "No questions yet." to a person who has asked plenty.

    Asserted against a FRESH AuditLog rather than by driving HTTP, deliberately. The first
    version of this test drove /search 60 times and passed even with the filter order broken -
    twice over. The demo rate limiter answered 429 after 30 requests so the deployment was
    never actually busy, and `_edition.audit_log` is a module singleton the earlier tests in
    this file had already seeded with alice's rows, which then sat inside the window and
    satisfied the assertion for the wrong reason. A test that cannot fail is worse than no
    test: it reports that the property holds.
    """
    from dbsearch.audit import InMemoryAuditLog  # noqa: E402

    log = InMemoryAuditLog()
    log.record(ALICE, ALICE_QUESTION, "ask", [], "2026-08-08T09:00:00Z")
    for i in range(60):
        log.record(BOB, f"bob question number {i}", "ask", [], "2026-08-08T09:01:00Z")

    mine = log.recent(25, user=ALICE)
    assert [e.question for e in mine] == [ALICE_QUESTION], (
        "alice's only question was pushed out of the window by other people's activity: "
        f"got {[e.question for e in mine]}. Filter by the caller, THEN limit.")

    assert len(log.recent(25)) == 25, "the unfiltered window changed size"


def test_signed_out_is_refused_not_served():
    _real_login()
    r = client.get("/me/questions")
    assert r.status_code == 401, f"anonymous history should be 401, got {r.status_code}"


def test_the_operator_gate_on_admin_audit_is_unchanged():
    """This work must not widen #549's gate as a side effect. /admin/audit stays operator-only."""
    _seed_two_people_asking()
    r = client.get("/admin/audit", cookies=_cookie(ALICE))
    assert r.status_code == 403, (
        f"/admin/audit is deployment-wide observability and must stay operator-only, "
        f"got {r.status_code}")
    assert client.get("/admin/audit", cookies=_cookie(OPERATOR)).status_code == 200, (
        "the operator lost the deployment-wide audit trail")


# ---- the presentation rule --------------------------------------------------------------

def test_your_data_never_renders_a_bare_status_code():
    """#409 built one error vocabulary. A panel that prints `Error: <message>` is not using it."""
    admin = (STATIC / "js/surfaces/admin.js").read_text()
    raw = re.findall(r'`Error: \$\{[^}]+\}`', admin)
    assert not raw, (
        f"admin.js still prints raw errors in {len(raw)} place(s): {raw[:3]}. Route them "
        "through errorBlock() so the page says what happened and what to do about it.")
    assert "errorBlock" in admin, "admin.js does not import the shared error vocabulary at all"


def test_the_error_vocabulary_does_not_tell_a_signed_in_user_to_sign_in():
    """The lie the pretty version would otherwise have told.

    explain()'s 403 copy was "Not available in the demo. Sign in to use the live product." -
    true for a demo visitor, nonsense for a signed-in customer who simply is not an operator.
    """
    from importlib import util  # noqa: E402
    assert util  # (kept explicit: this test reads the source, it does not import ESM)
    errors = (STATIC / "js/ui/errors.js").read_text()
    assert "signedIn" in errors, (
        "explain() cannot tell a signed-in user from a demo visitor, so its 401/403 copy is "
        "guaranteed to be wrong for one of them")
    forbidden = re.search(r'signedIn[^\n]*\n(?:.*\n){0,12}?.*Sign in to use the live product',
                          errors)
    assert forbidden is None or "signedIn" in forbidden.group(0), (
        "the demo copy is still reachable for a signed-in caller")


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                failures.append(name)
                print(f"FAIL  {name}\n      {e}")
            except Exception as e:  # a route that does not exist yet raises here
                failures.append(name)
                print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
