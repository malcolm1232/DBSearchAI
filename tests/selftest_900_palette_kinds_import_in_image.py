"""#900/#901 - every kind the PALETTE advertises must actually be servable by the image.

The bug this exists to stop has now shipped to prod three times, each time on a different
dependency, each time the same shape:

  #654  bigquery   the canvas offered BigQuery, sign-in consented to the bigquery scope, the
                   account panel said "Connected", and every probe died with
                   `No module named 'google'`.
  #900  cosmos_db  a fully-configured store - valid endpoint, database, container and key -
                   probed `No module named 'azure.cosmos'` in 0ms, never touching the network.
                   The `cosmos` extra was declared in pyproject and simply left out of the
                   Dockerfile's install list.
  #901  synapse    probed `libodbc.so.2: cannot open shared object file`. pyodbc was pip
                   installed, but pyodbc is a BINDING: it needs the unixODBC runtime, which no
                   pip extra can express and which python:3.11-slim does not ship.

#654 fixed its instance and left the CLASS open, which is why #900 and #901 were still there
to be found by the owner on a live canvas. This guard closes the class.

WHY IT IS SHAPED THIS WAY, and the trap that makes a naive version worthless:

  A DEVBOX HAS EVERY EXTRA INSTALLED AND EVERY SYSTEM LIBRARY PRESENT. A guard that just
  imports the drivers passes cheerfully in a working tree and on CI while prod is broken -
  which is exactly how #900 and #901 survived. So the ONLY meaningful subject is the IMAGE'S
  DECLARED CAPABILITY SET: what the Dockerfile actually installs. This test reads the
  Dockerfile and the palette and asserts they agree. It needs no Docker daemon, so it runs in
  the ordinary suite, every time, on every machine.

  The palette is parsed FROM canvas.js rather than restated here, for the same reason #823
  gives: a list copied into a test drifts silently, and a drifted list would let a newly
  advertised kind slip through unguarded - the precise failure being prevented.

    PYTHONPATH=src python3 tests/selftest_900_palette_kinds_import_in_image.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANVAS = ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js"
DOCKERFILE = ROOT / "Dockerfile"

#: kind -> what the image must provide for a probe of that kind to reach the network at all.
#: `extras` are pyproject optional-dependency names that must appear in the Dockerfile's pip
#: install list. `apt` are system packages that must appear in an apt-get install line - a pip
#: extra cannot express these, which is the whole of #901.
#: Kinds needing nothing beyond the base image map to an empty requirement.
REQUIREMENTS = {
    "azure_sql":    {"extras": {"azure-sql"}, "apt": set()},
    # #901: synapse FORCES use_odbc=True (a dedicated pool rejects the `USE <db>` that pymssql
    # issues), so unlike azure_sql it can never fall back to the pymssql path.
    "synapse":      {"extras": {"azure-sql"}, "apt": {"unixodbc", "msodbcsql18"}},
    "postgres":     {"extras": {"server"}, "apt": set()},
    "mysql":        {"extras": {"mysql"}, "apt": set()},
    "cosmos_db":    {"extras": {"cosmos"}, "apt": set()},          # #900
    "bigquery":     {"extras": {"gcp"}, "apt": set()},             # #654
    "gdrive":       {"extras": {"azure"}, "apt": set()},           # requests, via the azure extra
    "rds_postgres": {"extras": {"server", "aws"}, "apt": set()},
    "rds_mysql":    {"extras": {"mysql", "aws"}, "apt": set()},
    "redshift":     {"extras": {"aws"}, "apt": set()},
    "s3":           {"extras": {"aws"}, "apt": set()},
    "sharepoint":   {"extras": {"azure"}, "apt": set()},
    "sharepoint_link": {"extras": {"azure"}, "apt": set()},       # #924: requests, via the azure extra
    "upload":       {"extras": set(), "apt": set()},
    "folder":       {"extras": set(), "apt": set()},
    "csv":          {"extras": set(), "apt": set()},
    "local":        {"extras": set(), "apt": set()},
}

FAILURES = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def palette_kinds():
    """Every kind the ADD A SOURCE palette can offer, parsed from canvas.js PROVIDERS."""
    src = CANVAS.read_text()
    block = re.search(r"const PROVIDERS = \[(.*?)\n  \];", src, re.S)
    if not block:
        raise AssertionError(
            "could not find `const PROVIDERS = [ ... ];` in canvas.js. If the palette moved, "
            "FIX THIS PARSER - do not delete the test, or the #654/#900/#901 class reopens."
        )
    kinds = []
    for group in re.finditer(r"kinds:\s*\[([^\]]*)\]", block.group(1)):
        kinds += re.findall(r'"([a-z0-9_]+)"', group.group(1))
    return kinds


def image_capabilities():
    """(extras, apt_packages) the Dockerfile actually installs."""
    df = DOCKERFILE.read_text()
    pip = re.search(r"pip install[^\n]*'\.\[([^\]]+)\]'", df)
    if not pip:
        raise AssertionError("could not find the pip install extras list in the Dockerfile")
    extras = {e.strip() for e in pip.group(1).split(",")}
    apt = set()
    for line in re.findall(r"apt-get install[^\n\\]*", df):
        apt |= set(re.findall(r"[a-z0-9][a-z0-9.+-]*", line)) - {"apt", "get", "install", "y", "no", "recommends"}
    return extras, apt


def main():
    print("#900/#901 - the image must be able to serve every kind the palette advertises\n")

    kinds = palette_kinds()
    extras, apt = image_capabilities()
    print(f"  palette advertises {len(kinds)} kinds: {', '.join(kinds)}")
    print(f"  image installs extras: {', '.join(sorted(extras))}")
    print(f"  image installs apt:    {', '.join(sorted(p for p in apt if p in {'unixodbc', 'msodbcsql18'})) or '(none relevant)'}\n")

    # Control: the parser must actually find a palette. A silent empty list would make every
    # assertion below vacuously true - the failure mode this whole file is about.
    check("the palette parser found kinds", len(kinds) >= 10, f"found {len(kinds)}")

    unknown = [k for k in kinds if k not in REQUIREMENTS]
    check(
        "every advertised kind has a declared requirement",
        not unknown,
        f"unmapped: {unknown}. A kind was added to the palette without deciding what the "
        f"image must install for it. Add it to REQUIREMENTS." if unknown else "",
    )

    for kind in kinds:
        req = REQUIREMENTS.get(kind)
        if req is None:
            continue
        missing_extras = req["extras"] - extras
        check(
            f"{kind}: pip extras present",
            not missing_extras,
            f"MISSING {sorted(missing_extras)} from the Dockerfile install list" if missing_extras else "",
        )
        missing_apt = req["apt"] - apt
        check(
            f"{kind}: system packages present",
            not missing_apt,
            f"MISSING {sorted(missing_apt)} - a pip extra CANNOT supply these" if missing_apt else "",
        )

    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        print("\nThe palette is advertising a source the deployed image cannot serve. That is")
        print("#654/#900/#901. Either install the dependency or stop offering the kind.")
        return 1
    print(f"OK - all {len(kinds)} advertised kinds are supported by the image's install list.")
    print("\nNOTE this checks what the image DECLARES. Proving the drivers really load still")
    print("requires running inside the built image; see the note in the Dockerfile.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
