"""#368: THE regression test - user A composes, user B composes, A's catalog survives.

Simulates a real-login deployment (workspaces key per-user). The app boots in dev-auth
mode, so the test drives build_router_api directly with a stub current_user - the same
technique selftest_scope.py uses to stay hermetic.

    PYTHONPATH=src python3 tests/selftest_workspace_isolation.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
for _k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.edition import build_edition  # noqa: E402
from dbsearch.server.manifest_store import InMemoryManifestStore  # noqa: E402
from dbsearch.server import router_api  # noqa: E402

# Inline `tables:` (the DEMO_MANIFEST sales-figures form) so the csv store really BUILDS.
# A store that fails to build lands in `skipped` and never reaches the catalog, which
# would make the isolation assertions below vacuous.
_TABLE = {"sales": {"columns": ["region", "amount"], "rows": [["emea", 100]]}}

MANIFEST_A = {"tenant": "acme", "stores": [
    {"id": "csv-a", "kind": "csv", "business_unit": "eng", "acl": ["oid-a"],
     "config": {"tables": _TABLE}}]}
# B's store is ACL'd to BOTH principals on purpose. With `acl: ["oid-b"]` the headline
# test's `"csv-b" not in tree` half would pass on plain ACL filtering whether or not the
# workspaces were isolated at all - only the `"csv-a" in tree` half would discriminate.
# Making csv-b visible to oid-a means the ONLY thing that can keep it out of A's catalog
# is that it was composed into a different workspace, so the test is two-sided.
MANIFEST_B = {"tenant": "acme", "stores": [
    {"id": "csv-b", "kind": "csv", "business_unit": "ops", "acl": ["oid-a", "oid-b"],
     "config": {"tables": _TABLE}}]}


def _app(manifest_store=None, per_user=True):
    def current_user(request: Request) -> str:
        return request.headers["X-Test-User"]
    edition = build_edition()
    app = FastAPI()
    app.include_router(router_api.build_router_api(
        edition, current_user, manifest_store=manifest_store,
        force_per_user_workspaces=per_user))
    return app


def _hdr(u):
    return {"X-Test-User": u}


def test_second_users_compose_does_not_replace_firsts():
    client = TestClient(_app())
    ra = client.post("/router/compose", json={"manifest": MANIFEST_A}, headers=_hdr("oid-a"))
    assert ra.status_code == 200, ra.text
    assert ra.json()["stores"], f"csv-a must really compose, not land in skipped: {ra.text}"
    rb = client.post("/router/compose", json={"manifest": MANIFEST_B}, headers=_hdr("oid-b"))
    assert rb.status_code == 200, rb.text
    cat_a = client.get("/router/catalog", headers=_hdr("oid-a"))
    assert cat_a.status_code == 200, "#368: A must still have a catalog after B composed"
    tree = cat_a.text
    assert "csv-a" in tree and "csv-b" not in tree, \
        f"#368: A sees exactly A's stores, got: {tree[:400]}"
    print("  PASS  B's compose no longer deletes A's catalog (THE #368 regression)")


def test_workspace_rebuilds_from_stored_manifest_across_restart():
    store = InMemoryManifestStore()
    client = TestClient(_app(manifest_store=store))
    r = client.post("/router/compose", json={"manifest": MANIFEST_A}, headers=_hdr("oid-a"))
    assert r.status_code == 200, r.text
    assert store.get("oid-a") == MANIFEST_A, "compose must persist the raw manifest"
    # "Restart": a fresh app over the SAME store - the pool is empty, the row survives.
    client2 = TestClient(_app(manifest_store=store))
    cat = client2.get("/router/catalog", headers=_hdr("oid-a"))
    assert cat.status_code == 200 and "csv-a" in cat.text, \
        f"the workspace must rebuild from the stored manifest: {cat.status_code} {cat.text[:200]}"
    print("  PASS  catalog rebuilds from the stored manifest after a restart")


def test_get_manifest_returns_own_and_only_own():
    store = InMemoryManifestStore()
    client = TestClient(_app(manifest_store=store))
    client.post("/router/compose", json={"manifest": MANIFEST_A}, headers=_hdr("oid-a"))
    got = client.get("/router/manifest", headers=_hdr("oid-a"))
    assert got.json()["manifest"] == MANIFEST_A
    other = client.get("/router/manifest", headers=_hdr("oid-b"))
    assert other.json()["manifest"] is None, "B must never receive A's manifest"
    print("  PASS  GET /router/manifest is workspace-scoped")


def test_plaintext_password_in_compose_is_a_400_that_never_echoes():
    client = TestClient(_app())
    bad = {"tenant": "acme", "stores": [
        {"id": "pg-1", "kind": "postgres", "business_unit": "eng", "acl": ["oid-a"],
         "config": {"host": "h", "password": "SuperSecret99"}}]}
    r = client.post("/router/compose", json={"manifest": bad}, headers=_hdr("oid-a"))
    assert r.status_code == 400, r.text
    assert "SuperSecret99" not in r.text, f"THE ERROR ECHOED THE PASSWORD: {r.text}"
    assert "password" in r.text and "pg-1" in r.text, r.text
    print("  PASS  plaintext password -> 400 naming store+field, value never echoed")


def test_broken_manifest_store_is_a_503_not_an_empty_catalog():
    from dbsearch.server.manifest_store import ManifestStoreUnavailable

    class Broken:
        def get(self, owner):
            raise ManifestStoreUnavailable("pg down")

        def put(self, owner, manifest):
            raise ManifestStoreUnavailable("pg down")

    client = TestClient(_app(manifest_store=Broken()))
    r = client.get("/router/catalog", headers=_hdr("oid-a"))
    assert r.status_code == 503, \
        f"a broken store must 503, never look like an empty workspace: {r.status_code}"
    print("  PASS  broken manifest store -> 503 (fail closed, #200)")


def test_compose_that_cannot_persist_reports_503_not_success():
    """A compose that lands in memory but not in the store must NOT answer 200 - the
    user would be told their sources are saved and lose them on the next restart."""
    from dbsearch.server.manifest_store import ManifestStoreUnavailable

    class WriteBroken:
        def get(self, owner):
            return None

        def put(self, owner, manifest):
            raise ManifestStoreUnavailable("pg down")

    client = TestClient(_app(manifest_store=WriteBroken()))
    r = client.post("/router/compose", json={"manifest": MANIFEST_A}, headers=_hdr("oid-a"))
    assert r.status_code == 503, f"an unpersisted compose must not report success: {r.text}"
    print("  PASS  compose that composed but could not persist -> 503")


def test_rebuild_drops_a_plaintext_credential_store_without_killing_the_workspace():
    """#368 review finding 6. `_persisting_compose` guards the WRITE path, so every row it
    stores is clean - but that makes the invariant a comment, not a check. A row written
    out-of-band (a migration, or before the guard existed) must not compose a plaintext
    credential into a live workspace. It also must not crash the workspace: the owner's
    other, legitimate stores have to survive."""
    import logging

    store = InMemoryManifestStore()
    store.put("oid-a", {"tenant": "acme", "stores": [
        {"id": "csv-a", "kind": "csv", "business_unit": "eng", "acl": ["oid-a"],
         "config": {"tables": _TABLE}},
        {"id": "pg-1", "kind": "postgres", "business_unit": "eng", "acl": ["oid-a"],
         "config": {"host": "h", "password": "SuperSecret99"}}]})

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = _Capture()
    logger = logging.getLogger("dbsearch")
    logger.addHandler(handler)
    try:
        client = TestClient(_app(manifest_store=store))
        cat = client.get("/router/catalog", headers=_hdr("oid-a"))
    finally:
        logger.removeHandler(handler)

    assert cat.status_code == 200, \
        f"a guard hit must not crash the workspace: {cat.status_code} {cat.text[:200]}"
    assert "csv-a" in cat.text, "the owner's legitimate stores must survive the rebuild"
    assert "pg-1" not in cat.text, "the plaintext-credential store must be dropped"
    logged = "\n".join(records)
    assert "pg-1.password" in logged, \
        f"the drop must be logged, naming store+field: {logged}"
    assert "SuperSecret99" not in logged, f"THE LOG ECHOED THE PASSWORD: {logged}"
    print("  PASS  rebuild guard drops a plaintext-credential store, workspace survives, "
          "value never logged")


def test_one_failed_manifest_write_does_not_503_the_router_for_everyone():
    """#368 final review (CRITICAL 1), end to end through the HTTP surface with the REAL
    PgManifestStore. `_schema_done` used to be set inside the failing transaction, so the very
    first manifest write to fail rolled the CREATE TABLE back and left the flag set: every
    later manifest-store operation raised `relation "user_manifests" does not exist`, which
    the router maps to a 503 - for EVERY user, on /catalog, /ask, /route, /compose, /probe,
    /health, /setup/turn and /manifest, until the container restarts (app.py holds one
    process-wide store).

    The trigger is ordinary user data: a NUL byte in a manifest string, which jsonb refuses
    (#367 puts one there for real)."""
    dsn = os.environ.get("PGVECTOR_DSN", "")
    if not dsn:
        print("  SKIP  PGVECTOR_DSN unset - this one needs the live Postgres")
        return
    from dbsearch.server.manifest_store import PgManifestStore

    table = "user_manifests_router_selftest"
    import psycopg
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")       # a genuinely cold store

    store = PgManifestStore(dsn, table=table)
    client = TestClient(_app(manifest_store=store))
    poisoned = {"tenant": "acme", "stores": [
        {"id": "csv-nul", "kind": "csv", "business_unit": "eng", "acl": ["oid-a"],
         "title": "nul", "description": "a NUL byte: \x00", "config": {"tables": _TABLE}}]}

    # oid-a's very first compose is the one that cannot be stored: an honest 503 (it composed
    # but was not saved), NOT a success.
    r = client.post("/router/compose", json={"manifest": poisoned}, headers=_hdr("oid-a"))
    assert r.status_code == 503, f"an unstorable manifest must 503, got {r.status_code}: {r.text}"

    # ...and the router must still work, for that user and for everyone else.
    r2 = client.post("/router/compose", json={"manifest": MANIFEST_B}, headers=_hdr("oid-b"))
    assert r2.status_code == 200, (
        "one failed manifest write poisoned the store: every later user 503s. "
        f"got {r2.status_code}: {r2.text}")
    cat = client.get("/router/catalog", headers=_hdr("oid-b"))
    assert cat.status_code == 200 and "csv-b" in cat.text, f"{cat.status_code} {cat.text[:200]}"
    r3 = client.post("/router/compose", json={"manifest": MANIFEST_A}, headers=_hdr("oid-a"))
    assert r3.status_code == 200, f"the same user must recover too: {r3.text}"
    got = client.get("/router/manifest", headers=_hdr("oid-a"))
    assert got.status_code == 200 and got.json()["manifest"] == MANIFEST_A, got.text

    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    print("  PASS  one unstorable manifest 503s only ITS request - the router keeps serving "
          "every other user (and that user's next compose)")


def _count_composes(fn):
    """Run `fn()` and return (result, number of `_compose_manifest` calls it made)."""
    calls = []
    orig = router_api._compose_manifest

    def counting(*a, **k):
        calls.append(1)
        return orig(*a, **k)

    router_api._compose_manifest = counting
    try:
        return fn(), len(calls)
    finally:
        router_api._compose_manifest = orig


def test_cold_compose_composes_exactly_once():
    """#368 final review (IMPORTANT 3a). `_persisting_compose` called `_workspace(user)`,
    which on a COLD key rebuilt the entire catalog from the stored row - and then compose
    immediately overwrote it. Two full composes per cold compose: after a restart or an
    eviction the first canvas load connected and probed every one of the user's cloud
    databases twice and ran a connector-rail store's initial full crawl twice, double-firing
    the `sources_synced` telemetry."""
    store = InMemoryManifestStore()
    store.put("oid-a", MANIFEST_A)              # a stored row - the cold path WOULD rebuild it
    client = TestClient(_app(manifest_store=store))     # fresh pool: oid-a is cold

    r, n = _count_composes(lambda: client.post(
        "/router/compose", json={"manifest": MANIFEST_B}, headers=_hdr("oid-a")))
    assert r.status_code == 200, r.text
    assert n == 1, f"a cold compose must compose exactly once, composed {n} times"
    # ...and the compose that ran is the one that was ASKED for
    cat = client.get("/router/catalog", headers=_hdr("oid-a"))
    assert "csv-b" in cat.text and "csv-a" not in cat.text, cat.text
    assert store.get("oid-a") == MANIFEST_B, "the stored row must be the new manifest"

    # a WARM compose is one too (it always was) - guards against the fix double-counting
    r2, n2 = _count_composes(lambda: client.post(
        "/router/compose", json={"manifest": MANIFEST_A}, headers=_hdr("oid-a")))
    assert r2.status_code == 200 and n2 == 1, f"warm compose composed {n2} times"
    print("  PASS  a cold compose composes exactly ONCE (no throwaway rebuild), and so does "
          "a warm one")


def test_a_failed_cold_compose_leaves_the_stored_workspace_rebuildable():
    """The other half of not rebuilding first: the fresh workspace must not be registered
    until the compose has actually succeeded. Otherwise a rejected manifest would replace the
    owner's live catalog with an empty workspace that the pool then believes is warm - so
    their stored stores stay invisible until the process restarts."""
    store = InMemoryManifestStore()
    store.put("oid-a", MANIFEST_A)
    client = TestClient(_app(manifest_store=store))     # cold
    bad = {"stores": []}                                # no tenant -> 400 inside compose
    r = client.post("/router/compose", json={"manifest": bad}, headers=_hdr("oid-a"))
    assert r.status_code == 400, r.text
    cat = client.get("/router/catalog", headers=_hdr("oid-a"))
    assert cat.status_code == 200 and "csv-a" in cat.text, (
        "a failed compose blanked the workspace; the owner's stored stores must still "
        f"rebuild: {cat.status_code} {cat.text[:200]}")
    assert store.get("oid-a") == MANIFEST_A, "and the stored row must be untouched"
    print("  PASS  a failed cold compose leaves the stored workspace intact and rebuildable")


def test_probe_and_health_read_the_callers_own_workspace():
    """I3b revert (post-534a175 re-review, #378/#379). /probe, /health and the setup agent's
    health check must land on the CALLER's own workspace via `_workspace(user)`, not the
    shared `_kinds_state` template. `_kinds_state` owns an `IdentityBroker` with no
    delegations registered, so a store configured with `delegation:` silently fell through
    to a non-delegated `AccessContext` there - a health check could say "healthy" for a store
    the caller cannot actually query (#378). Sharing `_kinds_state` for connection testing
    also meant `mode: index` kinds ran their full crawl into process-global state, keyed by
    store id across every user (#379). This test only proves the routing (own workspace, not
    the template); test_health_agrees_with_ask_for_a_delegated_store_without_credentials below
    proves the actual #378 failure is gone."""
    store = InMemoryManifestStore()
    store.put("oid-a", MANIFEST_A)
    app = _app(manifest_store=store)
    client = TestClient(app)
    entry = {"id": "probe-me", "kind": "csv", "business_unit": "eng",
             "config": {"tables": _TABLE}}

    def _probe_and_health():
        p = client.post("/router/probe", json={"entry": entry}, headers=_hdr("oid-a"))
        h = client.post("/router/health", json={"entry": entry}, headers=_hdr("oid-a"))
        return p, h

    (p, h), n = _count_composes(_probe_and_health)
    assert p.status_code == 200 and p.json()["available"] is True, p.text
    assert h.status_code == 200, h.text
    assert n == 1, (
        f"/probe + /health composed {n} times - a cold workspace must rebuild ONCE (from the "
        "stored manifest, via _workspace) so the caller's own registry state is what answers, "
        "not the shared template")
    print("  PASS  /probe and /health rebuild (once) and read the CALLER's own workspace, not "
          "the shared _kinds_state template")


def test_health_agrees_with_ask_for_a_delegated_store_without_credentials():
    """#378: a store composed with a `delegation:` block (query-as-the-signed-in-user / OBO)
    must not report /router/health as `healthy` for a caller who has no delegated credential
    registered - the verdict must agree with what /router/ask can actually do.

    Before the I3b revert, /probe/health read `_kinds_state` - a template `_State` whose
    `IdentityBroker` is created once and NEVER composed against, so its `_delegations` dict
    stays permanently empty. `access_for` has no error path for "this store needed a
    delegation and none was registered" - it silently falls through to a plain, non-delegated
    `AccessContext` (see IdentityBroker.access_for in identity_broker.py), so the health round
    trip against an in-memory csv canary "succeeded" and the endpoint answered `healthy`, even
    though the SAME store, asked for real, fails with StaticTokenExchange's
    "no static token for user ...". A green health check on a store the caller cannot query is
    exactly the affirmative-looking failure `_compose_manifest` exists to forbid (#200)."""
    os.environ["DEV_STATIC_TOKEN_OTHER"] = "tok-for-someone-else"   # deliberately not oid-a
    try:
        manifest = {"tenant": "acme", "stores": [
            {"id": "sales-db", "kind": "csv", "business_unit": "eng", "acl": ["oid-a"],
             "config": {"tables": _TABLE},
             "delegation": {"kind": "static",
                            "tokens": {"someone-else": "${DEV_STATIC_TOKEN_OTHER}"}}}]}
        client = TestClient(_app())
        r = client.post("/router/compose", json={"manifest": manifest}, headers=_hdr("oid-a"))
        assert r.status_code == 200, r.text

        # /router/ask fails for oid-a: no token registered for oid-a in the delegation map.
        a = client.post("/router/ask", json={"question": "total amount by region"},
                        headers=_hdr("oid-a")).json()
        outcomes = a.get("outcomes", [])
        assert outcomes and outcomes[0]["status"] == "error", a
        assert "no static token for user" in outcomes[0]["error"], a

        # /router/health on the SAME store, as the SAME caller, must NOT say healthy - it must
        # agree with /router/ask that this caller cannot round-trip this store.
        entry = {"id": "sales-db", "kind": "csv", "business_unit": "eng",
                 "config": {"tables": _TABLE}}
        h = client.post("/router/health", json={"entry": entry}, headers=_hdr("oid-a")).json()
        assert h["status"] != "healthy", (
            f"health said {h['status']!r} for a store the caller cannot actually query - "
            f"an affirmative-looking failure (#200): {h}")
        assert "no static token for user" in (h.get("remediation") or ""), h
        print("  PASS  /router/health agrees with /router/ask for a delegated store the "
              "caller has no credential for (degraded, not healthy) - #378 stays fixed")
    finally:
        os.environ.pop("DEV_STATIC_TOKEN_OTHER", None)


def test_shared_key_keeps_compose_as_one_query_as_another():
    client = TestClient(_app(per_user=False))     # dev-header rig semantics
    both = {"tenant": "acme", "stores": [
        {"id": "csv-a", "kind": "csv", "business_unit": "eng", "acl": ["oid-a", "oid-b"],
         "config": {"tables": _TABLE}}]}
    client.post("/router/compose", json={"manifest": both}, headers=_hdr("oid-a"))
    cat_b = client.get("/router/catalog", headers=_hdr("oid-b"))
    assert cat_b.status_code == 200 and "csv-a" in cat_b.text, \
        "dev rigs share one workspace - the alice/bob demo depends on it"
    print("  PASS  shared-key rigs keep compose-as-A-query-as-B (alice/bob demo intact)")


if __name__ == "__main__":
    test_second_users_compose_does_not_replace_firsts()
    test_workspace_rebuilds_from_stored_manifest_across_restart()
    test_get_manifest_returns_own_and_only_own()
    test_plaintext_password_in_compose_is_a_400_that_never_echoes()
    test_broken_manifest_store_is_a_503_not_an_empty_catalog()
    test_compose_that_cannot_persist_reports_503_not_success()
    test_rebuild_drops_a_plaintext_credential_store_without_killing_the_workspace()
    test_one_failed_manifest_write_does_not_503_the_router_for_everyone()
    test_cold_compose_composes_exactly_once()
    test_a_failed_cold_compose_leaves_the_stored_workspace_rebuildable()
    test_probe_and_health_read_the_callers_own_workspace()
    test_health_agrees_with_ask_for_a_delegated_store_without_credentials()
    test_shared_key_keeps_compose_as_one_query_as_another()
    print("OK selftest_workspace_isolation")
