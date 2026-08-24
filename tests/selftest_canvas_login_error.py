"""#430: a failed Microsoft sign-in must SAY something the user can actually read.

/auth/callback hands failures back as /canvas?login=error&msg=<reason> (app.py _login_error).
handleLoginReturn() did set that message - into #statusbar, which renderStatus() overwrites
wholesale (bar.innerHTML=...) on the very next render. So the message was not missing, it was
written somewhere guaranteed to be erased, and in practice no user ever saw a sign-in failure.
Against a real foreign tenant that cost six retries and a trip to the prod logs before
AADSTS650052 was visible anywhere.

A failure the user cannot see is one they cannot report, and one they blame themselves for.
These tests pin the outcome to the toast, which owns its own element and its own lifetime.

    python3 tests/selftest_canvas_login_error.py
"""
import re
import sys
from pathlib import Path

CANVAS = (Path(__file__).resolve().parents[1]
          / "src" / "dbsearch" / "server" / "static" / "js" / "surfaces" / "canvas.js")
html = CANVAS.read_text()


def test_the_callbacks_reason_is_rendered():
    assert "handleLoginReturn" in html, "nothing reads the callback's outcome at all"
    fn = html.split("function handleLoginReturn")[1][:1400]
    assert '"login"' in fn or "'login'" in fn, "must read the login param"
    assert '"msg"' in fn or "'msg'" in fn, "must read the reason the server already sent"
    assert "toast(" in fn, "the reason must reach the user (#297: toast, never a dialog)"


def test_it_is_actually_called_on_boot():
    """A renderer nobody calls is the same as no renderer."""
    assert re.search(r"handleLoginReturn\(\)\s*;", html), "handleLoginReturn must be invoked"


def test_the_outcome_never_goes_to_the_statusbar_again():
    """THE ACTUAL #430 BUG. renderStatus() does bar.innerHTML=... on every render, so anything
    written to #statusbar here is erased almost immediately. One overwritten element is why a
    real foreign-tenant failure was invisible for an entire debugging session."""
    fn = html.split("function handleLoginReturn")[1][:1600]
    assert "statusbar" not in fn, \
        "the sign-in outcome must not be written to a bar that renderStatus() overwrites"


def test_the_consent_case_leads_with_what_the_user_can_do():
    """AADSTS65001/650052/650057 mean 'your org has not approved this yet'. The raw code is
    kept - a Microsoft admin needs it - but it must not be the whole message."""
    fn = html.split("function handleLoginReturn")[1][:1400]
    assert "650052" in fn, "the no-service-principal case must be recognised"
    assert "65001" in fn
    assert "hasn't approved" in fn or "has not approved" in fn, \
        "lead with the plain-language remedy"
    assert "+raw" in fn.replace(" ", "") or "raw" in fn, "the IdP's own reason must be carried"


def test_the_url_is_cleaned_so_a_reload_does_not_re_toast():
    fn = html.split("function handleLoginReturn")[1][:1400]
    assert "replaceState" in fn, "the login/msg params must be dropped after rendering"
    for k in ("login", "msg", "name"):
        assert k in fn, f"{k} should be cleaned from the URL"


def test_toast_dwell_scales_with_length():
    """3.2s is right for 'Saved' and unreadable for a full AADSTS reason. A message that
    disappears before it can be read is not a message."""
    fn = html.split("function toast(")[1][:900]
    assert "Math.min" in fn and "Math.max" in fn, "dwell must scale with message length"
    assert "12000" in fn, "and stay bounded so it still clears itself"


if __name__ == "__main__":
    test_the_callbacks_reason_is_rendered()
    test_it_is_actually_called_on_boot()
    test_the_consent_case_leads_with_what_the_user_can_do()
    test_the_url_is_cleaned_so_a_reload_does_not_re_toast()
    test_toast_dwell_scales_with_length()
    print("OK selftest_canvas_login_error")
