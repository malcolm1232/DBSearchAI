"""#279 Task 2 (3b): the demo/live SCOPE boundary - the hard LAW-2 merge gate (ADR 0009).

One server serves a fully-local DEMO (anonymous visitor acts as alice/bob over the bundled
fixture catalog) and the LIVE product (signed-in user over real stores), selected by auth
state. This test proves the security invariant by construction:

  A. resolve_identity (THE chokepoint, #184): a no-session/no-key request may name a demo
     selector, but ONLY the fixed allowlist {alice,bob} authenticates, and only as the
     NAMESPACED principal `demo:<name>` - never a real oid.
       - a real-looking oid or any unknown name in the demo selector -> AuthError;
       - a present session ALWAYS wins (the demo selector is ignored);
       - the demo path works even when a real login is configured (the hosted-demo case)
         WITHOUT reopening #183 (a bare `X-DBSearch-User` still never authenticates then).
  B. routing (LAW 2): a `demo:alice` identity reaches ONLY the pre-composed demo catalog -
     it appears in NO live store's visibility and cannot mutate the live catalog; a live
     user sees the live store and never a demo store.
  C. no vault reachability: the demo stores are LOCAL (SqliteEngine) - no delegation, so the
     `_subject_token_provider` path is never reachable for a `demo:*` identity; and the
     namespaced `demo:alice` is an inert principal in the shared identity (it matches
     nothing), so the ONLY place it is de-namespaced to `alice` is the router's demo scope.

    PYTHONPATH=src python3 tests/selftest_demo_scope_boundary.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
# Hermetic default model (ExtractiveLlm) regardless of the dev machine's env.
for _k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.api.auth import AuthError, resolve_identity  # noqa: E402
from dbsearch.router.structured import SqliteEngine  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.edition import build_edition  # noqa: E402
from dbsearch.server.router_api import compose_demo_catalog  # noqa: E402

_AUTH_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET")
_REAL_OID = "82d85111-1111-2222-3333-444455556666"


def hdr(d):
    return lambda n: d.get(n.lower())


def _tree_store_ids(tree: dict) -> set:
    """Flatten the visible_tree (tenant -> business_units -> sources -> stores) to store ids."""
    return {s["store_id"]
            for bu in tree.get("business_units", [])
            for src in bu.get("sources", [])
            for s in src.get("stores", [])}


def cookie(d):
    return lambda n: d.get(n)


# ---------------------------------------------------------------- A. the chokepoint

def test_allowlisted_demo_selector_returns_namespaced_principal():
    assert resolve_identity(hdr({"x-dbsearch-demo-user": "alice"})) == "demo:alice"
    assert resolve_identity(hdr({"x-dbsearch-demo-user": "bob"})) == "demo:bob"
    print("  PASS  demo selector alice/bob -> demo:alice / demo:bob (namespaced)")


def test_demo_selector_never_authenticates_a_real_or_unknown_identity():
    for bad in (_REAL_OID, "eve", "Alice", "admin", "demo:alice", ""):
        try:
            got = resolve_identity(hdr({"x-dbsearch-demo-user": bad}))
        except AuthError:
            continue
        raise AssertionError(
            f"VULNERABLE: demo selector {bad!r} authenticated as {got!r} - only the fixed "
            "allowlist {alice,bob} may pass, and never a real oid")
    print("  PASS  a real-looking oid / unknown / cased / pre-namespaced demo selector "
          "-> AuthError (never authenticates)")


def test_session_always_wins_over_demo_selector():
    token = user_auth.sign_session({"oid": _REAL_OID, "exp": int(time.time()) + 3600})
    got = resolve_identity(hdr({"x-dbsearch-demo-user": "alice"}),
                           cookie({user_auth.COOKIE: token}))
    assert got == _REAL_OID, f"session must win over the demo selector, got {got!r}"
    print("  PASS  a present session cookie wins - the demo selector is ignored")


def test_demo_path_works_under_real_login_without_reopening_183():
    """The hosted-demo case: the public site HAS a real login configured, yet an anonymous
    visitor must still be able to play the demo. The demo selector authenticates (as the
    namespaced principal), while a bare X-DBSearch-User still never authenticates (#183)."""
    saved = {k: os.environ.get(k) for k in _AUTH_VARS}
    try:
        os.environ["AUTH_TENANT_ID"] = "tid"
        os.environ["AUTH_CLIENT_ID"] = "cid"
        os.environ["AUTH_CLIENT_SECRET"] = "sec"
        assert user_auth.is_enabled(), "setup: real login should be enabled"
        # demo selector still works under a real login
        assert resolve_identity(hdr({"x-dbsearch-demo-user": "alice"})) == "demo:alice"
        # #183 stays closed: a bare dev header still never authenticates a real identity
        try:
            resolve_identity(hdr({"x-dbsearch-user": _REAL_OID}))
            raise AssertionError("VULNERABLE: bare X-DBSearch-User authenticated under a "
                                 "real login - #183 reopened")
        except AuthError:
            pass
    finally:
        for k in _AUTH_VARS:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    print("  PASS  demo selector authenticates under a real login; bare dev header still "
          "refused (#183 intact)")


# ---------------------------------------------------------------- B. routing (LAW 2)

def _live_manifest():
    """A live store ACL'd to a real oid ONLY - never to alice/all-staff/deal-team, so a demo
    principal can never be authorized against it."""
    return {"tenant": "acme-live", "stores": [
        {"id": "live-secret", "kind": "local", "business_unit": "exec", "acl": [_REAL_OID],
         "title": "Board Deck", "description": "confidential board strategy merger",
         "config": {
             "seed": [{"external_id": "deck", "title": "Board Deck", "uri": "live-u1",
                       "acl": [_REAL_OID], "text": "confidential merger price is nine billion"}],
         }},
    ]}


def test_demo_identity_reaches_only_the_demo_catalog_not_a_live_store():
    from fastapi.testclient import TestClient

    from dbsearch.server.app import app

    client = TestClient(app)
    # 1. a live user composes a live catalog with a confidential store (control).
    boss = {"x-dbsearch-user": _REAL_OID}
    r = client.post("/router/compose", json={"manifest": _live_manifest()}, headers=boss)
    assert r.status_code == 200, r.text
    boss_ids = _tree_store_ids(client.get("/router/catalog", headers=boss).json())
    assert "live-secret" in boss_ids, boss_ids   # positive control: the live user sees it

    # 2. a demo visitor: sees ONLY the demo fixture catalog, NEVER the live store.
    demo = {"x-dbsearch-demo-user": "alice"}
    demo_ids = _tree_store_ids(client.get("/router/catalog", headers=demo).json())
    assert "live-secret" not in demo_ids, ("LAW 2 BREACH: a demo identity can see a live "
                                           f"store - {demo_ids}")
    assert {"azure-deals", "hr-wiki", "fin-ledger"} <= demo_ids, demo_ids

    # 3. a demo ask answers from the demo fixture and never leaks the live store/content.
    a = client.post("/router/ask", json={"question": "total amount by region"}, headers=demo)
    assert a.status_code == 200, a.text
    blob = a.text
    assert "live-secret" not in blob and "nine billion" not in blob and "merger" not in blob, \
        "LAW 2 BREACH: a demo ask surfaced live-store content"

    # 4. a demo identity can NOT mutate the live catalog (compose is live-only).
    m = client.post("/router/compose", json={"manifest": _live_manifest()}, headers=demo)
    assert m.status_code == 403, f"demo must not compose the live catalog, got {m.status_code}"

    # 5. the live catalog is untouched by the demo activity (control still holds).
    assert "live-secret" in _tree_store_ids(client.get("/router/catalog", headers=boss).json())
    print("  PASS  demo:alice reaches ONLY the demo catalog (never the live store), cannot "
          "compose the live catalog; the live user is unaffected")


def test_demo_identity_is_refused_on_every_live_endpoint():
    """Default-deny (the regression the router-only guard missed): `resolve_identity` mints an
    authenticated `demo:alice` for an anonymous `?demo=`/header request, so EVERY live endpoint
    - which all depend on the live-only `current_user` - must 403 it. Otherwise an anonymous
    visitor reaches or mutates live customer data on a hosted (real-login) deployment."""
    from fastapi.testclient import TestClient

    from dbsearch.server.app import app

    client = TestClient(app)
    d = {"x-dbsearch-demo-user": "alice"}
    # a shareable `?demo=alice` link is the same authenticated identity - equally refused.
    q = "?demo=alice"

    live_mutating = [
        ("POST", "/ingest", {"external_id": "x", "title": "t", "text": "poison",
                             "acl": ["all-staff"], "uri": "u"}),
        ("POST", "/admin/resync", {"store_id": "s"}),
        ("POST", "/developer/keys", {"label": "k"}),
        ("POST", "/router/compose", {"manifest": _live_manifest()}),
    ]
    live_reading = [
        ("GET", "/admin/documents", None),
        ("GET", "/admin/identities", None),
        ("POST", "/admin/permission-test", {"user_oid": _REAL_OID, "question": "anything"}),
        ("POST", "/search", {"question": "confidential"}),
        ("POST", "/chat", {"question": "confidential"}),
        ("GET", "/router/kinds", None),
    ]
    for method, path, body in live_mutating + live_reading:
        for headers, suffix in ((d, ""), ({}, q)):
            r = (client.get(path + suffix, headers=headers) if method == "GET"
                 else client.post(path + suffix, json=body, headers=headers))
            assert r.status_code == 403, (
                f"LAW 2 BREACH: {method} {path}{suffix} admitted a demo identity "
                f"(status {r.status_code}) - a live endpoint must refuse demo:*")

    # positive control: the demo-safe read endpoints DO accept the demo identity.
    assert client.get("/router/demo", headers=d).status_code == 200
    assert client.get("/router/catalog", headers=d).status_code == 200
    # #939: allowlisting a route and the route actually admitting a demo identity are two
    # different facts, and only the second one is a control. 404 is an acceptable answer here
    # (the demo catalog may hold no such store id); 403 is not - that would mean the allowlist
    # above is describing a route that does not behave the way it claims.
    assert client.get("/router/stores/documents/documents",
                      headers=d).status_code in (200, 404, 409)
    print("  PASS  every live endpoint (ingest/admin/developer/search/chat/compose/kinds) 403s "
          "a demo identity; the demo-safe reads accept it (default-deny)")


def test_graphql_query_surface_is_live_only_for_demo():
    """The GraphQL query surface is LIVE-only: a `demo:*` identity is treated as unauthenticated
    there (it is served only by /router/*), so an anonymous demo visitor cannot route through the
    live query service."""
    from fastapi.testclient import TestClient

    from dbsearch.server.app import app

    client = TestClient(app)
    client.cookies.clear()
    r = client.post("/graphql",
                    json={"query": '{ search(question: "confidential") { authorizedDocs } }'},
                    headers={"x-dbsearch-demo-user": "alice"})
    body = r.json()
    # The invariant is that it does NOT ANSWER — not which wire shape the refusal takes. Since
    # #432 attached `current_user` to the route (default-deny belongs in the route table, and as
    # a Mount this surface was invisible to the sweep below), the refusal now arrives as an
    # HTTP 401/403 rather than a 200 with a GraphQL `errors` envelope. Both refuse; asserting the
    # envelope would pin a protocol choice and miss the point.
    assert body.get("data") is None, (
        f"LAW 2: /graphql must not answer a demo identity - {body}")
    assert body.get("errors") or r.status_code in (401, 403), (
        f"LAW 2: /graphql must REFUSE a demo identity, got {r.status_code} {body}")
    print("  PASS  /graphql treats a demo identity as unauthenticated (live-only query surface)")


def test_demo_selector_with_real_oid_is_rejected_end_to_end():
    from fastapi.testclient import TestClient

    from dbsearch.server.app import app

    client = TestClient(app)
    r = client.post("/router/ask", json={"question": "anything"},
                    headers={"x-dbsearch-demo-user": _REAL_OID})
    assert r.status_code == 401, f"a real oid in the demo selector must 401, got {r.status_code}"
    print("  PASS  a real oid smuggled through the demo selector is refused end-to-end (401)")


# ---------------------------------------------------------------- C. no vault reachability

def test_demo_stores_are_local_and_namespaced_principal_is_inert():
    edition = build_edition()
    demo = compose_demo_catalog(edition)
    store = demo.catalog.get("azure-deals").store
    assert isinstance(store._engine, SqliteEngine), \
        "demo azure-deals must be a LOCAL SqliteEngine (no delegation -> no subject token)"
    # the namespaced principal is inert in the shared identity: it expands to itself only,
    # so it matches NOTHING outside the router demo scope (which de-namespaces to `alice`).
    assert edition.identity.expand_groups("demo:alice") == ["demo:alice"], \
        edition.identity.expand_groups("demo:alice")
    # and the demo store is NOT visible to the namespaced principal (only the bare one is).
    ns_visible = {n.id for n in demo.catalog.visible_stores(["demo:alice"])}
    assert "fin-ledger" not in ns_visible, ns_visible
    print("  PASS  demo stores are local (no vault/subject-token path); the namespaced "
          "demo:alice is inert outside the router demo scope")


def test_340_demo_catalog_and_rerun_work_when_live_identity_ignores_alice():
    """#340: the hosted box's LIVE identity has never heard of bare alice, so
    /router/catalog returned an empty tree and /router/rerun 404d for demo
    visitors, while /router/ask worked. Reproduce that exact condition: real
    login configured AND edition.identity stripped of the dev alice/bob seeds."""
    from fastapi.testclient import TestClient

    from dbsearch.server import app as app_module
    from dbsearch.server.app import app

    for var in _AUTH_VARS:
        os.environ[var] = "test-" + var.lower()
    ident = app_module._edition.identity
    saved = {k: ident._user_groups.pop(k) for k in ("alice", "bob")
             if k in ident._user_groups}
    try:
        client = TestClient(app)
        demo = {"x-dbsearch-demo-user": "alice"}
        tree = client.get("/router/catalog", headers=demo)
        assert tree.status_code == 200, tree.text
        ids = _tree_store_ids(tree.json())
        assert {"hr-wiki", "fin-ledger", "azure-deals"} <= ids, \
            f"#340 regression: demo catalog empty/partial under real login - {ids}"
        bob_ids = _tree_store_ids(
            client.get("/router/catalog", headers={"x-dbsearch-demo-user": "bob"}).json())
        assert "fin-ledger" not in bob_ids, f"LAW 2: bob sees deal-team - {bob_ids}"
        assert "hr-wiki" in bob_ids, f"bob over-trimmed - {bob_ids}"

        a = client.post("/router/ask",
                        json={"question": "total closed deal amount by region"},
                        headers=demo)
        assert a.status_code == 200, a.text
        proof = next((c.get("proof") for c in a.json().get("citations", [])
                      if (c.get("proof") or {}).get("kind") == "sql"), None)
        assert proof, "demo ask produced no SQL proof to rerun"
        rr = client.post("/router/rerun",
                         json={"store_id": proof["store_id"], "sql": proof["sql"],
                               "token": proof["rerun_token"]}, headers=demo)
        assert rr.status_code == 200, f"#340 rerun regression: {rr.status_code} {rr.text}"
    finally:
        for k, v in saved.items():
            ident._user_groups[k] = v
        for var in _AUTH_VARS:
            os.environ.pop(var, None)
    print("  PASS  #340: demo catalog + rerun populated under real login with a "
          "live identity that ignores bare alice")


# ---------------------------------------------------------------- D. default-deny by construction

# The ONLY routes a demo:* identity may ever reach (#279/#336) - everything else
# in the app must depend on the live-only `current_user`.
#
# #562 added /router/stores/{store_id}/schema. It belongs here for the same reason
# /router/catalog does, not as an exception to the rule: it takes an injected RequestScope,
# so a demo identity resolves to the DEMO catalog and its own principals, and it names a
# store only after finding it in that scope's visible_stores(). It is the per-store detail
# view of the tree /router/catalog already returns to the same caller, and it returns schema
# shape only - never a row.
# #939: `/router/stores/{store_id}/documents` is classified DEMO_SAFE, and the reasoning is
# written here rather than assumed from its neighbour. It is the SAME SHAPE as
# `/schema` directly below it: the same `scoped` dependency, the same gate #1 resolution
# through `visible_stores()` (a store this caller cannot enumerate answers 404, never 403), and
# a metadata-only body. It is in fact narrower than `/schema` - that one returns every table and
# column of a visible store, while this one applies a further LAW 2 principals trim per
# DOCUMENT, so a caller sees only the files their own expanded principals admit them to.
# It returns titles and uris; it never returns chunk text, a snippet or a segment. What a demo
# identity can learn from it is the filenames of the demo seed, which is the same class of fact
# as the store titles `/router/catalog` already hands them.
DEMO_SAFE_PATHS = {
    ("GET", "/router/catalog"),
    ("GET", "/router/stores/{store_id}/schema"),
    ("GET", "/router/stores/{store_id}/documents"),
    ("GET", "/router/demo"),
    ("POST", "/router/ask"),
    ("POST", "/router/route"),
    ("POST", "/router/rerun"),
}

# Genuinely public infra - inspected below, not rubber-stamped:
#   - /health, /config, /version, /, /robots.txt, /canvas, the SHELL_PATHS (/app,
#     /ask, /chat, /draft, /admin, /developer): static HTML/JSON shells with no
#     document content or per-user data (app.py's own comments say so at each
#     route - "no content", "flags/usernames/backend only, no content").
#   - /auth/*, /connectors/sharepoint/callback: the sign-in machinery itself. It
#     MINTS the session current_user later reads, so it cannot itself depend on
#     current_user (bootstrap problem) - each leg is CSRF-protected by the signed
#     oauth state cookie instead of an identity dependency. /auth/dev/seed is the
#     same shape (a session-minting seam), additionally gated 404-unless-enabled
#     by DBSEARCH_DEV_SEED. /auth/local/signup and /auth/local/login (#574) are the
#     same bootstrap shape again - a password check stands in for the OAuth state
#     cookie as the thing that authorizes minting the session - and are additionally
#     gated 404-unless-enabled by DBSEARCH_LOCAL_AUTH, same as /auth/dev/seed.
#   - /developer/graphql-schema: the GraphQL SDL (type/field names only, e.g.
#     "type Citation { doc: String! }") - no document content or per-user data,
#     the schema equivalent of publishing an OpenAPI spec.
#   - /signin (#446): the page you visit IN ORDER TO authenticate, so it has the same
#     bootstrap shape as /auth/login - requiring a session to reach the sign-in page
#     is a contradiction. It is a static HTML shell with no per-user data: the only
#     thing it renders is which IdPs this box has configured, which /auth/me (also
#     public, and directly above) already returns to anyone who asks. It reads no
#     store, no index and no document. A session that DOES exist is sent to /canvas
#     client-side, which is a convenience, not a control.
#   - the #961 brand icons (/favicon.ico, /apple-touch-icon*.png, /icon-{192,512}.png,
#     /site.webmanifest): the same shape as /robots.txt, and public for a harder reason
#     than convenience. A browser fetches /favicon.ico and /apple-touch-icon.png on its
#     OWN initiative, before and regardless of any sign-in, so an icon behind an identity
#     dependency is simply a missing icon. They are five constant files plus one constant
#     JSON document, byte-identical for every caller: they read no store, no index and no
#     document, take no parameters, and cannot vary by who is asking - so there is no per-
#     user fact for a demo identity to reach. The manifest names only the product and its
#     own icon paths, all of which are already public.
PUBLIC_INFRA_PATHS = {
    ("GET", "/health"), ("GET", "/config"), ("GET", "/version"), ("GET", "/"),
    ("GET", "/robots.txt"), ("GET", "/canvas"), ("GET", "/signin"),
    ("GET", "/favicon.ico"), ("GET", "/apple-touch-icon.png"),
    ("GET", "/apple-touch-icon-precomposed.png"),
    ("GET", "/icon-192.png"), ("GET", "/icon-512.png"),
    ("GET", "/site.webmanifest"),
    ("GET", "/app"), ("GET", "/ask"), ("GET", "/chat"), ("GET", "/draft"),
    ("GET", "/admin"), ("GET", "/developer"),
    ("GET", "/auth/login"), ("GET", "/auth/callback"), ("POST", "/auth/logout"),
    ("GET", "/auth/google/login"), ("GET", "/auth/google/callback"),
    ("GET", "/auth/me"), ("POST", "/auth/dev/seed"),
    ("POST", "/auth/local/signup"), ("POST", "/auth/local/login"),
    ("GET", "/connectors/sharepoint/callback"),
    ("GET", "/developer/graphql-schema"),
}

# #605 / ADR 0021 - THE ONE DELIBERATE EXCEPTION, and it is kept in a set of its own rather
# than folded into PUBLIC_INFRA_PATHS above, because it is not public infra and must never be
# read as such. Everything in PUBLIC_INFRA_PATHS is a health probe, a build id, a login hop or
# an HTML shell with no customer data behind it. THESE FOUR ROUTES ANSWER FROM A CUSTOMER'S
# DOCUMENTS, for a caller with no identity at all. That is the accepted, written-down exception
# to LAW 2 that ADR 0021 exists to record: possession of an unguessable 128-bit token IS the
# authorization, bounded by the ADR's four invariants (the share's snapshot only, never the
# bytes, always an expiry, instantly revocable).
#
# THE POINT OF THE SEPARATE SET IS THAT IT CAN ONLY GROW ON PURPOSE. This sweep is what caught
# the routes when they were added, which is exactly what a default-deny route-table check is
# for; the correct response to that catch is to name the exception, not to weaken the rule. Any
# FIFTH anonymous route - added here or anywhere else in the app - fails this test until
# somebody writes it into this set, and writing it in means answering the ADR's question: what
# is this route allowed to hand to a stranger, and what bounds it?
#
# A `demo:*` identity reaching these is not a new exposure: they ignore identity entirely, so a
# demo caller gets exactly what an anonymous one gets, which is nothing without a live token.
ANONYMOUS_LINK_MODULE = "dbsearch.server.link_access"
ANONYMOUS_LINK_PATHS = {
    ("GET", "/c/{token}"),
    ("GET", "/c/{token}/transcript"),
    ("POST", "/c/{token}/chat"),
    ("POST", "/c/{token}/chat/stream"),
}

# #775 - A THIRD SHAPE, and it gets its own set for the same reason the one above does: it is
# neither public infra nor a LAW 2 exception, and reading it as either would be wrong.
#
# The Stripe webhook has no `current_user` because Stripe cannot hold a session cookie. It is
# not therefore unauthenticated: the HMAC signature over the raw body IS the authentication,
# checked before a single field is read (server/billing.py: verify_signature, constant-time
# compare, inside a tolerance window so a captured request cannot be replayed).
#
# What separates it from PUBLIC_INFRA_PATHS is that it is not a read at all. Nothing about a
# customer's documents is reachable through it: it answers with `{"received": true}` and the
# only state it can touch is one row of billing entitlement. What separates it from
# ANONYMOUS_LINK_PATHS is that it hands a stranger NOTHING - the ADR 0021 question ("what may
# this route give someone with no identity?") has the answer "nothing at all".
#
# The bound worth stating, because it is the one that matters: a forged request cannot grant
# storage. The signature is checked first, an unrecognised price grants no tier, and the tier
# a price DOES grant comes from the configured ladder rather than from anything in the request
# body. tests/selftest_775_webhook.py drives every one of those refusals.
#
# A SECOND signature-authenticated route needs the same paragraph written for it before it
# goes in here.
SIGNATURE_AUTHENTICATED_PATHS = {
    ("POST", "/stripe/webhook"),
}

# #962 - A FOURTH SHAPE, and it gets its own set for the reason the third one did: reading
# it as public infra would be wrong, and the difference is worth keeping visible.
#
# /demo-request is an unauthenticated WRITE. A prospect asking for a demo has no account by
# definition, so an identity dependency here is a contradiction - the same bootstrap shape
# as /signin, except that this one takes data rather than serving a page.
#
# What separates it from PUBLIC_INFRA_PATHS is that it is not a read and not a shell: it
# INSERTS a row. What separates it from ANONYMOUS_LINK_PATHS is the ADR 0021 question -
# "what may this route give someone with no identity?" - whose answer here is nothing at
# all. It returns `{"received": true}` and nothing else, on every path: a stored lead, a
# tripped honeypot and a duplicate all produce the same 202, and no field of any other
# submission is reachable through it. Reading the leads is a SEPARATE route,
# /admin/demo-requests, which is operator-gated like /admin/audit for the same reason -
# the rows are named people's work email addresses.
#
# The bounds, because an unauthenticated write is a spam surface by construction: the body
# is validated and length-capped server-side (demo_requests.clean - the browser check is a
# courtesy, not a control), a honeypot field drops bots without telling them which field
# gave them away, and it is rate-limited per real client IP via rate_limit.client_ip, not
# request.client.host - behind Caddy every request arrives from 127.0.0.1, so keying on the
# socket peer would put the entire internet in one bucket.
#
# A SECOND unauthenticated intake route needs this paragraph written for it before it goes
# in here - in particular the sentence about what it hands back, which is the one that
# makes it safe.
UNAUTHENTICATED_INTAKE_PATHS = {
    ("POST", "/demo-request"),
}

# Mounted ASGI sub-apps are not ordinary APIRoutes - there is no per-route Depends to
# inspect. /graphql enforces its OWN identity check inside get_context
# (graphql_app.py: a demo:* identity is treated as unauthenticated there), which is
# exactly what test_graphql_query_surface_is_live_only_for_demo above proves;
# /static serves only client-side JS/CSS/HTML assets, never document content.
# Mounts are opaque to the dependency sweep below, so each one is accounted for by hand.
# "" is the trailing catch-all StaticFiles that serves the SPA shell and its assets - public by
# design (the HTML is the before-login product; every DATA path it calls is a real route above and
# is swept). It is also why #432 happened: it silently answered /graphql, so a Mount here is a
# liability, not a convenience. /graphql is NO LONGER a mount - it is swept like any other route.
MOUNTED_SUBAPPS = {"/static", ""}

# FastAPI's own generated docs routes (Route, not APIRoute - framework internals with
# no endpoint of ours behind them, hence no Depends to check). Structural API
# metadata only, same class of exposure as /developer/graphql-schema.
FRAMEWORK_DOC_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


# #696: the route walk and the dependency read now live in tests/_route_walk.py so that every
# route-walking test gets them. They used to live HERE, privately, which is why
# selftest_graphql_mount_path.py and selftest_562_store_snapshot.py went red on modern FastAPI
# while this sweep did not. The shared version also carries include_router(dependencies=[...])
# down to the child routes, which is what /graphql needs (see that module's docstring).
from _route_walk import iter_routes as _iter_routes           # noqa: E402
from _route_walk import route_dependencies as _route_dependencies   # noqa: E402


def test_the_unauthenticated_route_allowlists_are_the_expected_size():
    """The three sets above are the COMPLETE list of routes that may be reached without
    `current_user`. Their SIZES are pinned here (#605 review round 2).

    The sweep below already refuses any route in none of the three, so it catches a new
    endpoint. What it cannot catch is somebody adding a line to one of the SETS - the sweep then
    passes, quietly, and an unauthenticated surface has grown with nothing to show for it in a
    diff review beyond one more tuple among two dozen. These three numbers make that edit fail
    until it is changed on purpose, which is the whole point: bumping one is the review moment.

    Deliberately a count and not a hash of the contents. A frozen copy of the sets would fail on
    a rename or a reordering, which teaches people to update it without reading it; a count fails
    on exactly one thing, and that thing is "the unauthenticated surface got bigger".

    THIS REPLACES A NUMBER THAT LIVED IN A PROSE COMMENT and was wrong. server/app.py and
    server/link_access.py both said "thirteen public-infra routes" when the set had 24 - a fact
    with nothing checking it, in comments written to correct a different unchecked claim. Both
    now name the sets and leave the counting to this assertion."""
    why = {
        "PUBLIC_INFRA_PATHS": ("a route reachable with NO identity was added or removed. If "
                               "added: it must be genuinely public infra - no customer data "
                               "behind it, inspected not rubber-stamped - and this number "
                               "bumped in the same commit"),
        "DEMO_SAFE_PATHS": ("the demo scope (#279 / ADR 0009) grew or shrank; a demo:* "
                            "identity reaches every route in it"),
        "ANONYMOUS_LINK_PATHS": ("the ADR 0021 exception is meant to be exactly the /c/{token} "
                                 "family: page, transcript, chat, chat/stream. A fifth "
                                 "anonymous route answering from customer documents needs the "
                                 "ADR's question answered first - what may it hand a stranger, "
                                 "and what bounds it?"),
        "UNAUTHENTICATED_INTAKE_PATHS": ("a route that ACCEPTS data with no identity was "
                                         "added. It must hand a caller nothing back but an "
                                         "ack, validate and cap its own body server-side, "
                                         "and be rate-limited on the real client IP - write "
                                         "that paragraph beside the set and bump this number "
                                         "in the same commit"),
        "SIGNATURE_AUTHENTICATED_PATHS": ("a route whose authentication is a payload signature "
                                          "rather than a session was added. It must hand a "
                                          "caller NOTHING about any customer, and the "
                                          "signature must be verified before any field of the "
                                          "body is read - write that paragraph beside the set "
                                          "and bump this number in the same commit"),
    }
    # Each expected number is written ONCE. Spelling it again inside the failure message is how
    # a message ends up disagreeing with the assertion it explains, which is a smaller version
    # of the defect this whole test replaced.
    # 24 -> 30: the six #961 brand-icon routes. Inspected, not rubber-stamped - the
    # paragraph beside PUBLIC_INFRA_PATHS says why a favicon cannot sit behind an identity
    # dependency, and what makes these six constant files rather than a surface.
    for name, expected, actual in (("PUBLIC_INFRA_PATHS", 30, len(PUBLIC_INFRA_PATHS)),
                                   ("DEMO_SAFE_PATHS", 7, len(DEMO_SAFE_PATHS)),
                                   ("ANONYMOUS_LINK_PATHS", 4, len(ANONYMOUS_LINK_PATHS)),
                                   ("SIGNATURE_AUTHENTICATED_PATHS", 1,
                                    len(SIGNATURE_AUTHENTICATED_PATHS)),
                                   ("UNAUTHENTICATED_INTAKE_PATHS", 1,
                                    len(UNAUTHENTICATED_INTAKE_PATHS))):
        assert actual == expected, (
            f"{name} is now {actual} routes, expected {expected} - {why[name]}.")
    print(f"  PASS  unauthenticated allowlists pinned: {len(PUBLIC_INFRA_PATHS)} public infra, "
          f"{len(DEMO_SAFE_PATHS)} demo-safe, {len(ANONYMOUS_LINK_PATHS)} anonymous link, "
          f"{len(SIGNATURE_AUTHENTICATED_PATHS)} signature-authenticated, "
          f"{len(UNAUTHENTICATED_INTAKE_PATHS)} unauthenticated intake")


def test_every_route_not_demo_safe_depends_on_the_live_only_current_user():
    """#336 final review finding 4: tests/selftest_demo_scope_boundary.py used to
    assert default-deny with a HAND-WRITTEN list of ~10 endpoints. Nothing walked
    app.routes to prove that EVERY route not on that list actually depends on
    current_user - a new endpoint that copy-pasted current_user_demo_ok or `scoped`
    onto a live/mutating surface would pass the whole sweep silently. This walks the
    live app's route table and asserts the boundary by construction: every route is
    either one of the five demo-safe reads, genuinely public infra (inspected above,
    not rubber-stamped), or depends on current_user.

    Uses `_iter_routes` (not a flat walk of `app.routes`) so the sweep still finds
    every route - including ones added via `include_router()` - on FastAPI's newer
    lazy-router internals (see `_iter_routes` docstring, #368 follow-up)."""
    import inspect

    import fastapi.params
    from fastapi.routing import APIRoute
    from starlette.routing import Mount, Route

    from dbsearch.server.app import app, current_user

    seen = set()
    for route, full_path, inherited in _iter_routes(app.routes):
        if isinstance(route, Mount):
            assert full_path in MOUNTED_SUBAPPS, f"unaccounted mounted sub-app: {full_path}"
            continue
        if isinstance(route, Route) and not isinstance(route, APIRoute):
            assert full_path in FRAMEWORK_DOC_PATHS, \
                f"unaccounted framework route: {full_path}"
            continue
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            key = (method, full_path)
            seen.add(key)
            if key in ANONYMOUS_LINK_PATHS:
                # #605 review, Finding 4: the exemption is NOT keyed on the path string alone.
                # A path is a name somebody can claim; the guarantee is that the endpoint
                # behind it is one of the four ADR 0021 handlers, which live in exactly one
                # module and are reviewed as one family. Without this, adding
                # ("GET", "/c/{token}/download") to the set above would silently make a
                # download route anonymously reachable - and invariant 2 ("read only, and
                # never the bytes") would be gone with one line of TEST edit and no code
                # review of the route at all.
                assert route.endpoint.__module__ == ANONYMOUS_LINK_MODULE, (
                    f"{method} {full_path} claims the ADR 0021 anonymous exemption but its "
                    f"endpoint lives in {route.endpoint.__module__}, not "
                    f"{ANONYMOUS_LINK_MODULE} - the exemption is for that route family, not "
                    "for whatever later claims one of these paths")
                continue
            if (key in DEMO_SAFE_PATHS or key in PUBLIC_INFRA_PATHS
                    or key in SIGNATURE_AUTHENTICATED_PATHS
                    or key in UNAUTHENTICATED_INTAKE_PATHS):
                continue
            deps = _route_dependencies(route, inherited)
            assert current_user in deps, (
                f"DEFAULT-DENY VIOLATION: {method} {full_path} is not demo-safe or "
                f"public infra, and does not depend on current_user (deps: "
                f"{[getattr(d, '__name__', d) for d in deps]}) - a demo identity may "
                "reach it")
    missing_demo = DEMO_SAFE_PATHS - seen
    missing_public = PUBLIC_INFRA_PATHS - seen
    # An exception that no longer names a live route is an exception nobody is reviewing:
    # it would sit here granting anonymous access to whatever later claims that path.
    missing_link = ANONYMOUS_LINK_PATHS - seen
    assert not missing_link, (
        f"anonymous-link route(s) missing from the live app: {missing_link} - the ADR 0021 "
        "exception must name routes that actually exist")
    assert not missing_demo, f"demo-safe route(s) missing from the live app: {missing_demo}"
    assert not missing_public, f"public-infra route(s) missing from the live app: {missing_public}"
    missing_intake = UNAUTHENTICATED_INTAKE_PATHS - seen
    assert not missing_intake, (
        f"unauthenticated-intake route(s) missing from the live app: {missing_intake} - an "
        "exception that names no live route is one nobody is reviewing")
    print(f"  PASS  {len(seen)} routes swept: every route is demo-safe, public infra, "
          "signature-authenticated, unauthenticated intake, or depends on current_user "
          "(default-deny by construction)")


def main():
    print("Demo/live scope boundary (#279 Task 2 / LAW-2 gate) self-test:")
    test_allowlisted_demo_selector_returns_namespaced_principal()
    test_demo_selector_never_authenticates_a_real_or_unknown_identity()
    test_session_always_wins_over_demo_selector()
    test_demo_path_works_under_real_login_without_reopening_183()
    test_demo_identity_reaches_only_the_demo_catalog_not_a_live_store()
    test_demo_identity_is_refused_on_every_live_endpoint()
    test_graphql_query_surface_is_live_only_for_demo()
    test_demo_selector_with_real_oid_is_rejected_end_to_end()
    test_demo_stores_are_local_and_namespaced_principal_is_inert()
    test_340_demo_catalog_and_rerun_work_when_live_identity_ignores_alice()
    test_the_unauthenticated_route_allowlists_are_the_expected_size()
    test_every_route_not_demo_safe_depends_on_the_live_only_current_user()
    print("\nDEMO/LIVE SCOPE BOUNDARY SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
