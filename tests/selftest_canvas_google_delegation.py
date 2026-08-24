"""#193 — the canvas must author a GOOGLE delegation for a bigquery store, not an Entra one.
A GCP store wired with entra_refresh would redeem a Microsoft refresh token against Google
and fail closed forever, so this mapping is load-bearing.

There is no JS test harness in this repo, so this asserts on the shipped asset's source.
Run: PYTHONPATH=src python3 tests/selftest_canvas_google_delegation.py
"""
import re
import sys
from pathlib import Path

CANVAS = (Path(__file__).resolve().parents[1]
          / "src/dbsearch/server/static/js/surfaces/canvas.js").read_text()


def test_bigquery_kind_offers_require_signin():
    # Each kind is declared on one physical line; match the whole line so the assertion
    # is independent of which position require_signin sits in (a [^}]* match would stop
    # at the first nested field and force require_signin to be field #1).
    row = re.search(r"^\s*bigquery:.*$", CANVAS, re.M)
    assert row, "bigquery must be a canvas kind"
    assert "require_signin" in row.group(0), \
        "bigquery needs the require_signin field or a user can never query as themselves"


def test_delegation_is_google_for_bigquery_and_entra_for_azure():
    assert "_DELEG_IDP" in CANVAS or "google_refresh" in CANVAS, \
        "canvas must be able to emit a google_refresh delegation"
    fn = re.search(r"function delegationFor\(kind\)\{.*?\n  \}", CANVAS, re.S)
    assert fn, "delegationFor must exist"
    body = fn.group(0)
    assert "google_refresh" in body, "delegationFor must emit google_refresh for GCP kinds"
    assert "GOOGLE_CLIENT_ID" in body and "GOOGLE_CLIENT_SECRET" in body, \
        "google delegation needs the Google app creds as ${ENV} refs (LAW 1)"
    assert "entra_refresh" in body, "azure kinds must keep entra_refresh"


def test_connect_google_chip_is_wired_to_the_link_route():
    assert "/auth/google/login" in CANVAS, "the canvas needs a Connect Google action"
    assert "google_enabled" in CANVAS, "the chip must only show when Google login is configured"


def test_preview_renders_delegation_from_its_real_keys_not_hardcoded_entra():
    # The YAML preview drawer must render each store's ACTUAL delegation fields. Hardcoding
    # tenant_id/${AUTH_*} would preview a google_refresh store with the wrong credentials
    # (the exact thing Task 8 Step 2 tells the tester to eyeball).
    for fn in ("function yamlHTML", "function yamlText"):
        i = CANVAS.find(fn)
        body = CANVAS[i:i + 1200]
        assert "AUTH_TENANT_ID" not in body, \
            f"{fn} hardcodes the Entra tenant ref in the delegation preview"
    assert "function delegHTML" in CANVAS and "function delegText" in CANVAS, \
        "the preview must build the delegation block from its real keys"


def test_signout_gates_on_signed_in_not_microsoft_only_enabled():
    """A Google-only deployment still mints a real session, so sign-out must be reachable.

    #643 moved WHERE this is enforced. It used to be the canvas's own renderAuth, and this
    test read that function for `if(authState.signed_in){` - the mechanism, not the property.
    The canvas has no identity chip any more; the shell's ONE account control renders for
    every surface, so the property is asserted there instead.

    The property is the same one #193 established and must not regress: the gate is
    `signed_in`, never `enabled`. `enabled` means "this box has Microsoft login configured",
    so gating on it would leave a Google-only deployment permanently unable to sign out and
    switch users."""
    account = (Path(__file__).resolve().parents[1]
               / "src/dbsearch/server/static/js/ui/account.js").read_text()
    i = account.find("export function renderAccount")
    body = account[i:]
    assert "if (!me.signed_in)" in body, \
        "the account control must decide from signed_in, not from a provider's enabled flag"
    assert "acct-signout" in body, "no sign-out control is rendered for a signed-in user"
    # The canvas must NOT have grown a second one back. Two controls answering "who am I"
    # on one screen is precisely the divergence #414 recorded and #643 removed.
    assert "signout" not in CANVAS, \
        "the canvas has a sign-out control again - identity belongs to ui/account.js alone"


for fn in [test_bigquery_kind_offers_require_signin,
           test_delegation_is_google_for_bigquery_and_entra_for_azure,
           test_connect_google_chip_is_wired_to_the_link_route]:
    fn()
