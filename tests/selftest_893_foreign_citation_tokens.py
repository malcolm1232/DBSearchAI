"""#893 - a model's NATIVE citation token must never reach the reader.

FOUND on prod, signed in as the owner, model Groq openai/gpt-oss-120b:

  ...two months' written notice (or salary in lieu) after your employment has been
  confirmed【9†L1-L4】.

Two failures in one string. It is not clickable - it never matches the [n] pattern the
renderer builds controls from - and "9" and "L1-L4" are a chunk index and a line range, which
is internal vocabulary on a user-facing page (DESIGN_SYSTEM.md s8 forbids it outright).

WHAT THE MEASUREMENT SHOWED, because the obvious reading was wrong. Prod serves a renderer
that DOES understand 【9†L1-L4】 - byte-identical to this tree, checked over https - and turns
it into a [9] control. So the final answer was never the leak. The leak was the STREAMED
PREVIEW: ask.js wrote the accumulating raw tokens to the page as plain text and only the
final string went through answerNodes, so the model's own convention was on screen for the
whole length of every generation. That is clause B.

Clause A is the hole the card warned about in its own words: "a whitelist of [n] silently
passes anything that is not [n]". The same generation family cites by FILENAME and with a
private-use sentinel run, and neither shape has a number for the range check to test, so
both sailed through the server sweep AND past the renderer - reaching every surface, final
answers included.

The two clauses are deliberately separable, and each test below goes red on its own clause
alone (#788's lesson: a fixture rescued by both halves at once proves neither).

    PYTHONPATH=src python3 tests/selftest_893_foreign_citation_tokens.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import _domgate  # noqa: E402  the shared jsdom gate (#792)

from dbsearch.query.service import QueryService  # noqa: E402

COMPONENTS = ROOT / "src/dbsearch/server/static/js/ui/components.js"
PROBE = ROOT / "tests/foreign_citation_dom_probe.mjs"

SENTINEL = "\ue200cite\ue202turn0file0\ue201"
_dom = None


def _report():
    global _dom
    if _dom is None:
        if not _domgate.gate("the #893 preview DOM check"):
            _dom = False
        else:
            _dom = _domgate.run_node(
                ["node", str(PROBE), str(_domgate.JSDOM), str(COMPONENTS)],
                "the streamed-preview citation format")
    return _domgate.resolve(_dom) if _dom is not False else None


def _swept(answer, n):
    return QueryService._drop_dangling_markers(answer, n)


# ---- clause A: the server drops the shapes nothing downstream can render -------------------

def test_a_citation_by_filename_never_reaches_the_reader():
    """The shape the range check could never catch: there is no number in it to be out of
    range, so every numeric guard in the pipeline passes it through."""
    out = _swept("Notice is two months【handbook.pdf†L1-L4】.", 9)
    assert "handbook.pdf" not in out, (
        f"a filename citation survived the sweep and reaches the reader: {out!r}")
    assert "【" not in out and "】" not in out, f"a foreign bracket survived: {out!r}"
    assert out == "Notice is two months.", f"the prose did not survive intact: {out!r}"


def test_a_chunk_name_citation_never_reaches_the_reader():
    out = _swept("Notice is two months【employment_terms】.", 9)
    assert "employment_terms" not in out, f"a chunk name reaches the reader: {out!r}"
    assert out == "Notice is two months.", f"the prose did not survive intact: {out!r}"


def test_the_private_use_sentinel_run_never_reaches_the_reader():
    out = _swept(f"Notice is two months{SENTINEL}.", 9)
    for ch in ("\ue200", "\ue202", "\ue201"):
        assert ch not in out, f"a sentinel character reaches the reader: {out!r}"
    assert "turn0file0" not in out, f"the sentinel payload reaches the reader: {out!r}"


def test_the_recognised_numeric_marker_is_NOT_eaten():
    """The control, and the reason the strip carries a negative lookahead. 【9†L1-L4】 IS
    resolvable - the renderer turns it into the [9] control and the line range becomes the
    element's title - so a strip that took it too would be deleting real provenance, which is
    the failure `_drop_dangling_markers` exists to prevent, arriving from the other side."""
    out = _swept("Notice is two months【9†L1-L4】.", 9)
    assert "【9†L1-L4】" in out, (
        f"the foreign-shape strip ate the marker the reader CAN resolve: {out!r}")
    out_bare = _swept("Notice is two months【2】.", 3)
    assert "【2】" in out_bare, f"the bare numeric marker was eaten: {out_bare!r}"


def test_the_range_rule_still_holds_for_both_spellings():
    """The other control: clause A must not have replaced clause 1. An out-of-range marker is
    still dropped in both spellings, which is #257 and #861."""
    assert _swept("Notice is two months【9†L1-L4】.", 2) == "Notice is two months."
    assert _swept("Notice is two months [7].", 3) == "Notice is two months."
    assert _swept("Notice is two months [1].", 3) == "Notice is two months [1]."


