"""#165 — provenance contract self-test.
Run: python3 tests/selftest_provenance.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.evidence import Evidence, CHUNK, ROW  # noqa: E402
from dbsearch.router.provenance import (  # noqa: E402
    DocProof, ProvenanceError, SqlProof, normalize_proof, sign_rerun, verify_rerun,
    PROOF_DOCUMENT, PROOF_SQL,
)


def test_row_evidence_normalizes_to_sql_proof():
    ev = Evidence(store_id="fin-db", business_unit="fin", kind=ROW,
                  content="region=EU, rev=42",
                  provenance={"sql": "SELECT region, rev FROM sales",
                              "table": "sales", "row_ids": [0]})
    p = normalize_proof(ev)
    assert isinstance(p, SqlProof), p
    d = p.to_dict()
    assert d["kind"] == PROOF_SQL and d["sql"].startswith("SELECT"), d
    assert d["table"] == "sales" and d["row_ids"] == [0] and d["store_id"] == "fin-db", d


def test_chunk_evidence_normalizes_to_doc_proof():
    ev = Evidence(store_id="hr-wiki", business_unit="hr", kind=CHUNK, content="…",
                  provenance={"doc": "handbook.md", "title": "Handbook",
                              "uri": "https://x.sharepoint.com/handbook.md", "locator": "c3"})
    p = normalize_proof(ev)
    assert isinstance(p, DocProof), p
    d = p.to_dict()
    assert d["kind"] == PROOF_DOCUMENT and d["uri"].startswith("https://"), d


def test_chunk_without_uri_still_document_proof():
    ev = Evidence(store_id="s", business_unit="", kind=CHUNK, content="…",
                  provenance={"doc": "a.txt", "title": "A"})
    d = normalize_proof(ev).to_dict()
    assert d["kind"] == PROOF_DOCUMENT and d["uri"] == "", d


def test_unclassifiable_provenance_raises():
    ev = Evidence(store_id="s", business_unit="", kind="mystery", content="…",
                  provenance={"weird": True})
    try:
        normalize_proof(ev)
        assert False, "must raise ProvenanceError"
    except ProvenanceError:
        pass


def test_hmac_roundtrip_and_tamper():
    t = sign_rerun("fin-db", "SELECT 1", "alice", key="k1")
    assert verify_rerun("fin-db", "SELECT 1", "alice", t, key="k1")
    assert not verify_rerun("fin-db", "SELECT 2", "alice", t, key="k1")      # tampered sql
    assert not verify_rerun("fin-db", "SELECT 1", "bob", t, key="k1")        # foreign user
    assert not verify_rerun("other", "SELECT 1", "alice", t, key="k1")       # other store
    assert not verify_rerun("fin-db", "SELECT 1", "alice", t, key="k2")      # other key


def main():
    for fn in (test_row_evidence_normalizes_to_sql_proof,
               test_chunk_evidence_normalizes_to_doc_proof,
               test_chunk_without_uri_still_document_proof,
               test_unclassifiable_provenance_raises,
               test_hmac_roundtrip_and_tamper):
        fn()
        print(f"ok {fn.__name__}")
    print("selftest_provenance: ALL OK")


if __name__ == "__main__":
    main()
