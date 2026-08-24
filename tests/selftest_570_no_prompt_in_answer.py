"""#570 - the self-host answer must not be the prompt.

Seen on a canvas screenshot, model "Extractive (fast, local)":

    Based on 2 retrieved source(s): [hr-wiki, hr] parental leave is sixteen weeks [style]
    Answer in plain prose, as briefly as the question allows. State the result. Do NOT show
    intermediate arithmetic, per-item working, or a step-by-step derivation - ...

The whole #449 style directive rendered as the answer. Two causes, and fixing one is not enough:

  1. #449 added a THIRD instruction label to the context ([style], alongside #206's [coverage]
     and #227's [query]) and never registered it with strip_instruction_markers - the very
     thing whose docstring says "fixed it over there is exactly how a bug hides".
  2. The extractive adapter joins context chunks VERBATIM, so it echoes the whole paragraph,
     not just the label. A real LLM obeys the directive instead of repeating it, which is why
     this is invisible on Anthropic/Groq and total on Extractive - the no-key path, i.e. the
     self-host edition and every demo rig without credentials.

    PYTHONPATH=src python3 tests/selftest_570_no_prompt_in_answer.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("SELFHOST_BACKEND", "memory")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.adapters.local import ExtractiveLlm  # noqa: E402
from dbsearch.router.synthesizer import strip_instruction_markers  # noqa: E402

STYLE = ("[style] Answer in plain prose, as briefly as the question allows. State the result. "
         "Do NOT show intermediate arithmetic, per-item working, or a step-by-step derivation.")
COVERAGE = "[coverage] these rows are a partial sample of the matching records."


def test_the_style_label_is_stripped_like_its_two_siblings():
    assert "[style]" not in strip_instruction_markers("the answer [style] is 18 weeks")


def test_the_older_two_labels_still_strip():
    assert strip_instruction_markers("a [coverage] b [query] c") == "a b c"


def test_an_unknown_bracketed_word_is_still_left_alone():
    """The author's own prose. Removing it would edit their meaning."""
    out = strip_instruction_markers("the clause [sic] applies")
    assert "[sic]" in out, out


def test_the_extractive_answer_never_quotes_an_instruction_chunk():
    """The visible half. Stripping the label alone leaves the paragraph on screen."""
    out = ExtractiveLlm().answer("how long is parental leave?",
                                 ["parental leave is eighteen weeks", STYLE, COVERAGE])["answer"]
    assert "eighteen weeks" in out, out
    for leaked in ("Answer in plain prose", "intermediate arithmetic", "partial sample",
                   "[style]", "[coverage]"):
        assert leaked not in out, f"instruction text reached the user: {leaked!r} in {out!r}"


def test_evidence_that_merely_starts_with_a_bracket_is_still_evidence():
    """A store's own evidence is prefixed [store-id ...] by the synthesizer. Dropping those
    would silently empty the answer - the opposite failure, and a worse one."""
    out = ExtractiveLlm().answer("q", ["[hr-wiki, hr] parental leave is sixteen weeks"])["answer"]
    assert "sixteen weeks" in out, out


def test_the_two_marker_lists_cannot_drift_apart():
    """The actual root cause was a THIRD label added in one place and not the other. The
    adapter deliberately does not import the router (that would invert the dependency this
    package keeps one-way), so this test is what holds the two in sync - and the next person
    who adds a [label] finds out here rather than on a customer's screen."""
    from dbsearch.router.synthesizer import _INSTRUCTION_MARKERS

    heads = {h.strip("[]").lower() for h in ExtractiveLlm._DIRECTIVE_HEADS}
    assert heads == set(_INSTRUCTION_MARKERS), (
        f"instruction labels have drifted: synthesizer={sorted(_INSTRUCTION_MARKERS)} "
        f"vs extractive adapter={sorted(heads)}. Add the new label to BOTH.")


def test_an_all_instruction_context_declines_rather_than_inventing():
    """If every chunk was a directive there is no evidence, and the honest answer says so."""
    out = ExtractiveLlm().answer("q", [STYLE, COVERAGE])["answer"]
    assert "Answer in plain prose" not in out, out


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
