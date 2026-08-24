"""One-shot SharePoint→index ingest against the live Azure data plane.

The connect-azure.sh installer runs this once (after provisioning + consent) so the
AI Search index is populated before the query server starts. It reuses the SAME proven
Azure wiring as scripts/smoke_azure.py — just the ingest half, no query — so the server
(which builds its own edition over the same managed AI Search index, LAW 6) can answer.

Env: everything in .env (DBSEARCH_BACKEND=azure + endpoints + Entra app + SHAREPOINT_DRIVE_ID).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.config import Settings  # noqa: E402
from dbsearch.factory import build_data_plane  # noqa: E402
from dbsearch.pipeline.runner import run_ingestion  # noqa: E402


def main() -> None:
    settings = Settings.from_env()
    if settings.backend != "azure":
        raise SystemExit("set DBSEARCH_BACKEND=azure (connect-azure.sh does this for you)")
    if not settings.sharepoint_drive_id:
        raise SystemExit("SHAREPOINT_DRIVE_ID is empty — pick a library first")

    dp = build_data_plane(settings)
    print("→ ensuring AI Search index exists...", flush=True)
    dp.index.ensure_index()
    print(f"→ ingesting SharePoint drive {settings.sharepoint_drive_id} ...", flush=True)
    run_ingestion(dp.connector, dp.queue, dp.store, dp.extractor, dp.embedder, dp.index)
    print("✓ ingest complete — the index is populated; the query server can now answer.", flush=True)


if __name__ == "__main__":
    main()
