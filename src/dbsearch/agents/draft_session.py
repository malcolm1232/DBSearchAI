"""Two-phase conversational proposal draft (#57).

A sibling of ConversationService / ProposalAgent that sequences them:

    GATHERING ──"ready"──► CONFIRMING ──"confirm"──► DONE
        ▲                      │
        └──────"cancel"────────┘

  - GATHERING : every turn is a CHEAP-model (Haiku) clarifying question — `chat_llm`.
  - "ready"   : `chat_llm` summarises the conversation into a requirements bullet list, shown
                back to the user for sign-off.
  - "confirm" : the STRONG model (Sonnet) — `draft_llm` — drafts the proposal via ProposalAgent,
                whose retrieval is the permission-trimmed QueryService core (LAW 2 inherited).

The model split is enforced HERE, in code: `chat_llm` for all chat, `draft_llm` only for the
proposal. State is in-memory and keyed by (conv_id, user_oid) — exactly like ConversationService
— so a guessed conv_id under another identity simply misses and starts fresh (no cross-user bleed).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dbsearch.agents.proposal import DEFAULT_SECTIONS, ProposalAgent
from dbsearch.ports.base import LlmPort
from dbsearch.query.service import QueryService

GATHERING, CONFIRMING, DONE = "gathering", "confirming", "done"


@dataclass
class _Session:
    state: str = GATHERING
    history: list[dict] = field(default_factory=list)   # [{"question","answer"}], most recent last
    requirements: str = ""


@dataclass
class DraftTurn:
    state: str
    reply: str = ""                 # assistant message for GATHERING / cancel
    requirements: str = ""          # the bullet list shown at CONFIRMING
    draft: dict | None = None       # the proposal (serialised) at DONE


class DraftSessionService:
    def __init__(self, query_service: QueryService, chat_llm: LlmPort, draft_llm: LlmPort,
                 sections: list[str] = DEFAULT_SECTIONS) -> None:
        self._qs = query_service
        self._chat = chat_llm        # cheap model — ALL gather chat + the requirements summary
        self._draft = draft_llm      # strong model — ONLY the proposal draft
        self._sections = list(sections)
        self._sessions: dict[tuple[str, str], _Session] = {}

    def reset(self, user_oid: str, conv_id: str) -> None:
        self._sessions.pop((conv_id, user_oid), None)

    def confirm_stream(self, user_oid: str, conv_id: str):
        """Streaming confirm (#61): yield the Sonnet proposal as plan/section/token/done events
        (see ProposalAgent.draft_stream). If there's nothing to confirm yet, yield a single
        {'type':'error'} and reset to GATHERING — the caller shows it as a nudge. State -> DONE
        on completion. Retrieval/trim is the permission-trimmed core (LAW 2)."""
        sess = self._sessions.setdefault((conv_id, user_oid), _Session())
        if sess.state != CONFIRMING or not sess.requirements:
            sess.state = GATHERING
            yield {"type": "error",
                   "message": "Let's capture the requirements first: describe the client and their need, then say 'ready'."}
            return
        agent = ProposalAgent(self._qs, self._draft, self._sections)   # STRONG model (Sonnet)
        for ev in agent.draft_stream(user_oid, sess.requirements):
            yield ev
        sess.state = DONE

    def turn(self, user_oid: str, conv_id: str, message: str = "", intent: str = "chat") -> DraftTurn:
        key = (conv_id, user_oid)
        sess = self._sessions.setdefault(key, _Session())

        if intent == "confirm":
            if sess.state != CONFIRMING or not sess.requirements:
                # nothing to confirm yet — nudge back to gathering rather than draft from nothing
                sess.state = GATHERING
                return DraftTurn(state=GATHERING,
                                 reply="Let's capture the requirements first: tell me about the client and their need, then say 'ready'.")
            agent = ProposalAgent(self._qs, self._draft, self._sections)   # STRONG model (Sonnet)
            draft = agent.draft(user_oid, sess.requirements)               # LAW-2 trimmed retrieval
            sess.state = DONE
            return DraftTurn(state=DONE, requirements=sess.requirements, draft=_draft_to_dict(draft))

        if intent == "ready":
            if message.strip():
                sess.history.append({"question": message.strip(), "answer": ""})
            if not any(h.get("question", "").strip() for h in sess.history):
                # nothing gathered yet — don't summarise empty content (the model would 400)
                sess.state = GATHERING
                return DraftTurn(state=GATHERING,
                                 reply="Tell me about the client and what they need first, then hit “Ready to draft”.")
            sess.requirements = self._chat.summarize_requirements(sess.history)  # CHEAP model
            sess.state = CONFIRMING
            return DraftTurn(state=CONFIRMING, requirements=sess.requirements)

        if intent in ("cancel", "edit"):
            sess.state = GATHERING
            return DraftTurn(state=GATHERING, reply="Okay, let's keep refining. What should change?")

        # default: a normal GATHERING chat turn on the CHEAP model
        pending = sess.history + [{"question": message.strip(), "answer": ""}]
        reply = self._chat.elicit_requirements(pending)
        sess.history.append({"question": message.strip(), "answer": reply})
        sess.state = GATHERING
        return DraftTurn(state=GATHERING, reply=reply)


def _draft_to_dict(d) -> dict:
    return {
        "brief": d.brief,
        "plan": d.plan,
        "sections": [
            {"title": s.title, "prose": s.prose, "citations": s.citations,
             "retrieved_docs": s.retrieved_docs,
             "authorized_docs": s.retrieved_docs}      # deprecated alias (#393)
            for s in d.sections
        ],
    }
