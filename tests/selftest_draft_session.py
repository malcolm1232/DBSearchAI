"""#57 — DraftSessionService: the two-phase conversational proposal draft.

Proves: (1) the state machine GATHERING -> CONFIRMING -> DONE; (2) the MODEL SPLIT — the cheap
chat model handles ALL chat + the requirements summary, the strong model handles ONLY the
proposal draft; (3) the confirmation gate; (4) LAW 2 — a draft never surfaces docs the user
can't retrieve.

    python3 tests/selftest_draft_session.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import (  # noqa: E402
    HashingEmbedding, InMemoryIdentity, InMemoryIndex, InMemoryObjectStore, InMemoryQueue,
    PlainTextExtractor,
)
from dbsearch.agents.draft_session import CONFIRMING, DONE, GATHERING, DraftSessionService  # noqa: E402
from dbsearch.connectors.upload import UploadConnector  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.ports.base import LlmPort  # noqa: E402
from dbsearch.query import QueryService  # noqa: E402


class RecordingLlm(LlmPort):
    """An LlmPort that stamps its label onto every output and records which methods were called,
    so a test can prove WHICH model did WHAT."""
    def __init__(self, label): self.label = label; self.called = []

    def answer(self, question, context_chunks):
        self.called.append("answer"); return {"answer": f"{self.label}:answer", "citations": []}

    def plan_subquestions(self, brief, sections):
        # echo the (term-rich) brief into each sub-question so retrieval actually has keywords
        self.called.append("plan"); return [f"{brief} :: {s}" for s in sections]

    def draft_section(self, title, brief, context_chunks):
        self.called.append("draft_section")
        return f"{self.label}:prose for {title} from {len(context_chunks)} chunk(s)"

    def elicit_requirements(self, history):
        self.called.append("elicit"); return f"{self.label}:next-question"

    def summarize_requirements(self, history):
        # real corpus terms so the Sonnet draft's retrieval reaches the right docs
        self.called.append("summarize")
        return ("- client: a bank needing a confidential acquisition valuation\n"
                "- staff onboarding and expenses")


def _build_qs():
    store, queue = InMemoryObjectStore(), InMemoryQueue()
    index = InMemoryIndex(store)
    embedder = HashingEmbedding()
    for ext, text, acl in [
        ("handbook", "general staff handbook onboarding expenses for all staff", ["all-staff"]),
        ("falcon", "confidential project falcon acquisition valuation deal team only", ["deal-team"]),
    ]:
        conn = UploadConnector("t", ext, ext, text.encode(), "text/plain", acl, "")
        run_ingestion(conn, queue, store, PlainTextExtractor(), embedder, index)
    identity = InMemoryIdentity({"alice": ["all-staff", "deal-team"], "bob": ["all-staff"]})
    return QueryService(index, identity, embedder, RecordingLlm("QS"), store, tenant_id="t")


def test_state_machine_and_model_split():
    qs = _build_qs()
    chat, draft = RecordingLlm("HAIKU"), RecordingLlm("SONNET")
    svc = DraftSessionService(qs, chat, draft)

    # GATHERING: chat turn uses the CHEAP model only
    t = svc.turn("alice", "c1", "draft a proposal for a bank's mobile app", "chat")
    assert t.state == GATHERING and t.reply == "HAIKU:next-question", t
    assert "elicit" in chat.called and draft.called == [], "chat must use Haiku; Sonnet untouched"

    # "ready": requirements summarised by the CHEAP model -> CONFIRMING
    t = svc.turn("alice", "c1", "", "ready")
    assert t.state == CONFIRMING and "client: a bank" in t.requirements, t
    assert "summarize" in chat.called and draft.called == [], "summary is Haiku; Sonnet still untouched"

    # "confirm": the proposal is drafted by the STRONG model ONLY
    t = svc.turn("alice", "c1", "", "confirm")
    assert t.state == DONE and t.draft is not None, t
    assert "plan" in draft.called and "draft_section" in draft.called, "draft must use Sonnet"
    assert "plan" not in chat.called and "draft_section" not in chat.called, "Haiku must NOT draft"
    assert any("SONNET:prose" in s["prose"] for s in t.draft["sections"]), t.draft
    print("  PASS  state machine GATHERING->CONFIRMING->DONE; Haiku=chat, Sonnet=draft")


def test_confirm_without_requirements_is_nudged():
    qs = _build_qs()
    svc = DraftSessionService(qs, RecordingLlm("HAIKU"), RecordingLlm("SONNET"))
    t = svc.turn("alice", "c2", "", "confirm")     # confirm with no gathering first
    assert t.state == GATHERING and "requirements first" in t.reply, t
    # "ready" with NOTHING gathered must NOT summarise empty content (would 400 the model)
    t2 = svc.turn("alice", "c2b", "", "ready")
    assert t2.state == GATHERING and not t2.requirements, t2
    print("  PASS  confirm/ready with nothing gathered -> nudged back to GATHERING (no empty model call)")


def test_law2_draft_trims_per_user():
    qs = _build_qs()
    svc = DraftSessionService(qs, RecordingLlm("HAIKU"), RecordingLlm("SONNET"))
    for u in ("alice", "bob"):
        svc.turn(u, f"c-{u}", "acquisition and onboarding proposal", "chat")
        svc.turn(u, f"c-{u}", "", "ready")
    a = svc.turn("alice", "c-alice", "", "confirm").draft
    b = svc.turn("bob", "c-bob", "", "confirm").draft
    a_docs = {d for s in a["sections"] for d in s["authorized_docs"]}
    b_docs = {d for s in b["sections"] for d in s["authorized_docs"]}
    assert "falcon" in a_docs, f"alice (deal-team) should reach Falcon: {a_docs}"
    assert "falcon" not in b_docs, f"LAW 2 BREACH: bob drafted from Falcon: {b_docs}"
    print(f"  PASS  draft is permission-trimmed per user  ->  alice={a_docs}  bob={b_docs}")


def test_confirm_stream_events_and_law2():
    qs = _build_qs()
    chat, draft = RecordingLlm("HAIKU"), RecordingLlm("SONNET")
    svc = DraftSessionService(qs, chat, draft)
    # no requirements yet -> a single error event, state back to GATHERING
    err = list(svc.confirm_stream("alice", "s1"))
    assert err and err[0]["type"] == "error", err

    svc.turn("alice", "s1", "acquisition and onboarding proposal", "chat")
    svc.turn("alice", "s1", "", "ready")
    evs = list(svc.confirm_stream("alice", "s1"))
    types = [e["type"] for e in evs]
    assert types[0] == "plan" and types[-1] == "done", types
    assert types.count("section_start") == len(svc._sections), types
    assert "token" in types, "must stream at least one token"
    # tokens come from the STRONG model only
    assert "draft_section" in draft.called and "draft_section" not in chat.called
    # LAW 2 in the stream: alice reaches Falcon, the trimmed citations carry it
    cited = {c["doc"] for e in evs if e["type"] == "section_done" for c in e["citations"]}
    assert "falcon" in cited, cited
    bcited = set()
    svc.turn("bob", "s2", "acquisition and onboarding proposal", "chat"); svc.turn("bob", "s2", "", "ready")
    for e in svc.confirm_stream("bob", "s2"):
        if e["type"] == "section_done":
            bcited |= {c["doc"] for c in e["citations"]}
    assert "falcon" not in bcited, f"LAW 2 BREACH in stream: bob saw Falcon: {bcited}"
    print("  PASS  confirm_stream emits plan/section/token/done; Sonnet-only; LAW-2 holds in stream")


def main():
    print("DraftSession self-test (#57):")
    test_state_machine_and_model_split()
    test_confirm_without_requirements_is_nudged()
    test_law2_draft_trims_per_user()
    test_confirm_stream_events_and_law2()
    print("\nALL DRAFT-SESSION TESTS PASSED — two-phase flow, model split, and LAW 2 hold.")


if __name__ == "__main__":
    main()
