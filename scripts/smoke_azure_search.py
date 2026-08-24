"""Minimal REAL-Azure smoke test — proves the crown jewels on live infrastructure:

  - real Azure OpenAI embeddings (text-embedding-3-small)
  - real Azure AI Search hybrid index + the MANDATORY security-trim filter (LAW 2)
  - real gpt-4.1-mini cited answers

Uses the seed SharePoint connector + in-memory store/queue/identity so it needs only the
two API keys (no SharePoint/Entra consent, no RBAC). This isolates the genuinely novel
risk: does our AISearchIndex security filter actually trim on real Azure?

Run:  set -a; source .env; set +a;  python3 scripts/smoke_azure_search.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Load .env if present (simple KEY=VALUE).
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

from azure.core.credentials import AzureKeyCredential  # noqa: E402

from dbsearch.adapters.azure.aisearch import AISearchIndex  # noqa: E402
from dbsearch.adapters.azure.aoai import AzureOpenAIEmbedding, AzureOpenAILlm  # noqa: E402
from dbsearch.adapters.local import (  # noqa: E402
    InMemoryIdentity,
    InMemoryObjectStore,
    InMemoryQueue,
    PlainTextExtractor,
)
from dbsearch.connectors.sharepoint import SharePointConnector  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402
from dbsearch.query import QueryService  # noqa: E402

FALCON = "falcon-ma-confidential"


def main():
    endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
    search_key = os.environ["AZURE_SEARCH_ADMIN_KEY"]
    aoai_endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
    aoai_key = os.environ["AZURE_OPENAI_API_KEY"]

    store = InMemoryObjectStore()
    queue = InMemoryQueue()
    embedder = AzureOpenAIEmbedding(aoai_endpoint, aoai_key, os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"])
    llm = AzureOpenAILlm(aoai_endpoint, aoai_key, os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"])
    index = AISearchIndex(endpoint, os.environ.get("AZURE_SEARCH_INDEX", "chunks"),
                          AzureKeyCredential(search_key), store, embedding_dim=1536)

    print("→ creating AI Search index (idempotent)...")
    index.ensure_index()

    print("→ ingesting seed docs through REAL embeddings + REAL AI Search...")
    run_ingestion(SharePointConnector("acme"), queue, store, PlainTextExtractor(), embedder, index)

    identity = InMemoryIdentity({
        "alice": ["grp-all-consultants", "grp-falcon-team"],
        "bob": ["grp-all-consultants"],
    })
    qs = QueryService(index, identity, embedder, llm, store)
    question = "What are the confidential Project Falcon merger details?"

    # AI Search indexing is near-real-time; poll briefly until docs are searchable.
    print("→ waiting for index to become searchable, then querying as alice & bob...")
    alice = None
    for _ in range(12):
        alice = qs.answer("alice", question)
        if alice.retrieved_docs:
            break
        time.sleep(3)
    bob = qs.answer("bob", question)

    print("\n--- alice (on the Falcon deal team) ---")
    print("authorized_docs:", alice.retrieved_docs)
    print("citations:", [c["doc"] for c in alice.citations])
    print("answer:", alice.answer)
    print("\n--- bob (NOT on the Falcon team) ---")
    print("authorized_docs:", bob.retrieved_docs)
    print("citations:", [c["doc"] for c in bob.citations])
    print("answer:", bob.answer)

    assert FALCON in alice.retrieved_docs, "alice should retrieve the Falcon doc"
    assert FALCON not in bob.retrieved_docs, "LAW 2 BREACH on real Azure: bob retrieved Falcon"
    assert all(c["doc"] != FALCON for c in bob.citations), "LAW 2 BREACH: bob's citations leaked Falcon"
    print("\n✅ REAL AZURE: permission-faithful retrieval holds — bob is denied the confidential doc (LAW 2).")


if __name__ == "__main__":
    main()
