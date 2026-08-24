"""#463 - the tally primitives must not fail a CORRECT answer phrased naturally.

Measured instances (three now): B-003's "Sep 2019" vs "September 2019" (fixed by
re-anchoring the fact), then two more in the #494 doc measurement - the product answered
"pre-recorded screen recordingS" (a trailing plural defeats the word anchor) and
"six months" against the verbatim fact "six (6) months" (a parenthetical numeral the
model rightly omits). The fix belongs HERE, in matching tolerance, not in ever-stiffer
fact authoring: a fact must stay verbatim-verifiable against the corpus, and a natural
correct phrasing of it must still score.

The load-bearing decoy guards stay: anchoring is what keeps "25 days" from matching
"125 days".

Run: PYTHONPATH=src python3 tests/selftest_tally.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.eval.tally import number_present, phrase_present  # noqa: E402


def test_the_original_anchoring_guards_hold():
    assert phrase_present("we spent 25 days there", "25 days")
    assert not phrase_present("we spent 125 days there", "25 days")
    assert not phrase_present("the SuperPro X shipped", "Pro X")
    assert phrase_present("The Pro X shipped", "pro x")          # case-insensitive


def test_a_trailing_plural_on_the_last_word_still_scores():
    """A-006: the product said 'pre-recorded screen recordings' against the fact
    'pre-recorded screen recording' - correct, and scored wrong."""
    assert phrase_present("facilitated using pre-recorded screen recordings",
                          "pre-recorded screen recording")
    assert phrase_present("request a Letter of Guarantee", "Letter of Guarantee")
    # the plural tolerance must not weaken the trailing anchor into a prefix match
    assert not phrase_present("the recordingstudio was booked", "recording")


def test_a_parenthetical_in_the_fact_is_optional_in_the_answer():
    """LAW2-003: the letter says 'six (6) months'; the product correctly answered
    'six months'. The parenthetical numeral is legalese the model rightly omits."""
    assert phrase_present("the probation period is six months", "six (6) months")
    assert phrase_present("you will be on probation for six (6) months", "six (6) months")
    assert not phrase_present("the probation period is sixteen months", "six (6) months")


def test_the_legalese_pair_accepts_the_digit_form_too():
    """#506 (found in the #499 doc gate, LAW2-003 red 3/3 on a CORRECT answer): the
    letter says 'six (6) months' and the live answer said 'The probation period is
    6 months'. The pair ITSELF declares word and digit interchangeable - that is what
    the legalese parenthetical is for - so the position must accept either form.
    Scoped to facts that carry the pair: a bare 'six months' fact is untouched."""
    assert phrase_present("the probation period is 6 months", "six (6) months")
    assert phrase_present("the probation period is six months", "six (6) months")
    assert not phrase_present("the probation period is 16 months", "six (6) months")
    assert not phrase_present("the probation period is 6.5 months", "six (6) months")
    # facts WITHOUT the pair keep exact matching - no new tolerance leaks
    assert not phrase_present("we hired 6 people", "six people")


def test_interior_words_do_not_get_plural_tolerance():
    """Only the FINAL word tolerates a plural - loosening every word would let
    'screens recording' satisfy 'screen recording'."""
    assert not phrase_present("the screens recording failed", "screen recording")


def test_number_present_is_untouched():
    assert number_present("we spent 812,000 dollars.", 812000)
    assert not number_present("we spent 8120001 dollars", 812000)


def main():
    test_the_original_anchoring_guards_hold()
    test_a_trailing_plural_on_the_last_word_still_scores()
    test_a_parenthetical_in_the_fact_is_optional_in_the_answer()
    test_the_legalese_pair_accepts_the_digit_form_too()
    test_interior_words_do_not_get_plural_tolerance()
    test_number_present_is_untouched()
    print("  PASS  #463 phrase tolerance: trailing plural + optional parenthetical, "
          "decoy anchors intact")
    print("\nTALLY SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
