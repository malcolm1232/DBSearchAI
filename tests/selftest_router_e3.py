"""Phase E E3 — end-to-end: ask() = route → fan-out → synthesize over an E1 catalog.
Proves the demo vertical (ask → route → answer → cite) AND the two security properties:
gate #1+#3 (an hr-only user's answer pipeline never touches finance content) and §8
partial-coverage disclosure. Run: python3 tests/selftest_router_e3.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import ExtractiveLlm, HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch import router  # noqa: E402

SPEC = {
    "tenant": "acme",
    "stores": [
        {"id": "hr-wiki", "kind": "local", "business_unit": "hr", "acl": ["hr-staff"],
         "title": "HR Wiki", "description": "human resources parental leave holidays onboarding benefits",
         "config": {"seed": [{"external_id": "handbook", "title": "Handbook", "uri": "u1",
                              "acl": ["hr-staff"], "text": "parental leave is sixteen weeks"}],
                    "user_groups": {"alice": ["hr-staff"], "carol": ["hr-staff", "fin-staff"]}}},
        {"id": "fin-ledger", "kind": "local", "business_unit": "finance", "acl": ["fin-staff"],
         "title": "Finance Ledger", "description": "revenue invoices tax numbers ledger accounting",
         "config": {"seed": [{"external_id": "q3", "title": "Q3 Ledger", "uri": "u2",
                              "acl": ["fin-staff"], "text": "confidential revenue four point two million"}],
                    "user_groups": {"eve": ["fin-staff"], "carol": ["hr-staff", "fin-staff"]}}},
    ],
}

IDENTITY = {"alice": ["hr-staff"], "eve": ["fin-staff"],
            "carol": ["hr-staff", "fin-staff"], "mallory": []}


class SpyLlm(ExtractiveLlm):
    def __init__(self):
        self.contexts = []

    def answer(self, question, context_chunks):
        self.contexts.append(list(context_chunks))
        return super().answer(question, context_chunks)


def _svc():
    reg = router.ProviderRegistry()
    reg.register(router.LocalIndexProvider())
    cat = router.load_manifest(SPEC, registry=reg)
    identity = InMemoryIdentity(IDENTITY)
    return router.RouterQueryService(cat, identity, HashingEmbedding()), cat


def test_ask_answers_with_citation_and_routing():
    svc, _ = _svc()
    res = svc.ask("carol", "what is our parental leave policy", ExtractiveLlm())
    assert "sixteen weeks" in res.answer, res.answer
    assert res.citations and res.citations[0]["doc"] == "handbook", res.citations
    assert res.routing["stores"][0]["store_id"] == "hr-wiki", res.routing
    assert res.disclosure == "", res.disclosure


def test_hr_only_user_never_touches_finance_content_anywhere():
    svc, _ = _svc()
    llm = SpyLlm()
    res = svc.ask("alice", "revenue invoices ledger parental leave", llm)
    blob = repr(res.to_dict()) + repr(llm.contexts)
    assert "four point two million" not in blob, "finance CONTENT leaked to hr-only user"
    assert "fin-ledger" not in blob, "finance store EXISTENCE leaked (gate #1)"


def test_no_access_user_gets_safe_answer():
    svc, _ = _svc()
    res = svc.ask("mallory", "anything at all", ExtractiveLlm())
    assert res.citations == [] and res.evidence == [], res.to_dict()
    assert "couldn't find" in res.answer.lower() or "no accessible" in res.answer.lower(), res.answer


def test_dropped_store_is_disclosed_not_fatal():
    svc, cat = _svc()

    class Boom:
        def authorize(self, user_oid):
            raise RuntimeError("source down")

        def retrieve(self, access, question, top_k=5):
            raise RuntimeError("unreachable")

        def profile(self):
            return cat.get("fin-ledger").profile

    cat.get("fin-ledger").store = Boom()
    # Force a deterministic fan-out to BOTH visible stores (handover gotcha: with the
    # default floor a lopsided score pair floors down to one store): margin=1.0 +
    # floor_frac=0.0 selects every visible candidate.
    svc2 = router.RouterQueryService(cat, InMemoryIdentity(IDENTITY), HashingEmbedding(),
                                     margin=1.0, floor_frac=0.0)
    res = svc2.ask("carol", "parental leave and revenue invoices ledger",
                   ExtractiveLlm(), timeout_s=2.0)
    assert any(o["store_id"] == "fin-ledger" and o["status"] == "error"
               for o in res.outcomes), res.outcomes
    assert "fin-ledger" in res.disclosure, res.to_dict()
    assert "sixteen weeks" in res.answer, "healthy store must still answer: " + res.answer


def main():
    print("Phase E E3 end-to-end self-test:")
    test_ask_answers_with_citation_and_routing()
    test_hr_only_user_never_touches_finance_content_anywhere()
    test_no_access_user_gets_safe_answer()
    test_dropped_store_is_disclosed_not_fatal()
    print("  PASS  cited answer / no cross-BU leak (content+existence) / safe no-access / "
          "disclosed drop")
    print("\nE3 END-TO-END SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
