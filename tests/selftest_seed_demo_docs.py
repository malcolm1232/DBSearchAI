"""#270 — the demo seeder must never ACL a document to a group that is not there.

The first version pinned two group oids from one tenant. Correct there, silently wrong
everywhere else: a clone run against another directory would seed documents ACL'd to
principals that do not exist, report success, and then answer every question with "I couldn't
find anything you have access to". Working software, by every visible signal, serving nobody.

So the seeder resolves groups BY NAME at run time and refuses the whole run if either is
missing — refusing loudly beats seeding quietly.

Run: python3 tests/selftest_seed_demo_docs.py
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location("seed", ROOT / "scripts" / "seed_demo_docs.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


def test_no_group_oid_is_hardcoded():
    """The regression itself: a bare oid in this file is a tenant-specific landmine."""
    src = (ROOT / "scripts" / "seed_demo_docs.py").read_text()
    import re
    # a GUID outside a comment would be a pinned principal
    for line in src.splitlines():
        code = line.split("#", 1)[0]
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                             code), f"a group/user oid is hardcoded: {line.strip()!r}"


def test_resolve_group_returns_the_oid_for_an_exact_name_match():
    def fake_get(url, tok):
        return {"value": [{"id": "oid-director", "displayName": "DBSearch — Director access"}]}

    got = seed.resolve_group("DBSearch — Director access", "t",
                             get_json=fake_get, token_fn=lambda _t: "tok")
    assert got == "oid-director", got


def test_resolve_group_never_matches_a_similar_name():
    """Graph's $filter is exact, but if it ever returned a near miss we must not ACL to it —
    granting 'Director access' to something merely called 'Director accounts' would be a
    silent authorization error, the worst kind."""
    def fake_get(url, tok):
        return {"value": [{"id": "oid-other", "displayName": "DBSearch — Director accounts"}]}

    assert seed.resolve_group("DBSearch — Director access", "t",
                              get_json=fake_get, token_fn=lambda _t: "tok") is None


def test_resolve_group_returns_none_only_when_genuinely_absent():
    """#312: absent -> None, but a failed LOOKUP raises. These used to be the same answer,
    and that told an operator two groups were missing from a tenant that had both."""
    assert seed.resolve_group("Nope", "t", get_json=lambda u, k: {"value": []},
                              token_fn=lambda _t: "tok") is None

    def boom(url, tok):
        raise RuntimeError("graph down")

    for label, kwargs in (
        # the transport itself failed
        ("transport", {"get_json": boom, "token_fn": lambda _t: "tok"}),
        # Graph reports auth failures as a 200 body with an `error` key, not an exception
        ("graph error body", {"get_json": lambda u, k: {"error": {"code": "InvalidAuthenticationToken",
                                                                  "message": "ArgumentNull"}},
                              "token_fn": lambda _t: "tok"}),
        # app_token() returns "" rather than raising when the connector creds are absent
        ("empty token", {"get_json": lambda u, k: {"value": []}, "token_fn": lambda _t: ""}),
    ):
        try:
            seed.resolve_group("Nope", "t", **kwargs)
        except RuntimeError:
            continue
        raise AssertionError(f"{label}: a failed lookup returned instead of raising - "
                             "it will be misreported as 'group not in tenant'")


def test_the_group_names_are_overridable_without_editing_the_file():
    """A tenant that names its tiers differently must not have to patch the script."""
    src = (ROOT / "scripts" / "seed_demo_docs.py").read_text()
    assert "DBSEARCH_DIRECTOR_GROUP" in src and "DBSEARCH_ADMIN_GROUP" in src, \
        "no env override for the group names"


def test_it_refuses_to_seed_when_a_group_is_missing(monkeypatch=None):
    """The behaviour that matters: stop, and say what to create — never upload."""
    uploaded = []
    seed.upload = lambda *a, **k: uploaded.append(a) or (200, {})
    seed.resolve_group = lambda name, tenant, **k: None          # nothing resolves
    seed._e2e = lambda: (_ for _ in ()).throw(AssertionError("must not even sign in"))

    import os
    os.environ["AUTH_TENANT_ID"] = "t"
    sys.argv = ["seed_demo_docs.py"]
    rc = seed.main()

    assert rc == 2, f"expected refusal exit code 2, got {rc}"
    assert not uploaded, "a document was uploaded against an unresolved group"


def test_a_broken_lookup_refuses_with_its_own_exit_code():
    """#312: exit 3, NOT the exit-2 'create these groups' recipe — the groups may well exist."""
    uploaded = []
    seed.upload = lambda *a, **k: uploaded.append(a) or (200, {})
    seed.resolve_group = lambda name, tenant, **k: (_ for _ in ()).throw(
        RuntimeError("could not obtain a Graph app token"))
    seed._e2e = lambda: (_ for _ in ()).throw(AssertionError("must not even sign in"))

    import os
    os.environ["AUTH_TENANT_ID"] = "t"
    sys.argv = ["seed_demo_docs.py"]
    rc = seed.main()

    assert rc == 3, f"expected lookup-failure exit code 3, got {rc}"
    assert not uploaded, "a document was uploaded after the group lookup failed"


def main():
    print("#270 demo seeder resolves groups by name (never a pinned oid):")
    test_no_group_oid_is_hardcoded()
    test_the_group_names_are_overridable_without_editing_the_file()
    print("  PASS  no oid is hardcoded, and the names are env-overridable")
    test_resolve_group_returns_the_oid_for_an_exact_name_match()
    test_resolve_group_never_matches_a_similar_name()
    test_resolve_group_returns_none_only_when_genuinely_absent()
    print("  PASS  exact-name resolution only; absent -> None, broken lookup -> raises (#312)")
    test_it_refuses_to_seed_when_a_group_is_missing()
    print("  PASS  a missing group REFUSES the run (exit 2) instead of seeding docs nobody "
          "can read")
    test_a_broken_lookup_refuses_with_its_own_exit_code()
    print("  PASS  a broken LOOKUP refuses with exit 3, not the 'create these groups' recipe")
    print("\nSEED-DEMO-DOCS SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
