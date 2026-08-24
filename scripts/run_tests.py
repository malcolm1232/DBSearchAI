#!/usr/bin/env python3
"""Run EVERY test in tests/ and report ONE number.

    python3 scripts/run_tests.py              # everything
    python3 scripts/run_tests.py --selftest   # selftest_*.py only
    python3 scripts/run_tests.py --e2e        # e2e_*.py only (needs Playwright browsers)
    python3 scripts/run_tests.py -k connector # only files whose name contains "connector"

#678. WHY THIS EXISTS, and it is not tidiness.

There was no runner. Everyone ran `for f in tests/selftest_*.py`, and the numbers quoted in
handovers and card writeups ("191/192", "179/179", "249/249") were all that glob. tests/ also
holds six `e2e_*.py` files, which that glob silently excludes - and three of them had been RED
since the #643 shell fold on 260811 without anyone noticing, because the figure everyone
reported stayed green while they rotted.

So the defect was never the three broken tests. It was that the number had nothing checking
it, and a green figure that omits a third of your browser coverage is worse than no figure:
it is an affirmative-looking failure, the same shape #200 forbids in the product itself.

This runner therefore reports the WHOLE directory by default, and prints what it skipped when
you narrow it. A count you have to caveat in prose is a count that will be quoted without the
caveat.

Each test file is its own process (they are standalone scripts with `main()`/`__main__`
blocks, not pytest cases), so one crash cannot take the run down with it.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# e2e_*.py drive a real browser via Playwright and boot their own uvicorn. They are part of
# the suite, not an optional extra - see the docstring - but they are slower and need browsers
# installed, so they are reported as their own group.
GROUPS = (("selftest", "selftest_*.py"), ("e2e", "e2e_*.py"))


def _read_ledger(ledger: Path) -> list[str]:
    """Every in-test skip taken this run, one per line, or nothing.

    Absent when no test skipped, which is the normal case and not an error. Read defensively:
    a runner that crashed on its own bookkeeping would turn a green suite red for no reason a
    reader could act on.
    """
    try:
        return ledger.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _run(path: Path, timeout: float, ledger: "Path | None" = None) -> tuple[str, float, str]:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    # #792: a test that skipped a DOM check writes one line here. It cannot report the skip any
    # other way - the branch below throws a passing file's stdout away, which is why 66 printed
    # skip lines per clean-clone run reached nobody.
    if ledger is not None:
        env["DBSEARCH_SKIP_LEDGER"] = str(ledger)
    started = time.monotonic()
    try:
        proc = subprocess.run([sys.executable, str(path)], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "TIMEOUT", time.monotonic() - started, f"exceeded {timeout:.0f}s"
    elapsed = time.monotonic() - started
    if proc.returncode == 0:
        return "PASS", elapsed, ""
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return "FAIL", elapsed, tail[-1] if tail else f"exit {proc.returncode}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true", help="selftest_*.py only")
    ap.add_argument("--e2e", action="store_true", help="e2e_*.py only")
    ap.add_argument("-k", metavar="SUBSTRING", default="", help="only files matching SUBSTRING")
    ap.add_argument("--timeout", type=float, default=300.0, help="per-file seconds (default 300)")
    args = ap.parse_args()

    chosen = [g for g in GROUPS
              if (args.selftest and g[0] == "selftest")
              or (args.e2e and g[0] == "e2e")
              or not (args.selftest or args.e2e)]
    omitted = [g[0] for g in GROUPS if g not in chosen]

    total_pass = total_fail = 0
    filtered_out = 0
    failures: list[tuple[str, str]] = []
    ledger = Path(tempfile.mkdtemp(prefix="dbsearch-skips-")) / "skips.tsv"
    for name, pattern in chosen:
        available = sorted(TESTS.glob(pattern))
        files = [f for f in available if args.k in f.name]
        filtered_out += len(available) - len(files)
        if not files:
            print(f"{name}: no files matched")
            continue
        print(f"\n{name} ({len(files)} files)")
        for f in files:
            status, elapsed, detail = _run(f, args.timeout, ledger)
            if status == "PASS":
                total_pass += 1
                if elapsed > 20:                     # slow enough to be worth naming
                    print(f"  PASS  {f.name}  ({elapsed:.0f}s)")
            else:
                total_fail += 1
                failures.append((f.name, detail))
                print(f"  {status}  {f.name}  ({elapsed:.0f}s)  {detail}")

    print(f"\n{'=' * 60}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for name, detail in failures:
            print(f"  {name}: {detail}")
    # #778: "263/263 passed" counted FILES, and one file whose every assertion skipped still
    # scored a whole pass. Eight writeups quoted that number as closing evidence for coverage
    # it never measured, so the unit it counts is now part of the sentence.
    verdict = f"{total_pass}/{total_pass + total_fail} files passed"
    # Say what was NOT run, in the same breath as the number. A figure quoted without its
    # scope is how the e2e files rotted for a day behind a green "249/249" - and `-k` is the
    # same hazard in miniature, so it is named too rather than left to the reader to remember
    # they typed it. This tool must not reproduce the defect it exists to prevent.
    caveats = []
    if omitted:
        caveats.append(f"group(s) not run: {', '.join(omitted)}")
    if filtered_out:
        caveats.append(f"{filtered_out} file(s) excluded by -k {args.k!r}")
    # #792: the skips a file took INSIDE itself, which no per-file verdict can express. Only
    # reachable under DBSEARCH_ALLOW_DOM_SKIP=1; without it a missing jsdom fails outright.
    skipped = [ln for ln in _read_ledger(ledger) if ln.strip()]
    if skipped:
        where = len({ln.split("\t")[0] for ln in skipped})
        caveats.append(f"{len(skipped)} DOM check(s) SKIPPED in {where} file(s) "
                       f"- run `npm ci --prefix site` and drop DBSEARCH_ALLOW_DOM_SKIP")
    if caveats:
        verdict += f"   [PARTIAL — {'; '.join(caveats)}]"
    else:
        verdict += "   (whole tests/ directory)"
    print(verdict)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
