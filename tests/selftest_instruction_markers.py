"""#272 — the model must not leave an instruction LABEL sitting in the prose.

Found by an independent audit agent, not by this suite and not by the author. On the
analytical path the answer carried markers [1][2][3][4][5][coverage] against sources [1]-[5],
so `[coverage]` resolved to nothing on screen. Reproduced on two consecutive runs.

`[coverage]` and `[query]` head INSTRUCTION lines folded into the model's context — #206's
"these rows are a partial sample" warning and #227's "here is the query that produced this
evidence". #233 already stopped them consuming a citable NUMBER, so a footnote can never point
at one. Nothing stopped the model reproducing the literal token.

The uncomfortable part: #257 fixed exactly this class of defect — a marker resolving to
nothing — on the /search DOCUMENT path, and left the analytical path untouched. Same bug, same
day, one path fixed. "Fixed it over there" is how a bug hides, which is why this test names
both paths.

Run: python3 tests/selftest_instruction_markers.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.router.synthesizer import strip_instruction_markers  # noqa: E402


def test_the_observed_failure_is_removed():
    """The exact prose the audit reported."""
    seen = ("Based on the sample data provided, here are the total revenues for each product "
            "SKU shown [coverage]: - **BK-T79U-60**: $37,191.49 [1]")
    out = strip_instruction_markers(seen)
    assert "[coverage]" not in out, out
    assert "[1]" in out, "a real citation was destroyed"
    assert "total revenues for each product" in out, "the prose was mangled"


def test_both_instruction_labels_go():
    assert strip_instruction_markers("a [coverage] b [query] c") == "a b c"


def test_numeric_citations_are_never_touched():
    """These are the ONLY markers that legitimately resolve to a source."""
    s = "Revenue was $37,191.49 [1] and $23,413.47 [5]."
    assert strip_instruction_markers(s) == s


def test_an_unknown_bracketed_word_is_left_alone():
    """Only the two known instruction labels are stripped. An unrecognised bracketed word is
    probably the author's own prose — '[sic]', '[see below]' — and deleting it would edit
    their meaning rather than remove our leak."""
    for s in ("This is [sic] correct.", "Details [see below] follow.", "Value [n/a] here."):
        assert strip_instruction_markers(s) == s, s


def test_empty_and_none_are_safe():
    assert strip_instruction_markers("") == ""
    assert strip_instruction_markers(None) is None


def test_the_synthesizer_actually_applies_it():
    """The function existing is not the fix — it has to be on the path."""
    src = (ROOT / "src/dbsearch/router/synthesizer.py").read_text()
    assert "answer = strip_instruction_markers(generated[\"answer\"])" in src, \
        "synthesize() returns the raw model answer — the stripper is dead code"


def test_the_document_path_still_has_its_own_guard():
    """#257's numeric fix on /search must not have been lost while fixing this one. The two
    paths have separate answer assembly and have already drifted apart once."""
    src = (ROOT / "src/dbsearch/query/service.py").read_text()
    assert "_drop_dangling_markers" in src, \
        "the /search path lost its dangling-marker guard (#257)"


def main():
    print("#272 instruction labels never survive into the prose:")
    test_the_observed_failure_is_removed()
    test_both_instruction_labels_go()
    print("  PASS  [coverage] and [query] are stripped, prose and real citations intact")
    test_numeric_citations_are_never_touched()
    test_an_unknown_bracketed_word_is_left_alone()
    test_empty_and_none_are_safe()
    print("  PASS  numeric citations and unknown bracketed words are left alone")
    test_the_synthesizer_actually_applies_it()
    test_the_document_path_still_has_its_own_guard()
    print("  PASS  applied on the analytical path, and #257's document guard is still in place")
    print("\nINSTRUCTION-MARKER SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
