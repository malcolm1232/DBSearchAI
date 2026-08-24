"""#622: the Sources rail says what it is, and one rail serves every surface.

THE DEFECT, seen on dbsearch.ai on 260811: "How many days of paid annual leave do full-time
employees get?" was answered purely from the leave policy and carried a single [1] marker,
while the card underneath read "Sources (2)" and listed the remote-work policy as [2]. A
refusal ("I do not have that information in the provided context") rendered the same way,
with two sources under an answer that used none. The list was never the documents the answer
USED - it is every document that survived the permission trim and the top-k cut and was
placed in the prompt as context. That is a useful thing to show. It is not what "Sources"
was being read as.

WHAT WAS DELIBERATELY NOT DONE, because it is the obvious fix and it is wrong: filtering the
list down to the marked documents. The model is handed every listed document's TEXT, so an
answer can be shaped by one it never marks - a filtered list would claim an independence
nothing can verify. And the numbering is lockstep with the context blocks (#257), so dropping
a row means renumbering, which is how you invent provenance rather than remove it. The share
scope is built from the same evidence set for the same reason and is NOT narrowed by this
card - see ADR 0021 invariant 1, corrected in this commit to say so in words.

HOW THIS IS TESTED, and why it matters here specifically: `citedMarkers` runs FOR REAL, in
node, imported from the shipped module. This repo has been bitten three times by tests that
grep a JS asset and stay green while the user receives something else, most recently the
four disclosure tests that pinned a sentence in ask.js which no visitor ever loaded. A grep
for "not cited in this answer" would pass against a file nobody imports.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import _domgate  # noqa: E402  the shared node/jsdom gate (#792)
JS = ROOT / "src/dbsearch/server/static/js"
COMPONENTS = JS / "ui/components.js"

# #632: chat.js is gone - Ask IS the conversational surface now, so three files
# carry the one rail rather than four.
#
# #689: there are now TWO rails, and this file governs one of them. `ui/components.js` owns the
# DOCUMENT rail (the pill and the sources panel), which is what every assertion here is about.
# `ui/proofs.js` owns the ROUTED rail (the numbered Sources block with Verify data), moved out
# of canvas.js by ADR 0025 so /ask and /canvas explain a routed answer identically. Both are ui
# modules and neither is a SURFACE, which is why the rule below - no surface hand-rolls a
# rail - still says exactly what it meant: a surface that assembles either one is drifting.
# `surfaces/canvas.js` is deliberately still absent from this list, for the reason it always
# was: it is the routed rail's original home and does not use the document pill at all.
SURFACES = ["surfaces/ask.js", "visitor.js", "surfaces/draft.js"]


def _node(script: str):
    """Run an ES-module snippet in node and return its parsed JSON stdout."""
    r = subprocess.run([sys.executable and "node", "--input-type=module", "-e", script],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, f"node failed:\n{r.stderr[:2000]}"
    return json.loads(r.stdout)


def _have_node() -> bool:
    """True: run it. False: a skip `tests/_domgate.py` has counted. Raises without the opt-out
    (#792) - a parse check that evaporates is a parse check nobody is running."""
    return _domgate.gate("the sources-component node check")


def test_cited_markers_reads_both_marker_spellings():
    """`[n]` is what the server leaves behind; 【n†Lx-Ly】 is the model's own convention that
    answerNodes renders as the same footnote. Reading only one spelling would mark a genuinely
    cited source as uncited wherever the other survived - and a false accusation is no better
    than the false credit this function removes."""
    if not _have_node():
        return          # permitted skip (DBSEARCH_ALLOW_DOM_SKIP), already counted
    out = _node(f"""
      import {{ citedMarkers }} from "{COMPONENTS.as_posix()}";
      const cases = {{
        plain:      "Staff receive 25 days [1].",
        model:      "Staff receive 25 days 【1†L4-L6】.",
        bare_model: "Staff receive 25 days 【2】.",
        both:       "A [1] and B 【3†L2】.",
        none:       "I do not have that information in the provided context.",
        repeated:   "First [2] and again [2].",
      }};
      const out = {{}};
      for (const [k, v] of Object.entries(cases)) out[k] = [...citedMarkers(v)].sort();
      console.log(JSON.stringify(out));
    """)
    assert out["plain"] == [1], out
    assert out["model"] == [1], out
    assert out["bare_model"] == [2], out
    assert out["both"] == [1, 3], out
    assert out["none"] == [], out
    assert out["repeated"] == [2], out
    print("  PASS  citedMarkers reads [n] and 【n†Lx】, and a refusal cites nothing")


def test_the_prod_case_marks_the_uncited_source_and_keeps_it_listed():
    """The exact shape seen on dbsearch.ai: two retrieved documents, one marker. The second
    row must still be THERE (it was evidence) and must be MARKED (the answer does not point
    at it). Asserted on the rendered DOM, not on the function that feeds it."""
    if not _have_node():
        return          # permitted skip (DBSEARCH_ALLOW_DOM_SKIP), already counted
    out = _node(f"""
      // A minimal DOM, built to what el() in components.js actually touches: createElement,
      // className, setAttribute, append, and the nodeType check that decides whether a child
      // is already a node. Deliberately NOT a mock of sourcesRail's behaviour - the function
      // under test is the shipped one, and only the browser around it is stand-in.
      globalThis.document = {{
        createElement(tag) {{
          return {{
            tag, nodeType: 1, className: "", attrs: {{}}, kids: [],
            setAttribute(k, v) {{ this.attrs[k] = v; }},
            append(...xs) {{ this.kids.push(...xs); }},
          }};
        }},
        createTextNode(t) {{ return {{ nodeType: 3, text: String(t) }}; }},
      }};
      const {{ buildSourcesPanel }} = await import("{COMPONENTS.as_posix()}");
      // Each ELEMENT contributes one row: its class, and the text of its direct text children.
      const flat = (n, acc = []) => {{
        if (n.nodeType !== 1) return acc;
        const text = (n.kids || []).filter((k) => k.nodeType === 3)
                                   .map((k) => k.text).join("");
        acc.push({{ cls: n.className || "", text }});
        (n.kids || []).forEach((k) => flat(k, acc));
        return acc;
      }};
      const CITES = [{{ doc: "leave", title: "Holiday and Annual Leave Policy.txt" }},
                     {{ doc: "remote", title: "Remote Work and Home Office Policy.txt" }}];
      const rail = buildSourcesPanel({{ cites: CITES,
        answer: "Full-time employees are entitled to 25 days of paid annual leave [1]." }});
      const unmarked = buildSourcesPanel({{ cites: CITES,
        answer: "Based on 2 retrieved source(s): HOLIDAY AND ANNUAL LEAVE POLICY ..." }});
      console.log(JSON.stringify({{
        rail: flat(rail), unmarked: flat(unmarked),
        empty: flat(buildSourcesPanel({{ cites: [], answer: "x" }}))
                 .filter((n) => n.cls === "source-card").length === 0,
      }}));
    """)
    nodes = out["rail"]
    titles = [n["text"] for n in nodes if n["cls"] == "title"]
    uncited = [n["text"] for n in nodes if n["cls"] == "source-uncited"]
    group_heads = [n["text"] for n in nodes if n["cls"] == "sources-group-head"]

    # BOTH rows survive - that is the rule, and it is the reason filtering was refused.
    assert titles == ["Holiday and Annual Leave Policy.txt",
                      "Remote Work and Home Office Policy.txt"], titles
    # #629 moved the DISCRIMINATION from a caption under each row to a group heading above
    # the uncited ones. Same claim, said once instead of once per row.
    assert group_heads == ["Also given to the model"], group_heads
    assert uncited == [], (
        "the per-row caption came back as well as the grouping - the reader is now told the "
        "same thing twice")
    print("  PASS  the uncited source is still listed, and is separated out")

    # An answer with NO markers anywhere marks nothing. Caught by looking at the rendered
    # page, not here: the Extractive model never emits a marker, so marking on marker-absence
    # labelled every row of every answer on any deployment pinned to it - telling the reader
    # that an answer which opens "Based on 2 retrieved source(s)" is not from those sources.
    # The mark means "this one, unlike the others"; with nothing to contrast it says nothing.
    assert [n for n in out["unmarked"] if n["cls"] == "sources-group-head"] == [], \
        out["unmarked"]
    print("  PASS  an answer that marks nothing leaves every source unlabelled")

    assert out["empty"] is True, "an empty citation list must render no source rows at all"
    print("  PASS  no citations -> no rows")


def test_every_surface_uses_the_one_rail():
    """Four surfaces rendered their own copy of this block, which is how three of them could
    be honest and one not. The rail is now built in exactly one place; a surface that hand-
    rolls `sources-title` again is drifting and this fails."""
    offenders = []
    for rel in SURFACES:
        text = (JS / rel).read_text()
        # draft.js legitimately uses `sources-title` for two NON-source headings ("These are
        # your requirements", "Plan · N steps"), so the tell is a rail built around a
        # citations array, not the class alone.
        if "sourceCard(" in text:
            offenders.append(f"{rel} calls sourceCard directly instead of the shared panel")
        if "`Sources (${" in text:
            offenders.append(f"{rel} builds its own Sources heading")
        if "buildSourcesPanel(" in text:
            offenders.append(f"{rel} builds panel contents itself instead of opening the one "
                             "the surface mounted")
    assert not offenders, "\n".join(offenders)
    print(f"  PASS  all {len(SURFACES)} surfaces render the shared pill and panel")


def test_components_is_the_only_place_that_knows_the_wording():
    """One string, one place. The phrase a user reads must not exist in two files where one
    can be updated and the other cannot - the wrong-comment failure, applied to UI copy."""
    text = COMPONENTS.read_text()
    # #629's wording, which is what a reader now sees: the group heading, and the two quote
    # captions that say whether a passage is what the answer POINTS AT or what retrieval
    # HANDED OVER (#633). Each defined once, and in no surface.
    for phrase in ("Also given to the model", "The lines the answer points at",
                   "Top passage given to the model"):
        assert text.count(phrase) == 1, f"{phrase!r} is defined more than once"
        for rel in SURFACES:
            assert phrase not in (JS / rel).read_text(), f"{phrase!r} leaked into {rel}"
    print("  PASS  the source wording lives only in components.js")


if __name__ == "__main__":
    test_cited_markers_reads_both_marker_spellings()
    test_the_prod_case_marks_the_uncited_source_and_keeps_it_listed()
    test_every_surface_uses_the_one_rail()
    test_components_is_the_only_place_that_knows_the_wording()
    print("OK  selftest_622_sources_say_what_they_are")
