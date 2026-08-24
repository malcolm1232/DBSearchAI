"""#478 - LAW 2 on the STRUCTURED rail, which the golden suite never exercised.

`golden_runner._sql_store` hardcoded `acl: [alice, bob]`, so every SQL store in the real
pack was visible to both identities. The doc rail has carried LAW2-001/002/003 since the
suite was built; the SQL rail had ZERO restricted items out of 38. The product's central
promise - never return a result a user is not authorized to see - was therefore verified
on documents only, and a structured-store regression would have passed the gate in
silence.

That is the worst shape of gap: not a failing test, an ABSENT one, behind a suite green
enough to be trusted.

This pins the boundary in-process (no rig, no model, deterministic) at all three places it
must hold, because "bob got no answer" can be true for boring reasons and would pass a
lazier test:

  1. VISIBILITY  - the restricted store is absent from bob's catalog entirely (gate #1).
  2. ROUTING     - bob's routing decision never NAMES it, in stores, candidates or reason.
                   A routing explanation that mentions a store bob cannot see is itself a
                   disclosure, even with no rows attached.
  3. ANSWER      - bob's answer and its whole serialized result carry neither the
                   restricted value nor the store id. Same blob surface the golden
                   scorer's `_leak_score` uses, so this test and the gate agree on what
                   "leak" means.

Alice is the control at every step: if she cannot answer, the test proves nothing about
bob, because a store nobody can read trivially leaks nothing.

Run: PYTHONPATH=src python3 tests/selftest_478_law2_sql_rail.py
"""
import json
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dbsearch.adapters.local import HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch.eval.golden import load_pack  # noqa: E402
from dbsearch.router.catalog import STORE, CatalogNode, StoreCatalog  # noqa: E402
from dbsearch.router.router_service import RouterQueryService  # noqa: E402
from dbsearch.router.structured import (  # noqa: E402
    CannotAnswerFromSchema, FederatedSqlStore, SqliteEngine,
)

SECRET = "3514142569"          # the restricted total; must never reach bob
PAYROLL_Q = "What was the total payroll across all clubs in the 2015 season?"
PUBLIC_Q = "How many clubs are in the register?"


class _Llm:
    """Answers from whatever it is given. Deliberately eager: a model that refuses
    everything would make a leak test pass for the wrong reason."""

    def answer(self, question, context):
        return {"answer": " | ".join(str(c) for c in context) or "no context"}


def _gen(mapping):
    def gen(question, schema):
        if question in mapping:
            return mapping[question]
        raise CannotAnswerFromSchema("this store holds nothing of that kind")
    return gen


def _fixture():
    payroll = FederatedSqlStore(
        "baseball-payroll", "sports", "Payroll",
        "salaries payroll pay compensation clubs season totals",
        SqliteEngine.from_tables({"salaries": {
            "columns": ["yearID", "teamID", "salary"],
            "rows": [[2015, "LAN", 3514142569], [2014, "BOS", 100]]}}),
        sql_generator=_gen({PAYROLL_Q: "SELECT SUM(salary) FROM salaries WHERE yearID=2015"}))
    public = FederatedSqlStore(
        "baseball-public", "sports", "Clubs",
        "clubs teams register names divisions",
        SqliteEngine.from_tables({"teams": {
            "columns": ["teamID", "name"], "rows": [["LAN", "Dodgers"], ["BOS", "Red Sox"]]}}),
        sql_generator=_gen({PUBLIC_Q: "SELECT COUNT(*) FROM teams"}))

    cat = StoreCatalog()
    cat.register(CatalogNode(id="t", kind="tenant", parent_id=None, acl=["alice", "bob"]))
    cat.register(CatalogNode(id="baseball-payroll", kind=STORE, parent_id="t",
                             acl=["alice"], profile=payroll.profile(), store=payroll))
    cat.register(CatalogNode(id="baseball-public", kind=STORE, parent_id="t",
                             acl=["alice", "bob"], profile=public.profile(), store=public))
    identity = InMemoryIdentity({"alice": ["alice"], "bob": ["bob"]})
    return RouterQueryService(cat, identity, HashingEmbedding()), cat


