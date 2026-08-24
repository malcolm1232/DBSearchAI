"""Proposal-drafting agent (one-shot). A sibling of ConversationService: pure
orchestration over the permission-trimmed QueryService + the pluggable LlmPort.

Flow: plan sub-questions -> retrieve each through the TRIMMED core -> draft each
section from post-trim text -> assemble citations from the trimmed hits (never the
model). The LAW-2 trim is inherited from QueryService.retrieve; the agent never sees
or returns anything the requesting user isn't authorized to retrieve.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dbsearch.ports.base import LlmPort
from dbsearch.query.service import QueryService

DEFAULT_SECTIONS = [
    "Understanding of Need",
    "Relevant Past Engagements",
    "Proposed Approach",
    "Why Us / Credentials",
]


@dataclass
class DraftSection:
    title: str
    prose: str
    citations: list[dict] = field(default_factory=list)     # [{doc,title,uri}] trimmed hits only
    # #393: named for what it is. This is the post-trim retrieval for THIS sub-question,
    # not what the caller may see; the surfaces printed it as the latter.
    retrieved_docs: list[str] = field(default_factory=list)


@dataclass
class ProposalDraft:
    brief: str
    plan: list[str]
    sections: list[DraftSection]


class ProposalAgent:
    def __init__(self, query_service: QueryService, llm: LlmPort,
                 sections: list[str] = DEFAULT_SECTIONS) -> None:
        self._qs = query_service
        self._llm = llm
        self._sections = list(sections)

    def draft(self, user_oid: str, brief: str) -> ProposalDraft:
        plan = self._llm.plan_subquestions(brief, self._sections)
        out: list[DraftSection] = []
        for title, subq in zip(self._sections, plan):
            chunks = self._qs.retrieve(user_oid, subq)        # TRIMMED CORE (LAW 2)
            prose = self._llm.draft_section(title, brief, [c.text for c in chunks])
            # citations + audit assembled from trimmed hits only, de-duped by doc id
            seen: set[str] = set()
            cites: list[dict] = []
            docs: list[str] = []
            for c in chunks:
                if c.doc_external_id in seen:
                    continue
                seen.add(c.doc_external_id)
                cites.append({"doc": c.doc_external_id, "title": c.title, "uri": c.uri})
                docs.append(c.doc_external_id)
            out.append(DraftSection(title=title, prose=prose, citations=cites, retrieved_docs=docs))
        return ProposalDraft(brief=brief, plan=plan, sections=out)

    def draft_stream(self, user_oid: str, brief: str):
        """Streaming twin of draft() (#61). Yields events: one {'type':'plan'}, then per section
        a {'section_start'}, a run of {'token'}, and a {'section_done'} carrying the trimmed
        citations; finally {'done'}. Retrieval/trim per sub-question is the SAME permission-trimmed
        core (LAW 2); citations are still assembled from the trimmed hits, never the model."""
        plan = self._llm.plan_subquestions(brief, self._sections)
        yield {"type": "plan", "plan": plan}
        for title, subq in zip(self._sections, plan):
            chunks = self._qs.retrieve(user_oid, subq)        # TRIMMED CORE (LAW 2)
            seen: set[str] = set()
            cites: list[dict] = []
            docs: list[str] = []
            for c in chunks:
                if c.doc_external_id in seen:
                    continue
                seen.add(c.doc_external_id)
                cites.append({"doc": c.doc_external_id, "title": c.title, "uri": c.uri})
                docs.append(c.doc_external_id)
            yield {"type": "section_start", "title": title}
            for tok in self._llm.draft_section_stream(title, brief, [c.text for c in chunks]):
                yield {"type": "token", "title": title, "text": tok}
            yield {"type": "section_done", "title": title, "citations": cites,
                   "retrieved_docs": docs, "authorized_docs": docs}   # alias (#393)
        yield {"type": "done"}
