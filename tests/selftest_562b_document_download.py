"""#562 part 2 - download what was actually ingested, trimmed to the caller.

The Admin snapshot has to answer "did this document ingest correctly", and the only honest
answer is the EXTRACTED TEXT - the thing the answer engine can actually see. A PDF that
parsed to garbage looks perfect in a document listing. The original bytes come with it so
the pair can be compared, which is the point of a bundle.

The rule that matters here is LAW 2, and it is stricter than the listing above it:

  /admin/documents is metadata and the OPERATOR sees all of it (#549) - whoever runs the
  deployment answers for its contents. Download is CONTENT, and content is trimmed to the
  caller's own principals for EVERYONE, operator included. "No document a user can't
  already open is ever retrieved" is the product's central promise; an operator escape
  hatch on the content path is exactly the hole that promise forbids, and an operator who
  needs a document can be granted it (#538).

Two identities throughout: one identity cannot tell a working trim from an absent one.

    PYTHONPATH=src python3 tests/selftest_562b_document_download.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("SELFHOST_BACKEND", "memory")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ALICE, BOB = "alice", "bob"


def _edition_with_a_private_doc():
    """Alice uploads a document nobody else is granted."""
    from dbsearch.server.edition import build_edition

    ed = build_edition()
    ed.ingest_file("secret-1", "Alice's offer letter",
                   b"Base salary is 180000 and the signing bonus is 20000.",
                   "text/plain", [ALICE], uri="upload://offer.txt", owner_oid=ALICE)
    return ed


def test_the_uploader_gets_the_extracted_text():
    ed = _edition_with_a_private_doc()
    b = ed.document_bundle("secret-1", ALICE)
    assert b is not None, "the uploader cannot download their own document"
    assert "180000" in (b["text"] or ""), b
    assert b["title"] == "Alice's offer letter"


def test_the_uploader_gets_the_original_bytes_too():
    ed = _edition_with_a_private_doc()
    b = ed.document_bundle("secret-1", ALICE)
    assert b["original"], "the original bytes are not retrievable"
    assert b"signing bonus" in b["original"]


def test_everyone_else_gets_nothing():
    """The whole point. Bob is a real user of this deployment and holds no grant."""
    ed = _edition_with_a_private_doc()
    assert ed.document_bundle("secret-1", BOB) is None, \
        "LAW 2 BREACH: bob downloaded a document he is not permitted to read"


def test_a_missing_document_is_indistinguishable_from_an_unauthorized_one():
    """Both answer None -> both 404. A different answer for 'exists but not yours' is an
    existence probe, and a document TITLE is routinely the whole secret."""
    ed = _edition_with_a_private_doc()
    assert ed.document_bundle("no-such-doc", ALICE) is None
    assert ed.document_bundle("secret-1", BOB) is None


def test_the_operator_is_not_exempt_from_the_content_trim():
    """#549 gave the operator an UNRESTRICTED metadata listing. Content is not metadata, and
    document_bundle takes no unrestricted flag at all - there is no argument to pass."""
    import inspect

    from dbsearch.server.edition import Edition

    params = inspect.signature(Edition.document_bundle).parameters
    assert "unrestricted" not in params, (
        "document_bundle grew an unrestricted escape hatch - the operator may enumerate "
        "documents, never read their content unless granted (#538)")


def test_the_endpoint_exists_and_is_identity_gated():
    from dbsearch.server.app import app

    routes = {getattr(r, "path", "") for r in app.routes}
    assert "/admin/documents/{doc_id}/download" in routes, "the download endpoint is not mounted"
    src = (ROOT / "src/dbsearch/server/app.py").read_text()
    body = src.split('@app.get("/admin/documents/{doc_id}/download")', 1)[1].split("\n@app.", 1)[0]
    assert "Depends(current_user)" in body, "the download endpoint does not resolve a caller"
    assert "404" in body, "an unauthorized download must 404"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
