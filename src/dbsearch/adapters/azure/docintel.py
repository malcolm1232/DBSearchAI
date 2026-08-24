"""DocIntelligenceExtractor — ExtractorPort backed by Azure AI Document Intelligence.

Requires: pip install azure-ai-documentintelligence azure-identity
Uses the prebuilt-read model for OCR/text extraction from PDFs, images, Office docs.
Plain text is decoded directly (no need to call the service).
"""
from __future__ import annotations

from dbsearch.ports.base import ExtractorPort


class DocIntelligenceExtractor(ExtractorPort):
    def __init__(self, endpoint: str, credential) -> None:
        from azure.ai.documentintelligence import DocumentIntelligenceClient

        self._client = DocumentIntelligenceClient(endpoint=endpoint, credential=credential)

    def extract(self, data: bytes, mime: str) -> str:
        if mime in ("text/plain", "text/markdown"):
            return data.decode("utf-8", "ignore")
        poller = self._client.begin_analyze_document(
            "prebuilt-read", body=data, content_type=mime or "application/octet-stream"
        )
        result = poller.result()
        return result.content or ""
