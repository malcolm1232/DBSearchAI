"""LangChain integration — done SAFELY.

The one rule (LAW 2): LangChain must NEVER see a document the user isn't authorized for.
So we expose DBSearch as a LangChain Retriever whose results are ALREADY permission-trimmed
by QueryService.retrieve() — the trim happens in our code, before LangChain touches anything.
LangChain then does whatever orchestration you like (RAG chain, agent) over the safe set.

This is the recommended way to use LangChain here: lean on it for orchestration, keep the
security boundary in our hands. Optional dep: pip install '.[langchain]'

    retriever = build_retriever(query_service, user_oid="<entra-oid-from-verified-token>")
    # drop `retriever` into any LangChain chain; it only ever yields trimmed docs.
"""
from __future__ import annotations

from dbsearch.query import QueryService


def build_retriever(query_service: QueryService, user_oid: str):
    """Return a LangChain BaseRetriever that yields ONLY this user's authorized chunks.

    `user_oid` must come from the verified auth token, never from client input (LAW 2).
    """
    from langchain_core.documents import Document as LCDocument
    from langchain_core.retrievers import BaseRetriever

    class DBSearchRetriever(BaseRetriever):
        def _get_relevant_documents(self, query: str, *, run_manager=None):  # noqa: ARG002
            # query_service.retrieve already applied the mandatory permission trim.
            chunks = query_service.retrieve(user_oid, query)
            return [
                LCDocument(
                    page_content=c.text,
                    metadata={"doc": c.doc_external_id, "title": c.title, "uri": c.uri, "score": c.score},
                )
                for c in chunks
            ]

    return DBSearchRetriever()
