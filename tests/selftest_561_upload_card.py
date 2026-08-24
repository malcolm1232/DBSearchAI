"""#561 - Connectors must offer a hosted user a way to put a file in.

`Files & Local` listed CSV / Files, Local index and Graph Search. The one card that ingests
documents from disk, `Folder`, is operatorOnly and must stay that way: compose refuses local
file sources for anyone else (router_api._reads_local_files), because a server-side path read
from an untrusted caller is a file-read primitive. So the gate was right and the consequence
was still a product with a "Connectors" surface that could not accept a document.

Upload already existed - POST /admin/upload, private-to-uploader (#539) - but only from the
Admin console, which is not where anyone looks to add a source.

What this pins:
  1. Files & Local offers an upload card, and it is NOT operator-gated.
  2. Folder is STILL operator-gated. This fix must not become a reason to loosen that.
  3. Upload is an ACTION, not a store: it must never become a canvas node, because a node
     goes into liveManifest() -> /router/compose, and a composed store with no provider
     behind it is the green-node-that-answers-nothing failure #200 exists to prevent. The
     uploaded document is already answerable without one - since #255 the document bridge
     is asked on every /router/ask.

    PYTHONPATH=src python3 tests/selftest_561_upload_card.py
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("SELFHOST_BACKEND", "memory")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
STATIC = ROOT / "src/dbsearch/server/static"
CANVAS = (STATIC / "js/surfaces/canvas.js").read_text()


def _kinds_block():
    return CANVAS.split("const KINDS", 1)[1].split("\n  };", 1)[0]


def _files_group():
    m = re.search(r'\{key:"files".*?\}', CANVAS, re.S)
    assert m, "the Files & Links palette group is gone"
    return m.group(0)


def test_files_and_local_offers_an_upload_card():
    assert '"upload"' in _files_group(), \
        "Files & Links does not list the upload kind - a hosted user still has no file door"
    assert re.search(r"\bupload:\s*\{", _kinds_block()), "KINDS has no upload card"


def test_the_upload_card_is_not_operator_gated():
    """The whole point. An operatorOnly upload card is the bug again."""
    card = re.search(r"\bupload:\s*\{[^}]*\}", _kinds_block(), re.S)
    assert card, "KINDS has no upload card"
    assert "operatorOnly" not in card.group(0), \
        "the upload card is operator-gated, which is the exact hole #561 set out to close"


def test_folder_stays_operator_gated():
    """A regression here is a file-read primitive handed to an untrusted caller."""
    card = re.search(r"\bfolder:\s*\{.*?\}\]\}", _kinds_block(), re.S) \
        or re.search(r"\bfolder:\s*\{[^\n]*(\n[^\n]*){0,6}", _kinds_block())
    assert card and "operatorOnly:true" in card.group(0), \
        "the folder card is no longer operatorOnly - compose still refuses it for non-operators, "
    api = (ROOT / "src/dbsearch/server/router_api.py").read_text()
    assert "_reads_local_files" in api, "the server-side local-file gate is gone"


def test_upload_reaches_the_endpoint_that_already_exists():
    assert "/admin/upload" in CANVAS, "the canvas never calls /admin/upload"
    from dbsearch.server.app import app  # noqa: E402
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/admin/upload" in paths, "the upload endpoint is gone"


def test_upload_never_becomes_a_composed_store():
    """A node enters liveManifest() and gets composed. Upload has no provider behind it, so a
    node would compose as an empty store: green, and answering nothing (#200)."""
    add = CANVAS.split("function addNode(kind)", 1)[1].split("\n  }", 1)[0]
    assert re.search(r'kind\s*===?\s*["\']upload["\']', add), (
        "addNode() does not special-case the upload kind, so clicking + drops an upload NODE "
        "onto the canvas and liveManifest() will try to compose it as a store")


def test_the_demo_visitor_is_not_offered_an_upload_they_cannot_do():
    """/admin/upload needs a real session; an anonymous demo visitor gets 401. Offering the
    card anyway would be a tile that always fails - worse than not showing one (#551's rule)."""
    assert re.search(r"isDemoMode\(\)|authState\.signed_in", CANVAS), \
        "the canvas lost its demo/sign-in state, so the upload card cannot be gated honestly"


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
