"""#266 — a restart must not silently strip a user's group memberships.

Two pieces of state die when the dev server restarts, but only ONE is surfaced:

  - TokenVault (in-memory by design, #210) -> the header honestly says "session expired —
    sign in again to query". Good.
  - the identity adapter's group registrations (set_user_groups, populated only at sign-in)
    -> NOTHING says anything. A document ACL'd to a GROUP the user genuinely belongs to just
    answers "I couldn't find anything you have access to about that."

The second is the #255 failure mode again: an abstention that SOUNDS careful and is false. The
user is in "DBSearch — Admin access", the document is ACL'd to it, and the product denies them.
It was hidden until #258 moved the demo docs onto group ACLs — before that, docs were ACL'd to
raw user OIDs, which need no group lookup. And it is not a memory-backend quirk: any deployment
whose identity adapter caches memberships in-process has the same hole after a restart or a
scale-out to a fresh worker.

Fix: resolve lazily. Group expansion uses the APP-ONLY Graph token, not the user's delegated
one, so memberships can be restored from a valid session WITHOUT the user re-authenticating.

Run: python3 tests/selftest_group_resolution.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.adapters.local import InMemoryIdentity  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402


def test_a_lookup_failure_is_distinguishable_from_no_groups():
    """The whole fix rests on this. If a failed Graph call is indistinguishable from "this user
    is in no groups", caching it lazily turns a transient blip into a PERMANENT silent denial
    for the process lifetime — strictly worse than the bug being fixed."""
    def boom(_tenant):
        raise RuntimeError("graph unreachable")

    assert user_auth.fetch_member_principals("t", "oid", token_fn=boom) is None, \
        "a failed lookup must return None, never [] — [] means 'genuinely no groups'"


def test_the_identity_knows_whether_it_has_ever_looked():
    """expand_groups() returns [oid] both for 'no groups' and for 'never asked'. Something has
    to tell those apart, or the lazy resolve either never fires or fires on every request."""
    ident = InMemoryIdentity({})
    assert ident.knows_groups("alice") is False, "never asked, but claims to know"

    ident.set_user_groups("alice", ["g1"])
    assert ident.knows_groups("alice") is True

    # a user genuinely in NO groups is still KNOWN — otherwise every request re-queries Graph
    ident.set_user_groups("bob", [])
    assert ident.knows_groups("bob") is True, \
        "an empty-but-resolved membership must count as known, or we re-query forever"
    assert ident.expand_groups("bob") == ["bob"]


def test_a_failed_resolution_is_not_cached_as_empty():
    """A Graph outage must leave the user UNRESOLVED so the next request retries — not
    registered as 'no groups', which would deny them until the process restarts."""
    from dbsearch.server.app import _resolve_groups_if_unknown

    ident = InMemoryIdentity({})
    calls = {"n": 0}

    def failing(_tenant, _oid, token_fn=None):
        calls["n"] += 1
        return None                      # lookup failed

    _resolve_groups_if_unknown(ident, "alice", "tenant", fetch=failing)
    assert ident.knows_groups("alice") is False, \
        "a failed lookup was cached — the user is now denied until the process restarts"

    # ...and the next request tries again
    _resolve_groups_if_unknown(ident, "alice", "tenant", fetch=failing)
    assert calls["n"] == 2, f"a failed lookup was not retried (calls={calls['n']})"


def test_groups_are_resolved_once_then_served_from_cache():
    from dbsearch.server.app import _resolve_groups_if_unknown

    ident = InMemoryIdentity({})
    calls = {"n": 0}

    def ok(_tenant, _oid, token_fn=None):
        calls["n"] += 1
        return ["admin-access-oid"]

    for _ in range(4):
        _resolve_groups_if_unknown(ident, "alice", "tenant", fetch=ok)

    assert calls["n"] == 1, f"Graph was queried {calls['n']}x — resolution is not cached"
    assert ident.expand_groups("alice") == ["alice", "admin-access-oid"]


def test_expansion_asks_for_every_principal_type_not_just_groups():
    """#872. getMemberGroups returns GROUPS ONLY. The owner's whole SharePoint library was ACL'd
    to `d99e09e2…`, which SharePoint reports under the identitySet's `group` key and which is
    really the DIRECTORY ROLE "Global Administrator" — so getMemberGroups could never return it,
    the trim denied him his own documents, and no amount of Graph consent would have helped.
    getMemberObjects returns groups AND roles AND administrative units.

    Asserts the URL actually requested, not a mock's return value: the whole defect was WHICH
    ENDPOINT was called, so a test that stubs the response proves nothing about the bug."""
    seen = {}

    class _Resp:
        def read(self):
            return b'{"value": ["group-1", "role-1"]}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def spy_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return _Resp()

    real = user_auth.urllib.request.urlopen
    user_auth.urllib.request.urlopen = spy_urlopen
    try:
        got = user_auth.fetch_member_principals("t", "the-oid", token_fn=lambda _t: "tok")
    finally:
        user_auth.urllib.request.urlopen = real

    assert got == ["group-1", "role-1"]
    assert seen["url"].endswith("/users/the-oid/getMemberObjects"), (
        f"expansion called {seen['url']} — getMemberGroups cannot return a directory role, "
        "which is exactly what the owner's documents were ACL'd to (#872)")
    assert seen["body"] == {"securityEnabledOnly": False}, \
        "securityEnabledOnly must stay False, or mail-enabled groups drop out of the trim"


def test_getByIds_carries_the_principal_KIND_not_just_the_name():
    """#881, link 1 of 3: capture the type where it is the only time it exists.

    getMemberObjects hands back bare oids, and a directory-role GUID is shaped exactly like a
    group's - so this response is the single moment in the whole product where a principal's
    type is knowable. It was being parsed and dropped one line below where it arrived, which
    is why the ACL picker offered the tenant's "Global Administrator" role as a group.

    Drives the real parser against a real Graph body rather than a stub of the return value:
    the defect was in what the parse KEPT, so asserting on a mocked return would prove
    nothing (the #872 test above records the same lesson about the same function)."""
    class _Resp:
        def read(self):
            return json.dumps({"value": [
                {"id": "role-1", "displayName": "Global Administrator",
                 "@odata.type": "#microsoft.graph.directoryRole"},
                {"id": "group-1", "displayName": "Deal Team",
                 "@odata.type": "#microsoft.graph.group"},
                {"id": "user-1", "userPrincipalName": "dana@x.test",
                 "@odata.type": "#microsoft.graph.user"},
            ]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = user_auth.urllib.request.urlopen
    user_auth.urllib.request.urlopen = lambda req, timeout=None: _Resp()
    try:
        got = user_auth.fetch_principal_facts("t", ["role-1", "group-1", "user-1"],
                                              token_fn=lambda _t: "tok")
    finally:
        user_auth.urllib.request.urlopen = real

    assert got["role-1"] == {"name": "Global Administrator", "kind": "directoryRole"}, \
        f"the role's type was dropped: {got.get('role-1')}"
    assert got["group-1"]["kind"] == "group", f"a group was mistyped: {got.get('group-1')}"
    assert got["user-1"] == {"name": "dana@x.test", "kind": "user"}, \
        f"a user's upn/type was dropped: {got.get('user-1')}"


def test_a_role_reaches_the_picker_labelled_a_role_and_not_a_group():
    """#881, links 2 and 3: persist the kind, then USE it.

    The end-to-end property, driven through the identity port the way the sign-in path does -
    because links 1-3 each look correct alone and the bug lived in the JOIN between them. The
    negative half is what makes it bite: before #881 `list_directory` hardcoded kind="group"
    for every non-user principal, so asserting only that the role is PRESENT passed against
    the defect. Deleting the store, or reverting list_directory to the literal, turns this
    red; #872's own guards do not, because a mislabelled principal is still a named one."""
    ident = InMemoryIdentity({"dana": ["role-1", "group-1"]})
    ident.set_principal_name("role-1", "Global Administrator")
    ident.set_principal_name("group-1", "Deal Team")
    ident.set_principal_name("dana", "Dana")
    ident.set_principal_kind("role-1", "directoryRole")
    ident.set_principal_kind("group-1", "group")

    by_oid = {p.oid: p for p in ident.list_directory()}
    assert by_oid["role-1"].kind == "directoryRole", \
        f"the ACL picker still calls a directory role a {by_oid['role-1'].kind!r} (#881)"
    assert by_oid["group-1"].kind == "group", "a real group must stay a group"
    assert by_oid["dana"].kind == "user", "the user side of the directory must be unchanged"


def test_an_unknown_kind_keeps_the_pre_881_label_rather_than_vanishing():
    """#881 fail-soft. A principal whose kind was never learned - a composed dev rig, a seed,
    any port with no set_principal_kind - must keep the label it had before this change. The
    kind is COSMETIC (LAW 2 compares oids), so the failure direction for an unknown type is
    "says group like it always did", never "drops out of the picker", which is the one
    outcome #872 spent a whole card preventing."""
    ident = InMemoryIdentity({"dana": ["mystery-1"]})
    ident.set_principal_name("mystery-1", "Some Principal")
    got = [p for p in ident.list_directory() if p.oid == "mystery-1"]
    assert got and got[0].kind == "group", \
        f"an untyped principal must stay pickable and labelled: {got}"


def test_a_directory_role_can_be_NAMED_or_it_vanishes_from_the_picker():
    """#872 second half. getByIds returns ONLY the types it is asked for, and answers a partial
    list with no error — with types ['group','user'] the role simply was not in the response.
    An unnamed principal is dropped by list_directory, so the ACL picker would silently omit a
    principal that now genuinely grants access: authorized, and invisible to whoever manages it.
    Verified against live Graph 2026-08-20 before being written down."""
    assert "directoryRole" in user_auth._NAMEABLE_TYPES, \
        "a directory role must be nameable, or it expands access the picker cannot show"
    assert {"group", "user"} <= set(user_auth._NAMEABLE_TYPES), \
        "groups and users must stay nameable — this widened the set, it must not narrow it"


def test_a_sign_in_never_caches_a_FAILED_lookup_as_an_answer():
    """#875, and the clause the sign-in path got wrong for months while the lazy path got it
    right. /auth/callback registered `fetch(...) or []`, which writes a Graph FAILURE into the
    cache as "resolved, belongs to nothing" — and set_user_groups then makes knows_groups true,
    so the retry above can never fire again. Permanent silent denial, from one transient 403.

    Driven through _resolve_groups_if_unknown with force=True, which is exactly what the
    callback now calls; the old code path cannot satisfy this."""
    from dbsearch.server.app import _resolve_groups_if_unknown

    ident = InMemoryIdentity({})
    _resolve_groups_if_unknown(ident, "alice", "tid", fetch=lambda *_a: None,
                               session_tid="tid", force=True)
    assert ident.knows_groups("alice") is False, \
        "a sign-in during a Graph outage cached the failure — alice is denied until a restart"
    assert ident.expand_groups("alice") == ["alice"], "fail-closed: never MORE than the oid"

    # and the next request repairs it, which is the whole point of not caching the failure
    _resolve_groups_if_unknown(ident, "alice", "tid", fetch=lambda *_a: ["g1"],
                               session_tid="tid")
    assert ident.expand_groups("alice") == ["alice", "g1", "tenant:tid"], \
        f"the retry did not restore the membership: {ident.expand_groups('alice')}"


def test_the_REAL_callback_route_never_caches_a_failed_lookup():
    """#875, driven through the actual /auth/callback route.

    The test above drives `_resolve_groups_if_unknown` directly, and mutation testing showed
    that is NOT ENOUGH: restoring the callback's own `or []` copy left it green, because the
    fixture never reached the sink. The defect lived in the ROUTE, so the guard has to drive
    the route. A guard that cannot fail is not a guard, and this one could not.

    Graph fails; the callback must complete the sign-in (a session cookie is still minted -
    a directory outage is not a login failure) while registering NOTHING, so the chokepoint
    retries on the next request instead of denying this user until the process restarts."""
    import os
    import time                                       # noqa: F401 - parity with the harness
    from fastapi.testclient import TestClient
    from dbsearch.server import app as app_module
    from dbsearch.server.app import app

    oid = "875-callback-oid"
    saved = (app_module._state_ok, user_auth.exchange_code,
             user_auth.fetch_member_principals, user_auth.fetch_principal_facts)
    env = {k: os.environ.get(k) for k in ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET")}
    os.environ.update({"AUTH_TENANT_ID": "tid-875", "AUTH_CLIENT_ID": "cid",
                       "AUTH_CLIENT_SECRET": "sec"})
    app_module._state_ok = lambda request, params: True
    user_auth.exchange_code = lambda code: {"oid": oid, "name": "Failed Lookup",
                                            "email": "f@x.test", "tid": "tid-875"}
    user_auth.fetch_member_principals = lambda tid, o: None      # THE GRAPH OUTAGE / 403
    user_auth.fetch_principal_facts = lambda tid, oids: {}
    client = TestClient(app)
    try:
        r = client.get("/auth/callback?code=c&state=s", follow_redirects=False)
        assert r.status_code == 302, (r.status_code, r.text)
        assert r.cookies.get(user_auth.COOKIE), \
            "a directory outage must not block the sign-in itself"

        ident = app_module._edition.identity
        assert ident.knows_groups(oid) is False, (
            "the sign-in cached a FAILED Graph lookup as 'resolved, belongs to nothing' - "
            "knows_groups is now true, so the chokepoint can never retry and this user is "
            "denied every group-ACL'd document until the process restarts (#875)")
        assert ident.expand_groups(oid) == [oid], \
            f"fail-closed: never more than the caller's own oid, got {ident.expand_groups(oid)}"
    finally:
        (app_module._state_ok, user_auth.exchange_code,
         user_auth.fetch_member_principals, user_auth.fetch_principal_facts) = saved
        for k, v in env.items():
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})
        client.cookies.clear()


def test_a_sign_in_RE_resolves_rather_than_trusting_a_stale_cache():
    """The other half of `force`, and it must be separately removable: without it the callback
    would skip on knows_groups and a user whose groups changed since the cached answer would
    keep the old expansion for the life of the process — the staleness #266 left behind."""
    from dbsearch.server.app import _resolve_groups_if_unknown

    ident = InMemoryIdentity({})
    ident.set_user_groups("alice", ["old-group"])
    _resolve_groups_if_unknown(ident, "alice", "tid", fetch=lambda *_a: ["new-group"],
                               session_tid="tid", force=True)
    assert ident.expand_groups("alice") == ["alice", "new-group", "tenant:tid"], \
        f"a fresh sign-in served a stale cached membership: {ident.expand_groups('alice')}"


def test_resolution_is_skipped_without_a_tenant():
    """No tenant configured (pure self-host, no Entra) — nothing to resolve against, and we
    must not stamp the user as 'known with no groups' on that basis."""
    from dbsearch.server.app import _resolve_groups_if_unknown

    ident = InMemoryIdentity({})
    called = {"n": 0}

    def spy(*a, **k):
        called["n"] += 1
        return []

    _resolve_groups_if_unknown(ident, "alice", "", fetch=spy)
    assert called["n"] == 0 and ident.knows_groups("alice") is False


def main():
    print("#266 group resolution survives a restart (without a re-sign-in):")
    test_a_lookup_failure_is_distinguishable_from_no_groups()
    test_the_identity_knows_whether_it_has_ever_looked()
    print("  PASS  a failed lookup is distinguishable from 'no groups', and the adapter knows "
          "whether it has ever looked")
    test_a_failed_resolution_is_not_cached_as_empty()
    test_groups_are_resolved_once_then_served_from_cache()
    test_resolution_is_skipped_without_a_tenant()
    print("  PASS  resolved once then cached; a failure retries instead of denying; no tenant "
          "means no resolution")
    test_expansion_asks_for_every_principal_type_not_just_groups()
    test_a_directory_role_can_be_NAMED_or_it_vanishes_from_the_picker()
    print("  PASS  #872 expansion calls getMemberObjects (roles too, not groups alone) and a "
          "directory role can be named")
    test_getByIds_carries_the_principal_KIND_not_just_the_name()
    test_a_role_reaches_the_picker_labelled_a_role_and_not_a_group()
    test_an_unknown_kind_keeps_the_pre_881_label_rather_than_vanishing()
    print("  PASS  #881 the principal KIND survives Graph -> store -> picker, and an unknown "
          "kind keeps its old label")
    test_a_sign_in_never_caches_a_FAILED_lookup_as_an_answer()
    test_the_REAL_callback_route_never_caches_a_failed_lookup()
    test_a_sign_in_RE_resolves_rather_than_trusting_a_stale_cache()
    print("  PASS  #875 a sign-in never caches a failed lookup, and does re-resolve a stale one")
    print("\nGROUP-RESOLUTION SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
