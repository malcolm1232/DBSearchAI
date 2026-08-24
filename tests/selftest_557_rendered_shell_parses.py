"""#557 — the shell the browser receives must be valid JavaScript.

A live outage found by opening /chat after a deploy: a blank white page and
"SyntaxError: Invalid or unexpected token". The served HTML contained

    <script>window.9f7d94f659a8 = "9f7d94f659a8";</script>

because the build-id substitution (app.py: a blanket `.replace("__DBS_BUILD__", build)`)
rewrote the token in the IDENTIFIER position as well as in the value. An identifier cannot
start with a digit, so the inline script failed to parse, main.js was never reached, and
every shell surface rendered nothing.

The hash is hex, so it starts with a digit 10 times in 16 — a ~62% chance PER DEPLOY, and a
~38% chance of looking perfect. It sat in the deploy path since #415.

WHY NOTHING CAUGHT IT, which is the point of this file: every existing check reads the
TEMPLATE on disk, where `__DBS_BUILD__` is a perfectly legal identifier. The bug only exists
in the RENDERED output. So this renders the shell through the real route, with a build id
chosen to be hostile, and asserts on what the browser would actually receive.

    PYTHONPATH=src python3 tests/selftest_557_rendered_shell_parses.py
"""
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import _domgate  # noqa: E402  the shared node/jsdom gate (#792)
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import app as app_mod  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)
SHELL_PATHS = ("/app", "/ask", "/chat", "/draft", "/admin", "/developer")

# Leading digit, which is the whole failure mode. "9f7d94f659a8" is the real one from the
# outage; a letter-leading id would have passed even with the bug present.
HOSTILE_BUILD = "9f7d94f659a8"


def _inline_scripts(html):
    return re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)


def _render(path, build):
    real = app_mod._build_id
    app_mod._build_id = lambda name: build          # pin the hash to the hostile one
    try:
        return client.get(path).text
    finally:
        app_mod._build_id = real


def test_no_inline_script_assigns_to_a_numeric_identifier():
    """The exact shape of the outage, asserted on rendered output."""
    for path in SHELL_PATHS:
        for js in _inline_scripts(_render(path, HOSTILE_BUILD)):
            bad = re.search(r"window\.\s*\d", js)
            assert not bad, f"{path} renders `window.<digit…>` — {js.strip()[:80]}"


def test_the_rendered_shell_actually_parses():
    """Belt and braces: hand the inline scripts to a real JS engine when one is available."""
    if not _domgate.gate("the rendered-shell parse check"):
        return          # permitted skip (DBSEARCH_ALLOW_DOM_SKIP), already counted
    for path in SHELL_PATHS:
        for js in _inline_scripts(_render(path, HOSTILE_BUILD)):
            r = subprocess.run(["node", "--check", "-"], input=js,
                               capture_output=True, text=True)
            assert r.returncode == 0, f"{path} inline script does not parse: {r.stderr[:200]}"


def test_the_build_id_still_reaches_the_page_and_the_assets():
    """The fix must not silently disable cache-busting — that was the whole point of #415."""
    html = _render("/ask", HOSTILE_BUILD)
    assert f'"{HOSTILE_BUILD}"' in html, "the build id no longer reaches the page as a value"
    assert f"?v={HOSTILE_BUILD}" in html, "assets are no longer cache-busted"


def test_the_global_and_its_reader_agree():
    """A rename that misses the reader is the same outage with a different symptom: no
    SyntaxError, just an undefined global and every module silently un-versioned again."""
    static = Path(__file__).resolve().parents[1] / "src/dbsearch/server/static"
    html = (static / "index.html").read_text()
    main = (static / "js/main.js").read_text()
    m = re.search(r"window\.([A-Za-z_$][\w$]*)\s*=\s*\"__DBS_BUILD__\"", html)
    assert m, "the shell no longer publishes the build id"
    assert f"window.{m.group(1)}" in main, \
        f"index.html sets window.{m.group(1)} but main.js reads a different global"


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
