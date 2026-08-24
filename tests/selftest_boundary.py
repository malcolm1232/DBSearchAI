"""Phase 0 exit test (ROADMAP): prove the uplink wire rejects any payload that
carries document content. Dependency-free — run with plain `python3`.

    python3 tests/selftest_boundary.py

This is the self-test the spec demands: walk deliberately-wrong payloads through the
boundary validator and confirm it rejects them. If this ever fails, LAW 1 is unenforced.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.boundary.validator import BoundaryValidator, BoundaryViolation  # noqa: E402


def expect_ok(v, payload, label):
    v.validate(payload)
    print(f"  PASS  accepted valid payload: {label}")


def expect_reject(v, payload, label):
    try:
        v.validate(payload)
    except BoundaryViolation as e:
        print(f"  PASS  rejected: {label}  ->  {e}")
        return
    raise AssertionError(f"BOUNDARY LEAK: validator accepted {label!r} — LAW 1 unenforced")


def main():
    v = BoundaryValidator()
    print("Boundary contract self-test (SKILL.md LAW 1):")

    # Legitimate telemetry — must pass.
    expect_ok(v, {
        "tenant_id": "acme-001",
        "event": "ingest.completed",
        "counts": {"docs_indexed": 12000, "errors": 1},
        "cost": {"embed_tokens": 1_200_000, "llm_tokens": 90_000},
        "health": {"index_pct": 0.94, "queue_depth": 12, "version": "2.3.1"},
        "ts": "2026-06-25T10:00:00Z",
    }, "ingest telemetry")

    # Deliberately wrong #1: a document snippet smuggled in.
    expect_reject(v, {
        "tenant_id": "acme-001", "event": "query.served",
        "snippet": "Project Falcon merger terms: $4.2B all-cash...",
        "ts": "2026-06-25T10:01:00Z",
    }, "payload carrying a document snippet")

    # Deliberately wrong #2: the user's query string (content + PII risk).
    expect_reject(v, {
        "tenant_id": "acme-001", "event": "query.served",
        "query": "what did we propose to client X last year",
        "ts": "2026-06-25T10:02:00Z",
    }, "payload carrying a query string")

    # Wrong #3: an unexpected field not in the contract (allow-list breach).
    expect_reject(v, {
        "tenant_id": "acme-001", "event": "heartbeat",
        "ts": "2026-06-25T10:03:00Z", "debug_dump": {"foo": "bar"},
    }, "unexpected field not in contract")

    # Wrong #4: content nested deep inside an otherwise-valid object.
    expect_reject(v, {
        "tenant_id": "acme-001", "event": "ingest.completed",
        "counts": {"docs_indexed": 1, "title": "secret_deck.pptx"},
        "ts": "2026-06-25T10:04:00Z",
    }, "content-bearing key nested inside counts")

    # Wrong #5: a made-up event value not in the contract's enum (#24 — LAW 1:
    # the schema IS the contract; a value outside the enum means code drifted from
    # boundary.schema.json without anyone updating it).
    expect_reject(v, {
        "tenant_id": "acme-001", "event": "totally.made.up",
        "ts": "2026-06-25T10:05:00Z",
    }, "event value outside the contract enum")

    # Wrong #6: a tenant_id that breaks the pattern (spaces/punctuation — could be a
    # customer name leaking in instead of an opaque id).
    expect_reject(v, {
        "tenant_id": "Acme Corp, Inc.!", "event": "heartbeat",
        "ts": "2026-06-25T10:06:00Z",
    }, "tenant_id violating its pattern")

    # Wrong #7: a string where the contract demands a numeric counter (type drift —
    # a stringly-typed 'count' could smuggle a label/identifier past a number meter).
    expect_reject(v, {
        "tenant_id": "acme-001", "event": "ingest.completed",
        "counts": {"docs_indexed": "twelve thousand"},
        "ts": "2026-06-25T10:07:00Z",
    }, "string value where contract requires a number")

    # Positive case: proposal.drafted telemetry — must pass (new event in boundary contract).
    expect_ok(v, {
        "tenant_id": "acme-001", "event": "proposal.drafted",
        "ts": "2026-06-25T10:08:00Z",
        "counts": {"proposals_drafted": 1, "sections": 4, "subquestions": 4, "authorized_docs": 3},
    }, "proposal.drafted telemetry")

    expect_ok(v, {"tenant_id": "acme-001", "event": "api_key.created",
                  "counts": {"keys_created": 1}, "ts": "2026-06-25T10:09:00Z"},
              "api_key.created telemetry")
    expect_ok(v, {"tenant_id": "acme-001", "event": "api_key.revoked",
                  "counts": {"keys_revoked": 1}, "ts": "2026-06-25T10:10:00Z"},
              "api_key.revoked telemetry")

    print("\nALL BOUNDARY SELF-TESTS PASSED — the uplink rejects content (LAW 1 enforced).")


if __name__ == "__main__":
    main()
