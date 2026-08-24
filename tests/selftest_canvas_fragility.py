"""Fragility guards for the canvas (#198, #199, #200).

Each of these shipped as a trap that made a WORKING system look broken, which is worse than
an outright failure: every affirmative signal said "fine" while the thing could not work.

Run: python3 tests/selftest_canvas_fragility.py
"""
import os
import re
import sys
from pathlib import Path

os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _upload import settle  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)
ALICE = {"X-DBSearch-User": "alice"}
CANVAS = (ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js").read_text()


def test_store_without_an_acl_is_refused_and_the_reason_is_actionable():
    """#200: LAW 2 is default-deny, so an ACL-less store is visible to NOBODY. Composing it
    anyway was the worst trap in the product: the node reported 'Connection healthy — a record
    round-tripped', went green and `live`, and then every ask said 'I couldn't find anything
    you have access to about that.' A store that can never be reached must never look healthy."""
    manifest = {"tenant": "acme", "stores": [
        {"id": "no-acl", "kind": "csv", "mode": "pushdown", "business_unit": "sales",
         "acl": [], "title": "orphan", "description": "sales rows",
         "config": {"tables": {"sales": {"columns": ["region", "amount"],
                                         "rows": [["apac", 10]]}}}},
        {"id": "has-acl", "kind": "csv", "mode": "pushdown", "business_unit": "sales",
         "acl": ["all-staff"], "title": "fine", "description": "sales rows",
         "config": {"tables": {"sales": {"columns": ["region", "amount"],
                                         "rows": [["apac", 10]]}}}},
    ]}
    r = client.post("/router/compose", headers=ALICE, json={"manifest": manifest})
    assert r.status_code == 200, r.text
    body = r.json()

    composed = {s["store_id"] for s in body["stores"]}
    assert "has-acl" in composed, body
    assert "no-acl" not in composed, "an ACL-less store must NOT compose as if it worked"

    skipped = {s["id"]: s["reason"] for s in body["skipped"]}
    assert "no-acl" in skipped, body
    reason = skipped["no-acl"].lower()
    assert "acl" in reason, reason
    # the remedy must be in the message — "invalid" tells the user nothing they can act on
    assert "add the principals" in reason or "oid" in reason, reason


def test_config_does_not_advertise_dev_auth_when_a_real_login_is_configured():
    """#198: with AUTH_*/GOOGLE_* set, resolve_identity REFUSES the X-DBSearch-User dev header
    (#183). Advertising dev_auth anyway made the canvas paint an alice/bob picker that could
    never authenticate — pick an identity, ask, get 401. Never offer what you will reject."""
    import dbsearch.server.app as appmod

    cfg = client.get("/config", headers=ALICE).json()
    assert cfg["dev_auth"] is True, "dev-auth server should still offer the switcher"

    real_login = [appmod.user_auth.is_enabled, appmod.google_auth.is_enabled]
    appmod.user_auth.is_enabled = lambda: True         # pretend a real login is configured
    try:
        cfg = client.get("/config", headers=ALICE).json()
        assert cfg["dev_auth"] is False, "dev switcher must be withdrawn under a real login"
        assert cfg["users"] == [], f"and it must not name users it will reject: {cfg['users']}"
    finally:
        appmod.user_auth.is_enabled, appmod.google_auth.is_enabled = real_login


def test_canvas_persists_across_the_sign_in_redirect():
    """#199: the canvas was rebuilt from /router/demo on every load, so the OAuth redirect
    back to /canvas silently destroyed the node you had just configured — and that redirect is
    exactly what the 'sign in required' health check tells you to go do. Configuring a source
    and then obeying the instruction erased the work."""
    assert "localStorage" in CANVAS and "SAVE_KEY" in CANVAS, "no canvas persistence at all"
    assert re.search(r"function saveCanvas\(\)", CANVAS), "no saveCanvas()"
    assert re.search(r"function restoreCanvas\(\)", CANVAS), "no restoreCanvas()"
    # persisted on every mutation: renderAll() is the choke point most changes go through
    assert re.search(r"function renderAll\(\)\{[\s\S]*?saveCanvas\(\);", CANVAS), \
        "renderAll() does not persist — a change could be lost"
    # ...but the PANEL's field edits deliberately skip renderAll() so they don't steal focus,
    # so they must persist themselves. Missing this shipped a canvas that survived a reload
    # with the node present but every field it had been given thrown away — arguably worse
    # than losing it outright, because it looks like it worked.
    for handler in ("data-fld", "data-cfg"):
        i = CANVAS.index(f'querySelectorAll("[{handler}]")')
        block = CANVAS[i:i + 700]          # the handler body registered right after
        assert "saveCanvas()" in block, \
            f"panel [{handler}] edits are not persisted — the node survives but its config does not"
    # boot restores rather than clobbering
    assert re.search(r"function loadLiveDemo\(opts\)\{[\s\S]*?restoreCanvas\(\)", CANVAS), \
        "boot does not restore the saved canvas"
    # ...and there is an escape hatch, so a bad saved canvas can never trap the user
    assert "loadLiveDemo({fresh:true})" in CANVAS, "no way to reset back to the demo"
    assert "removeItem(SAVE_KEY)" in CANVAS, "the reset does not clear the saved canvas"


def test_boot_paint_does_not_clobber_the_saved_canvas_before_restore():
    """#295: the mode decision was made async (refreshAuth().then(bootCanvas), added with #279),
    but the standalone boot `renderAll()` at wire-up stayed synchronous. renderAll() persists on
    EVERY call, so that first paint wrote the empty initial state=[] to localStorage BEFORE the
    async bootCanvas -> loadLiveUser -> restoreCanvas() could read it. Every reload therefore wiped
    the user's connected stores, and with no nodes left to compose the very next ask answered
    "no catalog composed yet — POST /router/compose first". A signed-in user could never query
    their own data after a reload. The #199 persistence test above still passed the whole time,
    because it only checks that the machinery EXISTS — not that boot ordering leaves it intact.

    Guard the ordering itself: saveCanvas must no-op until an authoritative loader has run."""
    # the mode decision really is deferred behind the auth fetch (the condition that makes the
    # synchronous boot paint dangerous) — if this stops being async the guard is still harmless
    assert re.search(r"refreshAuth\(\)\.then\(bootCanvas\)", CANVAS), \
        "boot no longer defers the mode decision behind /auth/me — re-examine #295"

    # saveCanvas must EARLY-RETURN on the boot flag, before it can touch localStorage
    save = re.search(r"function saveCanvas\(\)\{([\s\S]*?)\n  \}", CANVAS).group(1)
    guard = re.search(r"if\(booting\)\s*return;", save)
    assert guard, "saveCanvas() has no booting guard — the pre-auth paint can persist empty state"
    assert guard.start() < save.index("localStorage.setItem"), \
        "the booting guard runs AFTER localStorage.setItem — the wipe already happened"

    # the flag starts suppressed and is only released once bootCanvas (post-auth) is entered, so
    # the danger window (paint before restore) is fully covered
    assert re.search(r"let booting=true;", CANVAS), "booting must start true (suppressed at boot)"
    boot = re.search(r"function bootCanvas\(\)\{([\s\S]*?)\n  \}", CANVAS).group(1)
    assert re.search(r"booting=false;", boot), \
        "bootCanvas never clears booting — persistence would stay off forever, losing all work"
    # and nothing clears it earlier than bootCanvas (that would reopen the window)
    assert CANVAS.count("booting=false") == 1, \
        "booting is cleared somewhere other than bootCanvas — the boot-paint window may reopen"


def test_sharepoint_connect_failure_does_not_freeze_the_canvas_with_a_blocking_alert():
    """#297: an unconfigured SharePoint connector (SP_CONNECTOR_* unset) returns 503 — an
    EXPECTED operator-setup state on any deployment that hasn't registered the multi-tenant app.
    The connect handler surfaced it with a native alert(), which BLOCKS the whole page: the
    canvas froze on a known, benign failure (observed live — screenshots timed out with 'page is
    busy' because the modal dialog was open). A predictable non-error must never be able to wedge
    the UI; use the non-blocking toast the rest of the canvas already uses."""
    # isolate the sp-connect click handler
    i = CANVAS.index('const spBtn=el.querySelector(".sp-connect")')
    block = CANVAS[i:CANVAS.index('const pickBtn', i)]     # the whole sp-connect handler
    assert 'sharepoint/consent-url' in block, "sp-connect handler moved — re-anchor this guard"
    assert "alert(" not in block, (
        "#297 regression: the SharePoint connect handler uses a blocking alert() — an "
        "unconfigured-connector 503 (an expected state) will freeze the entire canvas")
    assert "toast(" in block, "the sp-connect error path no longer surfaces anything to the user"
    # the 503 message must be operator-actionable, not a raw env-var leak
    assert "isn't set up on this deployment" in block, \
        "the 503 is not explained as an operator-setup state"
    # global: a native blocking dialog anywhere in this SPA can wedge the canvas — the connect AND
    # the ingest handler both used to. Ban it outright; every error path has toast()/statusbar.
    assert "alert(" not in CANVAS, \
        "a native blocking alert() is back in the canvas surface — it freezes the whole canvas on the " \
        "path that calls it; use the non-blocking toast() instead"


def _strip_js_comments(src: str) -> str:
    """Executable text only.

    Written because the #880 guard below caught its OWN explanation: the region's comment
    NAMES the field it forbids ("the old succeed(r) wrote r.docs_indexed"), so a plain
    substring test failed on prose describing the very bug it was guarding against. This is
    the false positive `docs/HANDOVER` states as a rule for prod verification - byte-verify the
    executable line, not a grep - and it applies just as much to a guard as to a deploy check.
    A comment that cannot mention a defect is a comment that cannot explain one."""
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src[i] in "\"'`":
            q = src[i]; i += 1
            while i < n and src[i] != q:
                i += 2 if src[i] == "\\" else 1
            i += 1
        else:
            out.append(src[i]); i += 1
    return "".join(out)


def _sp_region():
    """The SharePoint picker + ingest machinery, as one region.

    These two guards used to slice from `async function openSpPicker` to the next `\n  }`.
    #879 made that function synchronous - deliberately, because awaiting the library list was
    the 11.2s blank modal - and #880 moved the run state OUT of the picker's closure so a job
    could outlive both the dialog and the list. Both guards broke on the SHAPE while the
    properties they exist to protect were being strengthened, which is DESIGN_SYSTEM s10's
    warning in the flesh: a test written around today's architecture becomes a guard on that
    architecture. Naming the region instead of the function survives the next such move."""
    marker = CANVAS.index("#880: an ingest is a JOB")
    # Back up to the banner comment's OPENING delimiter. Starting mid-comment would hand
    # _strip_js_comments a body with no `/*` to close on, so it would read the prose as code -
    # which is exactly how this guard first failed against its own explanation.
    i = CANVAS.rindex("/*", 0, marker)
    return CANVAS[i:CANVAS.index("function center(el)", i)]


def test_sharepoint_picker_offers_the_paste_a_sharing_link_path():
    """#300: the picker modal must offer BOTH ingest paths — a whole library (the drive list)
    and one folder from a sharing link (the input) — and the link path must POST share_link so
    the backend resolves it. Guards against the input silently disappearing or being wired to
    the wrong field."""
    block = _sp_region()
    assert 'id="spLink"' in block, "the picker has no sharing-link input (#300 entry point missing)"
    assert "sp-link-ingest" in block, "no 'Ingest folder' button for the sharing-link path"
    assert "share_link:link" in block or "share_link: link" in block, \
        "the link path does not POST share_link — the backend can't resolve the folder"
    assert "/connectors/sharepoint/finish" in block, "the link path does not reach the ingest endpoint"


def test_sharepoint_ingest_shows_live_progress_not_a_silent_wait():
    """#302: a SharePoint ingest is a multi-minute crawl, and the surface must show WHICH stage
    it is at rather than a silent wait.

    #880 changed WHICH endpoint supplies that. It was /connectors/sharepoint/ingest-progress,
    which is keyed by TENANT alone - so a second run silently overwrote the first one's phase -
    and which never receives `indexing` at all, because the runner writes that phase to the job
    checkpoint only. GET /ingest/jobs/{job_id} is per-run, reports every phase, and carries the
    terminal status the surface had no way to learn. Asserting the old URL here would now be a
    guard holding the product on the worse of two endpoints.

    The LIVE property - that a real browser sees the stepper advance through the real phases,
    and that the modal outlives the job - is tests/e2e_880_ingest_modal.py. This file can only
    see text, so it asserts what text can honestly carry."""
    block = _sp_region()
    assert "/ingest/jobs/" in block, \
        "the ingest does not follow its job — it's a silent wait again"
    assert "sp-progress" in block and "sp-prog-bar" in block, "no live progress panel rendered"
    assert "sp-steps" in block and "SP_STEP_OF" in block, \
        "no process stepper — the user can't see WHICH stage (find/fetch/extract/embed/index)"
    assert "clearInterval(poll)" in block, \
        "the progress poller is never cleared — it would keep polling after the ingest finishes"
    # #880: the defect was the surface deciding a QUEUED job had finished. A 202 carries no
    # count, so any code reading docs_indexed off the /finish response is that bug returning.
    assert "docs_indexed" not in _strip_js_comments(block), \
        "#880 regression: the surface is reading a document count off the /finish response, " \
        "which returns 202 'queued' and does not carry one — that is how 'undefined docs' and " \
        "a modal that closed before the crawl started both happened"


def test_route_advisor_has_a_way_out():
    """#307: the Route advisor writes into the SAME result area as the answer, so once you click
    Route there was no obvious way back — you had to re-Ask (re-running the query). The route panel
    must render an explicit close affordance that dismisses it."""
    i = CANVAS.index("function routeLive")
    block = CANVAS[i:CANVAS.index("\n  }", i + 50)]
    assert "qroute-close" in block, "the Route advisor has no close/back affordance (#307)"
    assert re.search(r"rc\.onclick\s*=", block) or "qroute-close" in CANVAS[i:i+2000], \
        "the close button is not wired to dismiss the panel"


def test_header_does_not_claim_signed_in_when_the_vault_has_no_credential():
    """#210: TokenVault is in-memory BY DESIGN, but the session cookie is signed and survives a
    restart. So "signed in" stayed true while the user had NO credential to query with, and
    every delegated ask failed with "sign in to query this source" under a header that said
    they were signed in. The server was already honest (/auth/me returns linked: []); the
    canvas ignored it.

    #643: the canvas has no identity chip any more - the shell's ONE account control renders
    for every surface (#414) - so the honesty rule is asserted where it now lives. The rule is
    unchanged and is the whole point of the test: `linked` must be consulted, the state must be
    named, and the remedy must be one click. What moved is the file, not the standard."""
    account = (ROOT / "src/dbsearch/server/static/js/ui/account.js").read_text()
    block = re.search(r"function providerRow\(p, me\) \{[\s\S]*?\n\}", account).group(0)
    assert "linked" in block, "the account control ignores /auth/me's linked list"
    # The STATE stays "Not connected" - true whether the vault lost the credential or the
    # sign-in never produced one, and the control cannot tell which. What #210 needs is that
    # the case is not silently identical to every other unconnected provider...
    assert "needsReSignIn" in block, \
        "signed-in-but-unlinked is not distinguished, so it is presented as a routine grant"
    # ...and that the remedy is one click, pointing at sign-in - NOT at the grant flow, which
    # is a dead end when the thing missing is the credential signing in would have minted.
    assert "Sign in again" in block, "no re-sign-in affordance for a session with no credential"
    assert '"/auth/login"' in block, "the remedy does not go to sign-in"
    # The canvas must not grow a second, differently-worded copy back. Keyed on the chip's
    # own markup rather than on its wording: the file still DISCUSSES the old chip in a
    # comment explaining where it went, and a prose match would fail on the explanation.
    assert 'class="wsub' not in CANVAS and 'class="who"' not in CANVAS, \
        "the canvas is answering the identity question again - that divergence IS #414"


def test_auth_me_reports_which_clouds_are_actually_linked():
    """The header can only be honest if the server tells it the truth."""
    r = client.get("/auth/me")
    assert r.status_code == 200, r.text
    assert "linked" in r.json(), r.json()


def test_health_leads_with_sign_in_when_that_is_the_actual_fix():
    """#196: 'you need to sign in' is the ONE health failure the user can fix themselves, and
    it was buried under the generic headline 'Reachable, but the round-trip could not complete
    under your identity' — which reads like a broken connector, not a one-click remedy."""
    from dbsearch.router.health import ConnectionTest, default_strategies

    class NotSignedIn(RuntimeError):
        idp = "entra"          # the structural marker health.py keys off (no import)

    class Store:
        def authorize(self, oid):
            raise NotSignedIn("sign in to query this source — queries run as you")

    class Provider:
        def probe(self, cfg):
            class P:
                schema = [{"table": "dbo.sales", "columns": []}]
            return P()

        def build(self, cfg):
            return Store()

    class Registry:
        _defaults = {"azure_sql": "pushdown"}

        def get(self, kind, mode):
            return Provider()

    v = ConnectionTest(Registry(), default_strategies()).run(
        {"kind": "azure_sql", "mode": "pushdown", "id": "s", "config": {}}, "malcolm")
    d = v.to_dict()
    assert "sign in" in d["summary"].lower(), d["summary"]
    assert "could not complete" not in d["summary"].lower(), \
        "the generic headline still buries the remedy"
    assert d["status"] == "degraded", d           # the CONNECTION is fine; the caller isn't


def test_canvas_never_persists_a_secret():
    """LAW 1: what the canvas persists must be a REFERENCE, never a credential. We write node
    config to localStorage and to the server-side manifest, so a seeded secret would be a
    durable one.

    ASSERTION REPLACED 260812 (#672), DELIBERATELY STRENGTHENED - read this before changing it
    back. The original guard asserted that every `password` placeholder starts with `${`, as a
    PROXY for "the panel only ever puts references in secret fields". That proxy became wrong:
    the RDS panels carry human placeholders ("your database password") precisely because
    nothing on a hosted box sets an RDS_* env var, and seeding an unresolvable ${ENV} ref is
    the #664 defect (a placeholder implying the system knows a value it has never heard of).

    A human placeholder is not a weakening - it is SAFER. addNode seeds
        config[f.k] = resolvable ? ph : ""
    where `resolvable = envRef && CFG_OPERATOR && ENV_PRESENT.has(...)`. An env-ref placeholder
    CAN be written into config (as a reference); a human one can never be written at all. So
    this now asserts the seeding RULE itself - the property the proxy was standing in for -
    which also covers every future field whatever its placeholder looks like.

    The real credential path is unchanged and lives elsewhere: a signed-in self-serve user's
    password goes to /secrets and only a `secret://` handle is stored (#319/ADR 0010), and
    compose refuses a plaintext value in a secret-typed field server-side (secret_fields.py)."""
    save = re.search(r"function saveCanvas\(\)\{[\s\S]*?\n  \}", CANVAS).group(0)
    # THE RULE: only a resolvable ${ENV} ref is ever seeded; everything else seeds empty.
    assert re.search(r"const\s+envRef\s*=\s*/\^\\\$\\\{\[A-Z0-9_\]\+\\\}\$/\.test\(ph\)", CANVAS), \
        "the env-ref test in addNode changed shape - re-read the seeding rule"
    assert re.search(r"const\s+resolvable\s*=\s*envRef\s*&&\s*CFG_OPERATOR\s*&&\s*ENV_PRESENT\.has",
                     CANVAS), \
        "seeding no longer requires an env ref the SERVER actually has (#320)"
    assert re.search(r"config\[f\.k\]\s*=\s*resolvable\s*\?\s*ph\s*:", CANVAS), \
        ("addNode no longer gates the seeded value on `resolvable` - a raw placeholder could "
         "now be persisted verbatim, which for a secret field is a durable credential")
    # And any secret placeholder that IS an env ref must stay a bare ref, never a literal
    # with a value baked into it.
    for field in re.finditer(r'\{k:"(?:password|key|token)",ph:"([^"]+)"', CANVAS):
        ph = field.group(1)
        if ph.startswith("${"):
            assert re.fullmatch(r"\$\{[A-Z0-9_]+\}", ph), f"malformed env ref in a secret field: {ph}"
    assert "refresh_token" not in save and "client_secret" not in save, save


def test_canvas_asks_the_document_bridge_regardless_of_sharepoint_connector_state():
    """#255: the canvas gated its document search on SharePoint-CONNECTOR state
    (`spOn = Object.keys(spIngested).length>0`), but the edition index holds documents from
    other paths too — /admin/upload, and any future non-SharePoint connector. So a user with
    genuinely authorized documents asked "what is holiday days", the canvas never called
    /search at all, and the router answered "None of the sources you can see hold that kind
    of data, so I have not guessed an answer from something else."

    That wording is the trap: it is the LAW-2 honesty phrasing, so it READS like a careful
    abstention, while being flatly false — the doc exists, the user is authorized, and the
    sibling /search surface answers it correctly. Manufacturing confidence in a wrong
    negative is worse than erroring out.

    /search enforces its own LAW-2 trim, so asking it unconditionally is safe: a user with no
    authorized docs simply gets the honest "couldn't find anything you have access to"."""
    ask = re.search(r"function askLive\(pin\)\{[\s\S]*?\n  \}", CANVAS)
    assert ask, "askLive() not found — canvas ask handler moved?"
    body = ask.group(0)

    # the document bridge must be reached on every ask, not only when a SharePoint library
    # happened to be ingested in this session
    assert "askSharePoint(q)" in body, "the canvas never calls the /search document bridge"
    assert not re.search(r"if\s*\(\s*spOn\s*\)\s*askSharePoint", body), (
        "#255 regression: the /search document bridge is gated on SharePoint-connector "
        "state (spOn). Documents reaching the index by any other path (e.g. /admin/upload) "
        "become permanently unaskable on the canvas, and the user is told no such data "
        "exists.")
    assert not re.search(r"const\s+spOn\s*=\s*Object\.keys\(spIngested\)", body), (
        "#255 regression: spOn is still derived from spIngested — the document panel is "
        "again conditioned on the SharePoint connector rather than on whether documents "
        "exist.")


def test_upload_only_documents_are_reachable_through_the_search_bridge():
    """The server half of #255: a document that arrived WITHOUT any SharePoint connector
    (plain /admin/upload) must still be answerable via /search for an authorized identity.
    If this fails, the canvas fix above is pointless — there would be nothing to find."""
    body = b"Public holidays are granted in addition to the 25 days of annual leave."
    files = {"file": ("holiday.txt", body, "text/plain")}
    data = {"acl": ["alice"], "title": "Holiday Policy Fixture"}
    r = client.post("/admin/upload", headers=ALICE, files=files, data=data)
    assert r.status_code == 202, r.text
    job = settle(client, r, headers=ALICE)
    assert job["status"] == "succeeded", job

    r = client.post("/search", headers=ALICE, json={"question": "how many days of annual leave"})
    assert r.status_code == 200, r.text
    titles = [c.get("title") for c in (r.json().get("citations") or [])]
    assert "Holiday Policy Fixture" in titles, (
        f"an upload-only document is invisible to /search — citations={titles}")

    # LAW 2 still holds on this surface: an identity outside the ACL must not see it
    r = client.post("/search", headers={"X-DBSearch-User": "bob"},
                    json={"question": "how many days of annual leave"})
    assert r.status_code == 200, r.text
    titles = [c.get("title") for c in (r.json().get("citations") or [])]
    assert "Holiday Policy Fixture" not in titles, (
        f"LAW 2 breach: bob is not in the ACL but sees the doc — citations={titles}")


def test_html_shells_force_revalidation():
    """#261: the HTML documents carried NO cache directive, so browsers applied heuristic
    caching to them. A deploy then left the operator on old JS with nothing on screen saying
    so — which is the worst kind of failure here, because a shipped feature simply appears
    to be missing. (It did: the ACL picker was live on the server while the canvas in the
    browser still showed the pre-#258 field.)

    /static assets are exempt: StaticFiles already sends ETag + Last-Modified, so they
    revalidate on their own."""
    for path in ("/", "/canvas"):
        r = client.get(path, headers=ALICE)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        cc = r.headers.get("cache-control", "")
        assert "no-cache" in cc, (
            f"{path} is served without a no-cache directive (Cache-Control={cc!r}); a "
            "browser will heuristically cache the shell and serve stale UI after a deploy")


def test_every_seeded_env_placeholder_is_a_variable_something_actually_sets():
    """#262: #204 seeds a field whose placeholder looks like ${ENV} as a REAL value, on the
    theory that such placeholders are valid defaults. That holds only while every referenced
    variable is actually provided somewhere. HR_SP_URL was not — defined in no .env, no
    compose file, no code — so every SharePoint node was born pointing at an unset variable
    and died in manifest substitution ("references unset env var 'HR_SP_URL'") before its
    probe could run. The node looked configured; it could never connect.

    Rule: a ${ENV} placeholder is a PROMISE that the deployment sets that variable. If
    nothing sets it, the placeholder must be prose so #204 seeds "" instead."""
    canvas = (ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js").read_text()
    kinds_block = canvas[canvas.index("const KINDS"):canvas.index("function demo(")]
    referenced = set(re.findall(r'ph:"\$\{([A-Z0-9_]+)\}"', kinds_block))

    # Only files that SET variables count. A var named in a design doc is not provided —
    # counting docs is how this bug hid: HR_SP_URL appears in a spec and a prototype, and
    # in nothing that ever exports it.
    env_text = ""
    for p in [ROOT / ".env", ROOT / ".env.example", *(ROOT / "secrets").glob("*.env"),
              *(ROOT / "scratchpad").glob("*.env")]:
        if p.exists():
            env_text += p.read_text()

    # Known-broken, same defect as #262, left as an explicit shrinking list rather than
    # silently excluded — each is a node kind that cannot connect out of the box:
    #   GCP_PROJECT / GCP_DATASET (bigquery)
    # Tracked on #263. Do not add to this list to make a failure go away.
    # RS_CLUSTER left the list 260812 (#666): the redshift panel now uses human placeholders
    # for the fields the engine actually reads — the shrinking is the list working.
    # GRAPH_TOKEN left 260818 (#816): the field itself was removed — nothing ever read
    # config.token, so the panel was collecting a credential wired to nowhere.
    KNOWN_BROKEN = {"GCP_PROJECT", "GCP_DATASET"}

    unset = sorted(v for v in referenced if v not in env_text and v not in KNOWN_BROKEN)
    assert not unset, (
        f"canvas seeds these ${{ENV}} placeholders as real config, but no env file sets them: "
        f"{unset}. A node created from such a field can never connect — manifest "
        f"substitution fails before the probe runs. Make the placeholder prose, or provide "
        f"the variable.")
    # and the list above must never grow silently
    assert KNOWN_BROKEN <= referenced, (
        f"KNOWN_BROKEN names placeholders the canvas no longer seeds: "
        f"{sorted(KNOWN_BROKEN - referenced)} — remove them from the list")


def test_document_answer_retracts_the_routers_no_evidence_verdict():
    """#256: the router and the document bridge resolve independently, so the page could show
    BOTH "None of the sources you can see hold that kind of data, so I have not guessed an
    answer from something else" AND, four lines below, a correct cited answer to exactly that
    question. Both on screen at once; one of them false. A user reading top-down stops at the
    decline and never scrolls — so the product looked broken while working.

    The reconciliation must key on the footnote COUNT, not on the answer's wording: #233 was
    flaky precisely because behaviour keyed off phrasing, and DECLINED/FAILED/EMPTY are three
    different sentences that all mean "no evidence".

    #724 NARROWED THE TRIGGER, and this test was pinning the older, wrong one. It required the
    guard to fire on `cl.length` - anything RETRIEVED - where `cl` is the set the model was
    SHOWN. So a question the documents also could not answer retracted the router's honest
    abstention in favour of "the answer below comes from your documents", one line above a
    paragraph saying it did not have the information either. Two false statements where there
    had been one true one. The trigger is now `ref.length`, the set the answer POINTS AT.

    That is a narrowing, not a weakening, and it keeps every property this test was written to
    defend: still structural, still a count and not a phrase, still #233-safe. The wording rule
    below is unchanged and is the one that matters most here."""
    canvas = (ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js").read_text()

    assert 'dataset.routerEvidence = fns.length ? "1" : "0"' in canvas, \
        "the router half does not record whether it produced evidence, so the document half " \
        "cannot know whether it needs to retract anything"
    # Matched as a WHOLE guard expression, not a substring: an earlier version of this test
    # asserted only that `dataset.routerEvidence==="0"` appeared somewhere, and passed happily
    # against `if(false && ...routerEvidence==="0")`. A grep that survives the bug it guards
    # is worse than no guard, because it certifies the regression.
    assert re.search(
        r'if\(\s*ref\.length\s*&&\s*outEl\s*&&\s*outEl\.dataset\.routerEvidence==="0"\s*\)',
        canvas), (
        "the retraction guard is missing or has been weakened — the document half must "
        "retract the router's verdict exactly when the ANSWER REFERENCED a document and the "
        "router had no evidence (#724: on cl.length it fired on mere retrieval)")
    # #724: and it must not drift back to the shown set. Named explicitly because that is the
    # exact regression, and because a mutation swapping ref->cl left the DOM suite green until
    # selftest_724_doc_block grew a scenario where the router itself finds nothing.
    assert not re.search(r'if\(\s*cl\.length\s*&&\s*outEl', canvas), \
        "the retraction is keyed on the SHOWN set again — retrieval is not contribution (#724)"
    assert re.search(r'ans\.textContent\s*=\s*"No structured source answered this', canvas), \
        "the retraction does not actually replace the router's answer text"
    # keyed on structure, not on the decline sentence
    for phrase in ("None of the sources you can see", "have not guessed an answer"):
        assert phrase not in canvas, (
            f"canvas matches the decline WORDING ({phrase!r}); reconciliation must key on "
            "footnote count — the server has three different no-evidence sentences and the "
            "wording is free to change (#233)")
    # the disclosure is still true and must survive the retraction
    assert 'class="qdisc"' in canvas, \
        "the disclosure naming WHICH stores declined was removed — that part stays true"


def test_the_canvas_can_detect_and_heal_its_own_staleness():
    """#265: #261 made the server say no-cache, which only governs entries stored AFTER it. A
    browser already holding a header-less copy keeps its heuristic freshness window and serves
    it without asking — observed repeatedly as navigation transferSize 0 on a pre-fix build,
    while the server had the new one. The user then hunts for a feature that shipped an hour
    ago. Nothing the server sends can retract a copy the browser already has, so the PAGE must
    be able to notice it is stale and replace itself.

    #643: still asserted through /canvas, and that is the point - /canvas is now the shell
    document, so the check that used to protect one page protects every surface. The self-heal
    moved out of canvas.html into index.html's head for exactly that reason."""
    r = client.get("/canvas", headers=ALICE)
    assert r.status_code == 200, r.text
    page = r.text

    # the served page carries the build it was rendered from — never the raw placeholder
    assert "__DBS_BUILD__" not in page, \
        "the build placeholder was served un-substituted — the page cannot know its own version"
    m = re.search(r'DBS_BUILD\s*=\s*"([0-9a-f]{8,})"', page)
    assert m, "the page does not embed a build id"
    embedded = m.group(1)

    # ...and the server will tell a page what the current build is
    v = client.get("/version")
    assert v.status_code == 200, v.text
    assert v.json().get("build") == embedded, \
        f"/version {v.json().get('build')!r} disagrees with the served page {embedded!r}"
    assert "no-store" in v.headers.get("cache-control", ""), \
        "/version must never itself be cached — a cached staleness check is useless"

    # the page must actually act on a mismatch, and must not loop while doing so
    assert "DBS_BUILD" in page and "/version" in page, "no staleness check in the page"
    assert "sessionStorage" in page, \
        "no once-only guard — a build that never matches would reload forever"


def test_the_documents_panel_renders_a_numbered_source_list():
    """#257: the panel rendered its answer's [n] markers over a bare doc PILL — no number
    anywhere on screen, so a marker could not be resolved by inspection. A footnote the reader
    cannot follow reads as corroboration the answer has not actually got. The router's Sources
    block (#177) already numbers; the document half did not."""
    canvas = (ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js").read_text()
    block = canvas[canvas.index("function askSharePoint"):]
    block = block[:block.index("\n  }")]

    assert 'class="snum"' in block, "the document sources are not numbered"
    assert 'id="dfn' in block, "no per-source anchor for a marker to resolve to"
    assert 'data-dfn' in block, "the answer's [n] markers are not linked to the sources"
    assert "shdr" in block and "Sources" in block, "no Sources header on the document panel"


def test_demo_landing_does_not_tell_a_visitor_to_connect_before_they_can_ask():
    """#356: the demo canvas starts EMPTY by design (#279 - the visitor performs the connect
    gesture themselves). That is fine. What is NOT fine is the copy that framed it: the hint
    said "Add a database from the left to begin, then ask a question across them", and the
    status line read "0 sources / 0 connected" - while the demo catalog is ALREADY composed
    server-side (state.composed=true) and six sample databases are immediately askable.

    A public visitor was therefore told to go and connect something first, which is both
    untrue and points them at the one path that is not finished (self-serve connect, #319).
    The strongest claim the demo has - "these are already federated and permission-trimmed,
    just ask" - was contradicted by its own first screen.
    """
    body = re.search(r"function renderDemoHint\(\)\{.*?\n  \}", CANVAS, re.S)
    assert body, "renderDemoHint is gone; this guard needs rewriting"
    # Assert on the COPY the visitor actually sees, not the whole function: a comment that
    # quotes the old wording to explain why it changed must not read as the defect itself
    # (the same comment-sensitivity trap the scope enforcement test hit).
    hint_src = " ".join(re.findall(r"h\.innerHTML\s*=\s*(.+?);\n", body.group(0), re.S))
    assert hint_src, "renderDemoHint no longer assigns innerHTML; this guard needs rewriting"

    assert "to begin" not in hint_src, (
        "demo hint still gates asking behind connecting: it says the visitor must add a "
        "database 'to begin', but the demo catalog is pre-composed and askable immediately")
    assert re.search(r"already (connected|composed)|ask (a question )?(right )?now|"
                     r"ready to (ask|query)", hint_src, re.I), (
        "demo hint must SAY the sample databases are already connected and askable; "
        f"got: {hint_src[:200]!r}")

    status = re.search(r'const bar=document\.getElementById\("statusbar"\);\s*\n\s*const bus=.*?'
                       r'drag to arrange', CANVAS, re.S)
    assert status, "the status-bar renderer moved; this guard needs rewriting"
    assert re.search(r"isDemoMode\(\)|DEMO_POOL", status.group(0)), (
        "the status bar reports a bare '0 sources / 0 connected' in demo mode while the "
        "pre-composed sample stores are askable; it must account for the demo pool")
    print("  PASS  #356  the demo landing tells the truth: the sample databases are already "
          "connected and askable, so the visitor is not sent to connect something first")


def main():
    print("canvas fragility guards (#198 / #199 / #200):")
    test_store_without_an_acl_is_refused_and_the_reason_is_actionable()
    print("  PASS  #200  an ACL-less store is REFUSED with an actionable reason, never "
          "composed green-and-invisible")
    test_config_does_not_advertise_dev_auth_when_a_real_login_is_configured()
    print("  PASS  #198  /config withdraws the dev switcher when a real login is configured")
    test_canvas_persists_across_the_sign_in_redirect()
    test_canvas_never_persists_a_secret()
    print("  PASS  #199  the canvas survives the sign-in redirect (with a reset escape "
          "hatch), and persists no secrets")
    test_boot_paint_does_not_clobber_the_saved_canvas_before_restore()
    print("  PASS  #295  the pre-auth boot paint no longer clobbers the saved canvas before "
          "restore — a signed-in user's stores survive a reload and stay queryable")
    test_sharepoint_connect_failure_does_not_freeze_the_canvas_with_a_blocking_alert()
    print("  PASS  #297  an unconfigured SharePoint connector (503) surfaces via a non-blocking "
          "toast, not a native alert() that freezes the whole canvas")
    test_sharepoint_picker_offers_the_paste_a_sharing_link_path()
    print("  PASS  #300  the SharePoint picker offers a paste-a-sharing-link input that ingests "
          "just that folder, alongside the whole-library list")
    test_sharepoint_ingest_shows_live_progress_not_a_silent_wait()
    print("  PASS  #302  a SharePoint ingest polls /ingest-progress and shows a live phase/bar "
          "(discover→fetch→extract→embed→index), not a silent 'Ingesting…'")
    test_route_advisor_has_a_way_out()
    print("  PASS  #307  the Route advisor renders an explicit ✕ close, so you can dismiss it "
          "instead of being stuck on the routing view")
    test_header_does_not_claim_signed_in_when_the_vault_has_no_credential()
    test_auth_me_reports_which_clouds_are_actually_linked()
    print("  PASS  #210  'signed in' is not claimed when the vault holds no credential — the "
          "header says 'session expired' and offers one-click re-sign-in")
    test_health_leads_with_sign_in_when_that_is_the_actual_fix()
    print("  PASS  #196  health LEADS with 'sign in to query this source' when that is the "
          "fix, instead of burying it under a generic round-trip failure")
    test_canvas_asks_the_document_bridge_regardless_of_sharepoint_connector_state()
    test_upload_only_documents_are_reachable_through_the_search_bridge()
    print("  PASS  #255  the canvas asks the /search document bridge on EVERY ask, so docs "
          "that never came through the SharePoint connector are still answerable")
    test_document_answer_retracts_the_routers_no_evidence_verdict()
    print("  PASS  #256  a document answer RETRACTS the router's no-evidence verdict instead "
          "of appearing beneath it as a contradiction")
    test_the_documents_panel_renders_a_numbered_source_list()
    print("  PASS  #257  the Documents panel numbers its sources, so every [n] in the answer "
          "resolves to something on screen")
    test_every_seeded_env_placeholder_is_a_variable_something_actually_sets()
    print("  PASS  #262  every ${ENV} placeholder the canvas seeds as config is a variable "
          "the deployment actually sets")
    test_html_shells_force_revalidation()
    test_the_canvas_can_detect_and_heal_its_own_staleness()
    test_demo_landing_does_not_tell_a_visitor_to_connect_before_they_can_ask()
    print("  PASS  #265  the page embeds its build and can detect + replace a stale copy the "
          "browser refuses to revalidate")
    print("  PASS  #261  / and /canvas force revalidation, so a deploy cannot leave the "
          "operator on stale UI that hides a shipped feature")
    print("\nCANVAS FRAGILITY SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
