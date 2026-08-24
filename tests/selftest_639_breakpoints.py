"""#639: at every width, the sources panel must never hide the answer it is about.

THE MEASUREMENTS BEHIND THESE NUMBERS were taken by driving real viewports with Playwright,
after Claude-in-Chrome's resize_window turned out to report success while innerWidth stayed at
1745 - so none of this responsive CSS had ever actually been rendered when it was written.
Three defects came out of finally looking:

  1. Between 640 and 1199 the panel was a 360px SIDE panel that did not push, so it sat on top
     of the answer. That is #636 all over again, in the band nobody had opened.
  2. The first fix (sheet below 1200) was worse at 1024x800: a 70vh sheet left NINE pixels of
     the answer visible. Pushing there costs the rail's 248px plus the panel's 376px and still
     leaves a ~372px column, which is narrow but readable - both visible beats one hidden.
  3. On a 390x844 phone the rail was 294px TALL, because #631's thread list stacked into the
     wrapping horizontal nav strip. A third of the screen gone before the answer began.

THE RULE, which is what this file actually pins: there are exactly two layouts and no gap
between them. At or above the threshold the reading column makes room and the panel sits
beside it; below it the panel takes the bottom of the screen. Overlapping is not a third
state, and the two media queries must therefore share one number.

    python3 tests/selftest_639_breakpoints.py
"""
import os
import re
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)


def _media_block(css: str, marker: str) -> str:
    """The full body of a media query, by BRACE COUNTING.

    Splitting on the first "\n}" reads only as far as the first nested rule's closing brace,
    which silently truncated this file's own assertions on its first run - the rule it was
    looking for was three rules further down and present all along. A test that cannot see
    the thing it checks fails identically to one whose subject is missing.
    """
    at = css.index(marker) + len(marker)
    at = css.index("{", at)
    depth, i = 0, at
    while i < len(css):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[at:i]
        i += 1
    raise AssertionError(f"unbalanced braces after {marker}")


def _css(path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    return r.text


def test_the_push_and_sheet_thresholds_leave_no_gap():
    """A gap between them is not a cosmetic issue: it is the band where the panel covered the
    answer for every viewport between 640 and 1199."""
    css = _css("/static/css/app.css")
    push = re.search(r"@media \(min-width: (\d+)px\) \{\s*[^}]*has-sources-panel", css)
    sheet = re.search(r"@media \(max-width: (\d+)px\) \{\s*\.sources-panel", css)
    assert push, "no min-width rule pushes the reading column"
    assert sheet, "no max-width rule turns the panel into a sheet"
    push_at, sheet_to = int(push.group(1)), int(sheet.group(1))
    assert sheet_to == push_at - 1, (
        f"the sheet stops at {sheet_to}px and the push starts at {push_at}px - every width "
        "in between gets a side panel that does not push, which lands on top of the answer")
    print(f"  PASS  sheet <= {sheet_to}px, push >= {push_at}px: no overlapping band")


def test_the_push_reserves_more_than_the_panel_is_wide():
    """Or the panel still overlaps the column it supposedly made room in."""
    css = _css("/static/css/app.css")
    width = int(re.search(r"\.sources-panel \{[^}]*?width: (\d+)px", css, re.S).group(1))
    pad = int(re.search(r"has-sources-panel \{ padding-right: (\d+)px", css).group(1))
    assert pad >= width, f"the column is padded {pad}px for a {width}px panel"
    print(f"  PASS  {pad}px of room reserved for a {width}px panel")


def test_the_phone_rail_does_not_carry_the_thread_list():
    """Measured at 390x844: with the slot visible the nav strip was 294px tall - a third of
    the screen - because it wraps horizontally there. Hiding it took the rail to 94px."""
    css = _css("/static/css/rail.css")
    block = _media_block(css, "@media (max-width: 760px)")
    assert ".navrail-slot { display: none; }" in block, (
        "the thread list renders inside the wrapping mobile nav strip again - it made the "
        "navigation 294px tall on an 844px phone")
    print("  PASS  the phone nav strip does not stack the thread list")


def test_the_phone_loses_no_horizontal_room_to_the_rail_band():
    """The band is a left-hand stripe. On a phone the rail is a full-width strip on TOP, so a
    left stripe would be a dark column beside a page that has no sidebar."""
    css = _css("/static/css/rail.css")
    block = _media_block(css, "@media (max-width: 760px)")
    assert "--rail-reserve: 0px" in block, "the phone still reserves a nav column"
    assert "background-image: none" in block, "the dark band still paints on a phone"
    print("  PASS  no reserved column and no band at phone widths")


def test_the_sheet_is_sized_for_what_is_left_of_the_screen():
    """70vh left nine pixels of the answer, 60vh left twelve. The number is load-bearing."""
    css = _css("/static/css/app.css")
    block = re.search(r"@media \(max-width: \d+px\) \{\s*\.sources-panel \{(.*?)\}", css, re.S)
    assert block, "no sheet rule found"
    h = re.search(r"height: (\d+)vh", block.group(1))
    assert h and int(h.group(1)) <= 55, (
        f"the sheet is {h.group(1) if h else '?'}vh - measured on a 390x844 phone, anything "
        "above ~55vh leaves almost none of the answer on screen")
    print(f"  PASS  the sheet is {h.group(1)}vh, sized to what the phone has left")


def test_opening_the_panel_keeps_the_answer_in_view_when_it_can():
    js = client.get("/static/js/ui/components.js").text
    assert "keepAnchorVisible" in js, (
        "nothing scrolls the answer back into view when the sheet opens over it")
    body = js.split("function keepAnchorVisible", 1)[1][:700]
    assert "innerWidth" in body, "the helper does not distinguish sheet mode from side-panel mode"
    print("  PASS  the sheet scrolls its answer into the strip that remains")


if __name__ == "__main__":
    test_the_push_and_sheet_thresholds_leave_no_gap()
    test_the_push_reserves_more_than_the_panel_is_wide()
    test_the_phone_rail_does_not_carry_the_thread_list()
    test_the_phone_loses_no_horizontal_room_to_the_rail_band()
    test_the_sheet_is_sized_for_what_is_left_of_the_screen()
    test_opening_the_panel_keeps_the_answer_in_view_when_it_can()
    print("\nBREAKPOINT SELF-TEST PASSED.")
