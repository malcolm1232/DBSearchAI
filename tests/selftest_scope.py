"""#336 - the demo/live scope seam (ADR 0009, hardened after #340).

RequestScope bundles principal, identity provider, catalog, service, chat model - resolved
at ONE seam above resolve_identity and injected into demo-safe read endpoints. Proves:
  - demo scope bundles demo catalog, service, llm, and an InMemoryIdentity with demo groups;
  - live scope bundles live catalog, service, default chat model, and the edition's identity;
  - catalog/service are lazy callables (building a scope never composes or 409s).

Run: PYTHONPATH=src python3 tests/selftest_scope.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
# Hermetic default model (ExtractiveLlm) regardless of a dev machine's local env
for _k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "DBSEARCH_FORCE_EXTRACTIVE"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.server.edition import build_edition  # noqa: E402
from dbsearch.server.scope import RequestScope, make_scope_builder  # noqa: E402


def _edition():
    return build_edition()


class _FakeDemoCatalog:
    catalog = "DEMOCAT"
    service = "DEMOSVC"


def _builder(edition):
    calls = {"demo": 0}

    def demo_catalog():
        calls["demo"] += 1
        return _FakeDemoCatalog

    build = make_scope_builder(
        edition=edition, demo_catalog=demo_catalog,
        live_catalog=lambda user: "LIVECAT", live_service=lambda user: "LIVESVC",
        demo_user_groups={"alice": ["all-staff", "deal-team"], "bob": ["all-staff"]})
    return build, calls


def test_demo_scope_bundles_only_demo_collaborators():
    edition = _edition()          # the build_edition() helper from the preamble
    build, _ = _builder(edition)
    s = build("demo:alice")
    assert s.kind == "demo" and s.user == "demo:alice" and s.principal == "alice"
    assert s.catalog() == "DEMOCAT" and s.service() == "DEMOSVC"
    assert s.chat_llm is edition.demo_chat_llm
    # THE #340 property: demo groups come from the demo map, never edition.identity
    assert s.identity is not edition.identity
    assert set(s.groups()) == {"alice", "all-staff", "deal-team"}
    print("  PASS  demo scope bundles demo catalog/service/llm/identity")


def test_live_scope_bundles_only_live_collaborators():
    edition = _edition()
    build, _ = _builder(edition)
    s = build("11112222-3333-4444-5555-666677778888")
    assert s.kind == "live" and s.principal == s.user
    assert s.identity is edition.identity
    assert s.catalog() == "LIVECAT" and s.service() == "LIVESVC"
    assert s.chat_llm is edition.chat_models[edition.chat_model_default]
    print("  PASS  live scope bundles live catalog/service/llm/identity")


def test_scope_build_is_lazy():
    # /router/demo serves a live user with NO composed catalog; building a scope
    # must therefore never compose or 409 eagerly.
    build, calls = _builder(_edition())
    build("demo:alice")
    build("some-oid")
    assert calls["demo"] == 0, "building a scope must not compose the demo catalog"
    print("  PASS  scope build is lazy (no compose, no 409 at build time)")


def test_live_catalog_and_service_receive_the_user():
    # #368: the live catalog/service must be a function of the requesting user, so
    # each user's live world can differ - this is the seam Task 5 wires to the pool.
    edition = _edition()
    seen = []
    build = make_scope_builder(
        edition=edition, demo_catalog=lambda: _FakeDemoCatalog,
        live_catalog=lambda user: seen.append(("cat", user)) or f"catalog-of-{user}",
        live_service=lambda user: seen.append(("svc", user)) or f"service-of-{user}",
        demo_user_groups={})
    scope = build("oid-a")
    assert scope.catalog() == "catalog-of-oid-a"
    assert scope.service() == "service-of-oid-a"
    scope_b = build("oid-b")
    assert scope_b.catalog() == "catalog-of-oid-b", \
        "#368: the live catalog must be a function of the requesting user"
    assert ("cat", "oid-a") in seen and ("cat", "oid-b") in seen
    print("  PASS  live catalog/service are functions of the requesting user")


def main():
    print("RequestScope (#336 Task 1) self-test:")
    test_demo_scope_bundles_only_demo_collaborators()
    test_live_scope_bundles_only_live_collaborators()
    test_scope_build_is_lazy()
    test_live_catalog_and_service_receive_the_user()
    print("\nSCOPE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
