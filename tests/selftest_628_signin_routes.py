"""#628: a control labelled "Sign in" navigates to the sign-in page, not to Connectors.

THE DEFECT, found by the owner on dbsearch.ai: from Admin's "YOUR DOCUMENTS / You are not
signed in. Sign in to search the documents you have access to." the Sign in action landed on
/canvas. Three call sites hardcoded it - the shell's identity chip and BOTH typed-error
actions - while ask.js:238 already sent people to /signin, so the product disagreed with
itself about where its own front door was.

/canvas remains a perfectly good target for OTHER actions ("try the demo", "Connect a
source"), and /signin itself offers "Or try the demo without signing in ->". What may not
exist is a control that says "Sign in" and goes somewhere that does not sign you in.

Asserted on the SERVED assets - the bytes a browser actually runs - rather than on the repo
files, because this repo has been bitten by tests that grep a file nobody loads.

    python3 tests/selftest_628_signin_routes.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)


def _asset(path: str) -> str:
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    return r.text


def test_the_account_control_signs_in_at_signin():
    """#630 moved the anonymous branch out of identity.js and into the account control. The
    RULE did not move: the one place the shell offers "Sign in" must go to the sign-in page.
    Asserted where the control now lives, and identity.js is checked to have no second copy -
    two files offering a sign-in link is how the product came to disagree with itself in the
    first place."""
    js = _asset("/static/js/ui/account.js")
    # The window right after the anonymous branch builds its anchor. Scoped deliberately:
    # /canvas appears elsewhere in this file and correctly so - it is where the "Connect"
    # link on an unconnected provider goes, which is a different act with a different
    # destination. This asserts about the SIGN-IN control only.
    branch = js.split('elx("a", "acct-signin"', 1)
    assert len(branch) == 2, "the account control has no anonymous Sign in branch"
    window = branch[1][:300]
    assert '"/signin"' in window, "the account control's Sign in does not target /signin"
    assert '"/canvas"' not in window, "the account control's Sign in points at Connectors"

    identity = _asset("/static/js/identity.js")
    assert "Sign in" not in identity, (
        "identity.js offers a second Sign in control - one definition, or the two drift")


def test_error_actions_sign_in_at_signin():
    js = _asset("/static/js/ui/errors.js")
    assert js.count('href: "/signin"') >= 2, "401/403 actions do not both target /signin"
    assert 'action: { label: "Sign in", href: "/canvas" }' not in js, \
        "a typed-error Sign in action still points at Connectors"


def test_signin_page_still_offers_the_demo_escape_hatch():
    """The reason routing every Sign in to /signin loses nothing: that page keeps its own
    door to the demo, so a visitor who did not mean to sign in is not stranded."""
    html = _asset("/signin")
    assert 'href="/canvas"' in html, "/signin no longer offers a way through to the demo"


if __name__ == "__main__":
    test_the_account_control_signs_in_at_signin()
    test_error_actions_sign_in_at_signin()
    test_signin_page_still_offers_the_demo_escape_hatch()
    print("ok")