# --- the control: without this, nothing below means anything --------------------------

def test_alice_CAN_read_the_restricted_store():
    """A store nobody can read leaks nothing trivially. Alice answering is what makes
    bob's silence evidence of a boundary rather than of an empty fixture."""
    svc, _ = _fixture()
    result = svc.ask("alice", PAYROLL_Q, _Llm())
    assert SECRET in json.dumps(result.answer), result.answer


# --- 1. visibility --------------------------------------------------------------------

def test_the_restricted_store_is_absent_from_bobs_catalog():
    _, cat = _fixture()
    assert "baseball-payroll" in {n.id for n in cat.visible_stores(["alice"])}
    assert "baseball-payroll" not in {n.id for n in cat.visible_stores(["bob"])}


# --- 2. routing -----------------------------------------------------------------------

def test_bobs_routing_never_names_the_restricted_store():
    """Gate #1. Naming an invisible store in an explanation is a disclosure even when no
    row is attached - it tells bob the store exists and is relevant to his question."""
    svc, _ = _fixture()
    decision = svc.route("bob", PAYROLL_Q)
    named = ([s.store_id for s in decision.stores]
             + [c.store_id for c in decision.candidates])
    assert "baseball-payroll" not in named, named
    assert "baseball-payroll" not in (decision.reason or ""), decision.reason


# --- 3. answer ------------------------------------------------------------------------

def test_bob_gets_neither_the_value_nor_the_store_id_anywhere_in_the_result():
    """The same blob surface the golden scorer's _leak_score uses - the WHOLE serialized
    result, not just the prose - so this test and the gate cannot disagree about what a
    leak is."""
    svc, _ = _fixture()
    result = svc.ask("bob", PAYROLL_Q, _Llm())
    blob = json.dumps({"answer": result.answer, "evidence": result.evidence,
                       "citations": result.citations, "outcomes": result.outcomes,
                       "disclosure": result.disclosure}, default=str).lower()
    assert SECRET not in blob, blob[:400]
    assert "baseball-payroll" not in blob, blob[:400]


def test_bob_still_gets_his_own_authorized_answer():
    """LAW 2 is a trim, not a mute. Over-restricting would also pass the leak tests."""
    svc, _ = _fixture()
    result = svc.ask("bob", PUBLIC_Q, _Llm())
    assert "2" in json.dumps(result.answer), result.answer


# --- the pack itself: guard against the hardcode coming back --------------------------

def test_the_real_pack_gives_a_restricted_sql_store_an_alice_only_acl():
    """#478's root cause was `acl: [alice, bob]` hardcoded in `_sql_store`. If that
    returns, every test above still passes - they build their own catalog - while the
    GATE silently stops exercising LAW 2 again. So the pack manifest is asserted too."""
    from golden_runner import pack_manifest

    pack = load_pack(Path(__file__).resolve().parents[1]
                     / "eval_fixtures" / "golden_pack_real")
    manifest = pack_manifest(pack, "alice", "bob")
    acls = {s["id"]: s["acl"] for s in manifest["stores"]}
    restricted = [sid for sid, acl in acls.items() if acl == ["alice"]]
    assert restricted, f"no SQL store is restricted; LAW 2 is untested on this rail: {acls}"
    shared = [sid for sid, acl in acls.items() if sorted(acl) == ["alice", "bob"]]
    assert shared, f"every store is restricted - bob's catalog would be empty: {acls}"


