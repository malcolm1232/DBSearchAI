"""#555 — an answer must read as prose, not as the model's raw output.

Pinned to the exact string the LIVE site produced in the first answer the #539 upload flow
ever returned:

    Primary carers receive **18 weeks** of fully paid parental leave, and the daily meal
    allowance while travelling is **65 euros** 【1†L4-L6】 【1†L9-L10】

Two defects in one line: markdown printed literally, and the model's OWN citation-marker
convention (【n†lines】) passed straight through — so the reader saw two citation systems at
once and one of them resolved to nothing, while a perfectly good "[1] hr-leave-policy.txt"
sat in the Sources block below.

There is no DOM here, so this asserts on the SHIPPED SOURCE rather than on rendered output:
that every surface which prints an answer routes it through a formatter, that the formatter
exists in both implementations (the module surfaces share `answerNodes`; the canvas has its
own `fmtAnswer` because it builds HTML strings), and — the part that actually matters for
safety — that the canvas one escapes BEFORE it inserts any markup, so model output can never
inject tags of its own.

    PYTHONPATH=src python3 tests/selftest_555_answer_prose.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/dbsearch/server/static"

LIVE_ANSWER = ("Primary carers receive **18 weeks** of fully paid parental leave, and the "
               "daily meal allowance while travelling is **65 euros** 【1†L4-L6】")
# A second live answer, from the folder connector: the SAME model emitted the bare form with
# no locator, which the first cut of the regex required. Both shapes must normalise.
LIVE_ANSWER_BARE = "laptops are issued by the Hamburg IT desk 【1】."


def test_the_shared_formatter_exists_and_handles_both_defects():
    src = (STATIC / "js/ui/components.js").read_text()
    assert "export function answerNodes" in src, "no shared answer formatter"
    assert "【" in src, "the citation-marker pattern is not handled"
    assert re.search(r"_BOLD\s*=", src), "markdown bold is not handled"


def test_every_answer_surface_uses_it():
    for surface in ("ask.js",):   # #632: chat.js merged into ask.js
        src = (STATIC / "js/surfaces" / surface).read_text()
        assert "answerNodes" in src, f"{surface} still prints the model's raw text"


def test_the_formatter_builds_nodes_rather_than_innerhtml():
    """Model output rendered inside an authenticated page must not be able to inject markup."""
    src = (STATIC / "js/ui/components.js").read_text()
    body = src[src.index("export function answerNodes"):]
    body = body[:body.index("export function provenanceNote")]
    assert "innerHTML" not in body, "answerNodes builds HTML by string — it must build nodes"
    assert "createTextNode" in body, "answerNodes should emit text nodes for the plain runs"


def test_the_canvas_escapes_before_it_inserts_markup():
    """The canvas builds HTML strings, so ORDER is the whole safety argument: esc() first,
    then the only tags in the string are the ones we added."""
    src = (STATIC / "js/surfaces/canvas.js").read_text()
    assert "function fmtAnswer" in src, "the canvas still prints the model's raw text"
    fn = src[src.index("function fmtAnswer"):]
    fn = fn[:fn.index("\n  }") + 4]
    assert "esc(s)" in fn, "fmtAnswer does not escape the model output"
    assert fn.index("esc(s)") < fn.index("<strong>"), "fmtAnswer inserts markup BEFORE escaping"
    assert "\\u3010" in fn or "【" in fn, "the canvas does not normalise the citation marker"


def test_the_live_string_would_be_fully_handled():
    """Belt and braces: both patterns present in the real answer are matched by the regexes
    the shipped code uses, so this test fails if either pattern is narrowed later."""
    cite = re.compile(r"【\s*(\d+)\s*(?:†([^】]*))?】")
    bold = re.compile(r"\*\*([^*]+)\*\*")
    assert cite.search(LIVE_ANSWER), "the live citation marker no longer matches"
    assert cite.search(LIVE_ANSWER_BARE), "the BARE 【1】 form (no locator) is not matched"
    assert [m.group(1) for m in bold.finditer(LIVE_ANSWER)] == ["18 weeks", "65 euros"], \
        "the live bold spans no longer match"
    assert cite.sub("", bold.sub(r"\1", LIVE_ANSWER)).count("*") == 0, "asterisks would survive"


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
