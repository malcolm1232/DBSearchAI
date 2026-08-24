"""#521 - an embedder that returns fewer vectors than it was asked for must DECLINE, not
raise and not silently rank a shorter candidate list.

LAW 9 invites a bring-your-own embedding provider, so a REMOTE embedder is a supported
deployment - and a remote service can return an empty or partial batch. Both rungs of the
value ladder that call `embedder.embed` unpacked the reply positionally OUTSIDE the try
that guards the call, which made a partial reply fail two different ways:

  * an EMPTY list raised IndexError out of `_nearest` / `_llm_pick`, up through
    `_repair_literal`'s caller, where `executor` catches it as an ERROR outcome. The user
    is then told "the source it routed to did not respond successfully" for a query that
    ran fine and simply matched nothing - the misattributed-decline class #218 exists to
    remove.
  * a SHORT list was worse than a crash: `zip` silently truncated the candidates, so
    values were ranked that the embedder had never scored. With the true best dropped, a
    lone survivor skips the MIN_MARGIN guard entirely and resolves the user's wording to
    a WRONG stored value - a confident falsehood, which is the exact failure class this
    whole module exists to remove.

The shape appeared twice (`_nearest` and `_llm_pick`'s shortlist), so both are pinned.

Run: PYTHONPATH=src python3 tests/selftest_521_short_vector_embedder.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router import dictionary  # noqa: E402
from dbsearch.router.dictionary import MIN_MARGIN, MIN_SIMILARITY, resolve_literal  # noqa: E402


class _Points:
    """Texts sit on a unit circle, so the cosine between two of them is just the product
    of their x coordinates plus the product of their y's - i.e. their configured
    similarity. Nothing here tests the embedder; what it pins is the LADDER's behaviour
    when the embedder's REPLY is malformed."""

    _POINTS = {"dominican republic": 1.0, "d.r.": 0.99, "dominica": 0.95, "usa": -1.0}

    def _vector(self, text: str) -> list:
        x = self._POINTS.get(str(text).strip().lower(), 0.0)
        return [x, (1.0 - x * x) ** 0.5]


class _EmptyEmbed(_Points):
    """The whole batch is lost - a remote embedder erroring into an empty body."""

    def __init__(self):
        self.calls = 0

    def embed(self, texts: list) -> list:
        self.calls += 1
        return []


class _DropsLastEmbed(_Points):
    """A partial batch: every text is embedded except the last, which is the one whose
    candidate would have won."""

    def __init__(self):
        self.calls = 0

    def embed(self, texts: list) -> list:
        self.calls += 1
        return [self._vector(t) for t in texts][:-1]


class _WholeEmbed(_Points):
    """The control: a well-behaved embedder, to prove the guard costs nothing."""

    def embed(self, texts: list) -> list:
        return [self._vector(t) for t in texts]


class _StubLlm:
    def __init__(self, reply, in_tenant=True):
        self.reply = reply
        self.in_tenant = in_tenant
        self.calls = 0

    def pick_value(self, written, candidates):
        self.calls += 1
        self.last_shortlist = list(candidates)
        return self.reply


# --- the premise: these candidates are ambiguous, and the full ladder knows it ---------

def test_the_control_declines_on_margin_so_a_resolution_would_be_the_bug():
    """'dominica' scores 0.95 and `D.R.` 0.99 - both clear MIN_SIMILARITY, and the 0.04
    gap is inside MIN_MARGIN. A correct embedder therefore DECLINES here. Any test below
    that returns a value is returning one the embedder never justified."""
    assert 0.95 >= MIN_SIMILARITY and (0.99 - 0.95) < MIN_MARGIN
    assert resolve_literal("dominican republic", ["dominica", "D.R."],
                           _WholeEmbed()) is None


# --- _nearest -------------------------------------------------------------------------

def test_an_empty_embedding_reply_declines_instead_of_raising():
    """The #521 report: IndexError out of the ladder, reported to the user as the SOURCE
    failing, for a query that ran fine."""
    embedder = _EmptyEmbed()
    assert resolve_literal("dominican republic", ["dominica", "D.R."], embedder) is None
    assert embedder.calls == 1                 # it was asked, and its reply was refused


