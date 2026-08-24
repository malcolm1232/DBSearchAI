"""#652 - one cloud's credential can be forgotten, without ending the session.

The gap this closes: TokenVault.drop has taken a per-cloud `idp` since #193 and NOTHING ever
called it that way. The only call site was /auth/logout, with no idp, which drops every cloud
AND ends the session. So the account panel could say "Connected" with no way back, and
/auth/google/callback's refusal told a stuck user to "disconnect it there first" about a
control that had never been built.

CREDENTIAL ONLY (owner's ruling 260812): this revokes what DBSearch may redeem. It does NOT
unlink the identity - signing in with that provider again re-links to the SAME account, which
is ADR 0013 decision 4's promise and not this route's to change.

    PYTHONPATH=src python3 tests/selftest_652_disconnect_a_provider.py
"""
import os
import sys
import time
from pathlib import Path

os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("DBSEARCH_SESSION_KEY", "test-key-652")

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import app as app_mod  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402

ALICE = "alice-oid"
BOB = "bob-oid"


def _client():
    return TestClient(app_mod.app)


def _session(oid, idp="entra"):
    return user_auth.sign_session({"oid": oid, "name": oid, "email": f"{oid}@x",
                                   "tid": "", "idp": idp,
                                   "exp": int(time.time()) + 600})


def _seed(oid, **creds):
    user_auth.VAULT.drop(oid)
    for idp, tok in creds.items():
        user_auth.VAULT.put(oid, tok, idp=idp)


def test_disconnect_drops_only_the_named_cloud():
    """The whole point. Google goes, Microsoft stays, and the answer comes from the vault."""
    _seed(ALICE, entra="e-tok", google="g-tok")
    c = _client()
    c.cookies.set(user_auth.COOKIE, _session(ALICE))
    r = c.post("/auth/disconnect/google")
    assert r.status_code == 200, r.text
    assert r.json()["linked"] == ["entra"], r.json()
    assert user_auth.VAULT.linked(ALICE) == ["entra"], user_auth.VAULT.linked(ALICE)


def test_the_session_survives_a_disconnect():
    """It is a disconnect, not a sign-out. /auth/logout drops everything AND ends the session;
    this must do neither of those to the session."""
    _seed(ALICE, entra="e-tok", google="g-tok")
    c = _client()
    c.cookies.set(user_auth.COOKIE, _session(ALICE))
    c.post("/auth/disconnect/google")
    me = c.get("/auth/me").json()
    assert me["signed_in"] is True, me
    assert me["oid"] == ALICE, me
    assert me["linked"] == ["entra"], me


def test_auth_me_and_the_route_can_never_disagree():
    """The panel repaints from the route's `linked`, so it must be the same list /auth/me
    reports - both read VAULT.linked, which refuses to report a credential it cannot decrypt."""
    _seed(ALICE, entra="e-tok", google="g-tok")
    c = _client()
    c.cookies.set(user_auth.COOKIE, _session(ALICE))
    from_route = c.post("/auth/disconnect/entra").json()["linked"]
    from_me = c.get("/auth/me").json()["linked"]
    assert from_route == from_me == ["google"], (from_route, from_me)


def test_an_anonymous_caller_cannot_disconnect_anything():
    _seed(ALICE, entra="e-tok", google="g-tok")
    r = _client().post("/auth/disconnect/google")
    assert r.status_code == 401, r.status_code
    assert user_auth.VAULT.linked(ALICE) == ["entra", "google"], "a stranger revoked a credential"


def test_the_vault_key_is_the_SESSION_oid_and_nothing_else():
    """LAW 2, and the reason this route reads the session directly instead of going through
    `current_user`: the dev-auth header can assert ANY identity (#183). A caller who can name
    a victim's oid must never be able to drop that victim's credential."""
    _seed(ALICE, entra="e-tok", google="g-tok")
    _seed(BOB, google="b-tok")
    c = _client()
    c.cookies.set(user_auth.COOKIE, _session(BOB))
    c.post("/auth/disconnect/google", headers={"X-DBSearch-User": ALICE})
    assert user_auth.VAULT.linked(ALICE) == ["entra", "google"], \
        "bob's request, carrying alice's identity header, dropped ALICE's credential"
    assert user_auth.VAULT.linked(BOB) == [], user_auth.VAULT.linked(BOB)


def test_an_unknown_provider_is_refused_not_shrugged_off():
    """A typo must never report success having dropped nothing - the same stance
    google_auth.scopes_for takes on an unknown channel."""
    _seed(ALICE, entra="e-tok", google="g-tok")
    c = _client()
    c.cookies.set(user_auth.COOKIE, _session(ALICE))
    r = c.post("/auth/disconnect/gooogle")
    assert r.status_code == 400, r.status_code
    assert user_auth.VAULT.linked(ALICE) == ["entra", "google"], "a typo dropped something"


def test_disconnecting_a_cloud_that_was_never_linked_is_a_no_op_not_an_error():
    """Idempotent: the user's intent ('DBSearch must not hold this') is already satisfied, and
    a 4xx here would make a double-click look like a failure."""
    _seed(ALICE, entra="e-tok")
    c = _client()
    c.cookies.set(user_auth.COOKIE, _session(ALICE))
    r = c.post("/auth/disconnect/google")
    assert r.status_code == 200, r.text
    assert r.json()["linked"] == ["entra"], r.json()


def test_the_google_refusal_no_longer_names_a_control_that_does_not_exist():
    """app.py's 'already connected to a different account' message used to end 'or disconnect
    it there first' - advice for a thing that had never been built.

    Scoped to auth_google_callback, NOT a grep of the whole file. The first version of this
    test grepped app.py and failed - on the new disconnect route's own DOCSTRING, which quotes
    the dead string while explaining why it was wrong. Asserting on a file's prose instead of
    on the code that emits it is the #648 lesson, repeated here at my own expense."""
    import inspect
    src = inspect.getsource(app_mod.auth_google_callback)
    assert "disconnect it there first" not in src, \
        "the refusal still points at a control that does not exist"
    assert "disconnect Google from the account menu" in src, \
        "the refusal should name the real control"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    print("FAILED" if fails else "all green")
    sys.exit(1 if fails else 0)
