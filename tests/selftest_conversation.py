"""Phase 2.5 self-test: ConversationService adds multi-turn memory + history-aware
retrieval (query rewriting) WITHOUT touching the LAW-2 trim core. Dependency-free.

    python3 tests/selftest_conversation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.query.conversation import ConversationService, Turn  # noqa: E402
from dbsearch.query.service import QueryResult  # noqa: E402


class RecordingQueryService:
    """Stands in for QueryService.answer — records the exact question it was asked and
    returns a canned, per-user result so we can prove the trim boundary is respected."""
    def __init__(self):
        self.calls = []  # list of (user_oid, question)

    def answer(self, user_oid, question, llm=None, tenant_id=None):
        self.calls.append((user_oid, question))
        return QueryResult(answer=f"[{user_oid}] {question}", citations=[],
                           retrieved_docs=[f"doc-for-{user_oid}"])


class ConcatLlm:
    """Fake LLM whose condense prefixes the previous question — deterministic + observable."""
    def condense_question(self, question, history):
        if not history:
            return question
        return f"{history[-1]['question']} {question}"


def check(label, cond):
    if not cond:
        raise AssertionError(f"FAIL: {label}")
    print(f"  PASS  {label}")


class CountingLlm:
    def __init__(self):
        self.max_seen = 0

    def condense_question(self, question, history):
        self.max_seen = max(self.max_seen, len(history))
        return question


def port_checks():
    from dbsearch.ports.base import LlmPort
    from dbsearch.adapters.local import ExtractiveLlm

    # Default no-op: a bare adapter that only implements answer() leaves the question alone.
    class BareLlm(LlmPort):
        def answer(self, question, context_chunks):
            return {"answer": "", "citations": []}

    check("LlmPort default condense_question is a no-op",
          BareLlm().condense_question("hi", [{"question": "x", "answer": "y"}]) == "hi")

    llm = ExtractiveLlm()
    check("ExtractiveLlm condense passes through when history is empty",
          llm.condense_question("first?", []) == "first?")
    check("ExtractiveLlm condense prefixes the previous question",
          llm.condense_question("what about the EU?",
                                [{"question": "holiday policy?", "answer": "..."}])
          == "holiday policy? what about the EU?")


def main():
    print("ConversationService self-test (Phase 2.5):")

    # 1. First turn: empty history -> raw question retrieved, no condensing.
    qs, llm = RecordingQueryService(), ConcatLlm()
    svc = ConversationService(qs, llm)
    svc.ask("alice", "c1", "What is our holiday policy?")
    check("first turn retrieves the raw question",
          qs.calls[-1] == ("alice", "What is our holiday policy?"))

    # 2. Follow-up: condensed to a standalone question BEFORE retrieval.
    svc.ask("alice", "c1", "what about the EU?")
    check("follow-up is condensed before retrieval",
          qs.calls[-1] == ("alice", "What is our holiday policy? what about the EU?"))

    # 3. Per-user history isolation: same conv_id, different user -> empty history.
    svc.ask("bob", "c1", "and onboarding?")
    check("a different user on the same conv_id sees empty history (raw question)",
          qs.calls[-1] == ("bob", "and onboarding?"))

    # 4. Every turn retrieves as the asking user (the trim runs per turn, never cross-user).
    check("retrieval user_oid is always the asking user",
          [u for u, _ in qs.calls] == ["alice", "alice", "bob"])

    # 5. History window: only the last max_history_turns turns reach condensing.
    qs2, llm2 = RecordingQueryService(), CountingLlm()
    svc2 = ConversationService(qs2, llm2, max_history_turns=2)
    for i in range(4):
        svc2.ask("alice", "c2", f"q{i}")
    check("condense receives exactly max_history_turns turns once the window fills",
          llm2.max_seen == 2)

    port_checks()
    http_checks()
    print("\nALL CONVERSATION SELF-TESTS PASSED — memory + history-aware retrieval, trim intact.")


def http_checks():
    import os
    os.environ["SELFHOST_BACKEND"] = "memory"
    os.environ["DBSEARCH_DEV_AUTH"] = "1"
    sys.modules.pop("dbsearch.server.app", None)
    from fastapi.testclient import TestClient
    from dbsearch.server.app import app

    c = TestClient(app)
    # Seed: a public doc (all-staff) and a restricted one (deal-team only).
    c.post("/ingest", headers={"X-DBSearch-User": "alice"}, json={"external_id": "handbook", "title": "Staff Handbook",
        "text": "Holidays and expenses for all staff.", "acl": ["all-staff"],
        "uri": "https://x/handbook"})
    c.post("/ingest", headers={"X-DBSearch-User": "alice"}, json={"external_id": "falcon", "title": "Project Falcon",
        "text": "Confidential Falcon valuation, deal team only.", "acl": ["deal-team"],
        "uri": "https://x/falcon"})

    def chat(user, conv, q):
        return c.post("/chat", json={"conv_id": conv, "question": q},
                      headers={"X-DBSearch-User": user}).json()

    r1 = chat("alice", "k1", "holidays and expenses for staff")
    check("/chat returns an answer and echoes conv_id",
          r1.get("conv_id") == "k1" and "answer" in r1)

    # The queries below lexically overlap the seeded doc TEXT on purpose: the hashing
    # embedding (dev stand-in) only scores overlapping terms, and InMemoryIndex drops any
    # chunk that scores <= 0. Overlap keeps the LAW-2 assertions NON-VACUOUS — the docs are
    # genuinely retrievable, so an absence is the ACL trim acting on identity, not a query
    # that simply found nothing.
    def docs(user, conv, q):
        return chat(user, conv, q)["authorized_docs"]

    # Positive control: BOTH users retrieve the public handbook (all-staff) — proves each
    # user's retrieval is alive before we assert what bob must NOT get.
    a_pub = docs("alice", "k1", "holidays and expenses for staff")
    b_pub = docs("bob", "k2", "holidays and expenses for staff")
    check("positive control: both alice and bob retrieve the public handbook",
          "handbook" in a_pub and "handbook" in b_pub)

    # Mid-conversation follow-up (these are turn 2+ in each thread): the SAME Falcon-
    # overlapping query for both users. alice (deal-team) gets Falcon; bob (all-staff only)
    # must NOT — identity/ACL is the only difference, so bob's absence is the per-turn trim.
    a_falcon = docs("alice", "k1", "confidential Falcon valuation")
    b_falcon = docs("bob", "k2", "confidential Falcon valuation")
    check("alice (deal-team) retrieves the restricted Falcon doc mid-conversation",
          "falcon" in a_falcon)
    check("bob (not deal-team) never retrieves Falcon mid-conversation — ACL trim per turn (LAW 2)",
          "falcon" not in b_falcon)


if __name__ == "__main__":
    main()