def test_the_real_pack_has_restricted_items_that_target_a_restricted_store():
    """A restricted STORE with no restricted QUESTION tests nothing: the runner only
    re-asks as bob when protection == 'restricted'."""
    from golden_runner import pack_manifest

    root = Path(__file__).resolve().parents[1] / "eval_fixtures" / "golden_pack_real"
    pack = load_pack(root)
    manifest = pack_manifest(pack, "alice", "bob")
    alice_only = {s["id"] for s in manifest["stores"] if s["acl"] == ["alice"]}
    items = [q for q in pack.questions if q.protection == "restricted"]
    assert items, "the SQL pack has no restricted items - bob is never re-asked"
    for q in items:
        assert set(q.expect_stores) & alice_only, (
            f"{q.id} is restricted but targets no alice-only store: {q.expect_stores}")
        assert q.forbidden_facts, f"{q.id} has no forbidden_facts - a leak is unscoreable"


def test_every_gold_sql_in_the_pack_is_scoreable():
    """Guard for the trap LAW2-102 fell into. `_execution_accuracy_score` does
    `float(gold)` unconditionally whenever an item has a gold_sql, so a TEXT-valued
    gold_sql raises deep inside scoring - which the runner turns into an inscrutable
    per-item ERROR. The pack's convention for a text answer is key_facts plus an entry in
    pack_meta["derivations"], and NO gold_sql (see C-004). This asserts the convention
    holds rather than trusting the next author to know it."""
    from dbsearch.eval.golden import gold_value

    pack = load_pack(Path(__file__).resolve().parents[1]
                     / "eval_fixtures" / "golden_pack_real")
    bad = []
    for q in pack.questions:
        if not q.gold_sql:
            continue
        try:
            float(gold_value(pack.tables, q.gold_sql))
        except (TypeError, ValueError) as exc:
            bad.append(f"{q.id}: {exc}")
    assert not bad, ("gold_sql must return a NUMERIC first cell; use key_facts + "
                     "pack_meta['derivations'] for a text answer:\n" + "\n".join(bad))


def _real_pack_root() -> Path:
    return Path(__file__).resolve().parents[1] / "eval_fixtures" / "golden_pack_real"


def _real_pack_available() -> bool:
    """#685/#692: the real pack is no longer shipped, so its tests SKIP rather than fail.

    It is built from three third-party Kaggle datasets (Olist, MovieLens, Baseball Databank)
    whose licences do not permit us to redistribute them - MovieLens' forbid it outright - so
    the pack is gitignored and lives only on machines that built it. `scripts/build_real_pack.py`
    regenerates it from your own download.

    The five assertions ABOVE this line are unaffected: they build their own catalog in
    process, so LAW 2 on the SQL rail is still verified on a fresh clone. Only the three
    guards that inspect the shipped pack's shape need the pack itself.
    """
    return (_real_pack_root() / "pack_meta.json").exists()


def main():
    test_alice_CAN_read_the_restricted_store()
    print("  PASS  control: alice CAN read the restricted store, so bob's silence is a "
          "boundary and not an empty fixture")
    test_the_restricted_store_is_absent_from_bobs_catalog()
    test_bobs_routing_never_names_the_restricted_store()
    test_bob_gets_neither_the_value_nor_the_store_id_anywhere_in_the_result()
    test_bob_still_gets_his_own_authorized_answer()
    print("  PASS  #478 LAW 2 holds on the SQL rail at all three layers - visibility, "
          "routing explanation, and the whole serialized answer - and bob keeps his own")
    if _real_pack_available():
        test_the_real_pack_gives_a_restricted_sql_store_an_alice_only_acl()
        test_the_real_pack_has_restricted_items_that_target_a_restricted_store()
        test_every_gold_sql_in_the_pack_is_scoreable()
        print("  PASS  #478 the real pack now exercises it: a restricted SQL store, and "
              "restricted items that target it with scoreable forbidden facts")
    else:
        print("  SKIP  eval_fixtures/golden_pack_real is not present - the three pack-shape "
              "guards need it. It is built from third-party datasets we may not redistribute "
              "(#692); run scripts/build_real_pack.py to rebuild it locally. The LAW 2 "
              "assertions above ran in full and do not depend on it.")
    print("\n#478 LAW 2 SQL-RAIL SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
