"""#630: who you are and what you are connected to are two different facts.

THE DEFECT the owner hit: /ask offers "Sign in", /canvas offers "Sign in with Microsoft", and
they are different acts. Signing in creates a SESSION; connecting a provider vaults a
credential DBSearch can redeem to read your documents. A user who had done the first but not
the second could not tell - so a signed-in user read as logged out, and the two screens looked
like they were contradicting each other.

FOUR PROVIDER CLAIMS, and blurring any two rebuilds the confusion:

  Connected            `linked` names it - a credential exists AND decrypts
  Not connected        wired on this deployment, simply not granted yet
  Not configured here  the implementation exists, this box lacks it (a client id; for
                       Amazon under ADR 0024, boto3 in the image)
  Not yet supported    no implementation exists at all (no roster row today - Amazon
                       graduated to a real capability with #666)

The last two are deliberately distinct: one is a deployment's choice an operator can change,
the other is a fact about the product that no amount of configuring will fix.

RULE 8 IS THE POINT OF THE LAST TEST. The shell once rendered a hardcoded "Signed in" without
checking for a token, so anonymous visitors were told they were signed in and then hit a 401
(#373). The mirror of that bug is asserting "Not signed in" when the server could not be
reached - so an unreachable /auth/me renders NOTHING.

    python3 tests/selftest_630_account_control.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import _domgate  # noqa: E402  the shared jsdom gate (#792)

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)

ACCOUNT = ROOT / "src/dbsearch/server/static/js/ui/account.js"
JSDOM = _domgate.JSDOM

BOOT = f"""
  import {{ pathToFileURL }} from "node:url";
  const {{ JSDOM }} = await import(pathToFileURL("{JSDOM.as_posix()}").href);
  const dom = new JSDOM("<!doctype html><html><body><span id=h></span></body></html>");
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.localStorage = {{ _v: {{}},
    getItem(k) {{ return k in this._v ? this._v[k] : null; }},
    setItem(k, v) {{ this._v[k] = String(v); }} }};
  const A = await import(pathToFileURL("{ACCOUNT.as_posix()}").href);
  const host = document.getElementById("h");
  const rows = () => [...host.querySelectorAll(".acct-provider")].map(r => ({{
    name: r.querySelector(".acct-provider-name").textContent,
    state: r.querySelector(".acct-provider-state").textContent,
    connect: !!r.querySelector("a.acct-connect"),
    action: (r.querySelector("a.acct-connect") || {{}}).textContent || "",
    href: (r.querySelector("a.acct-connect") || {{}}).getAttribute
          ? r.querySelector("a.acct-connect").getAttribute("href") : "",
  }}));
