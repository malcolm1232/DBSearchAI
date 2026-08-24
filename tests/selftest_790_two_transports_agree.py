"""#790 - REST and GraphQL must retrieve the SAME documents for the same identity.

THE DEFECT. `as_read_scope(value, default)` read `ReadScope(partition=value or default)`. `""`
is the FAIL-CLOSED partition - it matches no chunk, exactly as ADR 0012 requires - and it is
also falsy, so it was rewritten into the deployment constant. The comment three lines above the
statement swore it never did that.

The two transports differ only in WHAT THEY PASS, which is why one was safe and one was not:

    REST     app.py:/search  -> `_request_scope(request, user)`, a ReadScope OBJECT
                               -> the isinstance arm returns it untouched  -> FAIL CLOSED
    GraphQL  graphql_app.py  -> `resolve_tenant(...)`, a BARE STRING
                               -> `"" or "selfhost"` == "selfhost"          -> FAIL OPEN

Reachable by any signed-in user: `resolve_tenant` returns `""` for every NON-OPERATOR api key,
and anyone can mint one at POST /developer/keys. Measured before the fix, same identity and
same question: REST returned no documents, GraphQL returned the home partition's.

Severity, stated honestly: the ACL overlap still ran, so this was never an unauthenticated dump.
It was defence in depth collapsing from two predicates to one, plus a wrong-corpus correctness
bug - in the single property this repo claims to enforce everywhere.

WHY NO EXISTING TEST CAUGHT IT, which is the more useful half:

  * `selftest_tenant_partition.py` asserted `as_read_scope("").partition == ""` - the ONE-ARG
    form, where `default_partition` is itself `""`. It was structurally incapable of seeing the
    two-arg form that every real caller uses. Now fixed there.
  * `selftest_doc_store_owner_tenant.py` names `/search` and `/graphql` in its own docstring,
    but drives a RECORDING MOCK of QueryService - so it asserts which string was handed over
    and never reaches `as_read_scope` at all.

So this file exists to assert the property a USER would notice - the documents that come back -
across both transports at once, rather than the mechanism either one uses to get there.

    PYTHONPATH=src python3 tests/selftest_790_two_transports_agree.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.api.graphql_app import build_schema  # noqa: E402
from dbsearch.api.resolver import search_resolver  # noqa: E402
from dbsearch.ports.base import ReadScope, as_read_scope  # noqa: E402
from dbsearch.server.app import _edition, app  # noqa: E402

ALICE = "alice"
DOC = "hr-policy"
QUESTION = "how much parental leave is there"

client = TestClient(app)


def _seed():
    r = client.post("/ingest", headers={"X-DBSearch-User": ALICE},
                    json={"external_id": DOC, "title": "Parental leave",
                          "text": "Staff receive eighteen weeks of fully paid parental leave.",
                          "acl": ["all-staff"]})
    assert r.status_code == 200, r.text


def _graphql_docs(tenant_id):
    """The GraphQL path, through the resolver the schema actually calls."""
    d = search_resolver(_edition.query_service, ALICE, QUESTION, tenant_id=tenant_id)
    return sorted(d["retrieved_docs"])


def _rest_docs(tenant_id):
    """The REST path. `/search` passes `_request_scope(...)`, a ReadScope - so the shape under
    test here is the OBJECT, which is precisely the asymmetry that made REST the safe one."""
    r = _edition.query_service.answer(ALICE, QUESTION, tenant_id=tenant_id)
    return sorted(r.retrieved_docs)


def test_the_normalizer_distinguishes_none_from_empty():
    """The whole fix in two lines. `None` means "nothing was supplied"; `""` means "resolution
    ran and failed closed". `value or default` cannot tell them apart and `value is not None`
    can - which is why the near-miss fix `value if value is not None` is the one that works and
    `value or ...` is the one that shipped."""
    assert as_read_scope(None, "selfhost").partition == "selfhost", \
        "omitting a partition must still mean this service's own tenant"
    assert as_read_scope("", "selfhost").partition == "", \
        "a partition that RESOLVED TO EMPTY was widened into the deployment constant"
    assert as_read_scope(ReadScope(""), "selfhost").partition == "", \
        "the ReadScope arm was always fail-closed; it must stay that way"


def test_the_fixture_is_retrievable_at_all():
    """THE CONTROL, and without it every assertion below is vacuous.

    "GraphQL returned no documents" proves fail-closed only if the SAME question against the
    SAME index returns something when the partition is right. An empty index would satisfy the
    security assertions perfectly and prove nothing whatsoever."""
    assert _graphql_docs(None) == [DOC], \
        "the seeded document is not retrievable even on the default partition - " \
        "every other assertion in this file would pass vacuously"


def test_an_empty_partition_returns_NOTHING_over_graphql():
    """The defect, asserted on the thing a user would see. Before the fix this returned
    ['hr-policy'] - the home partition's document, handed to a caller whose partition
    resolution had explicitly failed closed."""
    assert _graphql_docs("") == [], \
        "GraphQL retrieved documents for a caller whose partition resolved to EMPTY " \
        "(fail-closed) - the LAW 2 partition predicate was skipped"


def test_the_two_transports_agree():
    """The property that matters, and the one nothing asserted before: for one identity and one
    question, the transport must not change the answer. Both directions are checked, so a
    future fix that closes GraphQL by ALSO breaking REST cannot pass."""
    for tenant, what in [("", "a partition that resolved to empty"),
                         (None, "no partition supplied"),
                         ("foreign-tid", "a foreign partition")]:
        rest = _rest_docs(ReadScope(tenant) if tenant is not None else None)
        gql = _graphql_docs(tenant)
        assert rest == gql, \
            f"REST and GraphQL disagree for {what}: REST={rest} GraphQL={gql}"


def test_a_foreign_partition_returns_nothing_either_way():
    """The neighbouring case, so the fix cannot be read as "empty is special". A partition that
    is not this deployment's matches no chunk on either transport."""
    assert _graphql_docs("foreign-tid") == []
    assert _rest_docs(ReadScope("foreign-tid")) == []


def test_the_graphql_schema_path_carries_the_same_answer():
    """One rung closer to the wire: through `build_schema`, the object the /graphql route
    actually executes, rather than the resolver function underneath it."""
    schema = build_schema(_edition.query_service)
    r = schema.execute_sync('{ search(question: "%s") { retrievedDocs } }' % QUESTION,
                            context_value={"user_oid": ALICE, "tenant_id": ""})
    assert r.errors is None, r.errors
    assert r.data["search"]["retrievedDocs"] == [], \
        "the mounted GraphQL schema still widens an empty partition"


def main():
    _seed()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = []
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            fails.append(t.__name__)
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{'FAILED' if fails else 'PASSED'} - {len(tests) - len(fails)} ok, "
          f"{len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
