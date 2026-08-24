"""#648: every way of signing in, crossed with every provider you might connect.

WHY THIS EXISTS. #646 was found by hand, by the owner, on the one cell nobody had looked at.
Every prior check used a Microsoft session - where the credential is vaulted at
`/auth/callback` and the row reads "Connected" - so the email-session-to-Microsoft cell had
never been rendered. One unexercised cell hid a defect where a signed-in user is silently
re-principaled into a different account.

So the matrix is the artefact, not the anecdote. This file enumerates every
(session idp) x (provider) x (deployment flags) combination through the REAL renderAccount,
and states, for each, what the panel says, what it offers, and where that offer goes.

IT ALSO COVERS PROVIDERS AHEAD OF THEIR WIRING - and that design paid out twice: Google
was in the matrix before prod had a client id (#649 turned it on), and Amazon was in it
before ADR 0024 (#666) made it a real capability. Amazon's rows are live now: `aws_enabled`
is implementation presence (boto3 in the image), the credential is the caller's own vaulted
access keys, and the Connect affordance is a key-form BUTTON, never an anchor - there is no
linking URL to send anyone to.

THE PART THAT MATTERS MOST: this must not become a test that protects the bug. The broken
cells are named in BROKEN_BY_646 and asserted to be EXACTLY that set. Fix #646 and this file
fails - loudly, naming the cell that changed - which is the point. A test that quietly
asserted today's behaviour would make the defect permanent.

    python3 tests/selftest_648_login_permutations.py
    python3 tests/selftest_648_login_permutations.py --print   # just show the matrix
"""
import json
import subprocess
import sys
from pathlib import Path

import _domgate  # the shared jsdom gate (#792)

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = ROOT / "src/dbsearch/server/static/js/ui/account.js"
JSDOM = _domgate.JSDOM
APP = ROOT / "src/dbsearch/server/app.py"
SIGNIN = ROOT / "src/dbsearch/server/static/signin.html"

# Every way a session can come to exist. "none" is the anonymous visitor; the rest are the
# `idp` values the three mints actually record (app.py: "entra" / "google" / "local").
# There is deliberately NO "amazon" session: the owner ruled the Amazon row means AWS as a
# data source, and ADR 0024 rejected Login with Amazon - no mint can ever record that idp,
# so a row for it would assert fiction (the same reason unreachable vault combinations are
# skipped below).
SESSIONS = [
    ("none",   None),
    ("local",  {"idp": "local",  "name": "avery",       "email": "avery@example.com"}),
    ("entra",  {"idp": "entra",  "name": "Avery Quinn", "email": "a@x.onmicrosoft.com"}),
    ("google", {"idp": "google", "name": "Avery",       "email": "a@gmail.com"}),
]

# What the deployment has client ids for - plus, since #666, whether the image can hold AWS
# keys at all (aws_enabled = boto3 present, not a client id). entra-off doubles as the
# no-boto3 archetype so the matrix keeps a "Not configured here" Amazon cell.
DEPLOYMENTS = {
    "prod-today":  {"enabled": True,  "google_enabled": False, "local_enabled": True,
                    "aws_enabled": True},
    "google-on":   {"enabled": True,  "google_enabled": True,  "local_enabled": True,
                    "aws_enabled": True},
    "entra-off":   {"enabled": False, "google_enabled": True,  "local_enabled": True,
                    "aws_enabled": False},
}

# Which credentials the vault holds. Cross this with the sessions above. "aws" arrives via
# /auth/aws/connect (a form, not a sign-in), so it composes with every session idp.
VAULTS = {"nothing": [], "entra": ["entra"], "google": ["google"], "both": ["entra", "google"],
          "aws": ["aws"], "aws+own": ["entra", "google", "aws"]}