"""


def _have():
    """True: run the DOM check. False: a skip that `tests/_domgate.py` has already counted.

    Raises when node or jsdom is missing and `DBSEARCH_ALLOW_DOM_SKIP=1` was not set. Before
    #792 this returned a bare False and every caller then reported a PASS, so these guards were
    green no-ops on every clean clone and in CI."""
    return _domgate.gate("the account-control DOM check")


def _node(script):
    p = subprocess.run(["node", "--input-type=module", "-e", BOOT + script],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert p.returncode == 0, f"node failed:\n{p.stderr[-2000:]}"
    return json.loads(p.stdout)


def _skip():
    """The DOM half did not run. `_have` has already printed and counted why."""
    return True


def test_the_server_reports_which_provider_a_session_came_through():
    """The control says "Signed in with Microsoft", so the server has to KNOW that. It must
    not be inferred from an email domain - a gmail address proves nothing about how somebody
    authenticated."""
    body = client.get("/auth/me").json()
    assert "idp" in body, "/auth/me does not report the session's identity provider"
    assert "linked" in body, "/auth/me does not report which providers are connected"
    # ADR 0024: the Amazon row's enablement is implementation presence (boto3), reported by
    # the server - the roster must never have to guess what this box can validate.
    assert "aws_enabled" in body, "/auth/me does not report whether AWS keys can be held"
    src = (ROOT / "src/dbsearch/server/app.py").read_text()
    assert src.count('"idp": "entra"') == 1, "the Entra callback does not record its idp"
    assert src.count('"idp": "google"') == 1, "the Google callback does not record its idp"
    assert src.count('"idp": "local"') == 1, "the local sign-in does not record its idp"
    print("  PASS  every session mint records its provider, and /auth/me reports it")


def test_the_four_provider_states_are_four_different_sentences():
    if not _have():
        return _skip()
    out = _node("""
      A.renderAccount(host, { signed_in: true, name: "malcolm tan", email: "m@x.com",
        idp: "entra", enabled: true, google_enabled: false, local_enabled: true,
        aws_enabled: true, linked: ["entra"] }, { dev_auth: false });
      console.log(JSON.stringify(rows()));
    """)
    by = {r["name"]: r["state"] for r in out}
    assert by["Microsoft"] == "Connected", by
    # google_enabled false = the implementation exists, this box has no client id.
    assert by["Google"] == "Not configured here", by
    # ADR 0024 (#666): Amazon is a real capability now - boto3 present and nothing vaulted
    # is exactly "Not connected", the same sentence every other wired-but-ungranted row gets.
    assert by["Amazon"] == "Not connected", by
    # And a box whose image lacks boto3 must say so, not offer a form that would 501.
    out = _node("""
      A.renderAccount(host, { signed_in: true, name: "malcolm tan", email: "m@x.com",
        idp: "entra", enabled: true, google_enabled: false, local_enabled: true,
        aws_enabled: false, linked: ["entra"] }, { dev_auth: false });
      console.log(JSON.stringify(rows()));
    """)
    by = {r["name"]: r["state"] for r in out}
    assert by["Amazon"] == "Not configured here", by
    print("  PASS  connected / not connected / not configured here are distinct")


def test_a_wired_but_ungranted_provider_offers_the_action_that_fixes_it():
    if not _have():
        return _skip()
    out = _node("""
      A.renderAccount(host, { signed_in: true, name: "n", email: "e@x.com", idp: "google",
        enabled: true, google_enabled: true, local_enabled: true, linked: ["google"] },
        { dev_auth: false });
      console.log(JSON.stringify(rows()));
    """)
    ms = [r for r in out if r["name"] == "Microsoft"][0]
    goog = [r for r in out if r["name"] == "Google"][0]
    assert ms["state"] == "Not connected" and ms["connect"], ms
    assert goog["state"] == "Connected" and not goog["connect"], goog
    # Never offered for something that cannot be done (no aws_enabled in this stub).
    amazon = [r for r in out if r["name"] == "Amazon"][0]
    assert not amazon["connect"], "an unsupported provider offered a Connect link"
    print("  PASS  Connect is offered exactly where connecting is possible")


def test_the_amazon_row_offers_a_key_form_not_a_navigation():
    """ADR 0024 (#666): AWS has no linking URL - Connect is a BUTTON that reveals the key
    form parked under the roster, never an anchor (an <a> would also trip the menu's
    follow-a-link auto-close, #647, exactly when the panel must stay open)."""
    if not _have():
        return _skip()
    out = _node("""
      A.renderAccount(host, { signed_in: true, name: "n", email: "e@x.com", idp: "entra",
        enabled: true, google_enabled: false, local_enabled: true, aws_enabled: true,
        linked: ["entra"] }, { dev_auth: false });
      const row = [...host.querySelectorAll(".acct-provider")]
        .find(r => r.querySelector(".acct-provider-name").textContent === "Amazon");
      const btn = row.querySelector("button.acct-connect");
      const form = host.querySelector(".acct-aws");
      const before = form ? form.hidden : null;
      if (btn) btn.click();
      console.log(JSON.stringify({
        hasButton: !!btn, hasAnchor: !!row.querySelector("a.acct-connect"),
        formExists: !!form, hiddenBefore: before,
        hiddenAfter: form ? form.hidden : null,
        inputs: form ? form.querySelectorAll("input").length : 0,
        secretMasked: form
          ? [...form.querySelectorAll("input")].some(i => i.type === "password") : false }));
    """)
    assert out["hasButton"] and not out["hasAnchor"], out
    assert out["formExists"], "the key form is missing from the panel"
    assert out["hiddenBefore"] is True and out["hiddenAfter"] is False, out
    assert out["inputs"] == 2, out
    assert out["secretMasked"], "the secret key renders unmasked"
    print("  PASS  the Amazon Connect reveals an inline key form (masked secret)")


def test_the_hidden_key_form_is_actually_INVISIBLE_not_merely_flagged():
    """FOUND IN CHROME ON PROD, and this file could not see it.

    The test above asserts `form.hidden` - the PROPERTY. It was true, it stayed true, and the
    form was nonetheless rendered fully expanded on dbsearch.ai with nobody having pressed
    Connect: an author `display:` rule beats the UA stylesheet's `[hidden] { display: none }`
    regardless of specificity, so `.acct-aws { display: grid }` un-hid it.

    jsdom loads no CSS, so no amount of DOM assertion here can reach this. The checkable
    fact is in the stylesheet, so that is what is asserted: any rule giving a
    JS-toggled panel a `display`, without a matching `[hidden]` rule to switch it off, is
    the same bug with a different class name."""
    css = (ROOT / "src/dbsearch/server/static/css/app.css").read_text()
    import re as _re
    # Every class this control toggles through the `hidden` property. `.acct-menu` has
    # carried its own `[hidden]` rule since #630 and is listed to keep it honest - the guard
    # existed twenty lines above the bug and simply was not copied. Add to this list when a
    # new toggled panel gets a display rule (#667 tracks sweeping the other surfaces).
    for cls in (".acct-aws", ".acct-menu"):
        gives_display = _re.search(_re.escape(cls) + r"\s*\{[^}]*display\s*:", css)
        if not gives_display:
            continue
        guard = _re.search(_re.escape(cls) + r"\[hidden\]\s*\{[^}]*display\s*:\s*none", css)
        assert guard, (
            f"{cls} sets `display` but has no `{cls}[hidden] {{ display: none }}` rule. An "
            "author display rule OVERRIDES the UA [hidden] behaviour, so the panel renders "
            "while its hidden property reads true - which is exactly what shipped to prod")
    print("  PASS  a hidden key form is hidden in CSS too, not just in the DOM property")


def test_a_connected_amazon_row_reads_from_the_vault_and_offers_disconnect():
    """Same standard as every other row: Connected only because `linked` says "aws", the
    undo is the #652 disconnect, and the form does not exist in this state."""
    if not _have():
        return _skip()
    out = _node("""
      A.renderAccount(host, { signed_in: true, name: "n", email: "e@x.com", idp: "entra",
        enabled: true, google_enabled: false, local_enabled: true, aws_enabled: true,
        linked: ["entra", "aws"] }, { dev_auth: false });
      const row = [...host.querySelectorAll(".acct-provider")]
        .find(r => r.querySelector(".acct-provider-name").textContent === "Amazon");
      console.log(JSON.stringify({
        state: row.querySelector(".acct-provider-state").textContent,
        disconnect: !!row.querySelector("button.acct-disconnect"),
        formExists: !!host.querySelector(".acct-aws") }));
    """)
    assert out["state"] == "Connected", out
    assert out["disconnect"], "a Connected Amazon row has no way back (#652)"
    assert not out["formExists"], "the key form renders even though a credential is vaulted"
    print("  PASS  a connected Amazon row is vault-driven and disconnectable")


def test_connected_is_only_ever_said_when_the_vault_says_so():
    """`VAULT.linked()` refuses to report a credential it cannot decrypt, because telling a
    user they are connected to a cloud that will fail on first use is worse than saying
    nothing. The UI must not soften that by inferring connection from anything else."""
    if not _have():
        return _skip()
    out = _node("""
      // Signed in THROUGH Microsoft, but nothing vaulted: signing in is not connecting.
      A.renderAccount(host, { signed_in: true, name: "n", email: "e@x.com", idp: "entra",
        enabled: true, google_enabled: true, local_enabled: true, linked: [] },
        { dev_auth: false });
      console.log(JSON.stringify({ rows: rows(),
        idp: host.querySelector(".acct-idp").textContent }));
    """)
    ms = [r for r in out["rows"] if r["name"] == "Microsoft"][0]
    assert ms["state"] == "Not connected", (
        "signing in with Microsoft was mistaken for being connected to it - that is exactly "
        "the confusion this card exists to remove")
    assert out["idp"] == "Signed in with Microsoft", out["idp"]
    print("  PASS  signed in with a provider is not the same as connected to it")


def test_the_provider_you_signed_in_through_is_sent_to_sign_in_not_to_the_grant_flow():
    """#210, arriving here via #643 when the canvas's own auth chip was removed.

    A session authenticated THROUGH Microsoft with nothing vaulted for Microsoft is a real
    state and a costly one: every delegated ask fails with "sign in to query this source"
    while the control above says you are signed in. The canvas used to name it and offer one
    click; that chip is gone, and the capability must not have gone with it.

    What is asserted is the REMEDY, not a cause. The control cannot know whether the vault
    lost the credential or the sign-in never produced one - app.py vaults only
    `if u.get("refresh_token")` - and both are the same /auth/me payload, so the state stays
    the honest "Not connected". The two causes have the same fix, and that fix is /auth/login:
    the grant flow on Connectors has nothing to grant against when the credential missing is
    the one signing in would have minted.

    Google in the same payload is the control case. It is not the session's provider, it was
    simply never granted, and sending that user to /auth/login would be a dead end."""
    if not _have():
        return _skip()
    out = _node("""
      A.renderAccount(host, { signed_in: true, name: "n", email: "e@x.com", idp: "entra",
        enabled: true, google_enabled: true, linked: [] }, { dev_auth: false });
      console.log(JSON.stringify({ rows: rows() }));
    """)
    ms = [r for r in out["rows"] if r["name"] == "Microsoft"][0]
    goog = [r for r in out["rows"] if r["name"] == "Google"][0]

    assert ms["state"] == "Not connected", \
        f"the state must stay the one the control can verify, got {ms['state']!r}"
    assert ms["action"] == "Sign in again", (
        f"the provider this session came through offers {ms['action']!r}; the credential is "
        "minted by signing in, so anything else sends the user somewhere that cannot help")
    assert ms["href"] == "/auth/login", f"the remedy points at {ms['href']!r}, not sign-in"

    # #646 moved this. It used to assert `/canvas`, which was the defect rather than the
    # contract: Connectors has no per-provider grant flow - its auth area builds from exactly
    # one branch, `google_enabled` - so /canvas was a grant flow only for Google, only when
    # Google was on, and a dead end for Microsoft always. Each provider now points at its own
    # linking route. The CONTRACT this test protects is unchanged: a provider you simply never
    # granted goes to the grant flow, not to sign-in.
    assert goog["action"] == "Connect" and goog["href"] == "/auth/google/login", (
        "a provider that was simply never granted must still go to the grant flow; it was "
        f"sent to {goog['href']!r} instead")
    print("  PASS  the session's own provider offers sign-in; the others offer the grant flow")


def test_an_old_session_without_an_idp_says_something_true_anyway():
    if not _have():
        return _skip()
    out = _node("""
      A.renderAccount(host, { signed_in: true, name: "n", email: "e@x.com",
        enabled: true, google_enabled: true, local_enabled: true, linked: [] },
        { dev_auth: false });
      console.log(JSON.stringify({ idp: host.querySelector(".acct-idp").textContent }));
    """)
    assert out["idp"] == "Signed in", (
        f"a session predating the idp field was given a provider it never named: {out['idp']}")
    print("  PASS  a session with no recorded provider claims none")


def test_anonymous_offers_sign_in_at_the_sign_in_page():
    if not _have():
        return _skip()
    out = _node("""
      A.renderAccount(host, { signed_in: false, enabled: true, google_enabled: true,
        local_enabled: true, linked: [] }, { dev_auth: false });
      console.log(JSON.stringify({
        href: host.querySelector("a.acct-signin")?.getAttribute("href") || "",
        text: host.textContent.trim(),
        menus: host.querySelectorAll(".acct-menu").length,
      }));
    """)
    assert out["href"] == "/signin", out            # #628
    assert out["text"] == "Sign in", out
    assert out["menus"] == 0, "an anonymous visitor was given an account dropdown"
    print("  PASS  anonymous gets Sign in, pointing at /signin")


def test_an_unreachable_auth_me_renders_nothing_at_all():
    """The mirror of #373. Asserting "Not signed in" when the server could not be asked is
    the same class of lie as the hardcoded "Signed in" that started all this."""
    if not _have():
        return _skip()
    out = _node("""
      host.innerHTML = "<span>stale</span>";
      A.renderAccount(host, null, { dev_auth: false });
      console.log(JSON.stringify({ html: host.innerHTML }));
    """)
    assert out["html"] == "", (
        f"a page that could not reach /auth/me still claimed something: {out['html']!r}")
    print("  PASS  unreachable means silent, never a guess in either direction")


def test_every_provider_row_contributes_the_same_number_of_cells():
    """#644: the roster is ONE grid and the rows are `display: contents`, so a row that emits
    fewer children than the others does not just look sparse - it shifts every row after it.

    THE DEFECT, seen in a browser: with a provider connected there is no action pill, the row
    emitted two children instead of three, and grid auto-placement pulled the next row's name
    up into the empty third track. The list folded into a snake reading "Microsoft | Connected
    | Google". It was invisible while testing the not-connected case, because there the only
    two-child row was the LAST one and there was nothing left to pull up.

    So this asserts the invariant rather than the appearance: same child count on every row,
    whatever the four states happen to be. Asserting the rendered columns would need a layout
    engine; asserting the cell count catches the same regression in jsdom."""
    if not _have():
        return _skip()
    out = _node("""
      const counts = (me) => {
        A.renderAccount(host, me, { dev_auth: false });
        return [...host.querySelectorAll(".acct-provider")].map(r => r.children.length);
      };
      console.log(JSON.stringify({
        none: counts({ signed_in: true, name: "n", email: "e@x.com", idp: "entra",
                       enabled: true, google_enabled: true, linked: [] }),
        all:  counts({ signed_in: true, name: "n", email: "e@x.com", idp: "entra",
                       enabled: true, google_enabled: true, linked: ["entra", "google"] }),
        unconfigured: counts({ signed_in: true, name: "n", email: "e@x.com", idp: "local",
                               enabled: false, google_enabled: false, linked: [] }),
      }));
    """)
    for case, got in out.items():
        assert len(set(got)) == 1, (
            f"the {case} roster emits rows of differing width {got} - on a shared grid that "
            "slides every following row into the gap, which is #644 exactly")
        assert got[0] == 3, f"the {case} roster emits {got[0]} cells per row, not 3"
    print("  PASS  every provider row emits the same three cells, in every state")


def test_the_dropdown_opens_and_closes():
    if not _have():
        return _skip()
    out = _node("""
      A.renderAccount(host, { signed_in: true, name: "malcolm tan", email: "m@x.com",
        idp: "entra", enabled: true, google_enabled: true, local_enabled: true,
        linked: ["entra"] }, { dev_auth: false });
      const btn = host.querySelector(".acct-avatar");
      const menu = host.querySelector(".acct-menu");
      const start = menu.hidden;
      btn.dispatchEvent(new window.Event("click", { bubbles: true }));
      const opened = !menu.hidden;
      const expanded = btn.getAttribute("aria-expanded");
      document.body.dispatchEvent(new window.Event("click", { bubbles: true }));
      console.log(JSON.stringify({ start, opened, expanded, closed: menu.hidden,
                                   initials: btn.textContent }));
    """)
    assert out["start"] is True, "the menu starts open"
    assert out["opened"] and out["expanded"] == "true", out
    assert out["closed"], "clicking away left the menu open over the page"
    assert out["initials"] == "MT", f"the avatar is not initials: {out['initials']!r}"
    print("  PASS  the dropdown opens, reports aria-expanded, and closes on click-away")


def test_following_a_link_in_the_panel_closes_it():
    """#647: the panel used to stay open on top of the surface it had just navigated to.

    The click-away guard exempts everything inside the control, which is right for the theme
    toggle and wrong for the two links - they leave the surface behind them. #643 is what
    exposed it: once every destination became an in-document route, following a link stopped
    tearing the document down, so nothing closed the panel.

    Asserted on the LINK, not on the theme button, because the distinction is the fix: a click
    on a control that stays on this page must NOT close the panel."""
    if not _have():
        return _skip()
    out = _node("""
      A.renderAccount(host, { signed_in: true, name: "n", email: "e@x.com", idp: "local",
        enabled: true, google_enabled: true, local_enabled: true, linked: [] },
        { dev_auth: false });
      const btn = host.querySelector(".acct-avatar");
      const menu = host.querySelector(".acct-menu");
      const click = (el) => el.dispatchEvent(new window.Event("click", { bubbles: true }));
      click(btn);
      const openBeforeTheme = !menu.hidden;
      click(host.querySelector(".acct-theme"));
      const stillOpenAfterTheme = !menu.hidden;
      click(host.querySelector("a.acct-connect"));
      console.log(JSON.stringify({ openBeforeTheme, stillOpenAfterTheme,
                                   closedAfterLink: menu.hidden }));
    """)
    assert out["openBeforeTheme"], out
    assert out["stillOpenAfterTheme"], (
        "the theme toggle closed the panel - it does not navigate, so the panel must stay")
    assert out["closedAfterLink"], (
        "following a link left the panel open over the surface it navigated to - that is #647")
    print("  PASS  a link closes the panel; a control that stays on the page does not")


def test_the_avatar_never_shows_a_raw_oid():
    """`shortLabel` used to render an Entra oid as "a1b2c3d4…" - a machine identifier shown
    to a person as though it meant something. /auth/me carries name and email."""
    if not _have():
        return _skip()
    out = _node("""
      A.renderAccount(host, { signed_in: true, name: "", email: "",
        oid: "6f1c2b9e-40aa-4d1e-9d3f-77aa1b2c3d4e",
        enabled: true, google_enabled: true, local_enabled: true, linked: [] },
        { dev_auth: false });
      console.log(JSON.stringify({ text: host.textContent }));
    """)
    assert "6f1c2b9e" not in out["text"], f"the raw oid reached the page: {out['text']!r}"
    print("  PASS  no raw oid is ever rendered at a human")


def test_the_topbar_has_one_control_not_three():
    html = client.get("/ask").text
    assert 'id="account"' in html, "the account control has no host in the shell"
    for gone in ('id="theme-toggle"', 'id="sign-out"', 'id="identity"'):
        assert gone not in html, f"{gone} is still a separate topbar control"
    main = client.get("/static/js/main.js").text
    assert "renderAccount" in main and "mountDevSwitcher" in main, \
        "the shell does not mount the account control"
    assert main.index("renderAccount") < main.index("mountDevSwitcher("), (
        "the dev switcher mounts BEFORE the control that creates its slot - the control "
        "would then clear it")
    print("  PASS  one control in the topbar, mounted in the right order")


if __name__ == "__main__":
    test_the_server_reports_which_provider_a_session_came_through()
    test_the_four_provider_states_are_four_different_sentences()
    test_a_wired_but_ungranted_provider_offers_the_action_that_fixes_it()
    test_the_amazon_row_offers_a_key_form_not_a_navigation()
    test_the_hidden_key_form_is_actually_INVISIBLE_not_merely_flagged()
    test_a_connected_amazon_row_reads_from_the_vault_and_offers_disconnect()
    test_connected_is_only_ever_said_when_the_vault_says_so()
    test_the_provider_you_signed_in_through_is_sent_to_sign_in_not_to_the_grant_flow()
    test_an_old_session_without_an_idp_says_something_true_anyway()
    test_anonymous_offers_sign_in_at_the_sign_in_page()
    test_an_unreachable_auth_me_renders_nothing_at_all()
    test_every_provider_row_contributes_the_same_number_of_cells()
    test_the_dropdown_opens_and_closes()
    test_following_a_link_in_the_panel_closes_it()
    test_the_avatar_never_shows_a_raw_oid()
    test_the_topbar_has_one_control_not_three()
    print("\nACCOUNT CONTROL SELF-TEST PASSED.")
