"""#592 - a control that says "Sign out" must end the session, not just change the URL.

The shell (index.html, which serves /ask, /chat, /draft, /admin and /developer) had a
"Sign out" button wired in login.js as:

    on("sign-out", "click", () => { location.href = "/"; });

It navigated home and did nothing else. The `dbs_session` cookie stayed valid for its full
8h and the vaulted refresh token was never dropped, so on a shared machine "Sign out" was a
lie: going back to /admin showed the user still signed in, with their delegated cloud
credential still redeemable. Confirmed on prod 260808 - clicked Sign out, then /auth/me
still answered signed_in:true, linked:["entra"].

Nothing caught it because the button LOOKED wired. There was a listener, it ran, and the
page moved. Only the destination was wrong, and no test ever asked what the click did to
the session.

The property pinned here is the one that was missing: EVERY control that offers to sign the
user out must reach POST /auth/logout. A handler that only navigates is the regression
itself. `signOut()` in api.js is the single definition, so there is not a second place to
forget.

#643 removed the second place entirely. There used to be two documents with two handlers -
the shell's and the canvas's - and this test enumerated both. Connectors is now a surface of
the shell, its identity chip is gone, and ui/account.js renders the ONE sign-out for every
surface. So the enumeration was replaced by its stronger form: assert there is exactly one
control, and that nothing has quietly grown a second.

    PYTHONPATH=src python3 tests/selftest_592_sign_out_ends_the_session.py
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("SELFHOST_BACKEND", "memory")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
STATIC = ROOT / "src/dbsearch/server/static"

LOGOUT_ROUTE = "/auth/logout"

#: Every DOCUMENT this app serves that a user can be signed in on. #630 made the shell's
#: sign-out JS-built rather than markup (ui/account.js, covered directly by
#: `test_the_js_built_sign_out_ends_the_session`), and #643 folded the canvas - the last
#: document with a hand-rolled one - into that same shell. So the scan below no longer
#: enumerates copies; it proves there are none.
DOCUMENTS = ["index.html", "signin.html", "visitor.html", "link_gone.html"]


def _sign_out_ids(html):
    """The id of every element whose visible text is "Sign out"."""
    return re.findall(r'<(?:button|span|a)\b[^>]*\bid="([^"]+)"[^>]*>\s*Sign out\s*<', html)


#: What a click binding looks like, whichever way it was written: on("id", "click", fn),
#: getElementById(id).addEventListener("click", ...), or querySelector("#id").onclick = ...
BINDING = re.compile(r'\bonclick\b|addEventListener|"click"|\'click\'')


def _binding_windows(source, element_id, span=400):
    """Every stretch of code that names this element AND then binds a click to it.

    Anchoring on the id alone is not enough: canvas.html mentions #signout first inside the
    HTML string that renders it, long before the line that wires it. That mention is markup,
    not a handler, so it is skipped rather than judged.
    """
    windows = []
    for needle in (f'"{element_id}"', f"'{element_id}'", f"#{element_id}"):
        for m in re.finditer(re.escape(needle), source):
            window = source[m.start():m.start() + span]
            if BINDING.search(window):
                windows.append(window)
    return windows


def test_api_exposes_one_sign_out_and_it_posts_to_the_logout_route():
    """The single definition. Both surfaces go through it, so it is the thing to get right."""
    api = (STATIC / "js/api.js").read_text()
    assert "export async function signOut(" in api or "export function signOut(" in api, (
        "api.js exports no signOut(). Both sign-out controls need one definition to call; "
        "two hand-rolled fetches is two things to forget.")
    body = api.split("signOut(", 1)[1][:400]
    assert LOGOUT_ROUTE in body, f"signOut() never calls {LOGOUT_ROUTE}"
    assert re.search(r'method:\s*"POST"', body), (
        f"signOut() must POST {LOGOUT_ROUTE}. The route is POST-only, so a GET is a 405 "
        "and the session survives - the exact failure #592 was.")


def test_no_document_renders_its_own_sign_out_control():
    """There is ONE sign-out, and it is the one ui/account.js builds.

    This is the enumeration test inverted, and it is the stronger assertion. It used to walk
    a list of documents that each rendered their own control and check every handler reached
    POST /auth/logout; the risk it managed was that one of the copies would be forgotten.
    #643 removed the copies. What can regress now is a NEW one appearing - a surface deciding
    it wants its own sign-out in markup - which would put a second, unaudited handler back on
    screen and reopen exactly the hole #592 was.

    A document may still MENTION sign-out (signin.html explains it); what it may not do is
    render a control with that as its visible label.
    """
    for doc in DOCUMENTS:
        html = (STATIC / doc).read_text()
        ids = _sign_out_ids(html)
        assert not ids, (
            f"{doc} renders its own sign-out control ({ids}). There is one definition - "
            "ui/account.js, which awaits signOut() and reports failure on the control. A "
            "second one in markup is a second handler nothing in this file audits.")
    # ...and the canvas, which is JS rather than a document, must not have kept its own.
    canvas = (STATIC / "js/surfaces/canvas.js").read_text()
    assert not _sign_out_ids(canvas), \
        "the canvas surface renders a sign-out control again - identity is ui/account.js's"


def test_the_js_built_sign_out_ends_the_session():
    """The shell's control, which #630 moved into the account dropdown.

    THE REGRESSION THIS GUARDS is not "the button is unwired" - it is subtler and it shipped
    once: the handler navigated away while the session was still alive, so the cookie stayed
    valid for its full 8h and the vaulted refresh token was never dropped. On a shared machine
    the next person was still signed in as the last one, and it looked exactly like success.

    So two things are asserted, not one: the handler calls signOut(), and the navigation is
    AFTER it in the same block. `signOut()` throws by design (api.js) precisely so a failed
    sign-out cannot be mistaken for a completed one."""
    src = (STATIC / "js/ui/account.js").read_text()
    assert "signOut" in src, "the account control never calls signOut()"
    block = src[src.index("acct-signout"):]
    assert "await signOut()" in block, (
        "the account control does not AWAIT the sign-out - a handler that navigates while "
        "the request is in flight leaves the session alive and looks identical to success")
    at_signout = block.index("await signOut()")
    at_nav = block.index("location.href")
    assert at_signout < at_nav, (
        "the account control navigates BEFORE ending the session - that is #592 exactly")
    # And the failure is reported on the control itself, never swallowed.
    assert "Sign out failed" in block, (
        "a failed sign-out says nothing, so the user walks away believing it worked")
    print("  PASS  the shell's JS-built sign-out ends the session before it navigates")


def test_no_sign_out_handler_is_merely_a_navigation():
    """Name the original shape so it cannot come back wearing a different id.

    `location.href = "/"` is a legitimate thing to do AFTER logging out. It is only a bug as
    the WHOLE handler, so this asserts the logout call is present alongside it, not that
    navigation is banned.
    """
    login = (STATIC / "js/login.js").read_text()
    for match in re.finditer(r'on\("sign-out",\s*"click",\s*(.{0,200}?)\);', login, re.S):
        handler = match.group(1)
        assert LOGOUT_ROUTE in handler or re.search(r'\bsignOut\s*\(', handler), (
            "the shell's sign-out handler only moves the URL:\n\n"
            f"    {handler.strip()}\n\n"
            "The cookie outlives it by up to 8h and the refresh token is never dropped.")


def test_the_route_the_client_calls_actually_exists():
    """A client-side fix that calls a URL the server does not serve is not a fix."""
    from dbsearch.server.app import app  # noqa: E402

    routes = {(r.path, m) for r in app.routes for m in getattr(r, "methods", []) or []}
    assert (LOGOUT_ROUTE, "POST") in routes, (
        f"the client posts {LOGOUT_ROUTE} but the server has no POST route for it")


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
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
