#!/usr/bin/env python3
"""Run every retrieval-efficacy analysis in order.

    python3 research/retrieval/analysis/run_all.py

Each script is read-only over committed artifacts - no server, no model, no network - so
the whole sweep takes a second or two and its output cannot drift from the README.
"""
import runpy
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SCRIPTS = sorted(p for p in HERE.glob("[0-9][0-9]_*.py"))


def main() -> int:
    failed = []
    for s in SCRIPTS:
        try:
            runpy.run_path(str(s), run_name="__main__")
        except Exception:
            failed.append(s.name)
            print(f"\n!! {s.name} raised:\n{traceback.format_exc()}")
    print()
    print("=" * 78)
    if failed:
        print(f"{len(SCRIPTS) - len(failed)}/{len(SCRIPTS)} ran clean. FAILED: {failed}")
        return 1
    print(f"all {len(SCRIPTS)} analyses ran clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
