"""#852: every mutation-matrix entry must still be anchored to code that exists.

WHY THIS IS A SUITE TEST AND NOT A NOTE IN THE RUNBOOK.

`scripts/mutate_guards.py` breaks the product on purpose, one edit at a time, and checks the
guard goes red. Each entry finds its target by an exact `old` string. When a refactor rewords
that line, the entry DETACHES: it stops describing any real code, and the mutation it was
supposed to apply never happens.

The runner already refuses to apply an unanchored entry - it reports UNANCHORED rather than
silently passing. But it only reports during a FULL run, and it reports in the same list as
genuine regressions. So the failure mode is not "nobody notices", it is "nobody notices UNTIL
the next full run, and then it arrives as noise among real surprises". Between those runs the
matrix's headline number is measuring fewer guards than it claims: the 260819 handover recorded
"114 entries, all caught" while five of them could not have been anchored at all.

FIVE DETACHED ENTRIES, and not one of them was anyone's mistake - each was a correct refactor
that nothing told the matrix about:

    842-ingest-skips-quota        #844 put a comment between two adjacent calls and gave the
    842-ingest-skips-disk-guard   quota call a `replaces_doc_id` argument
    831-incoming-bytes-ignored    #843 lifted the floor arithmetic into core.headroom so the
    831-fail-open-removed         ingest runner could share one definition of "is there room"
    833-upload-discriminator-...  #840 hoisted `uri = row.get("uri") or ""` onto its own line

That is the point: detachment is caused by GOOD changes, so it will keep happening, and the
only defence is something that runs on every change rather than on every full matrix run.

This test is that. It is cheap - no mutation is applied and no guard is executed, it only reads
files - so it can run in the suite and fail the moment a refactor orphans an entry, naming the
entry and its file while the person who moved the code is still looking at it.

    PYTHONPATH=src python3 tests/selftest_852_matrix_anchors.py
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX_SRC = ROOT / "scripts/mutate_guards.py"


def _matrix():
    """The matrix list, imported from the runner so there is ONE definition of it.

    Copying the entries here would recreate the very problem this file exists to prevent -
    a second place that has to be kept in step with the first."""
    spec = importlib.util.spec_from_file_location("_mutate_guards", MATRIX_SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_mutate_guards"] = mod
    spec.loader.exec_module(mod)
    for name, value in vars(mod).items():
        if (name.isupper() and isinstance(value, list) and value
                and isinstance(value[0], dict) and "old" in value[0]):
            return value
    raise AssertionError(f"no mutation matrix found in {MATRIX_SRC}")


def test_the_matrix_is_not_empty():
    """A guard against this file passing because it found nothing to check."""
    m = _matrix()
    assert len(m) > 100, f"expected the full matrix, got {len(m)} entries"


def test_every_entry_targets_a_file_that_exists():
    missing = [(e["card"], e["id"], e["path"]) for e in _matrix()
               if not (ROOT / e["path"]).exists()]
    assert not missing, "entries pointing at a file that is gone:\n" + "\n".join(
        f"  {c} {i} -> {p}" for c, i, p in missing)


def test_every_entry_is_anchored_exactly_once():
    """THE CARD. `old` must appear EXACTLY once in its target.

    Zero means the entry has detached and its mutation cannot be applied - it is proving
    nothing while being counted. More than one is just as bad in the other direction: the
    runner would edit whichever occurrence it happened to hit, so the mutation is not the one
    the `why` describes, and a CAUGHT would be evidence about the wrong line.

    Every offender is reported, not just the first: a refactor typically orphans several at
    once, and fixing them one failed run at a time is how the last five survived so long."""
    bad = []
    for e in _matrix():
        target = ROOT / e["path"]
        if not target.exists():
            continue                      # reported by the test above; not this one's claim
        n = target.read_text(errors="replace").count(e["old"])
        if n != 1:
            bad.append(f"  {e['card']:<6} {e['id']:<44} appears {n}x in {e['path']}")
    assert not bad, (
        "mutation-matrix entries are no longer anchored to real code.\n"
        "A detached entry is counted in the matrix total and proves nothing.\n"
        "Recover the current line from the file (never from memory of its shape - see\n"
        "feedback_an_unfaithful_mutation_proves_nothing) and re-anchor it:\n"
        + "\n".join(bad))


def test_no_entry_is_a_no_op():
    """`new` must differ from `old`, or the 'mutation' changes nothing and the guard is
    green for the most boring possible reason. Cheap to assert, impossible to notice."""
    noop = [f"  {e['card']} {e['id']}" for e in _matrix() if e["old"] == e["new"]]
    assert not noop, "entries whose mutation is a no-op:\n" + "\n".join(noop)


def test_entry_ids_are_unique():
    """`-k <substring>` selects by id, and two entries sharing one id makes a run ambiguous
    about which was exercised."""
    seen, dupes = set(), []
    for e in _matrix():
        if e["id"] in seen:
            dupes.append(e["id"])
        seen.add(e["id"])
    assert not dupes, f"duplicate entry ids: {sorted(set(dupes))}"


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'OK' if not failed else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)
