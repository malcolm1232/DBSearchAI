"""#629: sources collapse to one line, the marker is a control, detail lives in a panel.

THE DEFECT the owner reported, on a two-turn conversation: every answer ended with a Sources
block listing every retrieved document AND a separate grounding sentence. Two paragraphs of
apparatus per turn, on every turn - by the third exchange most of the screen is machinery
rather than answer. His word was "messy".

WHAT MOVED AND WHAT DID NOT. The apparatus collapsed; the PROMISE did not. "1 of 2 you can
access" stays on screen because that is LAW 2 demonstrated on this specific answer, and a
claim you have to click to see is a claim most people never see.

Every rule the old rail kept is asserted here again, because they all had to be carried
across by hand: uncited sources grouped and never filtered (#622), an answer with no markers
accusing nobody (the extractive carve-out), and #633's quotes appearing under CITED rows only
- a passage quoted under a document the answer never pointed at dresses evidence up as
attribution, which is the very confusion the grouping exists to prevent.

Run FOR REAL in node against the shipped module: this repo has been bitten three times by
tests that grep a JS asset and stay green while the user receives something else.

    python3 tests/selftest_629_sources_panel.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import _domgate  # noqa: E402  the shared jsdom gate (#792)

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)

COMPONENTS = ROOT / "src/dbsearch/server/static/js/ui/components.js"
JSDOM = _domgate.JSDOM

BOOT = f"""
  import {{ pathToFileURL }} from "node:url";
  const {{ JSDOM }} = await import(pathToFileURL("{JSDOM.as_posix()}").href);
  const dom = new JSDOM("<!doctype html><html><body><div id=root></div></body></html>");
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  const C = await import(pathToFileURL("{COMPONENTS.as_posix()}").href);
"""

CITES = ('[{doc:"a",title:"HR leave policy",uri:null},'
         '{doc:"b",title:"Remote work policy",uri:null}]')
CORPUS = "{indexed: true, authorized_docs: 2}"


def _have():
    """True: run the DOM check. False: a skip that `tests/_domgate.py` has already counted.

    Raises when node or jsdom is missing and `DBSEARCH_ALLOW_DOM_SKIP=1` was not set. Before
    #792 this returned a bare False and every caller then reported a PASS, so these guards were
    green no-ops on every clean clone and in CI."""
    return _domgate.gate("the sources-panel DOM check")


def _node(script):
    p = subprocess.run(["node", "--input-type=module", "-e", BOOT + script],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert p.returncode == 0, f"node failed:\n{p.stderr[-2000:]}"
    return json.loads(p.stdout)


def _skip():
    """The DOM half did not run. `_have` has already printed and counted why."""
    return True


def test_the_served_module_is_the_one_under_test():
    served = client.get("/static/js/ui/components.js").text
    assert served == COMPONENTS.read_text(), \
        "the served components.js differs from the file on disk - this test proves nothing"
    print("  PASS  the test drives the module the browser receives")


def test_a_marker_is_a_button_carrying_its_source_number():
    if not _have():
        return _skip()
    out = _node("""
      const d = document.createElement("div");
      d.append(C.answerNodes("leave is 26 days [1] and remote is two 【2†L3-L4】."));
      const b = [...d.querySelectorAll("button.cite-ref")];
      console.log(JSON.stringify({
        n: b.length,
        cites: b.map(x => x.getAttribute("data-cite")),
        loc: b[1] ? b[1].getAttribute("data-loc") : null,
        text: b.map(x => x.textContent),
        sups: d.querySelectorAll("sup.cite-ref").length,
      }));
    """)
    assert out["n"] == 2, out
    assert out["cites"] == ["1", "2"], out
    assert out["text"] == ["[1]", "[2]"], out
    assert out["loc"] == "L3-L4", out
    assert out["sups"] == 0, "a marker is still an unreachable <sup>"
    print("  PASS  both marker spellings render as buttons naming their source")


def test_the_pill_keeps_the_permission_count_on_screen():
    if not _have():
        return _skip()
    out = _node(f"""
      const pill = C.sourcesPill({CITES}, "x [1]", {{retrieved: 1, corpus: {CORPUS}}});
      const nocorpus = C.sourcesPill({CITES}, "x [1]", {{retrieved: 2, corpus: null}});
      const none = C.sourcesPill([], "x", {{retrieved: 0, corpus: {CORPUS}}});
      console.log(JSON.stringify({{
        text: pill.textContent, tag: pill.tagName,
        nocorpus: nocorpus.textContent,
        none: none === null,
      }}));
    """)
    assert "1 of 2 you can access" in out["text"], out["text"]
    assert out["tag"] == "BUTTON", out["tag"]
    # No denominator to state: say the count you DO know, claim nothing about entitlement.
    assert "you can access" not in out["nocorpus"], out["nocorpus"]
    assert out["none"] is True, "a pill was offered for an answer with no citations"
    print("  PASS  the trim count stays visible; no citations means no pill")


def test_uncited_sources_are_grouped_never_filtered():
    if not _have():
        return _skip()
    out = _node(f"""
      const p = C.buildSourcesPanel({{question: "how many days?", cites: {CITES},
        answer: "26 days [1].", retrieved: 1, corpus: {CORPUS}, as: "alice"}});
      console.log(JSON.stringify({{
        cards: p.querySelectorAll(".source-card").length,
        head: p.textContent.includes("Also given to the model"),
        grounded: p.textContent.includes("of the 2 documents you can access"),
        question: p.textContent.includes("how many days?"),
      }}));
    """)
    assert out["cards"] == 2, (
        "an uncited source was dropped - the numbering is lockstep with the context blocks, "
        "so removing a row renumbers every later marker (#257)")
    assert out["head"], "uncited sources are not grouped under their own heading"
    assert out["grounded"], "the full grounding sentence is missing from the panel"
    assert out["question"], "the panel does not say which question it belongs to"
    print("  PASS  uncited sources are grouped, never filtered; the panel names its question")


def test_an_answer_with_no_markers_accuses_nobody():
    """The extractive model never emits a marker in ANY answer. Labelling its rows would tell
    the reader, on every answer of every extractive deployment, that an answer built out of
    those documents does not come from them."""
    if not _have():
        return _skip()
    out = _node(f"""
      const p = C.buildSourcesPanel({{question: "q", cites: {CITES},
        answer: "Based on 2 retrieved sources: ...", retrieved: 2,
        corpus: {CORPUS}, as: "alice"}});
      console.log(JSON.stringify({{
        head: p.textContent.includes("Also given to the model"),
        cards: p.querySelectorAll(".source-card").length,
      }}));
    """)
    assert out["cards"] == 2, out
    assert not out["head"], "an answer that marks nothing had a source accused of being uncited"
    print("  PASS  an answer with no markers labels no row")


def test_a_quote_renders_under_a_cited_row_only_with_its_kind():
    """#633. A passage quoted under a document the answer never pointed at would dress
    evidence up as attribution - the confusion the grouping above exists to prevent."""
    if not _have():
        return _skip()
    cites = ('[{doc:"a",title:"HR leave policy",uri:null,'
             'quote:"employees receive 26 days",quote_kind:"pointed"},'
             '{doc:"b",title:"Remote work policy",uri:null,'
             'quote:"remote is two days",quote_kind:"retrieved"}]')
    out = _node(f"""
      const p = C.buildSourcesPanel({{question: "q", cites: {cites}, answer: "26 days [1].",
        retrieved: 1, corpus: {CORPUS}, as: "alice"}});
      const q = [...p.querySelectorAll(".source-quote")];
      console.log(JSON.stringify({{
        n: q.length,
        cap: q[0] ? q[0].querySelector(".source-quote-cap").textContent : "",
        body: q[0] ? q[0].textContent.includes("26 days") : false,
        leaked: p.textContent.includes("remote is two days"),
      }}));
    """)
    assert out["n"] == 1, "a quote rendered under an uncited row"
    assert out["cap"] == "The lines the answer points at", out["cap"]
    assert out["body"], out
    assert not out["leaked"], "the uncited document's passage was quoted anyway"
    print("  PASS  quotes appear under cited rows only, captioned by kind")


def test_a_retrieved_quote_says_it_was_retrieved():
    if not _have():
        return _skip()
    cites = ('[{doc:"a",title:"HR",uri:null,quote:"some passage",quote_kind:"retrieved"}]')
    out = _node(f"""
      const p = C.buildSourcesPanel({{question: "q", cites: {cites}, answer: "no markers",
        retrieved: 1, corpus: {CORPUS}, as: "alice"}});
      console.log(JSON.stringify({{
        cap: p.querySelector(".source-quote-cap").textContent}}));
    """)
    assert out["cap"] == "Top passage given to the model", out["cap"]
    print("  PASS  a retrieved passage never claims to be what the answer points at")


def test_the_panel_opens_repaints_and_closes():
    if not _have():
        return _skip()
    out = _node(f"""
      const root = document.getElementById("root");
      const panel = C.mountSourcesPanel(root);
      // On <body>, NOT inside root - see the panel-parent test below for why.
      const el = () => document.querySelector(".sources-panel");
      const before = el().hidden;
      panel.open({{question: "first question", cites: {CITES}, answer: "a [1]",
                  retrieved: 1, corpus: {CORPUS}, as: "alice"}});
      const openedText = el().textContent;
      panel.open({{question: "second question", cites: {CITES}, answer: "b [1]",
                  retrieved: 1, corpus: {CORPUS}, as: "alice"}});
      const repainted = el().textContent;
      const pushes = root.classList.contains("has-sources-panel");
      panel.close();
      console.log(JSON.stringify({{
        before, opened: !el().hidden ? false : true,
        first: openedText.includes("first question"),
        repaint_dropped_first: !repainted.includes("first question"),
        repaint_has_second: repainted.includes("second question"),
        pushes, closed: el().hidden,
        cleared: !root.classList.contains("has-sources-panel"),
        only_one: document.querySelectorAll(".sources-panel").length,
      }}));
    """)
    assert out["before"] is True, "the panel starts open"
    assert out["first"], "opening did not render the question it was opened for"
    assert out["repaint_dropped_first"] and out["repaint_has_second"], (
        "opening from a second answer left the first answer's contents behind - a reader "
        "would check a claim against the wrong evidence")
    assert out["only_one"] == 1, "more than one panel per surface"
    assert out["pushes"], "the reading column is never told to make room"
    assert out["closed"] and out["cleared"], "closing left the panel or its layout class"
    print("  PASS  one panel per surface: opens, repaints per answer, closes cleanly")


def test_the_panel_is_not_a_child_of_the_reading_column():
    """FOUND IN A BROWSER, and unfindable without one. The panel was appended INTO the
    surface, which carries

        #view-app .surface:has(.chat-composer) > * { width:100%; max-width:820px }

    to centre the thread - so the panel inherited an 820px width and covered the answer it
    exists to sit beside. jsdom has no layout, so every assertion here stayed green while the
    page was unusable.

    Mounting on <body> puts it outside that subtree, where no column rule can reach it. The
    host still gets the push class, so the column knows to make room."""
    if not _have():
        return _skip()
    out = _node("""
      const root = document.getElementById("root");
      C.mountSourcesPanel(root);
      const p = document.querySelector(".sources-panel");
      console.log(JSON.stringify({
        parent: p.parentElement.tagName,
        inside_root: root.contains(p),
      }));
    """)
    assert out["parent"] == "BODY", (
        f"the panel is mounted inside {out['parent']} - a column rule can size it again")
    assert not out["inside_root"], "the panel is still a descendant of the reading column"

    css = client.get("/static/css/app.css").text
    assert ".surface:has(.chat-composer).has-sources-panel" in css, (
        "the push rule no longer matches on BOTH conditions - it then ties with the reading "
        "column's own padding rule and loses on source order, which is invisible: the class "
        "is applied and nothing moves")
    print("  PASS  the panel sits outside the reading column, and the push rule wins")


def test_every_surface_uses_the_one_definition():
    """The rail existed in four copies once. `sourcesRail` is GONE, and each surface that
    shows sources imports the same pill and panel."""
    for path in ("surfaces/ask.js", "surfaces/draft.js", "visitor.js"):
        js = client.get(f"/static/js/{path}").text
        assert "sourcesRail" not in js, f"{path} still calls the deleted rail"
        assert "sourcesPill" in js and "mountSourcesPanel" in js, \
            f"{path} does not use the shared pill/panel"
    comp = client.get("/static/js/ui/components.js").text
    assert "export function sourcesRail" not in comp, "sourcesRail was left behind"
    print("  PASS  one definition, used by Ask, Draft and the link visitor")


if __name__ == "__main__":
    test_the_served_module_is_the_one_under_test()
    test_a_marker_is_a_button_carrying_its_source_number()
    test_the_pill_keeps_the_permission_count_on_screen()
    test_uncited_sources_are_grouped_never_filtered()
    test_an_answer_with_no_markers_accuses_nobody()
    test_a_quote_renders_under_a_cited_row_only_with_its_kind()
    test_a_retrieved_quote_says_it_was_retrieved()
    test_the_panel_opens_repaints_and_closes()
    test_the_panel_is_not_a_child_of_the_reading_column()
    test_every_surface_uses_the_one_definition()
    print("\nSOURCES PANEL SELF-TEST PASSED.")