# THE KNOWN-BROKEN SET (#646). Each entry is (session idp, provider) where the panel offers
# "Connect" pointing at /canvas for a session that did NOT come through Entra.
#
# The offer cannot succeed. /canvas's `renderAuth()` emits ONLY the Google pill, gated on
# google_enabled, so on the production box that area renders an empty string and nothing on
# the surface connects Microsoft. And the one route that does vault an Entra credential
# (/auth/login -> /auth/callback) never reads the existing session, so it REPLACES the
# principal rather than linking to it.
#
# FIXED 260812 (ADR 0023). The set is now EMPTY, and that is the assertion.
#
# The guard below did its job: it failed the moment #646 was fixed and named the cells that
# moved, which is why this set is being emptied by hand rather than discovered stale months
# later. Each of the three cells now offers Connect -> /auth/entra/link, a route that requires
# a session and hangs the credential off the identity you are ALREADY signed in as, instead of
# Connect -> /canvas, a surface with no Microsoft grant on it at all.
#
# Kept as an empty set rather than deleted: the assertion "no cell offers Connect -> /canvas"
# is a live invariant worth holding, and a future provider added with a lazy /canvas href
# should fail here.
BROKEN_BY_646 = set()

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
"""


def _have():
    """True: run the DOM check. False: a skip that `tests/_domgate.py` has already counted.

    Raises when node or jsdom is missing and `DBSEARCH_ALLOW_DOM_SKIP=1` was not set. Before
    #792 this returned a bare False and every caller then reported a PASS, so these guards were
    green no-ops on every clean clone and in CI."""
    return _domgate.gate("the login-permutations DOM check")


def _skip():
    """The DOM half did not run. `_have` has already printed and counted why."""
    return True


def _node(script):
    p = subprocess.run(["node", "--input-type=module", "-e", BOOT + script],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert p.returncode == 0, f"node failed:\n{p.stderr[-2000:]}"
    return json.loads(p.stdout)


def matrix():
    """Render every cell once and return it as a flat list of dicts.

    One node process for the whole matrix rather than one per cell: the point of the harness
    is that adding a provider or a deployment costs a line in a table up top, not a rewrite.
    """
    cases = []
    for dep_name, flags in DEPLOYMENTS.items():
        for sess_name, sess in SESSIONS:
            for vault_name, linked in VAULTS.items():
                # An anonymous visitor has no vault, and a vault entry for a provider you
                # never authenticated with is not reachable today; skip the combinations
                # that cannot exist rather than assert fiction about them.
                if sess is None and vault_name != "nothing":
                    continue
                # Some cells are UNREACHABLE today and kept on purpose - "local session
                # holding an entra credential" is precisely what #646 makes impossible. They
                # show what the panel will say once linking exists, so the fix can be checked
                # against a matrix that already contains its own expected outcome.
                cases.append({"deployment": dep_name, "session": sess_name,
                              "vault": vault_name,
                              "me": (None if sess is None
                                     else {"signed_in": True, **flags, **sess,
                                           "linked": linked})})
    out = _node(f"""
      const CASES = {json.dumps(cases)};
      const rendered = CASES.map((c) => {{
        A.renderAccount(host, c.me === null
          ? {{ signed_in: false, enabled: true, google_enabled: false, local_enabled: true,
               linked: [] }}
          : c.me, {{ dev_auth: false }});
        const signin = host.querySelector("a.acct-signin");
        return {{
          ...c,
          me: undefined,
          anonymous: !!signin,
          signinHref: signin ? signin.getAttribute("href") : "",
          idpLine: (host.querySelector(".acct-idp") || {{}}).textContent || "",
          rows: [...host.querySelectorAll(".acct-provider")].map((r) => {{
            const a = r.querySelector("a.acct-connect");
            return {{ provider: r.querySelector(".acct-provider-name").textContent,
                      says: r.querySelector(".acct-provider-state").textContent,
                      offers: a ? a.textContent : "",
                      goesTo: a ? a.getAttribute("href") : "",
                      cells: r.children.length }};
          }}),
        }};
      }});
      console.log(JSON.stringify(rendered));
    """)
    return out


def print_matrix(m):
    w = lambda s, n: str(s).ljust(n)  # noqa: E731
    print()
    print(w("DEPLOYMENT", 13) + w("SESSION", 9) + w("VAULT", 9) + w("PROVIDER", 11)
          + w("PANEL SAYS", 21) + w("OFFERS", 15) + "GOES TO")
    print("-" * 108)
    last = None
    for c in m:
        if c["anonymous"]:
            print(w(c["deployment"], 13) + w(c["session"], 9) + w(c["vault"], 9)
                  + w("(no panel)", 11) + w("Sign in", 21) + w("Sign in", 15) + c["signinHref"])
            continue
        for r in c["rows"]:
            key = (c["deployment"], c["session"], c["vault"])
            head = ("" if key == last else
                    w(c["deployment"], 13) + w(c["session"], 9) + w(c["vault"], 9))
            if key != last:
                print()
                last = key
            else:
                head = w("", 31)
            # Mark the row only when this row IS the dead offer. Keying the marker on
            # (session, provider) alone flagged "local + entra vault -> Connected" as broken,
            # which is a row with no offer on it at all - a printed artefact that lied about
            # its own subject.
            broken = ((c["session"], r["provider"]) in BROKEN_BY_646
                      and r["goesTo"] == "/canvas")
            flag = "   <-- #646 dead offer" if broken else ""
            print(head + w(r["provider"], 11) + w(r["says"], 21)
                  + w(r["offers"] or "-", 15) + (r["goesTo"] or "-") + flag)
    print()


def test_every_cell_emits_three_grid_cells():
    """#644's invariant, held across the whole matrix rather than the three states that
    happened to be on screen when it was written."""
    if not _have():
        return _skip()
    for c in matrix():
        if c["anonymous"]:
            continue
        widths = {r["cells"] for r in c["rows"]}
        assert widths == {3}, (
            f"{c['deployment']}/{c['session']}/{c['vault']} emits rows of width {widths}; on a "
            "shared grid that slides every following row into the gap (#644)")
    print("  PASS  every row of every cell emits three grid cells")


def test_amazon_renders_the_real_capability_states_and_never_an_anchor():
    """ADR 0024 (#666): Amazon is a real capability, and its states follow the same law as
    every other row - Connected only when the vault says "aws", Not connected when the box
    can hold keys and none are vaulted, Not configured here when the image lacks boto3.

    The offer is different in KIND, and that is asserted across the whole matrix: there is
    no linking URL for AWS, so Amazon must never emit an <a> pill (`offers` reads
    a.acct-connect) - its Connect is the key-form button, covered in selftest_630."""
    if not _have():
        return _skip()
    for c in matrix():
        if c["anonymous"]:
            continue
        amazon = [r for r in c["rows"] if r["provider"] == "Amazon"][0]
        aws_on = DEPLOYMENTS[c["deployment"]]["aws_enabled"]
        held = "aws" in VAULTS[c["vault"]]
        # Connected is a VAULT fact and outranks the deployment flag - "a credential exists
        # AND decrypts" - the same precedence every provider row has (a vaulted Google
        # credential on a google-disabled box also says Connected).
        want = ("Connected" if held
                else ("Not connected" if aws_on else "Not configured here"))
        assert amazon["says"] == want, (
            f"Amazon claimed {amazon['says']!r} in {c['deployment']}/{c['session']}/"
            f"{c['vault']} - expected {want!r}")
        assert not amazon["offers"], (
            f"Amazon offered the anchor {amazon['offers']!r} -> {amazon['goesTo']!r} - AWS "
            "has no linking URL; its Connect must be the key-form button")
    print("  PASS  Amazon's states are vault-and-capability honest, never an anchor")


def test_connected_is_said_only_where_the_vault_holds_that_provider():
    if not _have():
        return _skip()
    for c in matrix():
        if c["anonymous"]:
            continue
        held = VAULTS[c["vault"]]
        for r in c["rows"]:
            key = {"Microsoft": "entra", "Google": "google", "Amazon": "aws"}[r["provider"]]
            if r["says"] == "Connected":
                assert key in held, (
                    f"{c['deployment']}/{c['session']}: {r['provider']} claimed Connected with "
                    f"vault {held} - Connected may only ever restate what the vault reports")
            elif key in held:
                # Held but not reported: only legitimate when the deployment cannot use it.
                assert r["says"] in ("Not configured here", "Not yet supported"), (
                    f"{c['deployment']}/{c['session']}: {r['provider']} holds a credential but "
                    f"the panel said {r['says']!r}")
    print("  PASS  Connected is never said without a credential behind it")


def test_the_session_provider_is_sent_to_sign_in_and_the_others_to_the_grant_flow():
    """#210's remedy split. The provider you authenticated THROUGH mints its credential BY
    signing in, so /auth/login is the fix; the grant flow would be a dead end there. For any
    other provider it is the reverse."""
    if not _have():
        return _skip()
    for c in matrix():
        if c["anonymous"]:
            continue
        for r in c["rows"]:
            if not r["offers"]:
                continue
            key = {"Microsoft": "entra", "Google": "google", "Amazon": "aws"}[r["provider"]]
            if key == c["session"]:
                assert (r["offers"], r["goesTo"]) == ("Sign in again", "/auth/login"), (
                    f"{c['session']} session, own provider {r['provider']}: offered "
                    f"{r['offers']!r} -> {r['goesTo']!r}, expected Sign in again -> /auth/login")
            else:
                # #646/ADR 0023: each provider's OWN linking route, never /canvas. Derived
                # from the provider rather than hardcoded, so adding a provider to the ROSTER
                # without a `connect` target fails here instead of silently inheriting a dead
                # end.
                want = {"Microsoft": "/auth/entra/link",
                        "Google": "/auth/google/login"}[r["provider"]]
                assert (r["offers"], r["goesTo"]) == ("Connect", want), (
                    f"{c['session']} session, {r['provider']}: offered {r['offers']!r} -> "
                    f"{r['goesTo']!r}, expected Connect -> {want}")
    print("  PASS  own-provider offers sign-in; every other provider offers the grant flow")


def test_the_broken_cells_are_exactly_the_ones_646_names():
    """THE GUARD THAT MUST NOT BE SOFTENED.

    A cell is broken when the panel offers Connect -> /canvas to a session that did not come
    through Entra: the canvas has no Microsoft grant to offer (renderAuth emits only the
    Google pill), and the only route that vaults an Entra credential replaces the principal
    instead of linking to it.

    This asserts the set EXACTLY, in both directions. A new broken cell fails it. Fixing one
    ALSO fails it - deliberately - so nobody can quietly leave a stale entry behind and call
    the matrix green."""
    if not _have():
        return _skip()
    seen = set()
    for c in matrix():
        if c["anonymous"] or c["session"] == "entra":
            continue
        for r in c["rows"]:
            if r["provider"] == "Microsoft" and r["goesTo"] == "/canvas":
                seen.add((c["session"], r["provider"]))
    assert seen == BROKEN_BY_646, (
        f"the broken set moved.\n  now broken: {sorted(seen)}\n  expected  : "
        f"{sorted(BROKEN_BY_646)}\nIf you fixed #646, delete the fixed entries from "
        "BROKEN_BY_646 and add the positive assertion for the new behaviour.")
    print(f"  PASS  the broken set is exactly #646's {sorted(seen)} - no more, no fewer")


def test_the_canvas_still_cannot_deliver_what_connect_promises():
    """The other half of #646, asserted at the source rather than in a screenshot.

    `renderAuth()` on the canvas builds its html from ONE branch, `authState.google_enabled`.
    There is no Microsoft branch, so for the production box (google off) that area renders an
    empty string - which is what the owner hit. If a Microsoft grant is ever added there, this
    fails and the BROKEN_BY_646 set above should be revisited in the same change."""
    canvas = (ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js").read_text()
    start = canvas.index("function renderAuth()")
    body = canvas[start:canvas.index("\n  }", start)]
    # Comments only, stripped: the first draft of this test matched the word "Microsoft" in
    # the line "needs a linked Google account regardless of Microsoft state" and failed on
    # prose. Assert on what the function EMITS, which is the only thing a user can click.
    code = "\n".join(ln.split("//")[0] for ln in body.splitlines())
    assert "google_enabled" in code, "renderAuth no longer gates on google_enabled - re-read it"
    assert "/auth/login" not in code and "Microsoft" not in code, (
        "the canvas now emits something Microsoft-shaped in its auth area. That may be the "
        "#646 fix - if so, update BROKEN_BY_646 and this test together")
    print("  PASS  the canvas auth area still offers Google only, so Connect has no target")


def test_google_is_wired_end_to_end_and_only_waiting_on_a_client_id():
    """Google needs no new UI work: the sign-in page, the routes and the linking all exist.
    This is what makes the google-on rows of the matrix real rather than hypothetical."""
    app = APP.read_text()
    signin = SIGNIN.read_text()
    assert "/auth/google/login" in signin and "google_enabled" in signin, (
        "the sign-in page does not offer Google behind its enabled flag")
    assert '@app.get("/auth/google/login")' in app and '@app.get("/auth/google/callback")' in app
    # The linking behaviour Entra lacks (#646), asserted so a refactor cannot quietly drop it.
    cb = app[app.index('@app.get("/auth/google/callback")'):]
    cb = cb[:cb.index("\n@app.")]
    assert "read_session" in cb, (
        "the Google callback no longer reads the existing session - that is the account "
        "linking (#193 / ADR 0013 decision 4) whose absence in the Entra callback IS #646")
    print("  PASS  Google is implemented end to end; only a client id is missing")


def test_amazon_is_honest_about_itself():
    """Rewritten 260812 (#666, ADR 0024) - this test used to hold the OPPOSITE facts: that
    Amazon's roster entry was deliberately flagless and target-less and that no routes
    existed behind it. The owner's ruling (the Amazon row means AWS AS A DATA SOURCE, never
    Login with Amazon) became ADR 0024, and the facts flipped with it. The guard did its
    job: it failed the moment the capability landed and was rewritten by hand in the same
    change, exactly like BROKEN_BY_646 before it.

    The source-level facts now: the entry is live behind `aws_enabled` (implementation
    presence - boto3 - not a client id), it STILL has no `connect` URL because AWS has no
    linking redirect (the Connect affordance is the key-form button, `keyEntry`), and the
    routes behind the offer exist - /auth/aws/connect to vault, /auth/disconnect/aws via
    KNOWN_IDPS to undo.

    Matched with a tolerant regex rather than the literal entry text (this file's own
    lesson: the first version pinned the exact string and broke on a formatting change)."""
    import re as _re
    acct = ACCOUNT.read_text()
    roster = acct[acct.index("const ROSTER = ["):acct.index("];", acct.index("const ROSTER = ["))]
    entry = _re.search(r'\{[^{}]*key:\s*"aws"[^{}]*\}', roster)
    assert entry, f"no aws entry in the ROSTER:\n{roster}"
    body = entry.group(0)
    assert _re.search(r'enabledFlag:\s*"aws_enabled"', body), (
        f"Amazon's row is not gated on aws_enabled ({body}) - the panel would offer a form "
        "the deployment cannot validate (the 501 path)")
    assert _re.search(r"connect:\s*null", body), (
        f"Amazon has a connect URL ({body}) - no linking redirect exists for AWS; the offer "
        "must be the key-form button, or it navigates somewhere with nothing behind it")
    assert _re.search(r"keyEntry:\s*true", body), (
        f"Amazon's entry lost its keyEntry marker ({body}) - Connect would render as a dead "
        "hairline anchor to /canvas, which is #646's exact shape")
    app = APP.read_text()
    assert '"/auth/aws/connect"' in app, (
        "the roster offers an AWS key form but app.py has no /auth/aws/connect route - an "
        "offer with nothing behind it")
    assert '"aws_enabled"' in app, ("/auth/me does not report aws_enabled")
    assert "get_caller_identity" in app, (
        "the connect route no longer falsifies keys against STS before vaulting - an "
        "unvalidated put reports Connected and dies at first query")
    print("  PASS  Amazon is a live capability: gated, buttoned, and routed for real")


if __name__ == "__main__":
    if "--print" in sys.argv:
        if not _have():
            sys.exit("node or jsdom unavailable")
        print_matrix(matrix())
        sys.exit(0)
    print("LOGIN PERMUTATION MATRIX (#648)")
    test_every_cell_emits_three_grid_cells()
    test_amazon_renders_the_real_capability_states_and_never_an_anchor()
    test_connected_is_said_only_where_the_vault_holds_that_provider()
    test_the_session_provider_is_sent_to_sign_in_and_the_others_to_the_grant_flow()
    test_the_broken_cells_are_exactly_the_ones_646_names()
    test_the_canvas_still_cannot_deliver_what_connect_promises()
    test_google_is_wired_end_to_end_and_only_waiting_on_a_client_id()
    test_amazon_is_honest_about_itself()
    print("\nLOGIN PERMUTATION SELF-TEST PASSED.")
    print("Run with --print to see the matrix itself.")
