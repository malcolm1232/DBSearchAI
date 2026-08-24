"""Navigation self-test (#309): the landing hands off to /canvas, and the app shell
keeps its own real URLs instead of being a second product competing with "/".

Guards the three ways this regressed before:
  1. "/" auto-entered the app shell whenever dev auth was on, so "Launch demo" appeared
     to dump the visitor on #/ask with the URL unchanged.
  2. Nothing linked the shell and the canvas in EITHER direction — the canvas was a
     one-way door reachable only by typing /canvas by hand.
  3. The shell surfaces had no addressable URL of their own (hash routes only).

    python3 tests/selftest_nav_shell.py
"""
import os
import re
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SRC = Path(__file__).resolve().parents[1] / "src" / "dbsearch"

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import SHELL_PATHS, app  # noqa: E402

client = TestClient(app)


def main():
    print("Navigation self-test (#309):")

    # --- every shell path serves the shell, and none shadows an API route ---
    for path in SHELL_PATHS:
        r = client.get(path)
        assert r.status_code == 200, f"GET {path} -> {r.status_code}"
        assert "text/html" in r.headers["content-type"], f"{path}: {r.headers['content-type']}"
        assert "view-app" in r.text, f"{path} did not serve the app shell"
    print(f"  PASS  shell served at {', '.join(SHELL_PATHS)}")

    # GET /chat must not have swallowed POST /chat, nor GET /admin the /admin/* APIs.
    methods = {frozenset(r.methods) for r in app.routes if getattr(r, "path", "") == "/chat"}
    assert frozenset({"GET"}) in methods and frozenset({"POST"}) in methods, \
        f"/chat lost a method: {methods}"
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/config").json()["edition"] == "self-host"
    print("  PASS  GET /chat coexists with POST /chat; API routes unshadowed")

    # --- the client router resolves a bare path, not just a hash ---
    router_js = (SRC / "server/static/js/router.js").read_text()
    assert "currentRoute" in router_js, "router.js has no path-aware route resolution"
    assert "location.pathname" in router_js, "router.js still reads the hash only"
    print("  PASS  router.js resolves a bare path (so /chat mounts Chat)")

    # --- the landing hands off to /canvas and never auto-enters the app ---
    login_js = (SRC / "server/static/js/login.js").read_text()
    assert 'location.href = "/canvas"' in login_js, "landing does not navigate to /canvas"
    # The old rule was `showView(cfg && cfg.dev_auth ? "app" : "landing")`. Assert on the
    # expression, not the bare identifier — the comment above it names dev_auth on purpose.
    code = "\n".join(ln for ln in login_js.splitlines() if not ln.strip().startswith("//"))
    assert "dev_auth" not in code, \
        "initShell still branches on dev_auth - '/' will skip the landing again"
    assert "SHELL_PATHS" in login_js, "login.js does not decide the initial view from the path"
    # THE TWO SETS MUST BE EQUAL, IN BOTH DIRECTIONS (#605 task 12). This used to check one
    # direction only - every Python path appears in the JS - which leaves the JS free to claim
    # a path the server does not serve, and that is a URL whose reload 404s while the in-page
    # router happily renders it. Two hand-maintained copies of one fact is a known hazard in
    # this repo; making them provably identical is what stands in for collapsing them.
    js_paths = set(re.findall(r'"(/[^"]*)"',
                              login_js.split("SHELL_PATHS = new Set([", 1)[1]
                                      .split("])", 1)[0]))
    assert js_paths == set(SHELL_PATHS), (
        f"login.js SHELL_PATHS and app.py SHELL_PATHS disagree: only in js "
        f"{sorted(js_paths - set(SHELL_PATHS))}, only in python "
        f"{sorted(set(SHELL_PATHS) - js_paths)}")
    print("  PASS  '/' is landing-only; Launch demo -> /canvas")

    # --- the canvas is no longer a one-way door ---
    # #413: the canvas stopped carrying its own brand link and its own nav cluster, and
    # mounted the SAME rail the shell uses. #643 went further: /canvas now SERVES the shell,
    # so the rail is not "also mounted here", it is the same rail on the same page. Requested
    # over HTTP rather than read off disk, because the thing being asserted is what the
    # BROWSER receives at that URL.
    canvas = client.get("/canvas").text
    assert "/static/js/main.js" in canvas, "/canvas no longer serves the app shell"
    assert "/static/css/rail.css" in canvas, "canvas does not load the shared rail styles"
    assert "/static/css/canvas.css" in canvas, \
        "the shell does not load the Connectors stylesheet, so the surface renders unstyled"
    rail_js = (SRC / "server/static/js/ui/rail.js").read_text()
    assert "ui/rail.js" in (SRC / "server/static/js/main.js").read_text(), \
        "the shell does not mount the shared nav rail"
    assert 'brand.href = "/"' in rail_js, "shared rail brand does not link back to /"
    print("  PASS  /canvas serves the shell; the rail brand links back to /")

    # --- signed-in path already lands on the canvas (unchanged, asserted so it stays) ---
    app_py = (SRC / "server/app.py").read_text()
    assert '"/canvas?login=ok' in app_py, "/auth/callback no longer returns to /canvas"
    print("  PASS  /auth/callback returns to /canvas")

    # --- #313/#415: a deploy must never leave stale JS on fresh HTML ---
    # #415 raised code assets from no-cache to no-store, because the CDN in front of
    # prod rewrote no-cache to max-age=14400 on /static/* while passing it through on
    # HTML. no-store is strictly stronger and survives that rewrite, so it satisfies
    # #313's requirement rather than relaxing it. Fonts and images keep no-cache.
    for asset in ("/static/js/login.js", "/static/js/router.js", "/static/css/app.css"):
        r = client.get(asset)
        assert r.status_code == 200, f"{asset} -> {r.status_code}"
        cc = r.headers.get("cache-control", "")
        assert "no-store" in cc or "no-cache" in cc, (
            f"{asset} has Cache-Control={cc!r}; without no-store/no-cache a browser may "
            "apply heuristic freshness and never revalidate")
        assert "max-age" not in cc, (
            f"{asset} has Cache-Control={cc!r}; a max-age lets a browser serve a stale "
            "module against a fresh shell, which is how #415 deleted the navigation")
    for asset in ("/static/fonts/instrument-serif-latin.woff2",):
        r = client.get(asset)
        assert r.status_code == 200, f"{asset} -> {r.status_code}"
        assert r.headers.get("etag"), f"{asset} lost its ETag - revalidation would refetch"
    print("  PASS  /static code assets are non-cacheable; fonts keep ETag (#313/#415)")

    # --- house rule: no em dash in the landing copy (#276) ---
    index_html = (SRC / "server/static/index.html").read_text()
    assert "—" not in index_html, "index.html contains an em dash"
    print("  PASS  landing copy is em-dash free (#276)")

    # --- #386: the landing must offer a way IN, and exactly one PRIMARY action ---
    # This assertion used to be the exact inverse ("lp-signin not in index_html", #346), on the
    # reasoning that a visitor has nothing to sign in TO yet. That stopped being true when
    # multi-tenant Entra sign-in, per-owner workspaces and the credential panel shipped - and
    # the test then actively protected the bug: the live landing contained zero occurrences of
    # "sign in", so a returning user had no door at all. Keep BOTH halves asserted, because the
    # failure mode runs in both directions: no sign-in strands returning users, two primary
    # buttons split the one action we want from new ones.
    assert 'id="lp-signin"' in index_html, \
        'the landing has no sign-in entry point again (#386) - a returning user cannot get in'
    login_js = (SRC / "server/static/js/login.js").read_text()
    assert 'on("lp-signin' in login_js, "the landing sign-in button is not wired to a handler (#386)"
    # #446: to OUR page, not straight to the IdP. Jumping direct to /auth/login dropped the
    # visitor on an "unverified publisher" consent screen with no context, which reads as
    # phishing at the exact moment they are deciding whether to trust us with a database.
    assert '"/signin"' in login_js, \
        "the landing sign-in bypasses /signin and jumps straight to the IdP again (#446)"
    # Sign-in is the QUIET affordance; the demo stays the one primary CTA (#348).
    assert 'class="btn-ghost" id="lp-signin"' in index_html, \
        "the landing sign-in became a second primary button - it must stay ghost (#348/#386)"
    # Guard the BUTTONS by id, not by label - #348 relabelled them "Use Free!" and copy will
    # keep changing. What must not change is that both entry points into the product exist.
    assert index_html.count('id="lp-demo') == 2, "the landing lost a demo CTA button"
    # ...and the in-context way in must survive too: the landing one is an ADDITION, not a move.
    # #643: it is no longer a "Sign in with Microsoft" button on the canvas. That button was
    # half of the divergence #414 recorded - the shell offered "Sign in", the canvas offered
    # "Sign in with Microsoft", and they are different acts. ui/account.js renders the ONE
    # control now, on every surface including Connectors, so the affordance is asserted there.
    account_js = (SRC / "server/static/js/ui/account.js").read_text()
    assert '"acct-signin"' in account_js and '"/signin"' in account_js, \
        "the account control offers no way in - a signed-out user has no in-context sign-in"
    print("  PASS  landing offers sign-in (ghost) + demo (primary); every surface has the "
          "account control's own (#386/#643)")

    # --- #446: the sign-in page itself ---
    signin_html = (SRC / "server/static/signin.html").read_text()
    assert "—" not in signin_html, "signin.html contains an em dash"
    # It must be served by the APP, not only by the marketing export: a self-hoster has no
    # site/out on disk, and they are precisely who needs a sign-in page to exist.
    app_py = (SRC / "server/app.py").read_text()
    assert '@app.get("/signin")' in app_py, "the /signin route is gone (#446)"
    assert '_html("signin.html")' in app_py, \
        "/signin no longer serves signin.html through _html (it would lose cache revalidation)"
    # Both providers reachable. Google previously had NO entry point outside the canvas.
    assert "/auth/login" in signin_html, "the sign-in page lost the Microsoft path (#446)"
    assert "/auth/google/login" in signin_html, \
        "the sign-in page lost the Google path - it is the only place a Google-primary user can start (#446)"
    # Gate the buttons on what this box actually has, or we advertise an IdP that 503s.
    assert "google_enabled" in signin_html and "a.enabled" in signin_html, \
        "the sign-in page stopped gating providers on /auth/me (#446)"
    # Never show a sign-in page to someone already signed in.
    assert "signed_in" in signin_html, "the sign-in page no longer redirects an existing session (#446)"
    # The anonymous demo must stay one click away - this page is a door, not a wall.
    assert "/canvas" in signin_html, "the sign-in page lost its escape hatch to the demo (#446)"
    print("  PASS  /signin is app-served, gates providers on /auth/me, keeps the demo escape (#446)")

    print("\nNAVIGATION SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
