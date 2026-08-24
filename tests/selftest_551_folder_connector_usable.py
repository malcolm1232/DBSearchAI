"""#551 — the folder connector is reachable from the palette, and it indexes a flat folder.

Two defects, one card:

1. NO PALETTE CARD. `folder` is a registered provider and is in PLANNED_KINDS server-side, but
   the canvas had no KINDS row for it — the source even said so in a comment. So the only
   document connector a self-hoster can actually use (SharePoint needs a licensed tenant) was
   reachable only via the setup agent or a hand-written manifest.

2. A FLAT FOLDER INDEXED NOTHING, SILENTLY. The connector reads a document's audience from its
   immediate sub-directory name and skips files sitting loose in the root — default-deny, and
   correct. But point it at an ordinary folder of HR documents and it ingested zero files while
   still reporting a healthy store. A silently empty index is the worst failure shape there is,
   because nothing anywhere says the word "empty". The store's own acl is now the fallback
   audience, which cannot widen access: the store acl already gates who may query it at all.

    PYTHONPATH=src python3 tests/selftest_551_folder_connector_usable.py
"""
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
CANVAS = (ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js").read_text()

from dbsearch.router.providers.connector import folder_connector_factory  # noqa: E402


def test_the_palette_has_a_folder_card():
    assert re.search(r"^\s*folder:\s*\{", CANVAS, re.M), "no KINDS row for `folder`"
    files_row = re.search(r'\{key:"files".*?kinds:\[([^\]]*)\]', CANVAS, re.S)
    assert files_row and '"folder"' in files_row.group(1), \
        "`folder` is defined but not offered under Files & Links"


def test_the_card_is_operator_only_and_the_gate_is_not_loosened():
    """Compose refuses local file sources for non-operators — a server-side path read from an
    untrusted caller is a file-read primitive. The card must respect that, never the reverse:
    a tile that always 403s is worse than no tile."""
    row = re.search(r"^\s*folder:\s*\{[^\n]*", CANVAS, re.M).group(0)
    assert "operatorOnly:true" in row, "the folder card is offered to non-operators"
    gate = (ROOT / "src/dbsearch/server/router_api.py").read_text()
    assert "local file sources are operator-only on this deployment" in gate, \
        "the server-side local-file gate was removed to make the card work"


def test_the_count_and_the_flyout_agree():
    """If the row count and the menu filter differently, a row says '3 services' and lists 2."""
    assert "function visibleKinds(p)" in CANVAS, "no single definition of the visible services"
    assert CANVAS.count("visibleKinds(p)") >= 3, \
        "visibleKinds must drive the row count AND the flyout, not one of them"


def test_a_flat_folder_is_indexed_using_the_stores_own_acl():
    """The behaviour a user actually hits: an ordinary folder, no group sub-directories."""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "leave-policy.txt").write_text("Primary carers receive 18 weeks.")
        conn = folder_connector_factory({"id": "hr", "path": d, "acl": ["owner-oid"]})
        items = conn.list_changes(None)[0]
        assert items, "a flat folder still indexes nothing — the silent-empty-store bug"
        assert items[0]["acl"] == ["owner-oid"], \
            f"document did not inherit the store's audience: {items[0]['acl']}"


def test_an_explicit_default_acl_still_wins():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "policy.txt").write_text("x")
        conn = folder_connector_factory({"id": "hr", "path": d, "acl": ["owner-oid"],
                                         "default_acl": ["hr-team"]})
        items = conn.list_changes(None)[0]
        assert items[0]["acl"] == ["hr-team"], "explicit default_acl was overridden"


def test_sub_directory_groups_are_untouched():
    """The fallback must apply ONLY to loose files — a folder that uses sub-directories as
    groups is the documented convention and must keep working exactly as before."""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "deal-team").mkdir()
        (Path(d) / "deal-team" / "falcon.txt").write_text("secret")
        conn = folder_connector_factory({"id": "hr", "path": d, "acl": ["owner-oid"]})
        items = conn.list_changes(None)[0]
        assert items[0]["acl"] == ["deal-team"], \
            f"a sub-directory group was overridden by the fallback: {items[0]['acl']}"


def test_the_store_acl_reaches_the_provider_at_all():
    """provisioning builds the config dict the factory sees; if `acl` stops riding along, the
    fallback silently becomes a no-op and the flat-folder bug returns."""
    prov = (ROOT / "src/dbsearch/router/provisioning.py").read_text()
    assert '"acl": e.get("acl", [])' in prov, "the store acl no longer reaches the provider"


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
