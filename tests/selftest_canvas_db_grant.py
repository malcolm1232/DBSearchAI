"""Self-test: the canvas offers the #429 incremental database-consent round, and offers it
only where it means something.

Structural checks over js/surfaces/canvas.js, same style as the other canvas selftests. Sign-in no
longer requests the Azure SQL delegation (asking up front AADSTS650052'd any org with no
Azure SQL service principal - they could not sign in at all), so the canvas is now the ONLY
place a user can grant it. If this button disappears in a refactor, query-as-user silently
becomes unreachable for every new organisation, and nothing else in the suite would notice.

    python3 tests/selftest_canvas_db_grant.py
"""
import sys
from pathlib import Path

CANVAS = (Path(__file__).resolve().parents[1]
          / "src" / "dbsearch" / "server" / "static" / "js" / "surfaces" / "canvas.js")
html = CANVAS.read_text()


def test_the_grant_round_is_reachable_from_a_node():
    assert "dbGrantButton" in html, "no DB-grant affordance on the canvas at all"
    assert "/auth/grant/db-url" in html, "the grant button must call the JSON grant endpoint"
    assert ".db-grant" in html, "the button needs a hook the click handler can find"


def test_it_is_offered_only_for_delegated_database_nodes():
    """A node using a STORED credential delegates nothing, so asking its owner to approve
    Microsoft database access would be a consent prompt for a flow that will never run."""
    assert "require_signin" in html.split("function dbGrantButton")[1][:600], \
        "the button must be gated on require_signin"
    for kind in ("azure_sql", "postgres", "mysql", "synapse", "cosmos_db"):
        assert kind in html.split("DB_DELEGATED_KINDS")[1][:300], f"{kind} missing from the DB family"
    assert "sharepoint" not in html.split("DB_DELEGATED_KINDS")[1][:300], \
        "SharePoint has its own connect flow - it must not be in the DB-grant family"


def test_expected_states_are_explained_not_leaked():
    """401 (nothing to upgrade) and 503 (no tenant app on this deployment) are expected
    operator/user states. #297's law: say what happened, never leak an env-var name, and
    never use a blocking dialog."""
    handler = html.split('querySelector(".db-grant")')[1][:1600]
    assert "401" in handler and "503" in handler, "the two expected states must be handled"
    assert "toast(" in handler, "failures must use the non-blocking toast (#297)"
    assert "alert(" not in handler, "a blocking dialog freezes the whole canvas (#297)"
    assert "AUTH_CLIENT" not in handler and "AUTH_TENANT" not in handler, \
        "never leak env-var names to the user"


def test_the_button_re_enables_after_a_failure():
    """A dead-end disabled button is how a user concludes the product is broken."""
    handler = html.split('querySelector(".db-grant")')[1][:1600]
    assert "disabled=false" in handler.replace(" ", ""), \
        "the catch must re-enable the button so the user can retry"



def test_a_chrome_changing_config_edit_rerenders_the_node():
    """require_signin gates the grant button, so editing it MUST re-render the node. Without
    this the button appeared only after a Compose-up and a page reload - the control that is
    meant to be the obvious next step was invisible at the moment the user enabled it.

    On `change` (blur), never on `input`: renderAll() rebuilds the DOM and would steal focus
    mid-word."""
    assert "CHROME_FIELDS" in html, "no notion of config fields that affect node chrome"
    # from the declaration to the end of the [data-cfg] wiring block
    block = html.split("const CHROME_FIELDS")[1].split("[data-sechint]")[0]
    assert "require_signin" in block, "require_signin must be a chrome field"
    assert 'addEventListener("change"' in block, "must re-render on change (blur), not input"
    assert "renderAll()" in block
    assert 'addEventListener("input"' in block, "the plain persistence path must survive"

if __name__ == "__main__":
    test_the_grant_round_is_reachable_from_a_node()
    test_it_is_offered_only_for_delegated_database_nodes()
    test_expected_states_are_explained_not_leaked()
    test_the_button_re_enables_after_a_failure()
    test_a_chrome_changing_config_edit_rerenders_the_node()
    print("OK selftest_canvas_db_grant")
