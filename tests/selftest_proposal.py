"""Proposal agent — planning + drafting defaults and the LAW-2 end-to-end proof.

    DBSEARCH_DEV_AUTH=1 SELFHOST_BACKEND=memory python3 tests/selftest_proposal.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import ExtractiveLlm  # noqa: E402


def test_port_defaults():
    llm = ExtractiveLlm()
    sections = ["Understanding of Need", "Proposed Approach"]
    plan = llm.plan_subquestions("retail bank mobile app", sections)
    assert plan == ["Understanding of Need for: retail bank mobile app",
                    "Proposed Approach for: retail bank mobile app"], plan

    prose = llm.draft_section("Proposed Approach", "retail bank mobile app",
                              ["We migrated core banking to cloud.", "Mobile redesign delivered."])
    assert "core banking" in prose or "Mobile redesign" in prose, prose

    empty = llm.draft_section("X", "y", [])
    assert "No authorized source material" in empty, empty
    print("  PASS  test_port_defaults")


def test_agent_law2_end_to_end():
    from dbsearch.server.edition import build_edition
    from dbsearch.agents.proposal import ProposalAgent

    ed = build_edition()
    # seed two docs with mismatched ACLs (alice in deal-team; bob is not)
    ed.ingest_document("public-handbook", "Staff Handbook",
                       "General staff handbook: holidays, expenses, onboarding for all staff.",
                       ["all-staff"], "https://example/handbook")
    ed.ingest_document("deal-falcon", "Project Falcon — Confidential",
                       "Confidential Project Falcon merger acquisition valuation, deal team only.",
                       ["deal-team"], "https://example/falcon")

    agent = ProposalAgent(ed.query_service, ed.query_service._llm)
    brief = "Advise a retail bank on a confidential acquisition and staff onboarding."

    alice = agent.draft("alice", brief)
    assert alice.plan and len(alice.plan) == len(agent._sections), alice.plan
    alice_docs = {d for s in alice.sections for d in s.retrieved_docs}
    assert "deal-falcon" in alice_docs, alice_docs        # alice (deal-team) CAN see Falcon

    bob = agent.draft("bob", brief)
    bob_docs = {d for s in bob.sections for d in s.retrieved_docs}
    assert "deal-falcon" not in bob_docs, f"LAW 2 BREACH: bob saw Falcon: {bob_docs}"
    bob_cites = {c["doc"] for s in bob.sections for c in s.citations}
    assert "deal-falcon" not in bob_cites, f"LAW 2 BREACH in citations: {bob_cites}"
    print("  PASS  test_agent_law2_end_to_end")


def test_edition_draft_and_telemetry():
    from dbsearch.server.edition import build_edition
    ed = build_edition()
    ed.ingest_document("public-handbook", "Staff Handbook",
                       "Onboarding and expenses for all staff.", ["all-staff"], "u")
    draft = ed.draft_proposal("alice", "Onboarding programme for a bank.")
    assert draft.sections and len(draft.plan) == 4, draft.plan
    # proposal.drafted must validate against the boundary contract (no exception)
    print("  PASS  test_edition_draft_and_telemetry")


if __name__ == "__main__":
    test_port_defaults()
    test_agent_law2_end_to_end()
    test_edition_draft_and_telemetry()
    print("PASS selftest_proposal")
