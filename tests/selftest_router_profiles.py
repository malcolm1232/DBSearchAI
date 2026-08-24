"""Phase E E2 — profile vectors + scoring + relative floor self-test.
Run: python3 tests/selftest_router_profiles.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import HashingEmbedding  # noqa: E402
from dbsearch.router.catalog import CatalogNode, STORE  # noqa: E402
from dbsearch.router.profiles import (  # noqa: E402
    cosine, ensure_profile_vector, profile_text, relative_floor, score_stores,
)
from dbsearch.router.store import INDEXED, SEMANTIC, StoreProfile  # noqa: E402


def _node(sid, bu, desc, topics):
    p = StoreProfile(store_id=sid, title=sid, description=desc, kind=INDEXED,
                     capabilities={SEMANTIC}, business_unit=bu, topics=topics)
    return CatalogNode(sid, STORE, sid + "--src", acl=[bu], profile=p, store=None)


def test_profile_text_includes_topics():
    p = StoreProfile(store_id="s", title="HR Wiki", description="policies",
                     kind=INDEXED, business_unit="hr", topics=["parental leave", "holidays"])
    t = profile_text(p)
    assert "HR Wiki" in t and "policies" in t and "parental leave" in t and "hr" in t, t


def test_profile_text_includes_schema_tables_and_columns():
    """#296: the strongest evidence of what an analytical store holds is its SCHEMA, but
    profile_text only ever used title/description/business_unit/topics. A store connected
    through the UI (which seeds an EMPTY description and no topics) therefore had a profile
    text of just its id -> it scored 0 against every question and never cleared the relevance
    floor, and the router mislabeled that unroutable store as "you don't have access". Fold the
    retrieved schema in so structure alone can route it — and split compound identifiers
    (dbo.sales, total_revenue) into word tokens the whitespace-tokenizing embedder can match."""
    p = StoreProfile(store_id="s", title="s", description="", kind=INDEXED,
                     business_unit="", topics=[],
                     schema=[{"table": "dbo.sales",
                              "columns": [{"name": "product"}, {"name": "amount"},
                                          {"name": "region"}, {"name": "closed_on"}]}])
    t = profile_text(p).lower().split()
    for term in ("sales", "product", "amount", "region", "closed"):
        assert term in t, f"schema term {term!r} not folded into profile_text: {t}"
    assert "dbo.sales" not in t, "compound identifier was not split into word tokens"


def test_a_store_with_only_a_schema_still_clears_the_relevance_floor():
    """#296 end-to-end at the scoring layer: a store with NO description/topics but a real
    schema must produce a non-zero match for a question that names its columns, and survive
    the relative floor. Before the fix its score was exactly 0.00 (the observed symptom)."""
    emb = HashingEmbedding()
    p = StoreProfile(store_id="azure_sql-1", title="azure_sql-1", description="", kind=INDEXED,
                     capabilities={SEMANTIC}, business_unit="", topics=[],
                     schema=[{"table": "dbo.sales",
                              "columns": [{"name": "region"}, {"name": "product"},
                                          {"name": "amount"}, {"name": "closed_on"}]}])
    node = CatalogNode("azure_sql-1", STORE, "azure_sql-1--src", acl=["u"], profile=p, store=None)
    qv = emb.embed(["total amount by region for each product"])[0]
    scored = score_stores(qv, [node], emb, "total amount by region for each product")
    assert scored[0].score > 0.0, f"a schema-only store still scores 0 — unroutable: {scored}"
    assert relative_floor(scored, frac=0.6), "the schema-only store does not clear the floor"


def test_ensure_caches_vector():
    emb = HashingEmbedding()
    p = StoreProfile(store_id="s", title="t", description="parental leave policy",
                     kind=INDEXED, business_unit="hr")
    v1 = ensure_profile_vector(p, emb)
    assert p.profile_vector is v1 and len(v1) == 128, p
    v2 = ensure_profile_vector(p, emb)   # cached, not re-embedded
    assert v2 is v1


def test_scoring_ranks_topical_store_first():
    emb = HashingEmbedding()
    nodes = [
        _node("hr-wiki", "hr", "human resources parental leave holidays onboarding", ["parental leave"]),
        _node("fin-ledger", "finance", "revenue invoices tax numbers ledger", ["revenue"]),
    ]
    qv = emb.embed(["parental leave holidays"])[0]
    scored = score_stores(qv, nodes, emb)
    assert scored[0].store_id == "hr-wiki", scored
    assert scored[0].score >= scored[1].score, scored


def test_relative_floor_drops_weak():
    from dbsearch.router.decision import RoutedStore
    scored = [RoutedStore("a", "x", 0.9), RoutedStore("b", "x", 0.6), RoutedStore("c", "x", 0.2)]
    kept = relative_floor(scored, frac=0.6)   # keep >= 0.54
    assert {s.store_id for s in kept} == {"a", "b"}, kept


def test_cosine_bounds():
    assert abs(cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9
    assert abs(cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0   # zero vector -> 0, no divide error


def main():
    print("Phase E E2 profiles self-test:")
    test_profile_text_includes_topics()
    test_profile_text_includes_schema_tables_and_columns()
    test_a_store_with_only_a_schema_still_clears_the_relevance_floor()
    print("  PASS  #296  a store's schema (tables+columns) feeds routing, so a DB connected "
          "with no description still routes on its structure")
    test_ensure_caches_vector()
    test_scoring_ranks_topical_store_first()
    test_relative_floor_drops_weak()
    test_cosine_bounds()
    print("  PASS  profile_text / vector cache / scoring / relative floor / cosine")
    print("\nE2 PROFILES SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
