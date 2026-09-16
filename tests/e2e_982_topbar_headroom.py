#!/usr/bin/env python3
"""#982 - the topbar must have ROOM to spare at the width where it stops shedding items.

WHY THIS IS A HEADROOM TEST AND NOT A "DOES IT FIT" TEST.

#639 measured that the topbar's full row (edition pill, trust chip, model picker, account
control) stops fitting below about 901px, and put the rule that sheds the two informational
items at `@media (max-width: 900px)`. That is the correct DESIGN. The number was the problem:
901 was measured on one machine's fonts, so the rule fired exactly one pixel below the width
at which the row ran out of room.

The row's width is font metrics all the way down, and a CI runner does not have a Mac's fonts.
Measured on the same commit, same page, same viewport:

    item            macOS    ubuntu-latest
    model-pick      281.8    313.0
    edition-pill     82.3     89.1
    .acct           118.5    125.3
    ROW MIN-CONTENT   646      673
    VIEWPORT NEEDED   901      921

So every viewport from 901 to 921 scrolled the page sideways on Linux, which DESIGN_SYSTEM s10
forbids at any width, while passing on the machine the number came from. It was found by
#975's guard asserting `scrollWidth == clientWidth` at 919 and going red only in CI.

A test that asks "does the row fit at breakpoint+1" would have passed on macOS with 7px to
spare and reported nothing, which is exactly how the bug shipped. So this one measures the
MARGIN and requires it to be a real fraction of the row rather than a rounding error. 10%
covers the 4.2% spread between the two font stacks measured above with room left over, and
being a ratio it tracks the row's own content instead of being one more pixel count to re-tune.

It reads the breakpoint OUT OF THE CSS rather than hard-coding it, so moving the rule moves the
test with it and the two can never describe different layouts (the same trick #639 uses to keep
its two media queries sharing one number).

It SERVES ITSELF on an ephemeral port, for the reason #975's guard does: scripts/mutate_guards.py
runs a guard inside a `git archive` scratch tree with one edit applied, and a guard pointed at an
already-running localhost would measure the unmutated working tree and prove nothing.

Run:  python3 tests/e2e_982_topbar_headroom.py              # serves itself
      python3 tests/e2e_982_topbar_headroom.py https://dbsearch.ai
"""
import contextlib
import os
import re
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE = sys.argv[1] if len(sys.argv) > 1 else None

# The margin the row must keep at the narrowest width that still shows every item, as a
# fraction of the row's own min-content width. See the docstring for where 10% comes from.
MIN_HEADROOM_RATIO = 0.10

# Dense around the breakpoint, because a band 20px wide is exactly what the device-width
# sweeps everyone writes step straight over, plus the widths DESIGN_SYSTEM s10 names.
SWEEP = [360, 390, 430, 768, 820, 900, 901, 919, 920, 960, 1000, 1001, 1024, 1280, 1440]

passed, failed = [], []


def check(name, ok, detail=""):
    """A failure carries its NUMBERS into the summary block, not just its name.

    scripts/run_tests.py reports a failing file by quoting that block, and a block of bare
    names told CI "at 1001px the full row keeps >=10% of itself in headroom" with no headroom
    in it - which needed another round trip to turn into a number. What failed is half the
    report; by how much is the other half.
    """
    (passed if ok else failed).append(name if ok or not detail else name + "  -- " + detail)
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  -- " + detail) if detail and not ok else ""))


def _free_port():
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _origin():
    if BASE:
        yield BASE.rstrip("/")
        return
    os.environ.setdefault("SELFHOST_BACKEND", "memory")
    os.environ.setdefault("DBSEARCH_DEV_AUTH", "1")
    os.environ.setdefault("DBSEARCH_DEV_SEED", "1")
    from _e2eserver import serving                      # noqa: E402
    from dbsearch.server.app import app                 # noqa: E402

    port = _free_port()
    with serving(app, "127.0.0.1", port):
        yield "http://127.0.0.1:%d" % port


