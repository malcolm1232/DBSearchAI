"""Real end-to-end smoke test against Azure (you run this AFTER provisioning).

Prereqs:
  pip install -e '.[azure]'
  export the env vars in .env.example (DBSEARCH_BACKEND=azure, endpoints, Entra app,
  SHAREPOINT_DRIVE_ID, TEST_USER_OID, TEST_QUESTION)

What it does:
  1. ensure the AI Search index exists (idempotent)
  2. ingest the configured SharePoint drive end-to-end (delta crawl -> parse -> embed -> index)
  3. ask a question AS a specific user oid and print the permission-trimmed, cited answer

This is the Phase 1b exit test: a real cited answer from real SharePoint content, with
real Entra-group security trimming. Run it twice with two different TEST_USER_OIDs (one
who can see a restricted doc, one who can't) to confirm LAW 2 holds on live data.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.config import Settings  # noqa: E402
from dbsearch.factory import build_data_plane  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402


def main() -> None:
    settings = Settings.from_env()
    if settings.backend != "azure":
        raise SystemExit("set DBSEARCH_BACKEND=azure (and the rest of .env.example)")

    dp = build_data_plane(settings)

    print("→ ensuring AI Search index exists...")
    dp.index.ensure_index()

    print(f"→ ingesting SharePoint drive {settings.sharepoint_drive_id} ...")
    run_ingestion(dp.connector, dp.queue, dp.store, dp.extractor, dp.embedder, dp.index)

    user = os.environ.get("TEST_USER_OID")
    if not user:
        raise SystemExit("set TEST_USER_OID to an Entra user object id to query as")
    question = os.environ.get("TEST_QUESTION", "What proposals or case studies do we have?")

    result = dp.query_service().answer(user, question)
    print("\nQ:", question)
    print("A:", result.answer)
    print("Citations:", result.citations)
    print("Authorized docs:", result.retrieved_docs)
    print("\nRe-run with a different TEST_USER_OID to confirm permission trimming (LAW 2).")


if __name__ == "__main__":
    main()
