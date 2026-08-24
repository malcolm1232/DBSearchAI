"""#393: the answer surfaces may not report a RETRIEVAL count as an ENTITLEMENT claim.

The bug, measured on prod 260731 and reproduced here: every answer surface ends with a
line of the shape "Searched N documents you can access." N came from
`QueryResult.authorized_docs`, which despite its name holds the docs the query actually
RETRIEVED - the post-trim top-k for that one question. So:

  - the number changed when the QUESTION changed, though nothing about the user's access
    changed. "You can access" is a property of the caller, never of the question;
  - with an empty index it read "Searched 0 documents you can access", which is the same
    sentence a real permissions refusal would produce. That is what sent the operator
    hunting for an access bug when the truth was "nothing is indexed yet" (#392).

The fix this file pins has two halves, and both can regress independently:

  1. the field that carries retrieval is NAMED for retrieval (`retrieved_docs`), so no
     future reader can mistake it for an entitlement, and
  2. the answer response carries the corpus status - the honest denominator #392 shipped -
     computed through the SAME mandatory ACL predicate as search (LAW 2) and the SAME
     per-request tenant partition as retrieval (LAW 5, the #439 bug class).

    PYTHONPATH=src python3 tests/selftest_ask_footer_honesty.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")
os.environ.setdefault("DBSEARCH_DEV_AUTH", "1")

# A tenant app must be configured for `resolve_tenant` to honour the session's tid; with no
# real login configured every cookie collapses onto the deployment constant, and the
# partition assertions below would silently test nothing. Set before any cookie is minted:
# sign_session() keys off AUTH_CLIENT_SECRET.
os.environ.update({"AUTH_TENANT_ID": "home-tid", "AUTH_CLIENT_ID": "cid",
                   "AUTH_CLIENT_SECRET": "footer-test-secret"})

from fastapi.testclient import TestClient  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)

TID = "footer-tenant"
OID = "u-footer"
# ACL the fixtures to the caller's own principal. The in-memory directory expands an
# unknown oid to just itself, so a group name here would admit nobody and the rig would
# "pass" by retrieving nothing - which is the very state this file exists to tell apart.
GROUP = OID

# Deliberately more documents than the retrieval top-k (default 5), on two clearly
# different topics, so that NO single question can retrieve everything the user may see.
# That gap between "retrieved" and "entitled" is the whole subject of this card.
DOCS = [
    ("policy-leave", "Parental leave", "parental leave is sixteen weeks for all staff"),
    ("policy-remote", "Remote work", "remote work is allowed three days a week"),
    ("policy-expense", "Expenses", "expenses are reimbursed within thirty days"),
    ("policy-security", "Security", "laptops must use full disk encryption"),
    ("policy-travel", "Travel", "book travel through the corporate portal"),
    ("ship-hulls", "Hull maintenance", "hull plating is inspected every drydock cycle"),
    ("ship-engines", "Engine overhaul", "turbine bearings are replaced at ten thousand hours"),
    ("ship-navigation", "Navigation", "charts are updated from the weekly notices"),
]


def _cookie(tid: str = TID, oid: str = OID) -> dict:
    return {user_auth.COOKIE: user_auth.sign_session(
        {"oid": oid, "tid": tid, "exp": int(time.time()) + 3600})}


def _seed(cookies: dict) -> None:
    for ext, title, text in DOCS:
        r = client.post("/ingest", cookies=cookies, json={
            "external_id": ext, "title": title, "text": text,
            "acl": [GROUP], "uri": f"https://example.invalid/{ext}"})
        assert r.status_code == 200, (r.status_code, r.text)


def _ask(question: str, cookies: dict) -> dict:
    r = client.post("/chat", cookies=cookies,
                    json={"conv_id": f"c-{abs(hash(question))}", "question": question})
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


def test_retrieval_field_is_named_for_retrieval():
    """Half 1: the name. `authorized_docs` on a retrieval result is the lie's origin - it
    invites exactly the mislabel this card is about, and a comment cannot stop the next
    reader. The honest name is asserted at the dataclass, not just on the wire."""
    from dbsearch.query.service import QueryResult
    fields = QueryResult.__dataclass_fields__
    assert "retrieved_docs" in fields, (
        "QueryResult must name its retrieval list `retrieved_docs`; `authorized_docs` on a "
        "retrieval result is what produced the false entitlement claim in #393")
    print("  PASS  QueryResult names retrieval `retrieved_docs`")


def test_entitlement_count_does_not_move_with_the_question():
    """THE REGRESSION, stated exactly. Same caller, same permissions, two unrelated
    questions. What the caller MAY SEE cannot depend on what they ASKED - so if the
    surfaces are to say "documents you can access" at all, the number behind it must be
    identical across both asks. Before the fix the only count available was the top-k for
    that question, and these two differ."""
    ck = _cookie()
    _seed(ck)

    a = _ask("what is our parental leave policy", ck)
    b = _ask("how often are turbine bearings replaced", ck)

    for body, label in ((a, "leave question"), (b, "engine question")):
        assert "corpus" in body, (
            f"{label}: the answer response must carry the corpus status so the footer has an "
            "honest denominator; without it the surface can only report retrieval (#393)")

    assert a["corpus"]["authorized_docs"] == b["corpus"]["authorized_docs"] == len(DOCS), (
        "entitlement moved with the question: "
        f"{a['corpus']['authorized_docs']} vs {b['corpus']['authorized_docs']}, "
        f"expected {len(DOCS)} for both")

    # And the thing it must NOT be confused with: retrieval, which legitimately differs.
    assert a["retrieved_docs"] != [] and b["retrieved_docs"] != [], "both asks should retrieve"
    assert len(a["retrieved_docs"]) < len(DOCS), (
        "test rig is wrong: retrieval should be a strict subset of what the user may see, "
        "otherwise this test cannot tell the two numbers apart")
    print(f"  PASS  entitlement is stable at {len(DOCS)} across questions "
          f"(retrieved {len(a['retrieved_docs'])} vs {len(b['retrieved_docs'])})")


def test_empty_corpus_is_distinguishable_from_a_permissions_refusal():
    """The damaging case. A tenant with nothing indexed must be reported as an EMPTY
    CORPUS, not as "0 documents you can access" - the sentence a real refusal produces.
    Collapsing the two is the mislabel that cost an operator a debugging session (#392)."""
    ck = _cookie(tid="footer-empty-tenant", oid="u-empty")
    body = _ask("what is our parental leave policy", ck)
    assert "corpus" in body, "answer response must carry corpus status"
    assert body["corpus"]["indexed"] is False, (
        "an empty partition must report indexed=false so the surface can say 'nothing is "
        "indexed yet' instead of a permission-shaped sentence")
    assert body["corpus"]["authorized_docs"] == 0
    print("  PASS  empty corpus reports indexed=false (not a permissions refusal)")


def test_entitlement_is_trimmed_and_partitioned():
    """LAW 2 + LAW 5 in one: the denominator is computed with the caller's own principals
    through the per-request tenant, so it can neither overstate what a query would return
    nor count another tenant's documents. A count that leaked across the partition would
    be a fresh data-residency bug inside the fix for an honesty bug."""
    stranger = _cookie(tid="footer-other-tenant", oid="u-stranger")
    body = _ask("what is our parental leave policy", stranger)
    assert body["corpus"]["authorized_docs"] == 0, (
        "a foreign tenant must not be counted into this caller's denominator (LAW 5)")
    assert body["corpus"]["indexed"] is False, (
        "`indexed` must describe the CALLER's partition, not the box")
    print("  PASS  denominator is per-tenant and per-principal (LAW 2 + LAW 5)")


def main():
    print("Ask-footer honesty self-test (#393 - retrieval is not entitlement):")
    test_retrieval_field_is_named_for_retrieval()
    test_entitlement_count_does_not_move_with_the_question()
    test_empty_corpus_is_distinguishable_from_a_permissions_refusal()
    test_entitlement_is_trimmed_and_partitioned()
    print("ALL PASS")


if __name__ == "__main__":
    main()