def shed_breakpoint(page, origin):
    """The width at or below which the topbar drops its two informational items, from the CSS.

    Hard-coding it here would let the stylesheet move without the test noticing, which is the
    failure this file exists to prevent, one level up.
    """
    css = page.request.get(origin + "/static/css/app.css").text()
    m = re.search(r"@media \(max-width: (\d+)px\) \{[^{}]*\{[^{}]*\}[^{}]*"
                  r"\.topbar \.edition-pill", css)
    if not m:
        m = re.search(r"@media \(max-width: (\d+)px\) \{\s*\.topbar \.edition-pill", css)
    assert m, "no media query sheds .topbar .edition-pill - has the rule moved?"
    return int(m.group(1))


# `width: min-content` is the browser's own answer to "how narrow can this row get", which is
# the number the breakpoint has to clear. Reading it beats summing the children: it accounts for
# the gaps, the padding and each item's own minimum without this test restating the layout.
MEASURE = """() => {
  const t = document.querySelector('.topbar');
  const d = document.documentElement;
  const avail = t.getBoundingClientRect().width;
  const prev = t.style.width;
  t.style.width = 'min-content';
  const need = t.getBoundingClientRect().width;
  t.style.width = prev;
  const pill = document.querySelector('.topbar .edition-pill');
  return {avail: avail, need: need, headroom: avail - need,
          clientWidth: d.clientWidth, scrollWidth: d.scrollWidth,
          pillShown: !!pill && getComputedStyle(pill).display !== 'none'};
}"""


def measure(page, w):
    page.set_viewport_size({"width": w, "height": 844})
    page.wait_for_timeout(80)          # let resize handlers and layout settle before reading
    return page.evaluate(MEASURE)


def main():
    from playwright.sync_api import sync_playwright

    with _origin() as origin:
        url = origin + "/canvas"
        print("#982 topbar-headroom guard  ->  " + url)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 844})
            try:
                page.goto(url, wait_until="load")
                # The account control is the widest variable in the row and identity.js sizes it
                # from the options it fetches, so measuring before those land measures a row that
                # never existed. Wait for the control, not for a number of milliseconds.
                page.wait_for_selector(".topbar .acct", timeout=30000)
                page.wait_for_function(
                    "() => { const s = document.querySelector('.topbar .model-pick select');"
                    "  return !!s && s.options.length > 0; }", timeout=30000)

                bp = shed_breakpoint(page, origin)
                print("shed breakpoint read from app.css: %dpx\n" % bp)

                # Both sides of the rule, so the test pins the layout and not just one width.
                at = measure(page, bp)
                check("at %dpx the informational items are shed" % bp, not at["pillShown"],
                      str(at))

                full = measure(page, bp + 1)
                check("at %dpx the full row is back" % (bp + 1), full["pillShown"], str(full))

                floor = full["need"] * MIN_HEADROOM_RATIO
                check("at %dpx the full row keeps >=%d%% of itself in headroom" % (
                          bp + 1, MIN_HEADROOM_RATIO * 100),
                      full["headroom"] >= floor,
                      "needs %.1fpx, has %.1fpx available, headroom %.1fpx (floor %.1fpx) - "
                      "a margin this thin is a coincidence, not a breakpoint"
                      % (full["need"], full["avail"], full["headroom"], floor))

                # The property s10 actually states, across the band and the named widths.
                for w in SWEEP:
                    m = measure(page, w)
                    check("%d: no horizontal scroll (s10)" % w,
                          m["scrollWidth"] == m["clientWidth"],
                          "scrollWidth %s vs clientWidth %s" % (m["scrollWidth"], m["clientWidth"]))
            finally:
                browser.close()

    print("\n%d passed, %d failed" % (len(passed), len(failed)))
    if failed:
        print("FAILED:")
        for f in failed:
            print("  - " + f)
        return 1
    print("\n#982 TOPBAR-HEADROOM GUARD PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
