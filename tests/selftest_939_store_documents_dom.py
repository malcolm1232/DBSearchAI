"""#939 / #895 - a connected node shows its doc count, its REAL freshness, and its files.

This is the launch gate's failing clause: "connect a node, verify it shows synced + doc count".
Measured on prod 260823 - `/router/catalog` said `ingested@08:58:31` while the node badge still
read `syncing`, because the node's freshness is a snapshot taken AT COMPOSE and the crawl
finishes afterwards. There was no doc count anywhere, and no way to learn whether a particular
file had landed. The owner's report was exactly that: "still no data, but idk if its ingesting
or not".

THE FIXTURE'S COMPOSE RESPONSE IS STALE IN EVERY SCENARIO, deliberately. It reports
`syncing@08:50` while the documents endpoint reports `ingested@08:58`. If the fixture agreed
with itself there would be nothing under test - the whole defect is one surface trusting a
snapshot that another surface has already superseded.

    ingested   crawl finished   -> doc count shown, stale `syncing` GONE, files named
    syncing    crawl running    -> `syncing` stays, NO count invented (CONTROL)
    unreadable a file failed    -> #725, it is said out loud rather than omitted
    unknown    store can't say  -> nothing claimed: no count, no "0 documents" (CONTROL)

  PYTHONPATH=src python3 tests/selftest_939_store_documents_dom.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import _domgate  # noqa: E402  the shared jsdom gate (#792)

PROBE = ROOT / "tests/canvas_store_documents_dom_probe.mjs"
CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
JSDOM = ROOT / "tests/node_modules/jsdom/lib/api.js"

_CACHE: dict = {}
STALE = "syncing@2026-08-23T08:50:00Z"


def _report(scenario):
    if scenario in _CACHE:
        return _CACHE[scenario]
    if not _domgate.gate(f"the canvas store-documents DOM probe ({scenario})"):
        return None
    out = subprocess.run(["node", str(PROBE), str(JSDOM), str(CANVAS), scenario],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(f"probe failed:\n{out.stderr[-2000:]}")
    _CACHE[scenario] = json.loads(out.stdout)
    return _CACHE[scenario]


def test_a_finished_crawl_stops_saying_syncing():
    """The stale badge, which is half of #895's failing clause. The compose snapshot said
    `syncing` and the crawl has since finished; the node must not still be repeating it."""
    r = _report("ingested")
    if r is None:
        return
    assert STALE not in r["node_text"], (
        f"the node is still showing its compose-time snapshot: {r['node_pills']}")


def test_a_finished_crawl_shows_a_doc_count():
    """The other half of the clause. Two documents, so the card must say two - not a
    timestamp, and not nothing."""
    r = _report("ingested")
    if r is None:
        return
    # A PILL THAT IS THE COUNT, not a substring that happens to contain one. The first
    # version of this asserted `"2" in pills and "doc" in pills` and passed BEFORE the fix:
    # the `documents` capability pill supplies "doc" and the timestamp `2026` supplies "2".
    # An assertion that green on the defect is worse than no assertion.
    import re
    counts = [p for p in r["node_pills"] if re.fullmatch(r"\d+ docs?", p.strip())]
    assert counts == ["2 docs"], (
        f"no doc-count pill on the card (pills were {r['node_pills']})")


def test_the_panel_names_the_files_that_landed():
    """The owner's actual question - 'did DBSNotes.txt land?' - must be answerable by reading
    the panel, not by asking a question and inspecting citations."""
    r = _report("ingested")
    if r is None:
        return
    assert r["docs_section_present"], "no documents section in the panel"
    rows = " ".join(r["doc_rows"]) or r["docs_section_text"]
    assert "DBSNotes.txt" in rows, f"the file is not named: {r['docs_section_text']!r}"
    assert "handbook.pdf" in rows, f"only one of two files listed: {r['doc_rows']}"


def test_a_running_crawl_does_not_invent_a_count():
    """CONTROL. Mid-crawl the count is 0 and will not be 0 in a minute. Printing "0 docs"
    there is the #392 error in miniature - a number we have, about a state that is still
    moving, presented as the answer."""
    r = _report("syncing")
    if r is None:
        return
    pills = " ".join(r["node_pills"]).lower()
    assert "0 doc" not in pills, f"a mid-crawl zero was printed as a count: {r['node_pills']}"
    assert "syncing" in " ".join(r["node_pills"]).lower(), (
        f"a running crawl stopped saying so: {r['node_pills']}")


def test_a_store_that_cannot_say_is_not_reported_as_empty():
    """CONTROL, and the one a careless fix fails. `known: false` is a SQL store, or a listing
    that errored. Rendering "0 documents" or an empty file list there states something we did
    not measure - #392's rule, which is the whole reason this product has a corpus block."""
    r = _report("unknown")
    if r is None:
        return
    pills = " ".join(r["node_pills"]).lower()
    assert "0 doc" not in pills, f"unknown was rendered as empty: {r['node_pills']}"
    assert not r["doc_rows"], f"file rows invented for a store that cannot list: {r['doc_rows']}"


def test_a_file_the_crawl_could_not_read_is_said_out_loud():
    """#725, riding along because a file list makes its absence WORSE. The file is in the
    user's folder, missing from the list, and without this nothing anywhere says why."""
    r = _report("unreadable")
    if r is None:
        return
    seen = (r["docs_section_text"] + " " + " ".join(r["node_pills"])).lower()
    assert "2" in seen and ("couldn't read" in seen or "could not read" in seen
                            or "unreadable" in seen), (
        f"two unfetchable files vanished silently: {r['docs_section_text']!r}")


def test_the_file_rows_are_the_uploads_panel_shell_not_a_lookalike():
    """The owner asked for "similar font and UI" to Your documents. The way to keep two lists
    looking alike is to make them the SAME list, so this pins the shared classes rather than
    any particular pixel: .updoc-list around .updoc-row around .updoc-title, exactly as
    renderDocsPanel builds them. A parallel set of classes would pass a screenshot review on
    the day it shipped and drift the first time either one was touched."""
    r = _report("ingested")
    if r is None:
        return
    assert r["list_is_updoc_list"], "the file list is not the uploads .updoc-list shell"
    assert len(r["row_classes"]) == 2, f"expected two .updoc-row rows, got {r['row_classes']}"
    assert all("updoc-row" in c for c in r["row_classes"]), r["row_classes"]
    assert sorted(r["row_titles"]) == ["DBSNotes.txt", "handbook.pdf"], r["row_titles"]


def test_every_file_row_offers_a_way_to_reach_the_source():
    """The per-row action. Deliberately Open and not Delete: DELETE /documents/{id} reaches the
    EDITION's index by owner_oid and a connector document lives in its store's own index, so
    the button would 404 - and a delete that DID land would be undone by the next crawl, which
    is the #731/#941 family (a gesture that looks like it worked and does not stick). #943
    holds that decision."""
    r = _report("ingested")
    if r is None:
        return
    opens = [a for a in r["row_actions"] if a["text"] == "Open"]
    assert len(opens) == 2, f"not every file can be opened: {r['row_actions']}"
    assert all(a["href"].startswith("gdrive://") for a in opens), opens
    assert all(a["target"] == "_blank" for a in opens), (
        f"Open navigates the canvas away instead of opening the source: {opens}")


if __name__ == "__main__":
    fails = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                fails.append(name)
                print(f"FAIL {name}\n     {exc}")
    print(f"\n{'FAILED' if fails else 'PASSED'}: {len(fails)} failure(s)")
    sys.exit(1 if fails else 0)
