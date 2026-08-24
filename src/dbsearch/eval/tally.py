"""Deterministic tally primitives shared by the usecase suite (#337) and the golden
suite (spec 2026-07-31). Extracted from scripts/usecase_runner.py so src code never
imports from scripts/.

The semantics here are load-bearing: anchoring and separator tolerance are what keep
"25 days" from matching "125 days" and "812,000" counting as 812000. Change only with
a failing test first.
"""
from __future__ import annotations

import re


def _fact_tokens(phrase: str) -> list:
    """(escaped_pattern, optional) per token of the fact. Parentheticals are optional;
    a "word (digit)" pair rewrites the word to accept either form (#506)."""
    tokens: list = []
    for part in re.split(r"(\([^)]*\))", phrase):
        part = part.strip()
        if not part:
            continue
        if part.startswith("(") and part.endswith(")"):
            digits = part[1:-1].strip()
            if tokens and not tokens[-1][1] and digits.isdigit():
                word = tokens[-1][0]           # the word half of a "word (digit)" pair
                tokens[-1] = (f"(?:{word}|{re.escape(digits)})", False)
            tokens.append((re.escape(part), True))
        else:
            tokens.extend((re.escape(w), False) for w in part.split())
    return tokens


def _phrase_pattern(phrase: str) -> str:
    """The anchored pattern for one key fact, with exactly three tolerances (#463, #506):

    - a parenthetical group in the FACT is optional in the answer - the letter says
      "six (6) months" and a correct answer says "six months"; the numeral is legalese
      the model rightly omits, while the fact must stay verbatim-verifiable in the doc;
    - the legalese pair "word (digit)" accepts the DIGIT form at the word's position -
      the pair itself declares them interchangeable, and the live answer "6 months"
      against "six (6) months" is correct (#506, LAW2-003 red 3/3 on a correct answer).
      Scoped to facts that CARRY the pair: a bare "six months" fact gains nothing;
    - the FINAL word tolerates a trailing plural - "screen recording" must accept
      "screen recordings". Final word only: loosening every word would let
      "screens recording" satisfy "screen recording".

    Everything decoy-guarding stays: word anchors at both ends ("25 days" is still not
    satisfied by "125 days" and "6 months" not by "16 months"), and interior tokens
    match exactly."""
    tokens = _fact_tokens(str(phrase))
    required = [i for i, (_e, opt) in enumerate(tokens) if not opt]
    if not required:                           # a purely parenthetical fact: match as-is
        return rf"(?<!\w){re.escape(str(phrase))}(?!\w)"
    pattern = ""
    for i, (escaped, optional) in enumerate(tokens):
        if i == required[-1]:
            escaped += r"(?:e?s)?"
        if not pattern:
            pattern = f"(?:{escaped}\\s+)?" if optional else escaped
        else:
            pattern += f"(?:\\s+{escaped})?" if optional else f"\\s+{escaped}"
    return rf"(?<!\w){pattern}(?!\w)"


def phrase_present(text: str, phrase) -> bool:
    """Word-anchored containment.

    Plain `in` would let "25 days" be satisfied by "125 days", and "Pro X" by
    "SuperPro X". That is the same decoy class the number check already guards.
    Tolerances (trailing plural, optional parenthetical) live in `_phrase_pattern`.
    """
    return re.search(_phrase_pattern(phrase), text, re.I) is not None


def number_present(answer: str, expected: float) -> bool:
    """Is the expected figure actually asserted in the prose?

    Anchored so 812000 is not satisfied by 8120001, and separator-tolerant so
    "812,000" counts. Only a following DIGIT disqualifies a match, not a following
    period: "We spent 812000." is a correct answer and must not be failed by
    punctuation. Exact after normalisation rather than a percentage band, because a
    tolerance wide enough to absorb rounding is also wide enough to absorb a decoy.
    """
    text = answer.replace(",", "")
    for raw in re.findall(r"(?<![\d.])\d+(?:\.\d+)?(?!\d)", text):
        try:
            if abs(float(raw.rstrip(".")) - float(expected)) < 0.005:
                return True
        except ValueError:
            continue
    return False


def num_str(value) -> str:
    """812000.0 must read as '812000', the way the figure is actually written in
    prose, not '812000.0' which would never match."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def is_numeric_token(token: str) -> bool:
    return re.fullmatch(r"-?\d+(?:\.\d+)?", token) is not None