# ---- clause B: the streamed preview speaks the product's format ----------------------------

def test_the_streamed_preview_shows_our_format_not_the_models():
    r = _report()
    if r is None:
        return
    p = r["preview"]
    assert p["numeric"].endswith("confirmed[9]."), (
        f"the preview still shows the model's own citation convention: {p['numeric']!r}")
    assert p["bare_numeric"].endswith("confirmation[2]."), p["bare_numeric"]


def test_the_preview_drops_what_it_cannot_render():
    r = _report()
    if r is None:
        return
    p = r["preview"]
    assert p["filename"] == "Two months after confirmation.", p["filename"]
    assert p["chunk_name"] == "Two months after confirmation.", p["chunk_name"]
    assert p["sentinel"] == "Two months after confirmation.", repr(p["sentinel"])
    assert p["plain"] == "Two months after confirmation [1].", (
        f"the preview mangled our OWN marker: {p['plain']!r}")


def test_a_half_arrived_marker_is_held_back_not_flashed():
    """Streaming-specific, and the half a completed-answer test cannot reach: a marker arrives
    in pieces, so the text really does end '…confirmed【9†L1-' at some token."""
    r = _report()
    if r is None:
        return
    p = r["preview"]
    assert p["partial"] == "Two months after confirmation", (
        f"a half-arrived marker is on screen: {p['partial']!r}")
    assert p["partial_sent"] == "Two months after confirmation", repr(p["partial_sent"])
    assert r["everyPrefixClean"], (
        "some prefix of a streaming answer showed a marker character: "
        f"{r['firstDirtyPrefix']}")


def test_the_streaming_surface_actually_calls_the_helper():
    """The wiring, which every test above is blind to.

    They all exercise `previewText` directly, so a perfect helper that nothing calls passes
    every one of them - and that is precisely the defect, since the leak WAS the call site.
    The mutation matrix found this: reverting ask.js to `textContent = acc` left the guard
    green.

    A source assertion, and it is worth naming what that does and does not buy. It cannot
    prove the rendered page is clean (driving ask.js needs a mocked SSE transport, which the
    #893 fix does not otherwise need); it CAN prove the one line that carries the fix has not
    been reverted, which is the failure that actually happened."""
    src = (ROOT / "src/dbsearch/server/static/js/surfaces/ask.js").read_text()
    stream_cb = [ln for ln in src.splitlines()
                 if "acc += tok" in ln and "textContent" in ln]
    assert stream_cb, (
        "the streaming token callback is gone or has been rewritten - re-read this test "
        "against the new shape rather than deleting it")
    for ln in stream_cb:
        assert "previewText(acc)" in ln, (
            f"the streamed preview writes the model's RAW output to the page: {ln.strip()!r}")
        assert "textContent = acc" not in ln, (
            f"the raw accumulator still reaches the page: {ln.strip()!r}")
    assert "previewText" in src.split("from \"../ui/components.js\"")[0], (
        "previewText is used but not imported, so the surface would throw at the first token")


def test_the_final_render_still_builds_a_control():
    """The over-broad-fix control for clause B: the preview strips, the FINAL render must
    still produce the clickable [n] the reader can open the source with."""
    r = _report()
    if r is None:
        return
    assert r["rendered"]["numeric"]["citeRefs"] == ["[9]"], r["rendered"]["numeric"]
    assert r["rendered"]["plain"]["citeRefs"] == ["[1]"], r["rendered"]["plain"]


def test_the_client_alone_would_not_have_been_enough():
    """Why clause A exists even though clause B fixed the reported string. answerNodes builds
    controls from a whitelist, so a foreign shape passes through it as literal text - if the
    server did not strip these, the FINAL answer would carry them on every surface."""
    r = _report()
    if r is None:
        return
    assert "handbook.pdf" in r["rendered"]["filename"]["text"], (
        "this test's premise has changed - the renderer now strips foreign shapes itself, so "
        "re-read whether the server clause is still the load-bearing one")
    assert r["rendered"]["filename"]["citeRefs"] == [], r["rendered"]["filename"]


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                failures.append(name)
                print(f"FAIL  {name}\n      {e}")
            except Exception as e:
                failures.append(name)
                print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
