"""#432: POST /graphql must work at the ADVERTISED path, with no trailing slash.

The GraphQL API was dead in production - `POST https://dbsearch.ai/graphql` returned
405 {"detail":"Method Not Allowed"} and GET returned 404 - while `POST /graphql/` worked fine.

Why: it was wired with `app.mount("/graphql", ...)`, and Starlette compiles a Mount's path to
`^/graphql(?P<path>/.*)$` - the bare `/graphql` does NOT match. Normally the router's
redirect_slashes would 307 to `/graphql/`, but this app ends with a catch-all `Mount("")`
serving the SPA/static files, which matches first and answers 405 (static files allow only
GET/HEAD) or 404. The catch-all silently swallowed the redirect that would have saved it.

Two reasons this must be routes and not a Mount, even if the redirect worked:
  - `/graphql` is the path the Developer surface (#29) and the docs advertise; clients POST there.
  - A 307 on POST is a trap regardless: not every HTTP client replays the body on redirect.

Three LAW-2 boundary tests (selftest_server, selftest_demo_scope_boundary,
selftest_devheader_login_exclusion) were red for this reason - which means the identity
enforcement on /graphql was UNPROVEN for as long as the surface was dead. Those tests assert
the semantics; this one only pins the path, because that is what regressed and nothing covered.

    PYTHONPATH=src python3 tests/selftest_graphql_mount_path.py
"""
import os
import sys
from pathlib import Path

os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi.testclient import TestClient

from dbsearch.server.app import app
from _route_walk import iter_routes  # noqa: E402

client = TestClient(app)
QUERY = {"query": "{__typename}"}


def test_post_graphql_without_a_trailing_slash_is_served():
    """THE REGRESSION. 405 here means the catch-all answered instead of GraphQL."""
    r = client.post("/graphql", json=QUERY, headers={"X-DBSearch-User": "alice"})
    assert r.status_code != 405, (
        "POST /graphql got 405 - the request fell through to the static-files catch-all, "
        "which is exactly the production outage this test exists to prevent")
    assert r.status_code == 200, r.text
    assert "data" in r.json() or "errors" in r.json(), r.text


def test_it_is_a_real_route_not_a_prefix_mount():
    """A Mount cannot answer the bare path, so the route table itself is the invariant. Asserted
    structurally so the failure names the cause instead of just showing a 405."""
    # #696: /graphql is registered via include_router(prefix="/graphql"), and FastAPI made
    # that lazy - app.routes holds a wrapper, not the child routes - so a flat walk finds
    # nothing here and reports the route missing when it is present and serving.
    exact = [r for r, full, _d in iter_routes(app.routes) if full == "/graphql"]
    assert exact, "no route registered at exactly /graphql (a Mount does not count)"
    methods = set()
    for r in exact:
        methods |= set(getattr(r, "methods", None) or [])
    assert "POST" in methods, f"/graphql must accept POST, got {sorted(methods)}"


def test_the_catch_all_is_still_last():
    """The SPA/static catch-all must remain the final route. If anything is registered after it,
    that thing is unreachable - this is the shape of the bug, not just this instance of it."""
    # Deliberately a FLAT walk: this invariant is about the order of the app's OWN route
    # list, so unwrapping included routers would destroy the index it asserts on. But the
    # lazy include wrappers have no `path`, and defaulting them to "" made them look like
    # catch-all Mounts (#696) - so they are skipped by identity, not by path string.
    top = [r for r in app.routes if getattr(r, "original_router", None) is None]
    paths = [getattr(r, "path", "") for r in top]
    catch_alls = [i for i, p in enumerate(paths) if p == ""]
    if catch_alls:
        assert catch_alls[-1] == len(paths) - 1, (
            f"a catch-all Mount('') sits at index {catch_alls[-1]} of {len(paths)} routes - "
            f"everything after it is shadowed: {paths[catch_alls[-1] + 1:]}")



def test_a_refusal_is_spoken_in_graphql():
    """The refusal happens at the transport (Depends(current_user), #432) - but a GraphQL
    client reads `errors[]`, not `{"detail": ...}`. Both halves of the contract are pinned:
    the HTTP status STAYS 401/403 (proxies, logs and monitoring must see a refusal, and a
    200 must never carry a failure), and the body is the GraphQL envelope with an
    `extensions.code` a client can branch on. Regressing either half breaks a different
    consumer."""
    import os

    saved = {k: os.environ.get(k) for k in
             ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET")}
    try:
        # real login configured -> the dev header must NOT authenticate (#183), so this
        # request is genuinely unauthenticated even though it carries the dev header.
        os.environ.update({"AUTH_TENANT_ID": "t", "AUTH_CLIENT_ID": "c",
                           "AUTH_CLIENT_SECRET": "s"})
        r = client.post("/graphql", json=QUERY, headers={"X-DBSearch-User": "alice"})
        assert r.status_code in (401, 403), (r.status_code, r.text)
        body = r.json()
        assert body.get("data") is None, body
        errs = body.get("errors")
        assert errs and errs[0].get("message"), f"no GraphQL errors envelope: {body}"
        assert errs[0].get("extensions", {}).get("code") in ("UNAUTHENTICATED", "FORBIDDEN"), body
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

if __name__ == "__main__":
    test_post_graphql_without_a_trailing_slash_is_served()
    test_it_is_a_real_route_not_a_prefix_mount()
    test_the_catch_all_is_still_last()
    test_a_refusal_is_spoken_in_graphql()
    print("OK selftest_graphql_mount_path")
