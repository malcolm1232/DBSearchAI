"""Phase E E1 — Evidence + Store contracts self-test.
Run: python3 tests/selftest_router_contracts.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.evidence import Evidence, CHUNK, ROW  # noqa: E402
from dbsearch.router.store import (  # noqa: E402
    AccessContext, StorePort, StoreProfile, INDEXED, SEMANTIC,
)
from dbsearch.router.synthesizer import citations_from  # noqa: E402


def test_evidence_defaults():
    e = Evidence(store_id="s1", business_unit="hr", kind=CHUNK, content="hello")
    assert e.provenance == {}, e
    assert e.score is None, e
    d = e.to_dict()
    assert d["kind"] == CHUNK and d["content"] == "hello", d


def test_profile_and_access_defaults():
    p = StoreProfile(store_id="s1", title="Wiki", description="company wiki",
                     kind=INDEXED, capabilities={SEMANTIC}, business_unit="hr")
    assert p.profile_vector is None and p.schema is None, p
    assert p.freshness == "live", p
    ac = AccessContext(user_oid="alice", principals=["all-staff"])
    assert ac.delegated_credential is None and ac.row_policy is None, ac


def test_storeport_is_abstract():
    try:
        StorePort()
        assert False, "StorePort must not instantiate"
    except TypeError:
        pass


def test_fake_store_satisfies_port():
    class FakeStore(StorePort):
        def profile(self):
            return StoreProfile(store_id="s1", title="t", description="d",
                                kind=INDEXED, capabilities={SEMANTIC})

        def authorize(self, user_oid):
            return AccessContext(user_oid=user_oid, principals=["all-staff"])

        def retrieve(self, access, question, top_k=5):
            return [Evidence(store_id="s1", business_unit="", kind=CHUNK, content=question)]

    fs = FakeStore()
    assert fs.capabilities() == {SEMANTIC}, fs.capabilities()
    ac = fs.authorize("alice")
    assert fs.retrieve(ac, "hi")[0].content == "hi"


def test_citations_carry_typed_proof():
    evs = [
        Evidence(store_id="db", business_unit="fin", kind=ROW, content="a=1",
                 provenance={"sql": "SELECT a FROM t", "table": "t", "row_ids": [0]}),
        Evidence(store_id="wiki", business_unit="hr", kind=CHUNK, content="…",
                 provenance={"doc": "h.md", "title": "H", "uri": "https://sp/h.md",
                             "locator": "c1"}),
    ]
    cites = citations_from(evs)
    kinds = {c["proof"]["kind"] for c in cites}
    assert kinds == {"sql", "document"}, cites
    sql_cite = next(c for c in cites if c["proof"]["kind"] == "sql")
    assert sql_cite["sql"] == "SELECT a FROM t", sql_cite          # legacy flat field KEPT
    assert sql_cite["proof"]["rerun_token"] == "", sql_cite        # stamped later, at API


def test_bad_provenance_degrades_not_crashes():
    evs = [Evidence(store_id="s", business_unit="", kind="mystery", content="…",
                    provenance={"weird": True})]
    cites = citations_from(evs)
    assert len(cites) == 1 and "proof" not in cites[0], cites      # degrade, keep answer


def test_profiles_declare_proof_kind():
    p = StoreProfile(store_id="s1", title="t", description="d", kind=INDEXED,
                     capabilities={SEMANTIC})
    assert p.proof_kind == "", p                                    # default exists


def test_profile_origin_defaults_none():
    p = StoreProfile(store_id="s1", title="t", description="d", kind=INDEXED,
                     capabilities={SEMANTIC})
    assert p.origin is None, p


def main():
    print("Phase E E1 contracts self-test:")
    test_evidence_defaults()
    test_profile_and_access_defaults()
    test_storeport_is_abstract()
    test_fake_store_satisfies_port()
    print("  PASS  Evidence / StoreProfile / AccessContext / StorePort")
    test_citations_carry_typed_proof()
    test_bad_provenance_degrades_not_crashes()
    test_profiles_declare_proof_kind()
    test_profile_origin_defaults_none()
    print("  PASS  typed proof on citations / degrade / proof_kind (#165) / origin (#176)")
    print("\nE1 CONTRACTS SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
