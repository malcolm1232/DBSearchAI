"""Ingestion pipeline (LAW 4, ADR 0005) — Phase 1.

Decoupled, queue-joined stages; each a stateless, idempotent worker:

    connectors -> [queue] -> parse/ocr -> [queue] -> chunk+embed -> [queue] -> index
                                                                       \\-> acl store

Idempotency key: (tenant_id, external_id, content_hash). Messages carry references,
not bytes. Stages scale independently. No synchronous "do it all" path.
"""
