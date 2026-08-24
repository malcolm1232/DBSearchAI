"""#715 (disclosure line) and #729 (polish sweep) - what the answer LOOKS LIKE around itself.

These are one file because they are one render: the block that surrounds a canvas answer with
its routing story. Every assertion is DOM - the canvas mounted in jsdom and the real ask dock
driven - for the reason tests/canvas_doc_block_dom_probe.mjs records at length: the defects
here are about what a reader meets on screen and in what order, which no string search sees.

#715, THE DISCLOSURE LINE ONLY. When several stores match a question the router picks one and
says nothing about the others. On prod "what is our parental leave policy" was answered out of
a demo store while the owner's real HR documents sat unconsulted - and if the winner DECLINES,
the ask simply ends, with a rival that would have answered it never named. The routing engine's
half of that (fan-out on a tight cluster, or rescue on decline) is a design pass of its own and
is deliberately NOT touched here. What is cheap and worth having now is honesty about the
choice: the candidates are already in the routing response, so the answer can say which other
stores matched and were not consulted.

#729, THE POLISH SWEEP - the items that live in this same block:
  - the routing telemetry was the FIRST LINE of every answer, above the answer itself
  - "How this was answered (1 source)" sat beside citation markers [1][2][3]
  - a full stop left floating a space clear of the sentence after a superscript

    PYTHONPATH=src python3 tests/selftest_715_729_answer_surface.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import _domgate  # the shared jsdom gate (#792)

ROOT = Path(__file__).resolve().parents[1]
CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
#: #689: the Sources rail and its helpers MOVED here so /ask and /canvas explain an answer
#: the same way. This file follows the code rather than pinning where it used to live.
PROOFS = ROOT / "src/dbsearch/server/static/js/ui/proofs.js"
CANVAS_CSS = ROOT / "src/dbsearch/server/static/css/canvas.css"
JSDOM = _domgate.JSDOM
PROBE = ROOT / "tests/canvas_doc_block_dom_probe.mjs"

_cache: dict = {}


def _probe(scenario="answers", with_css=False):
    """Same helper as selftest_724, and the same rules: a CRASH is not a SKIP, and since #792
    missing tooling is not a skip either - it FAILS unless `DBSEARCH_ALLOW_DOM_SKIP=1` says
    otherwise, because these guards were silent no-ops on every clean clone and in CI.

    `with_css` injects canvas.css into the probe's document, which switches on jsdom's cascade
    and makes `getComputedStyle` answerable (#760). It is off by default so that every existing
    caller keeps reading the exact JSON it read before."""
    key = scenario + ("+css" if with_css else "")
    if key not in _cache:
        if not _domgate.gate(f"the answer-surface DOM check ({key})"):
            _cache[key] = None                         # permitted skip, already counted
        else:
            argv = ["node", str(PROBE), str(JSDOM), str(CANVAS), scenario]
            if with_css:
                argv.append(str(CANVAS_CSS))
            _cache[key] = _domgate.run_node(argv, f"the canvas ({key})")
    return _domgate.resolve(_cache[key])


def _fixtures():
    """#761: every footnote fixture the DOM probe carries, as data - so the fixtures themselves
    can be checked against the server function that builds the field, rather than being trusted
    because I wrote them."""
    return _probe("__fixtures__")


def _skip():
    """The DOM half of this test did not run. `_probe` has already printed and counted why."""
    return True


# ---- #715: the answer was a CHOICE, and it says so ----------------------------------------

def test_the_near_ties_that_were_not_consulted_are_named():
    r = _probe()
    if r is None:
        return _skip()
    line = r["also_matched"]
    assert line, "nothing on screen says another store matched this question"
    assert "storefront" in line and "support-tickets" in line, \
        f"a store inside the near-tie window is not disclosed: {line!r}"
    assert "not consulted" in line, \
        f"the line names rivals without saying what happened to them: {line!r}"


def test_the_store_that_answered_is_not_listed_as_a_rival():
    """It is in `candidates` too - it is a candidate that WON. Listing it under 'not
    consulted' would be false about the one store the reader can already see answered."""
    r = _probe()
    if r is None:
        return _skip()
    assert "azure_sql-1" not in r["also_matched"], \
        f"the selected store is listed as unconsulted: {r['also_matched']!r}"


def test_a_distant_match_is_not_disclosed():
    """The rule has to LEAVE THINGS OUT to be worth anything. hr-wiki scored 0.201 against a
    winning 0.584 - it is not a near-tie, and at thirteen connected stores a disclosure that
    lists everything is a wall of names the reader learns to skip."""
    r = _probe()
    if r is None:
        return _skip()
    assert "hr-wiki" not in (r["also_matched"] or ""), \
        f"a store far outside the near-tie window is disclosed: {r['also_matched']!r}"


# ---- #729: the polish items in this block --------------------------------------------------

def test_the_telemetry_is_no_longer_the_first_thing_on_screen():
    """"analytical · prefilter · conf 0.72 · matched revenue columns" was line one of every
    answer - engineer diagnostics above the thing the user asked for."""
    r = _probe()
    if r is None:
        return _skip()
    first = r["first_block_text"] or ""
    for jargon in ("prefilter", "conf 0.72", "analytical"):
        assert jargon not in first, \
            f"the answer still opens with routing telemetry ({jargon!r}): {first!r}"
    assert "4.2M" in first or "Revenue" in first, \
        f"the first thing on screen should be the answer: {first!r}"


def test_the_telemetry_is_kept_not_deleted():
    """It is real provenance and a developer surface depends on it - it moves into the trace,
    it does not disappear. A test that only checked the line above would pass if the fix had
    simply thrown the information away."""
    r = _probe()
    if r is None:
        return _skip()
    trace = r["trace_text"] or ""
    assert "prefilter" in trace and "conf 0.72" in trace, \
        f"the routing telemetry was dropped rather than moved: {trace!r}"
    assert "matched revenue columns" in trace, "the routing reason was dropped"


def test_the_summary_count_matches_what_is_beside_it():
    """"(1 source)" next to [1][2][3]: the summary counted stores, the rail numbered
    citations. Both counts were true, and together they read as one wrong count."""
    r = _probe()
    if r is None:
        return _skip()
    s = r["trace_summary"] or ""
    assert "1 source" in s, f"the store count is gone: {s!r}"
    assert "citation" in s, \
        f"the summary still names only one of the two counts on screen: {s!r}"
    # the router half has 2 footnotes and the document half 2 more; the summary describes the
    # ROUTER's rail, which is the one it sits inside.
    assert "2 citations" in s, f"the citation count is wrong: {s!r}"
    # #729 second review: the count went wrong again once #724 put the document half's marker
    # on the same screen - "1 citation" beside a visible [1] AND [2]. The trace counts the
    # ROUTER's rail, so it must say which rail it is counting rather than imply a total.
    assert "from your databases" in s, \
        f"the count is unscoped, so it reads as a total it does not count: {s!r}"


def test_the_document_card_does_not_say_the_same_thing_three_times():
    """Live: "[2] Document document hr-leave-policy.txt · uploaded · hr-leave-policy.txt".
    The system slot was the hardcoded word "Document" beside a "document" tag, and the title
    and the rewritten uri were both the filename - while the ROUTER's card beside it named its
    store, host and database. The one thing a reader needs here (which surface answered) was
    the one thing missing."""
    r = _probe()
    if r is None:
        return _skip()
    rows = r["doc_source_rows"]
    assert rows, "no document sources rendered"
    for d in rows:
        # A repeated SEGMENT is the defect ("name · uploaded · name"). A URL that happens to
        # contain the filename is not - that is the location doing its job, and asserting on a
        # substring rather than a segment failed on correct output.
        segs = [x for x in (d["loc"] or "").split(" · ") if x]
        assert len(set(segs)) == len(segs), f"the card repeats itself: {d['loc']!r}"
    txt = (r["doc_panel_text"] or "")
    assert "Your documents" in txt, \
        f"the document card still names no surface: {txt!r}"
    assert "Document document" not in txt, f"the word prints twice: {txt!r}"


def test_the_trace_survives_when_nothing_answered():
    """It became unconditional so the telemetry could live in it - which means the
    no-outcome case now renders a summary that must still read as English."""
    r = _probe("silent_router")
    if r is None:
        return _skip()
    s = r["trace_summary"] or ""
    assert s, "the trace vanished, taking the telemetry with it"
    assert "0 source" not in s, f"reads as a count where it should read as an outcome: {s!r}"
    assert "no source answered" in s, s


def test_a_citation_says_which_object_answered_not_just_which_store():
    """Three citations from one query rendered as three identical cards - "Azure SQL / host /
    database" each time, separable only by squinting at their snippets. The object was already
    computed server-side into `origin`, and the rail renders `system` + `location` and never
    `origin`, so it was discarded at the last step."""
    r = _probe()
    if r is None:
        return _skip()
    rows = r["router_source_rows"]
    assert len(rows) == 2, rows
    locs = [x["loc"] for x in rows]
    assert all("SalesOrderHeader" in locs[0] or "SalesOrderDetail" in locs[0] for _ in [0]), locs
    assert "SalesOrderHeader" in locs[0] and "SalesOrderDetail" in locs[1], \
        f"the citations do not say which table answered: {locs}"
    assert len(set(locs)) == len(locs), f"two citations are still indistinguishable: {locs}"
    assert all("database.windows.net" in x for x in locs), \
        f"the store was dropped in favour of the object - both are needed: {locs}"


def test_evidence_values_are_never_rewritten():
    """THE RULE THAT DIED, AND WHY IT IS NOT COMING BACK NARROWER.

    Grouping any value carrying a decimal point was falsified by independent review in exactly
    the shape it was written to guarantee: app_version=2024.1.0 -> 2,024.1.0, period=2024.06 ->
    2,024.06, po=PO-12345.67 -> PO-12,345.67. Every narrower version leaks too, because
    `2024.06` and `1200.50` are THE SAME SHAPE - only the column's type separates them, and the
    evidence payload does not carry it.

    This text sits in the Sources rail, whose whole job is to be checkable against the source. A
    value the reader cannot paste back into a query is a worse defect than an ugly one."""
    r = _probe()
    if r is None:
        return _skip()
    snip = r["router_source_rows"][0]["snippet"]
    assert "43962.7901" in snip, f"a measure was rewritten: {snip!r}"
    # digit-comma-three-digits is the grouping signature. Slicing a fixed width past the value
    # runs into the field separator, which is a comma too - that read the fixture, not the code.
    assert not re.search(r"\d,\d{3}", snip), f"thousands grouping is back: {snip!r}"
    assert "customer_id=29485" in snip, f"an identifier was altered: {snip!r}"


def test_a_version_string_survives_untouched():
    """The concrete falsifier. Pinned so a future "readability" pass cannot reintroduce it."""
    r = _probe("versions")
    if r is None:
        return _skip()
    snip = r["router_source_rows"][0]["snippet"]
    for token in ("app_version=2024.1.0", "period=2024.06", "po=PO-12345.67"):
        assert token in snip, f"{token!r} was corrupted: {snip!r}"


def test_a_zoned_timestamp_keeps_its_offset():
    """`2008-06-01 00:00:00+05:30` is a real instant, not a driver artefact. The first cut
    stripped the time and stranded the offset, printing `2008-06-01+05:30` - a rendering that
    states something false about the row."""
    r = _probe("versions")
    if r is None:
        return _skip()
    snip = r["router_source_rows"][1]["snippet"]
    assert "2008-06-01 00:00:00+05:30" in snip, f"a zoned instant was mangled: {snip!r}"
    assert "2008-06-01+05:30" not in snip, f"the offset was stranded: {snip!r}"
    assert "2008-06-02 00:00:00 UTC" in snip, f"a zone NAME was stripped: {snip!r}"


def test_the_snippet_is_still_escaped():
    """humanizeSnippet replaced a bare esc() call. If it ever stops escaping, model- or
    row-borne markup lands in an authenticated page.

    #689 moved it to ui/proofs.js and this guard got STRONGER on the way, rather than being
    re-pointed and left as it was. It used to assert that the literal text `esc(` appeared
    somewhere in the function body - which a rewrite that called `esc` on the wrong half of
    the string would sail straight through. It now RUNS the real function over hostile input
    from both the typed and untyped paths and asserts no markup survives, which is the
    property the page depends on and the one #786 says to assert."""
    if not _domgate.gate("the humanizeSnippet escaping check"):
        return _skip()
    script = f"""
      import {{ humanizeSnippet }} from "{PROOFS.as_uri()}";
      const hostile = 'name=<img src=x onerror=alert(1)>, price=1200.50, note="a<b>"';
      console.log(JSON.stringify({{
        typed: humanizeSnippet(hostile, {{price: "num"}}),
        untyped: humanizeSnippet(hostile, null),
      }}));
    """
    got = json.loads(subprocess.run(["node", "--input-type=module", "-e", script],
                                    capture_output=True, text=True, check=True).stdout)
    for path, out in got.items():
        assert "<img" not in out and "<b>" not in out, \
            f"humanizeSnippet let raw markup through on the {path} path: {out!r}"
        assert "&lt;img" in out, f"the {path} path did not escape at all: {out!r}"
    assert "1,200.50" in got["typed"], \
        f"the typed path stopped formatting while escaping: {got['typed']!r}"


def test_no_punctuation_is_left_stranded_after_a_marker():
    r = _probe()
    if r is None:
        return _skip()
    assert not r["stranded_punctuation"], \
        "a full stop is still floating a space clear of the sentence after a superscript"


def test_the_answer_text_itself_is_not_altered():
    """The stranded-punctuation fix is typographic. If it can move a word, a number or a
    marker, it is not a polish fix any more - it is editing the answer."""
    r = _probe()
    if r is None:
        return _skip()
    t = r["router_answer_text"]
    for token in ("Revenue for EMEA was 4.2M", "3.8M", "[1]", "[2]"):
        assert token in t, f"{token!r} did not survive the tidy: {t!r}"


# ---- #729: the placeholder business unit, on EVERY surface ---------------------------------

def test_qualify_drops_a_placeholder_and_never_prints_empty_parens():
    """The first pass fixed three surfaces and the commit message called the last of them "the
    third and last surface". That was FALSE - an independent review found three more, each with
    its own copy of the same f-string. Six copies of a rule is six chances to fix five of them,
    so the rule now lives in one function."""
    sys.path.insert(0, str(ROOT / "src"))
    from dbsearch.router.decision import qualify
    assert qualify("s3-1", "unassigned") == "s3-1"
    assert qualify("s3-1", "sales") == "s3-1 (sales)"
    for empty in ("", None, "   "):
        assert qualify("s3-1", empty) == "s3-1", f"empty parens for {empty!r}"
    # the executor's status rides in the same parentheses
    assert qualify("s3-1", "sales", "error") == "s3-1 (sales: error)"
    assert qualify("s3-1", "unassigned", "error") == "s3-1 (error)"
    assert qualify("s3-1", None, "error") == "s3-1 (error)"


def _snips(scenario):
    return {r["num"]: r["snippet"] for r in _probe(scenario)["router_source_rows"]}


def test_a_typed_measure_is_grouped_and_an_identifier_is_not():
    """#729(a). The claim is that the DECLARED TYPE, and nothing about the string, separates a
    measure from an identifier - so this asserts both halves of the same snippet at once.

    `price` and `TotalDue` are decimal columns and become readable. `customer_id` is an integer
    column and must not move: no type tells a count from an identifier, which is why only
    FRACTIONAL types are classed "num", and `29,485` is a value the reader cannot paste back
    into a query."""
    if _probe("typed") is None:
        return _skip()
    s = _snips("typed")["[1]"]
    assert "price=1,200.50" in s, f"a decimal column was not made readable: {s!r}"
    assert "TotalDue=43,962.7901" in s, f"the measure was not grouped: {s!r}"
    assert "customer_id=29485" in s, \
        f"an identifier was grouped - the #729 falsification, from the other direction: {s!r}"


def test_no_currency_is_invented_and_no_digit_is_lost():
    """The rail exists to be checkable against the source. Grouping is a separator; a currency
    symbol would be a CLAIM, and rounding 43962.7901 to two places would be a different number.
    Neither is in the payload and neither is ours to add."""
    if _probe("typed") is None:
        return _skip()
    s = _snips("typed")["[1]"]
    for invented in ("$", "€", "£", "USD"):
        assert invented not in s, f"a currency the payload never carried: {s!r}"
    assert "43,962.79," not in s and "43,962.79 " not in s, \
        f"the measure was rounded rather than grouped: {s!r}"


def test_the_falsifiers_still_survive_when_their_column_has_no_type():
    """`period=2024.06` and `price=1200.50` are the same string shape - that is the whole
    reason the first cut was removed. Here they sit in ONE snippet, and only the type differs.

    `app_version` and `po` carry no entry at all (text columns), and `total` is an alias the
    schema cannot resolve. All three must arrive exactly as the database returned them."""
    if _probe("typed") is None:
        return _skip()
    s = _snips("typed")["[1]"]
    assert "app_version=2024.1.0" in s, f"a version string was grouped: {s!r}"
    assert "po=PO-12345.67" in s, f"a purchase order was grouped: {s!r}"
    assert "period=2024.06" in s, \
        f"a value the same SHAPE as the measure beside it was grouped: {s!r}"
    assert "total=98765.4" in s, \
        f"an alias the schema cannot resolve was formatted on a guess: {s!r}"


def test_a_date_loses_its_midnight_and_a_timestamp_keeps_it():
    """The correctness fix the blind rule could not make. Both values are byte-identical apart
    from the column they sit in: on a DATE column the `00:00:00` is a driver artefact, on a
    TIMESTAMP it is the instant the row records - and the old shape-based rule deleted both."""
    if _probe("typed") is None:
        return _skip()
    s = _snips("typed")["[2]"]
    assert "booked=2008-06-03" in s and "booked=2008-06-03 00:00:00" not in s, \
        f"a date column kept a midnight it cannot have: {s!r}"
    assert "seen_at=2008-06-04 00:00:00" in s, \
        f"a real midnight instant was deleted from a timestamp column: {s!r}"


def test_an_untyped_store_renders_exactly_as_before():
    """The fallback, asserted as a live state rather than assumed. `versions` is the same
    evidence with NO column_types - every document citation and every store whose schema cannot
    be resolved takes this path, and it must be the behaviour that shipped."""
    if _probe("versions") is None:
        return _skip()
    s = " ".join(_snips("versions").values())
    for untouched in ("app_version=2024.1.0", "period=2024.06", "po=PO-12345.67",
                      "TotalDue=43962.7901"):
        assert untouched in s, f"an untyped value was rewritten on a guess: {s!r}"


def test_a_compound_ask_does_not_open_with_telemetry_either():
    """#729(b). The twin of `test_the_telemetry_is_no_longer_the_first_thing_on_screen`, and
    the reason that test was not enough.

    It asserts on the `answers` scenario, and every fixture in the probe carried
    `sub_queries: []` - so it only ever measured the SIMPLE ask. A compound ask rendered N
    "↳ <sub-question> → store (analytical)" lines above the answer: same store ids, same
    query_type vocabulary, same muted mono style as the line #729 moved. The rule held for the
    easy shape of question and failed for the hard one, and both live verification asks were
    simple, which is exactly why it survived wave 1."""
    r = _probe("compound")
    if r is None:
        return _skip()
    assert not r["sub_lines_before_answer"], (
        "a compound ask still opens with per-sub-question routing lines: "
        f"{r['sub_lines_before_answer']!r}")
    first = r["first_block_text"] or ""
    for jargon in ("↳", "azure_sql-1", "analytical", "lookup"):
        assert jargon not in first, \
            f"the compound answer still opens with routing telemetry ({jargon!r}): {first!r}"
    assert "EMEA" in first, f"the first thing on screen should be the answer: {first!r}"


def test_the_sub_questions_are_kept_not_deleted():
    """The same guard `test_the_telemetry_is_kept_not_deleted` provides for the line above it:
    deleting the sub-question block would pass the test above and lose real provenance. How a
    question was BROKEN UP is the one thing a compound trace has to say - including the
    sub-question that reached no store at all, which the answer itself only hints at."""
    r = _probe("compound")
    if r is None:
        return _skip()
    lines = r["sub_lines_in_trace"]
    assert len(lines) == 2, f"the sub-question lines were dropped rather than moved: {lines!r}"
    joined = " ".join(lines)
    assert "what did EMEA bill?" in joined and "what is the Krakow headcount?" in joined, joined
    assert "azure_sql-1" in joined, f"the sub-question lost its target store: {joined!r}"
    assert "no accessible source" in joined, \
        f"the sub-question that reached no store is not on screen: {joined!r}"


def test_the_reader_is_told_their_question_was_split():
    """Folding the sub-questions away costs the reader the fact that folding happened - and a
    reader who does not know their question became two has no reason to open the trace and
    find out. The summary carries it, and only for a genuinely compound ask."""
    r = _probe("compound")
    if r is None:
        return _skip()
    s = r["trace_summary"] or ""
    assert "2 questions" in s, f"the summary does not say the ask was split: {s!r}"
    simple = _probe("answers")["trace_summary"] or ""
    assert "question" not in simple, \
        f"a simple ask is described as though it had been split: {simple!r}"


def test_the_summary_uses_one_separator_not_two():
    """#765. `nsub` joined with a middot and `cnt` joined with a comma, so one parenthetical
    carried two separators doing the same job three words apart: "(2 questions · 2 sources, 4
    citations from your sources)". Mine, from #729(b) - I added the prefix and matched neither
    the segment style beside it nor the comma already there.

    Checked on BOTH shapes, because the compound path is the only one where the two separators
    ever met and a simple ask would pass this vacuously."""
    import re as _re
    for scenario in ("answers", "compound", "compound_covered"):
        if _probe(scenario) is None:
            return _skip()
        s = _probe(scenario)["trace_summary"] or ""
        inner = _re.search(r"\((.*)\)", s)
        assert inner, f"{scenario}: the summary has no counted parenthetical at all: {s!r}"
        assert "," not in inner.group(1), \
            f"{scenario}: the summary mixes a comma with the middot beside it: {s!r}"


def test_the_summary_does_not_call_a_folder_a_database():
    """Verifier finding D4, seen live. The citation count carried a hardcoded "from your
    databases", and on a compound ask that sentence sat four lines above a card reading
    "Folder · folder-1 · leave-policy.txt".

    This is #728's defect in a different place: a label the evidence directly beneath it
    contradicts, on the one surface whose job is to be checkable. The noun is read off the
    citation kinds now - and the all-SQL case must keep saying "databases", or the fix has
    just traded a wrong word for a vague one."""
    if _probe("compound") is None:
        return _skip()
    mixed = _probe("compound")["trace_summary"] or ""
    assert "from your databases" not in mixed, \
        f"a document citation is described as coming from your databases: {mixed!r}"
    assert "from your sources" in mixed, \
        f"a mixed rail should name neither kind exclusively: {mixed!r}"
    # ...and the precision is not thrown away where it IS true.
    sql_only = _probe("answers")["trace_summary"] or ""
    assert "from your databases" in sql_only, \
        f"an all-database rail went vague rather than staying accurate: {sql_only!r}"


def _rules(css):
    """(selector, body) for every rule in a stylesheet, with AT-RULE WRAPPERS REPORTED.

    Returns (selector, body, media) where `media` is the enclosing at-rule prelude or "" for a
    top-level rule. Crude, and deliberately so - but a real split beats the nested negated
    character classes this used to be (#760), where `[^{]*` silently swallowed the rest of a
    selector and made a rule that targets NOTHING look like a rule that targets the glyph.

    #787: the previous version was a flat `([^{}]+)\{([^{}]*)\}` scan, whose body group cannot
    cross an inner brace - so for `@media (min-width:1px){ .tri {display:inline} }` it skipped the
    at-rule and matched only the INNER rule, handing it to the guard as though it were top level.
    The prelude vanished. Combined with a guard that used `any(...)`, a `display:inline` hidden
    inside a media query could win the real cascade while an untouched `display:inline-block`
    elsewhere kept the assertion green - #745 round one, reinstated, invisibly.

    The caller has to DECIDE what to do about a media-wrapped rule; it must not be handed one
    that looks unconditional. canvas.css already contains four @media blocks, so this is the
    file's normal shape, not a hypothetical."""
    out, media, i = [], "", 0
    while i < len(css):
        brace = css.find("{", i)
        if brace == -1:
            break
        head = css[i:brace].strip().split("}")[-1].strip()
        if head.startswith("@"):
            media = head                      # entering an at-rule block
            i = brace + 1
            continue
        close = css.find("}", brace)
        if close == -1:
            break
        out.append((head, css[brace + 1:close], media))
        i = close + 1
        # leaving the at-rule block: the next `}` with no intervening `{` closes it
        nxt_open, nxt_close = css.find("{", i), css.find("}", i)
        if media and nxt_close != -1 and (nxt_open == -1 or nxt_close < nxt_open):
            media = ""
            i = nxt_close + 1
    return out


def _transform_moves(value):
    """Does this `transform` value actually move the element? Shared by the static and the
    computed guard on purpose (#760): `rotate(0deg)` and `rotate(360deg)` are both a CHANGE to
    the declaration and both a no-op on screen, and a guard that only notices the change is the
    defect that card exists to remove.

    #787: the periodicity rule used to be gated on the literal string "deg" being in the
    argument, so THREE no-ops were judged as movement and could be shipped past both guards:

        rotate(1turn)            360 degrees. `turn` is not "deg", so it took the `n != 0` arm.
        rotate(400grad)          same, via a unit nobody writes but the parser accepts.
        rotate(90deg) rotate(-90deg)   net zero, but `moved |=` OR-ed the two independently.
        rotate3d(0,0,1,0deg)     the AXIS literal 1 was read as if it were the angle.

    Because the helper is shared, one bug defeated the static and the computed guard at once -
    which is the argument for sharing it and also the reason it has to be right. Rotations are
    now converted to degrees by UNIT and SUMMED, so composition is judged the way a browser
    composes it, and only the angle argument of rotate3d is read."""
    import math
    import re as _re

    units = {"deg": 1.0, "grad": 0.9, "rad": 180.0 / math.pi, "turn": 360.0, "": 1.0}
    NUM = r"(-?\d+(?:\.\d+)?)(deg|grad|rad|turn)?"
    rotation = 0.0
    moved = False
    for fn, arg in _re.findall(r"(\w+)\(([^)]*)\)", value or ""):
        parts = _re.findall(NUM, arg)
        if not parts:
            continue
        if fn.startswith("rotate"):
            if fn == "rotate3d":
                # rotate3d(x, y, z, angle): only the LAST argument is an angle, and a zero axis
                # is a no-op whatever the angle says.
                if len(parts) < 4 or all(float(n) == 0 for n, _u in parts[:3]):
                    continue
                parts = parts[-1:]
            for n, u in parts:
                rotation += float(n) * units.get(u, 1.0)
        elif fn.startswith("scale"):
            moved |= any(float(n) != 1 for n, _u in parts)
        else:
            moved |= any(float(n) != 0 for n, _u in parts)
    # A rotation is periodic; a translation or a scale is not. Compared with a TOLERANCE rather
    # than exactly: `6.28318rad` is a truncated 2*pi and lands 0.00002 degrees off a full turn,
    # which is float noise, not motion. 0.01 degrees is far below anything a reader could see
    # and far above the error in any sane unit conversion.
    net = abs((rotation % 360 + 180) % 360 - 180)
    return moved or net > 0.01


def _own_marker_disclosures():
    """Every `<details class=X>` in canvas.js whose summary draws a marker glyph of its own,
    as (details class, glyph element class). A `<details>` that leaves the marker to the
    browser is fine and must not be made to fail; what must never happen is BOTH."""
    import re as _re
    found = []
    for m in _re.finditer(r"<details class=\\?\"([\w-]+)\\?\"><summary>(.{0,80})",
                          CANVAS.read_text()):
        cls, head = m.group(1), m.group(2)
        if not any(g in head for g in "▸▾▶►▼"):
            continue
        g = _re.search(r"<span class=\\?\"([\w-]+)\\?\"[^>]*>\s*[▸▾▶►▼]", head)
        found.append((cls, g.group(1) if g else None))
    return found


def test_a_summary_that_draws_its_own_marker_suppresses_the_native_one():
    """#745 round 1: the summary carries its own "▸" and the browser was drawing its native
    marker beside it. Two triangles for one control.

    Grep-shaped because a pseudo-element rule is the one half of this no cascade can answer:
    jsdom does not implement `::-webkit-details-marker`, so its presence can only be read off
    the source. The half that IS answerable - does anything actually turn - moved to
    `test_the_own_marker_actually_turns_when_the_disclosure_opens`, because asserting it here
    is what shipped #745's defect a second time (#760)."""
    css = CANVAS_CSS.read_text()
    import re as _re
    seen = 0
    for cls, _glyph in _own_marker_disclosures():
        seen += 1
        assert _re.search(rf"\.{cls} summary\s*{{[^}}]*list-style\s*:\s*none", css), \
            f".{cls}'s summary draws its own marker and does not set list-style:none - " \
            "the browser paints its marker too"
        # #782: this was `f"...::-webkit-details-marker" in css` - raw substring containment,
        # with no reference to the declaration BODY. Flip `display:none` to `display:block` and
        # the substring is untouched, so the native marker comes back on Safari and older
        # Chromium - #745 round one, reinstated, with all three guards green. Its own sibling
        # three lines up already parsed the body and pinned the value; this now does the same.
        # It stays a source PARSE (not a presence check) because jsdom cannot compute a
        # ::-webkit- pseudo-element, which is the honest limit here.
        assert _re.search(
            rf"\.{cls} summary::-webkit-details-marker\s*{{[^}}]*display\s*:\s*none", css), \
            f".{cls} needs a ::-webkit-details-marker rule that actually SUPPRESSES the native " \
            "marker - Safari and older Chromium ignore list-style on a summary, so a rule that " \
            "is merely present, or sets any other display, paints a second triangle"
    assert seen, "the guard matched no summary at all - it has stopped testing anything"


def test_the_open_state_rule_targets_the_glyph_and_turns_it_by_a_real_angle():
    """#760, the static half. My round-2 guard for #745 asserted the WORD `transform` appears
    somewhere inside an `[open]` rule. Three mutations passed it while the arrow did not move:
    dropping `display:inline-block` (a transform is a no-op on a non-replaced inline element),
    changing `rotate(90deg)` to `rotate(0deg)`, and pointing the `[open]` rule at a class that
    does not exist. The first reproduces #745's ROUND-ONE defect exactly.

    So the three things that make a rotation real are asserted separately, against the class the
    JS actually emits rather than against the one I remember writing:
      1. the `[open]` selector ends at the glyph element,
      2. its transform carries a non-zero number,
      3. the glyph's base rule gives it a display a transform can apply to."""
    import re as _re
    css = CANVAS_CSS.read_text()
    rules = _rules(css)
    # #787: a rule wrapped in an at-rule is NOT unconditional, and this guard cannot reason
    # about when it applies. Refuse it loudly rather than judging it as if it were top level -
    # the old flat scan dropped the prelude silently, which let a `display:inline` hidden in a
    # media query win the real cascade while an untouched `display:inline-block` elsewhere kept
    # the assertion green. Reporting "I cannot evaluate this" is the honest outcome; passing is
    # not. jsdom cannot help here either: its evaluateMediaList matches only a query that is
    # literally `all` or `screen`, so the computed guard silently drops feature-bearing blocks.
    wrapped = [(m, sel) for sel, _body, m in rules if m
               and any(f".{c}" in sel for c, _g in _own_marker_disclosures())]
    assert not wrapped, (
        "a disclosure-marker rule is wrapped in an at-rule this guard cannot evaluate, so it "
        "cannot say whether the marker turns. Move it out, or teach the guard the condition:\n"
        + "\n".join(f"  {m} {{ {sel} ... }}" for m, sel in wrapped))
    rules = [(sel, body) for sel, body, _m in rules]
    TRANSFORMABLE = ("inline-block", "block", "flex", "inline-flex", "grid", "inline-grid",
                     "table", "inline-table", "list-item")
    seen = 0
    for cls, glyph in _own_marker_disclosures():
        seen += 1
        assert glyph, \
            f".{cls}'s summary draws its own marker but not in an element of its own, so " \
            "nothing can be rotated without turning the whole summary line"

        # 1 + 2. The rule that fires on open must SELECT the glyph, not merely mention the class
        # somewhere to its left, and must turn it by an angle that is not zero.
        opened = [(sel, body) for sel, body in rules
                  if f".{cls}[open]" in sel and _re.search(rf"\.{glyph}\s*$", sel)
                  and "transform" in body]
        assert opened, \
            f".{cls} suppresses the native marker but no [open] rule TARGETS .{glyph} with a " \
            f"transform, so nothing turns when it opens - the disclosure has no open/closed " \
            f"indicator. Rules seen for .{cls}[open]: " \
            + repr([s for s, _ in rules if f".{cls}[open]" in s])
        for sel, body in opened:
            val = _re.search(r"transform\s*:([^;}]*)", body).group(1)
            assert _transform_moves(val), \
                f"`{sel}` declares `transform:{val.strip()}` - a transform that moves nothing. " \
                "The disclosure looks identical open and closed."

        # 3. And the transform must have something to apply to. CSS Transforms: a non-replaced
        # INLINE element is not transformable, so `display` on the glyph's own rule is
        # load-bearing - deleting it is invisible to every check above.
        base = [body for sel, body in rules
                if _re.search(rf"\.{cls}\b(?![^,{{]*\[open\])[^,{{]*\.{glyph}\s*$", sel)
                and "display" in body]
        assert base, \
            f".{glyph} has no base rule declaring a display, so it stays inline and the " \
            f"rotation on .{cls}[open] is a no-op - which is exactly how #745 shipped twice"
        assert any(any(d in _re.search(r"display\s*:([^;}]*)", b).group(1) for d in TRANSFORMABLE)
                   for b in base), \
            f".{glyph} is not given a transformable display ({TRANSFORMABLE}); a transform on " \
            "a non-replaced inline element does nothing"
    assert seen, "the guard matched no summary at all - it has stopped testing anything"


def test_the_own_marker_actually_turns_when_the_disclosure_opens():
    """#760, the load-bearing half. The static guard above encodes what I currently believe makes
    a rotation real; this one asks the cascade, on the REAL rendered element, and so can fail for
    a reason I did not think of - which is the whole category of miss that #745/#760 belong to.

    jsdom gives no layout, so this still cannot say "the arrow is painted turned". What it CAN
    say, and what every previous version of this guard could not, is that the open-state rule
    resolves onto the element the canvas actually rendered, that the computed transform CHANGES
    between closed and open, and that the glyph's computed display is one a transform applies
    to."""
    if _probe("answers", with_css=True) is None:
        return _skip()
    r = _probe("answers", with_css=True)
    assert r["css_loaded"], "the probe ran without the stylesheet - it is measuring nothing"
    TRANSFORMABLE = ("inline-block", "block", "flex", "inline-flex", "grid", "inline-grid",
                     "table", "inline-table", "list-item")
    own = [d for d in r["disclosures"] if d["own_glyph"]]
    assert own, "no rendered <details> draws a marker of its own - the guard tests nothing"
    for d in own:
        closed, opened = d["glyph_closed"], d["glyph_open"]
        assert opened["transform"] != "none", \
            f".{d['details_class']}'s own marker ({d['glyph_text']!r}) has NO computed " \
            "transform once the disclosure opens. Either no [open] rule exists or it selects " \
            f"an element that is not this one. Glyph class: {d['glyph_class']!r}"
        assert opened["transform"] != closed["transform"], \
            f".{d['details_class']}'s marker computes the same transform open and closed " \
            f"({opened['transform']!r}) - opening the disclosure changes nothing on screen"
        # ...and the change is not `rotate(0deg)`. A guard that only asks whether the computed
        # value CHANGED accepts a rotation to nowhere - which is #745's round-two defect written
        # against computed style instead of against source.
        assert _transform_moves(opened["transform"]), \
            f".{d['details_class']}'s marker computes `transform:{opened['transform']}` when " \
            "open - a transform that moves nothing. The arrow does not turn"
        assert opened["display"] in TRANSFORMABLE, \
            f".{d['details_class']}'s marker computes `display:{opened['display']}`, which a " \
            "transform cannot apply to (CSS Transforms: a non-replaced inline element is not " \
            "transformable). The rotation is declared and does nothing - #745's round-one defect"
        # And the suppression half, now measured rather than grepped.
        assert d["summary_style"]["listStyle"] == "none", \
            f".{d['details_class']}'s summary computes `list-style:" \
            f"{d['summary_style']['listStyle']}` beside its own marker - two triangles"


def test_every_footnote_fixture_carries_the_origin_the_server_would_have_built():
    """#761. All fourteen footnote fixtures in the DOM probe set `origin` to
    `system · business_unit`. The server has only ever emitted `system · location · object`
    (`router_api._origin_str`). So every guard in this file that reads the outcome-row label was
    reading a shape production does not produce - and the visible consequence was sitting in the
    probe output I shipped: the canvas takes the first two origin segments as the store label and
    then appends the business unit, so `Azure SQL · finance · finance · ok · 2 results` printed
    the business unit TWICE and no assertion noticed.

    I had already written the rule this breaks and applied it selectively: for #751 I insisted the
    answer fixture be the production string byte-for-byte, because a fixture I invent encodes what
    I THINK the server emits - and in the same commits I invented fourteen origins.

    Checked against the server function rather than against a literal, so the fixtures cannot
    drift again and cannot be "corrected" to a second thing I merely believe."""
    if _fixtures() is None:
        return _skip()
    sys.path.insert(0, str(ROOT / "src"))
    from dbsearch.server.router_api import _origin_str
    bad = []
    for f in _fixtures():
        want = _origin_str({"system": f.get("system", ""), "location": f.get("location", "")},
                           f.get("object", ""),
                           f.get("business_unit") or f["store_id"])
        if f["origin"] != want:
            bad.append(f"  {f['fixture']} n={f['n']}: fixture {f['origin']!r} "
                       f"but _origin_str would build {want!r}")
    assert not bad, ("a DOM fixture encodes an `origin` the server never emits, so every "
                     "assertion reading it is reading a shape prod does not produce:\n"
                     + "\n".join(bad))


def test_the_origin_fallback_is_asserted_against_a_literal_not_against_itself():
    """#789. The guard above is the right shape and has one blind spot: it derives `want` from
    `_origin_str` and compares it to a fixture, so any behaviour BOTH sides share is invisible.
    Every one of the seventeen footnote fixtures supplies a non-empty `system` AND `location`, so
    `parts` is never empty and the `else fallback` branch is never evaluated on either side of the
    comparison. Delete the fallback and nothing goes red.

    It is not decoration. `canvas.js` reads `f.system || f.origin.split(" · ")[0] || "Source"`, so
    a footnote whose origin collapses to "" renders the Sources card's system slot as the literal
    word "Source" - a label that looks deliberate and names nothing.

    Asserted against LITERAL strings, which is the only way to test a function against itself."""
    sys.path.insert(0, str(ROOT / "src"))
    from dbsearch.server.router_api import _origin_str
    # the shape every fixture has: system + location + object
    assert _origin_str({"system": "Azure SQL", "location": "srv / sales"}, "table Billing",
                       "finance") == "Azure SQL \u00b7 srv / sales \u00b7 table Billing"
    # the branch NO fixture reaches: nothing to build from, so the fallback must carry it
    assert _origin_str({}, "", "azure_sql-1") == "azure_sql-1", \
        "a footnote with no system, location or object lost its fallback - the Sources card " \
        "then renders the literal word 'Source' as the system name"
    assert _origin_str({"system": "", "location": ""}, "", "finance") == "finance"
    # and a partial: present values are used, absent ones are not padded with separators
    assert _origin_str({"system": "Folder", "location": ""}, "", "folder-1") == "Folder", \
        "an absent segment left a dangling separator or an empty slot"


def test_an_outcome_row_never_prints_the_same_segment_twice():
    """#761's visible symptom, and the assertion whose absence let it live. The outcome row is
    built from two sources - the store label sliced out of `origin`, and the business unit
    appended after it - and nothing checked that those two do not overlap.

    This is a real-render check on purpose: the duplication is not visible in either input, only
    in the string they compose into.

    #784: THIS GUARD WENT DEAD AND IS NOW RE-POINTED. It was load-bearing when written - the
    label was two concatenated strings, `lbl` + `obu`, so a business unit matching an origin
    segment rendered "... finance · finance ...". Six commits later #753 round 2 rebuilt the
    label as ONE array with a first-occurrence-wins filter, so the duplicate is never COMPOSED
    and no input could make this go red: product code shielding a guard from its own defect. The
    dedup is a genuine improvement and stays. What is asserted now is the DEDUP itself, driven by
    `colliding_label`, whose business_unit is deliberately also its second origin segment - the
    exact shape #761 rendered wrongly. Without that fixture the other four scenarios have no
    overlapping segments and this whole function is decoration."""
    for scenario in ("answers", "compound", "compound_covered", "compound_same_store",
                     "colliding_label"):
        if _probe(scenario) is None:
            return _skip()
        for row in _probe(scenario)["outcome_rows"]:
            # The row's own separator. A sub-question quote is appended after " · ↳ " and is
            # free to repeat anything, so the label is everything before it.
            label = row.split(" · ↳ ")[0]
            seg = [s.strip() for s in label.split("·") if s.strip()]
            dupes = [s for s in set(seg) if seg.count(s) > 1]
            assert not dupes, \
                f"the {scenario} outcome row prints {dupes} more than once: {row!r}"


def test_an_outcome_row_names_the_store_it_reports_on():
    """#753 round 2. My own #753 commit dropped the sub-question quote from the outcome rows on
    the stated ground that "the store name is enough to join the two". It was not. The ↳ lines
    above print `store_id`; the row below printed `origin`'s first two segments, which is
    `system · location` - a name for a human, not the id. On a compound ask `azure_sql-1`
    appeared nowhere in its own row.

    #783: a sentence here claimed the quote "was dropped exactly where the join failed and kept
    where it worked." That is FALSE, and correcting it is worth more than deleting it. There was
    never a mechanism: the old predicate read `subs` and `perStore` only and never consulted the
    footnote, so quote-retention and label-usefulness were INDEPENDENT. Worked through the two
    compound fixtures they land OPPOSITE - compound_same_store kept the quote while its label
    carried no store_id (join failed), and compound_covered's folder-1 dropped it while its label
    did carry the id (join worked). The fallback case the sentence invoked, a store with no
    footnote, is in no fixture at all.

    Checked against the fixture's own outcome list rather than by pattern, so the row must name
    the store it REPORTS ON and not merely some store."""
    for scenario in ("answers", "compound", "compound_covered", "compound_same_store"):
        if _probe(scenario) is None:
            return _skip()
        r = _probe(scenario)
        rows, ids = r["outcome_rows"], r["outcome_store_ids"]
        assert len(rows) == len(ids), \
            f"{scenario}: {len(ids)} outcomes but {len(rows)} rows rendered: {rows}"
        for store_id, row in zip(ids, rows):
            assert store_id in row, \
                f"{scenario}: the outcome row for {store_id!r} does not name it, so the " \
                f'"↳ question → {store_id}" line above cannot be joined to it: {row!r}'


def test_the_route_advisor_maps_each_sub_question_to_its_store():
    """#759. #753's server half removed the sub-question enumeration from `reason`, and my commit
    claimed there were two consumers of it. There are three: `routeLive`, the Route advisor
    behind the Route button, renders `reason` and never touched `sub_queries`, so a compound
    question went from

        "decomposed into 2 sub-queries: 'parental leave' → hr-wiki; 'revenue' → fin-ledger"
    to
        "decomposed into 2 sub-queries"

    with the question→store mapping unrecoverable - its candidate rows being the UNION across
    sub-questions, carrying no mapping of their own. I did not notice because I grepped for
    consumers of `reason` only in the two places I was already looking.

    A different button and a different endpoint, so no ask-driven test in this file could have
    caught it. The probe now clicks #qroute."""
    if _probe("route_compound") is None:
        return _skip()
    r = _probe("route_compound")
    lines = r["route_sub_lines"]
    assert r["route_header"], "the Route advisor rendered no header - the panel is not up"
    assert len(lines) == 2, \
        f"the Route advisor lost the per-sub-question mapping: {lines}"
    joined = " ".join(lines)
    for q in ("what did EMEA bill?", "what is the Krakow headcount?"):
        assert q in joined, f"the advisor does not name the sub-question {q!r}: {lines}"
    assert "azure_sql-1" in joined, \
        f"the advisor names no store for the sub-question it routed: {lines}"
    assert "no accessible source" in joined, \
        "the advisor does not distinguish a sub-question with no reachable store from one " \
        f"that routed - the distinction the structured field exists to carry: {lines}"


def test_no_surface_still_builds_the_label_by_hand():
    """The regression guard that matters more than the unit test above: a NEW call site that
    formats it inline is invisible to every behavioural test until a user with an unassigned
    store hits that exact path - which is precisely how these three survived."""
    import re as _re
    bad = []
    # decision.py is where qualify() itself lives - its own return statement is the rule, not
    # a copy of it.
    for rel in ("src/dbsearch/router/router_service.py", "src/dbsearch/router/synthesizer.py"):
        for i, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
            if "qualify(" in line or line.lstrip().startswith(("#", "*")):
                continue
            if _re.search(r"\{[\w.]*store_id\}\s*\(\{", line) or \
               _re.search(r"business_unit\}\)", line):
                bad.append(f"{rel}:{i}: {line.strip()}")
    assert not bad, "a surface formats the business unit by hand instead of calling qualify():\n" \
                    + "\n".join(bad)



# ---- #751: the model writes structure; the surface must render it ---------------------------

def test_the_models_list_becomes_a_list():
    """Measured on prod before this was written: a 200-character compound answer carried five
    newlines, one blank line and three bullet lines, and `fmtAnswer` discarded every one - so it
    rendered as "Freight costs by region are: - EMEA: ... - APAC: ..." on one run-on line. The
    fixture is that real answer, byte for byte, because a fixture I invent encodes what I think
    the model emits rather than what it does."""
    r = _probe("structured")
    if r is None:
        return _skip()
    assert r["answer_blocks"] == ["p", "p", "ul"], \
        f"the model's paragraphs and list did not survive: {r['answer_blocks']}"
    items = r["answer_list_items"]
    assert len(items) == 3, f"the three bullet lines did not become three items: {items}"
    assert any("EMEA" in i for i in items) and any("AMER" in i for i in items), items


def test_the_bullet_character_is_not_printed_twice():
    """The dash is a LIST MARKER, not content. Leaving it in prints it beside the one the <li>
    supplies - the classic way a markdown renderer betrays itself."""
    r = _probe("structured")
    if r is None:
        return _skip()
    for item in r["answer_list_items"]:
        assert not item.lstrip().startswith(("-", "\u2013", "\u2014", "*", "\u2022")), \
            f"a list item still carries its own bullet: {item!r}"


def test_model_text_still_cannot_become_an_element():
    """The injection argument, re-asserted BECAUSE fmtAnswer now emits block tags. esc() runs
    first and this pass only ever adds a fixed whitelist, so a <script> in the model's answer has
    to arrive as visible text. The fixture splices one in for exactly this reason."""
    r = _probe("structured")
    if r is None:
        return _skip()
    assert r["answer_has_element_from_model"] is False, \
        "model output became a live element - esc() is no longer the first pass"
    assert "<script>" in (r["router_answer_text"] or ""), \
        "the probe's injection string vanished, so this test is no longer testing anything"


def test_an_answer_with_no_structure_is_left_exactly_as_it_was():
    """The early return. Every other fixture in the probe is a single line, and none of them may
    gain a paragraph wrapper - a change that reflowed answers which had nothing to reflow would
    be a regression dressed as a feature."""
    r = _probe("answers")
    if r is None:
        return _skip()
    # `answer_blocks` is every element child, and a single-line answer legitimately has one
    # <sup> per citation marker. What must be absent is BLOCK structure it never asked for.
    blocks = [t for t in r["answer_blocks"] if t in ("p", "ul", "ol", "li", "br")]
    assert blocks == [], \
        f"a single-line answer was wrapped in block tags: {r['answer_blocks']}"
    assert "sup" in r["answer_blocks"], \
        f"the citation markers vanished from the answer: {r['answer_blocks']}"


def test_a_trailing_newline_is_not_structure():
    """#751 round 2, bug 1. The test above passes because every fixture it reads happens to have
    no trailing newline. `_blockify`'s early return - the thing that makes "an answer with no
    structure is left exactly as it was" TRUE - was applied to the raw string, so a single
    trailing "\\n", which models emit constantly, counted as structure and wrapped a plain
    sentence in a <p> it never had.

    The claim in that comment was mine and it was false for the commonest string in the corpus."""
    r = _probe("trailing_newline")
    if r is None:
        return _skip()
    blocks = [t for t in r["answer_blocks"] if t in ("p", "ul", "ol", "li", "br")]
    assert blocks == [], \
        f"an answer whose only newline is a TRAILING one was given block structure: " \
        f"{r['answer_blocks']} / {r['answer_html']!r}"
    assert "sup" in r["answer_blocks"], \
        f"the citation marker vanished from the answer: {r['answer_blocks']}"


def test_a_wrapped_bullet_stays_in_its_own_list_item():
    """#751 round 2, bug 2. A bullet whose text runs long is written by the model as an INDENTED
    continuation line. `_blockify` trimmed every line before looking at it, which destroyed the
    only signal distinguishing that continuation from a new paragraph - so it flushed the list,
    emitted the continuation as body text, and opened a SECOND list for the next bullet.

    One three-item list rendered as two lists with a stray paragraph wedged between them."""
    r = _probe("structured_edge")
    if r is None:
        return _skip()
    lists = [t for t in r["answer_blocks"] if t == "ul"]
    assert len(lists) == 1, \
        f"a wrapped bullet split one list into {len(lists)}: {r['answer_blocks']} / " \
        f"{r['answer_html']!r}"
    items = r["answer_list_items"]
    assert len(items) == 2, f"the list lost or gained items: {items}"
    # #789: this was a SUBSTRING test, so dropping the joining space in
    # `items[items.length-1]+=" "+t` fused the two into "43 962.79including the fuel surcharge"
    # and the assertion still passed - the words were all present, in order, unreadable. Assert
    # the exact string a reader would see.
    assert items[0] == "EMEA: 43 962.79 including the fuel surcharge", \
        f"the continuation did not join cleanly onto the bullet it continues: {items[0]!r}"


def test_a_paragraph_break_with_a_space_in_it_still_breaks():
    """#788(e). Models emit "\\n \\n" constantly - a blank line that is not quite blank. Every
    fixture in this file used a bare "\\n\\n", so both rules that tolerate the space
    (`html.split(/\\n\\s*\\n/)` and the emphasis rule's `(?!\\n\\s*\\n)`) could be narrowed to
    `\\n\\n` with nothing going red - while a real answer stopped splitting into paragraphs at
    all and ran together on screen."""
    r = _probe("loose_structure")
    if r is None:
        return _skip()
    blocks = r["answer_blocks"]
    assert blocks.count("p") == 2, \
        f'"\\n \\n" stopped being a paragraph break, so the answer ran together: ' \
        f'{blocks} / {r["answer_html"]!r}'
    html = r["answer_html"] or ""
    assert "<strong>Q2 figures</strong>" in html, \
        f"emphasis was lost or leaked across the paragraph break: {html!r}"


def test_an_indented_line_with_no_list_open_is_not_swallowed():
    """#788(f), and it is CONTENT LOSS rather than a layout complaint.

    The continuation arm is guarded by `items.length &&`. Every continuation fixture followed a
    bullet, so that guard was unreachable and could be deleted. Without it an indented line with
    no list open does `items[items.length-1] += ...` on an EMPTY array - writing a "-1" property
    that `length` never sees - so `flushList()` emits nothing and the line VANISHES from the
    answer entirely. No error, no empty element, just absent text."""
    r = _probe("loose_structure")
    if r is None:
        return _skip()
    html = r["answer_html"] or ""
    assert "this indented line has no list open and must still reach the reader" in html, \
        f"an indented line with no list open disappeared from the answer: {html!r}"


def test_a_hard_line_break_inside_a_paragraph_survives():
    """#789. `grep -rn '<br>' tests/` returned ZERO across the whole suite: nothing anywhere
    mentioned the element, so `buf.join("<br>")` could become `buf.join(" ")` and every guard
    stayed green while a multi-line paragraph collapsed into one run-on line.

    It is structurally invisible to the usual reads: the probe reports `answer_blocks` as ELEMENT
    CHILDREN only, and `<br>` is a leaf inside a <p>, so the block list does not change. This
    asserts against `answer_html`, which the probe already captures."""
    r = _probe("numbers_in_prose")
    if r is None:
        return _skip()
    html = r["answer_html"] or ""
    assert "<br>" in html, \
        f"a hard line break inside a paragraph was dropped, fusing separate lines: {html!r}"
    assert "2008. Revenue was 4.2M" in html and "2009. Revenue was 5.1M" in html, \
        f"the two lines did not survive as their own lines: {html!r}"
    assert "4.2M2009." not in html.replace(" ", ""), \
        f"two lines were fused into one run-on line: {html!r}"


def test_emphasis_does_not_cross_a_paragraph_break():
    """#751 round 2, bug 3. `**([^*]+)**` crossed newlines, so `**a\\n\\nb**` bolded across a
    paragraph break - and `_blockify` then split those paragraphs, emitting
    `<p><strong>a</p><p>b</strong></p>`: unbalanced markup assembled out of model text.

    THIS IS WHY THE ASSERTION IS ON `<strong>` COUNT AND NOT ON THE MARKUP. An HTML parser repairs
    unbalanced tags before any DOM read can see them - jsdom turned the above into two well-formed
    <strong> elements - so reading the DOM back could never show the defect that produced it. What
    IS observable is the count: one `**…**` pair produced TWO <strong> elements, and now produces
    none, because emphasis stops at a blank line as it does in CommonMark. The asterisks stay
    literal, which is honest about what the model actually wrote."""
    r = _probe("structured_edge")
    if r is None:
        return _skip()
    html = r["answer_html"] or ""
    assert "<strong>" not in html, \
        "a `**…**` pair spanning a blank line still became <strong>, which means the surface " \
        f"built markup that crosses a block boundary: {html!r}"
    assert html.count("<p>") == html.count("</p>"), f"unbalanced paragraphs: {html!r}"
    assert "**Note that these figures" in html, \
        f"the unmatched emphasis was neither rendered nor left literal - text was lost: {html!r}"


def test_a_numbered_list_and_a_heading_are_structure_too():
    """#764. #751 handled "- " because that is what the one production answer I measured used.
    Models emit numbered lists at least as often, and an ATX heading puts its HASHES on screen -
    "## Freight costs" reaching the reader verbatim is worse than no heading at all.

    The ordered and unordered runs in the fixture are ADJACENT on purpose: a change of marker
    kind is the only signal that they are two lists, and merging them would renumber the dashed
    item as though the model had asked for it.

    A heading renders as a bold line, not an <h*>: the panel has its own type scale and a real
    heading element would need a size of its own. The semantics are deliberately not carried -
    a screen reader gets emphasis, not a level - and that limit is recorded in the code."""
    r = _probe("structured_numbered")
    if r is None:
        return _skip()
    blocks = r["answer_blocks"]
    assert blocks == ["p", "ol", "ul", "p", "p"], \
        f"the mixed answer did not resolve into heading/ordered/unordered/heading/body: " \
        f"{blocks} / {r['answer_html']!r}"
    html = r["answer_html"] or ""
    assert "#" not in html, f"the heading's hashes reached the reader: {html!r}"
    for lead in ("1.", "2."):
        assert not any(i.startswith(lead) for i in r["answer_list_items"]), \
            f"the list marker was printed as content beside the one <ol> supplies: " \
            f"{r['answer_list_items']}"
    assert "<sup" in html, "the citation marker was lost on the numbered-list path"


def test_model_text_cannot_break_out_of_an_attribute():
    """#786. `esc()` is correct at HEAD and the outcome row does call it, so there is NO live
    vulnerability - this closes the hole where nothing would notice if that stopped being true.

    Only two tests in the repo mention `esc(` and both do substring arithmetic on the SOURCE
    TEXT ("the literal `esc(` appears inside this function"). The one behavioural check exercises
    the `<` branch, in element CONTENT, in `.qanswer`. `grep -rn "&quot;" tests/` returned zero.
    So deleting the double quote from esc()'s character class left seven suites green while
    eleven ATTRIBUTE sinks became injectable - href=, title=, data-pin=, six value= inputs and
    two <option value=> - including the footnote title, fed straight from the model's own text.

    WHY THE OLD GUARDS CANNOT SEE IT, which is the part worth remembering: they read
    `textContent`, and an injected `<img onerror=...>` contributes ZERO characters to
    textContent. Measured on a mutated canvas, the outcome row's text is BYTE-IDENTICAL either
    way - so the guard gets greener as the surface gets less safe. This asserts on the DOM
    itself: no element nobody rendered on purpose, and no event-handler attribute anywhere."""
    r = _probe("hostile_labels")
    if r is None:
        return _skip()
    assert r["event_handler_attrs"] == [], \
        f"model-derived text broke out of an attribute and became an EVENT HANDLER: " \
        f"{r['event_handler_attrs']} - esc() is no longer escaping the double quote"
    assert r["injected_elements"] == [], \
        f"model-derived text became a live element: {r['injected_elements']}"
    hrefs = [a["value"] for a in r["attr_values"] if a["name"] == "href"]
    assert hrefs, "the fixture no longer renders an href sink, so this guard tests nothing"
    assert any('"><img' in h for h in hrefs), \
        f"the hostile uri did not arrive as DATA inside one attribute value: {hrefs}"


def test_esc_escapes_every_character_that_can_leave_its_context():
    """#786, the unit half. Asserted by CALLING esc, not by grepping for its name.

    The two existing mentions of `esc(` in the whole repo are index arithmetic over the source
    text - "the literal string `esc(` occurs inside this function, before `<strong>`". Both stay
    green when the function's character class is gutted, because the call site is untouched and
    only its behaviour changed.

    #689 moved `esc` into ui/proofs.js and this guard FOLLOWED THE CODE rather than being
    relaxed to keep passing. It now imports the real module, which is strictly stronger than
    the old source-slicing: it proves the escaper the shipped surfaces actually load behaves,
    not that a line of text somewhere behaves when evaluated on its own."""
    if not _domgate.gate("the esc() unit check"):
        return _skip()
    # #689: `esc` lives in ui/proofs.js now. Imported and CALLED rather than sliced out of the
    # source text - the point of this guard is behaviour, and a real import also proves the
    # module the canvas actually loads is the one being checked.
    script = f"""
      import {{ esc }} from "{PROOFS.as_uri()}";

      const cases = ["&", "<", ">", '"', 'a"b', "<img>", "a&b", "plain"];
      console.log(JSON.stringify(Object.fromEntries(cases.map(c => [c, esc(c)]))));
    """
    got = json.loads(subprocess.run(["node", "--input-type=module", "-e", script],
                                    capture_output=True, text=True, check=True).stdout)
    assert got == {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
                   'a"b': "a&quot;b", "<img>": "&lt;img&gt;", "a&b": "a&amp;b",
                   "plain": "plain"}, \
        f"esc() no longer escapes a character that can break out of its context: {got}"


def test_a_store_that_did_not_succeed_is_not_dressed_as_one():
    """#788. Every outcome fixture in this file was status:"ok", so the OTHER side of
    `o.status==="ok"` was unreachable - and the branch is read TWICE, once for the glyph and once
    for the result count. Change it to `!=="error"` and a timed-out store renders
    `✓ azure_sql-1 · timeout · 3 results`: a failure reporting success, with a result count
    attached, on the surface whose whole job is saying how the answer was reached.

    Not hypothetical. The 260818 prod pass rendered `✗ bigquery-1 · declined` on the first
    compound ask asked of the live product.

    The fixture gives the declined store `count: 7` on purpose. A guard that merely checked for
    the absence of "0 results" would pass on a broken renderer; what must be absent is the count
    ITSELF, because a store that declined has no results to report."""
    r = _probe("declined_and_docs")
    if r is None:
        return _skip()
    rows = {row.split(" ")[1]: row for row in r["outcome_rows"]}
    ok, declined, timeout = rows["folder-1"], rows["bigquery-1"], rows["azure_sql-1"]
    assert ok.startswith("\u2713"), f"a store that succeeded is not marked as such: {ok!r}"
    for bad in (declined, timeout):
        assert bad.startswith("\u2717"), \
            f"a store that did NOT succeed is marked with the success glyph: {bad!r}"
        assert "result" not in bad, \
            f"a failed store reports a result count, which it does not have: {bad!r}"
    assert "2 results" in ok, f"the succeeding store lost its count: {ok!r}"
    assert "declined" in declined and "timeout" in timeout, \
        f"the outcome status word is gone, so the row says only that something went wrong: {rows}"


def test_an_all_document_rail_is_not_called_a_database():
    """#788, and it reinstates #750/#728 when it breaks. `whose` has three arms and every fixture
    reached two of them: no fixture had citations that were ALL documents, so the
    `kinds.has('document')` arm was dead and could be rewritten to ' from your databases' with
    nothing noticing - a folder calling itself a database, which is the exact defect #750 fixed."""
    r = _probe("declined_and_docs")
    if r is None:
        return _skip()
    s = r["trace_summary"] or ""
    assert "from your documents" in s, \
        f"an all-document rail does not say what it drew on: {s!r}"
    assert "from your databases" not in s, \
        f"a rail with no database citation calls itself a database: {s!r}"


def test_unassigned_is_a_placeholder_and_never_reaches_the_reader():
    """#788. `unassigned` is what the canvas WRITES when a node has no business unit
    (canvas.js:1310, :1394) and what `router/decision.py:qualify()` refuses to print. The canvas
    has its own copies of that rule at :1970 and :2042, and the only guard on it walks
    router_service.py and synthesizer.py with PYTHON f-string regexes that cannot match JS - so
    the JS copies were unguarded, and no fixture carried the value anyway.

    Asserted alongside a REAL business unit in the same render, so a fix that simply stopped
    printing business units altogether cannot pass."""
    r = _probe("declined_and_docs")
    if r is None:
        return _skip()
    rows = " || ".join(r["outcome_rows"])
    assert "unassigned" not in rows, \
        f"the placeholder for 'no business unit' is being shown to the reader as one: {rows!r}"
    assert "finance" in rows, \
        f"a real business unit stopped being labelled, so the rule is over-tightened: {rows!r}"


def test_a_number_that_opens_a_line_is_not_eaten_as_a_list_marker():
    """#793, and it is CONTENT LOSS, not a layout complaint.

    #764 read a list marker as `\\d+[.)]\\s+` and DELETES the marker when it pushes the item. So
    "2008. Revenue was 4.2M" became an <ol> whose only item read "Revenue was 4.2M", beside a
    counter printing "1." - the year gone from the screen and a wrong number in its place. Years,
    page numbers, citation numbers and figure numbers all open lines in real answers.

    Asserted on the CHARACTERS A READER SEES, not on the block structure: a guard that only
    checked for <ol> would have passed throughout the defect, because an <ol> is exactly what the
    broken code produced.

    THE FIX IS TWO NARROWINGS AND EACH IS ASSERTED SEPARATELY. A first cut of this guard had
    every case saved by both at once, and the mutation matrix promptly showed either could be
    removed with nothing going red - the #788 shape, where a fixture only ever exercises one
    side. So: two ADJACENT years are a genuine run and only the DIGIT CAP keeps them out of a
    list, while a LONE two-digit line passes the cap and only the RUN REQUIREMENT keeps its
    marker."""
    r = _probe("numbers_in_prose")
    if r is None:
        return _skip()
    html = r["answer_html"] or ""
    assert "2008." in html and "2009." in html, \
        f"a run of two years was read as a list and the years deleted - the DIGIT CAP is what " \
        f"prevents this, and nothing else can: {html!r}"
    assert "12. items were reconciled" in html, \
        f"a lone numbered line was read as a list item and its marker deleted - the RUN " \
        f"requirement is what prevents this, and nothing else can: {html!r}"
    assert "1997)" in html, \
        f"the page number was deleted from the answer: {html!r}"
    assert r["answer_blocks"] == ["p", "p", "p", "p", "ol"], \
        f"a line opening with a number was treated as a list: {r['answer_blocks']} / {html!r}"
    # The citation must survive the prose path too - #764's ol path carried it and this one is new.
    assert "<sup" in html, "the citation marker was lost on the numbers-in-prose path"


def test_an_ordered_run_keeps_the_number_the_model_wrote():
    """#793 second half. `<ol>` renumbers from 1 whatever the model said, so "12. reconcile /
    13. archive" rendered as "1. / 2." - the counter beside the text silently contradicting an
    answer that may refer to step 12 by name three lines further down.

    This also pins the two-digit reach: the run is 12-13, so narrowing the marker to a single
    digit (which would break every list at item 10) now has a fixture that notices."""
    r = _probe("numbers_in_prose")
    if r is None:
        return _skip()
    html = r["answer_html"] or ""
    assert 'start="12"' in html, \
        f"the ordered list restarted at 1, renumbering the model's own steps: {html!r}"
    assert r["answer_list_items"] == ["reconcile the ledger", "archive the quarter"], \
        f"a genuine two-item run stopped rendering as a list: {r['answer_list_items']}"


def test_a_sub_question_is_printed_once_not_three_times():
    """#753. One open trace used to carry each sub-question three times: the routing REASON
    enumerated them, the relocated arrow lines listed them, and every outcome row quoted its own
    again. The reason was fixed server-side; this is the client half.

    Once the arrow lines are on screen they already map question to store, so the outcome row's
    quote is a third copy - and since #753 round 2 the row leads with `store_id`, so the arrow
    line above genuinely joins to it.

    #763, WHAT THIS TEST DOES NOT COVER, because the docstring above used to imply it did. The
    fixture's `reason` is now the server's own post-#753 string ("decomposed into 2 sub-queries")
    rather than an invented phrase, but it is still a FIXTURE: if a server change reinstated the
    enumeration, this test would stay green because nothing here derives the reason from the
    server. That regression is caught in selftest_router_e6, positively, and it has to be."""
    r = _probe("compound")
    if r is None:
        return _skip()
    trace = r["trace_text"] or ""
    for q in ("what did EMEA bill?", "what is the Krakow headcount?"):
        assert trace.count(q) == 1, \
            f"{q!r} appears {trace.count(q)} times in one trace: {trace!r}"


def test_the_quote_survives_where_the_store_cannot_disambiguate():
    """The exception, and the reason this is not just a deletion. Two sub-questions dispatched to
    the SAME store produce two outcome rows that are otherwise byte-identical - same store, same
    status, same count - so without the quote a reader cannot tell which result answered which
    half. It is redundancy only while the store name is a unique key."""
    r = _probe("compound_same_store")
    if r is None:
        return _skip()
    trace = r["trace_text"] or ""
    for q in ("what did EMEA bill?", "what did APAC bill?"):
        assert trace.count(q) == 2, \
            f"{q!r} lost the quote that told two identical rows apart: {trace!r}"


if __name__ == "__main__":
    fails = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                fails.append(name)
                print(f"  FAIL {name}: {e}")
    print(f"\n{'FAILED' if fails else 'PASSED'} - {len(fails)} failed")
    sys.exit(1 if fails else 0)
