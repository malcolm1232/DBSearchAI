"""The one place that decides whether a DOM guard may run, and what it costs when it cannot.

Ten selftest files mount a surface in jsdom and read the result back. Each carried its OWN copy
of this gate, and every copy made the same call: node or jsdom missing -> print an indented line,
return a PASS.

That is the #792 defect. `site/node_modules` is gitignored (`site/.gitignore:4`) and untracked, so
a clean clone has no jsdom; `.github/workflows/tests.yml` installed no Node, so CI never had it
either. Measured on a `git archive HEAD` tree: 111 DOM guards printed "skipping" and reported ok,
against 0 on this machine. Worse, the file-level summary was BYTE-IDENTICAL both ways -
`selftest_724_doc_block.py` prints `PASSED - 25 ok, 0 failed` whether all 16 of its DOM guards ran
or none of them did. Nothing downstream could tell the two apart, which is exactly the shape
`CONTRIBUTING.md` forbids: a green figure that quietly skipped its coverage is worse than no figure.

THE RULE NOW: MISSING TOOLING IS A FAILURE, NOT A SKIP. jsdom is pinned exactly in
`tests/package.json` with a tracked `tests/package-lock.json`, so obtaining it is one
deterministic command and there is no good reason for a guard to evaporate instead.

WHY tests/ AND NOT site/. It used to resolve out of `site/node_modules` - the Next.js MARKETING
SITE's devDependency tree - which the suite never declared and which only existed if somebody had
run `npm install` there for unrelated frontend work. That coupling is the reason this broke: a
clean clone and CI simply had no jsdom. Declaring it beside the tests that use it also stops the
suite failing for reasons that belong to the marketing site - the site's own lockfile is currently
out of sync (picomatch 2.3.2 vs 4.0.5), which npm 11 tolerates and npm 10 refuses, so
`npm ci --prefix site` passed locally and failed in CI.

`DBSEARCH_ALLOW_DOM_SKIP=1` buys the old behaviour back for someone who genuinely cannot install
Node. It is not free: every permitted skip is counted into the ledger named by
`DBSEARCH_SKIP_LEDGER`, so `scripts/run_tests.py` prints the number beside its verdict as
`[PARTIAL]` rather than letting it hide.

A CRASH IS STILL NOT A SKIP. `run_node` re-raises a probe crash for every caller that asks, which
is the discipline `selftest_602` earned the hard way ("one real failure reported, five silently
turned green") and which two files, `606` and `607`, still got wrong by seeding their cache with
None before asserting.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JSDOM = ROOT / "tests/node_modules/jsdom/lib/api.js"

REMEDY = ("install it with `npm ci --prefix tests` (jsdom is pinned exactly in "
          "tests/package.json and tests/package-lock.json is tracked, so this is "
          "deterministic), or set "
          "DBSEARCH_ALLOW_DOM_SKIP=1 to run without the DOM guards and have the skips counted")

_node: "bool | None" = None


def have_node() -> bool:
    """Is `node` on PATH? Cached, because ten files ask it once per guard."""
    global _node
    if _node is None:
        _node = subprocess.run(["which", "node"], capture_output=True).returncode == 0
    return _node


def unavailable_reason() -> "str | None":
    """None when a DOM guard can run. Otherwise the precise missing piece, named.

    Both halves are reported separately on purpose: CI's ubuntu-latest DOES ship a `node`
    binary, so "node or jsdom unavailable" pointed at the wrong half of the problem for the
    entire time this was broken there.
    """
    if not have_node():
        return "`node` is not on PATH"
    if not JSDOM.exists():
        return f"jsdom is not installed ({JSDOM.relative_to(ROOT)} is missing)"
    return None


def allow_skip() -> bool:
    """Explicit opt-in only. Follows `dev_auth_enabled`'s allowlist rule (#315): any value that
    is not plainly affirmative means OFF, so `DBSEARCH_ALLOW_DOM_SKIP=flase` cannot quietly
    disarm the whole suite the way the old denylist let `DBSEARCH_DEV_AUTH=flase` enable a
    dev header."""
    return os.environ.get("DBSEARCH_ALLOW_DOM_SKIP", "0").strip().lower() in (
        "1", "true", "yes", "on")


def _record(what: str, reason: str) -> None:
    """Count a permitted skip where something other than this process can see it.

    Printing is not enough: `scripts/run_tests.py` captures a passing file's stdout and never
    emits it, so every one of the 111 skip lines was written and then discarded.
    """
    print(f"      (SKIPPED - {what}: {reason})")
    ledger = os.environ.get("DBSEARCH_SKIP_LEDGER")
    if not ledger:
        return
    try:
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(f"{Path(sys.argv[0]).name}\t{what}\t{reason}\n")
    except OSError:
        pass          # a missing ledger must never turn a green guard red


def gate(what: str = "the DOM check") -> bool:
    """True: run the guard. False: a PERMITTED skip, already printed and counted.

    Raises when the tooling is missing and no opt-out was given, which is the whole point.
    `what` names the guard so the ledger and the failure both say which coverage was lost.
    """
    reason = unavailable_reason()
    if reason is None:
        return True
    if not allow_skip():
        raise AssertionError(
            f"cannot run {what}: {reason}. This guard asserts against a real DOM and there is "
            f"nothing to assert against, so it FAILS rather than passing silently (#792). "
            f"Fix: {REMEDY}.")
    _record(what, reason)
    return False


def run_node(argv, what: str):
    """Run a probe and return its JSON. A non-zero exit is an ERROR, never a skip.

    Callers cache the return value. Cache the EXCEPTION too and re-raise it per caller: seeding
    a cache with None on failure is how a surface that threw on mount produced a green line for
    every later test in the file.
    """
    p = subprocess.run(argv, capture_output=True, text=True, cwd=str(ROOT))
    if p.returncode != 0:
        return RuntimeError(f"{what} blew up in a real DOM: {p.stderr[-1200:]}")
    return json.loads(p.stdout)


def resolve(cached):
    """Unwrap what a caller's cache holds: re-raise a stored crash, else hand back the report."""
    if isinstance(cached, Exception):
        raise AssertionError(str(cached))
    return cached