def test_a_partial_embedding_reply_never_ranks_candidates_it_did_not_score():
    """The dangerous half. Dropping the last vector leaves `D.R.` - the true best -
    unscored, and a lone survivor bypasses the margin guard. Before the guard this
    returned 'dominica': a wrong real value, asserted confidently."""
    embedder = _DropsLastEmbed()
    assert resolve_literal("dominican republic", ["dominica", "D.R."], embedder) is None
    assert embedder.calls == 1


def test_a_reply_that_is_not_a_sized_sequence_declines():
    """An adapter handing back None (or anything unsized) is a failed call, not a match."""

    class _NoneEmbed:
        def embed(self, texts):
            return None

    assert resolve_literal("dominican republic", ["dominica", "D.R."],
                           _NoneEmbed()) is None


# --- _llm_pick's shortlist (the same shape, second occurrence) -------------------------

def _many(n: int) -> list:
    """More candidates than _LLM_SHORTLIST, so the rung must embed to shortlist."""
    return ["dominica", "D.R."] + [f"filler{i}" for i in range(n)]


def test_the_llm_rung_declines_when_its_shortlist_embedding_comes_back_empty():
    """`_llm_pick` carried the identical unpack. Reached through the full ladder: the
    embedding rung must decline first, and the LLM rung must not then raise."""
    candidates = _many(dictionary._LLM_SHORTLIST)
    assert len(candidates) > dictionary._LLM_SHORTLIST
    llm = _StubLlm("D.R.")
    assert resolve_literal("dominican republic", candidates, _EmptyEmbed(), llm=llm) is None
    assert llm.calls == 0                      # no shortlist means nothing honest to ask


def test_the_llm_rung_declines_when_its_shortlist_embedding_is_partial():
    candidates = _many(dictionary._LLM_SHORTLIST)
    llm = _StubLlm("D.R.")
    assert resolve_literal("dominican republic", candidates,
                           _DropsLastEmbed(), llm=llm) is None
    assert llm.calls == 0


def test_the_llm_rung_is_called_directly_with_a_broken_embedder_and_still_declines():
    """Pinned at the rung itself too: the ladder above it must not be the only thing
    standing between a malformed reply and an exception."""
    candidates = _many(dictionary._LLM_SHORTLIST)
    for embedder in (_EmptyEmbed(), _DropsLastEmbed()):
        llm = _StubLlm("D.R.")
        assert dictionary._llm_pick("dominican republic", candidates,
                                    embedder, llm) is None
        assert llm.calls == 0


def test_a_whole_reply_still_shortlists_and_the_llm_still_resolves():
    """The control for the rung: the guard must reject malformed replies only."""
    llm = _StubLlm("D.R.")
    assert dictionary._llm_pick("dominican republic", _many(dictionary._LLM_SHORTLIST),
                                _WholeEmbed(), llm) == "D.R."
    assert llm.calls == 1
    assert "D.R." in llm.last_shortlist
    assert len(llm.last_shortlist) == dictionary._LLM_SHORTLIST


def main():
    test_the_control_declines_on_margin_so_a_resolution_would_be_the_bug()
    test_an_empty_embedding_reply_declines_instead_of_raising()
    test_a_partial_embedding_reply_never_ranks_candidates_it_did_not_score()
    test_a_reply_that_is_not_a_sized_sequence_declines()
    print("  PASS  #521 _nearest: an empty, partial or unsized embedding reply DECLINES "
          "- it never raises, and never ranks candidates it did not score")
    test_the_llm_rung_declines_when_its_shortlist_embedding_comes_back_empty()
    test_the_llm_rung_declines_when_its_shortlist_embedding_is_partial()
    test_the_llm_rung_is_called_directly_with_a_broken_embedder_and_still_declines()
    test_a_whole_reply_still_shortlists_and_the_llm_still_resolves()
    print("  PASS  #521 _llm_pick's shortlist carries the same guard, and a well-formed "
          "reply still shortlists and resolves")
    print("\n#521 SHORT-VECTOR EMBEDDER SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
