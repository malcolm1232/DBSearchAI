"""#961 - the brand mark reaches every surface that asks for an icon.

dbsearch.ai shipped for months serving `create-next-app`'s stock favicon: a black disc
holding the VERCEL triangle. It was never noticed because a favicon is the one asset
nothing links to and no test renders - the browser fetches /favicon.ico on its own
initiative, and a wrong-but-present icon looks exactly like a right one in every check
that only asserts 200.

So this file does not ask "is there an icon". It asks:

  * is it OUR icon - the tile is the design system's ink, the way the approved LinkedIn
    mark is, and specifically NOT the stock file (pinned by hash, so re-running
    create-next-app or restoring app/favicon.ico turns this red rather than quiet)
  * does it survive the SIZES a browser actually asks for - a .ico carrying only a 256px
    frame is what you get if the small frames are dropped, and it is a smudge in a tab
  * does the phone case work at all - apple-touch-icon and a manifest whose icon entries
    RESOLVE, which is the half that was entirely missing and the half the owner reported
  * does it work for a SELF-HOSTER, who has no site/out on disk, which is why these are
    FastAPI routes and not files in the Next export

The self-hoster case is the one that dictates the shape of the test: DBSEARCH_SITE_DIR is
pointed at an empty directory BEFORE the app is imported, so the marketing mount is never
registered. Every assertion below therefore runs against the app alone. If the icons only
worked because the export happened to be built, this file would go red.

    PYTHONPATH=src python3 tests/selftest_961_brand_icons.py
"""
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

# BEFORE the import below: app.py reads this at module scope to decide whether to mount
# the export. An empty dir here IS the self-hoster, and it means every pass below is
# testing the routes rather than the Next build sitting in the working tree.
_EMPTY = tempfile.mkdtemp(prefix="dbs961-no-site-")
os.environ["DBSEARCH_SITE_DIR"] = _EMPTY

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)

STATIC = ROOT / "src" / "dbsearch" / "server" / "static"

#: Design-system ink / paper, and the two colours the approved LinkedIn mark is built from
#: (brand_linkedin/dbs_logo_linkedin_400.png). Sampled, not typed: see make_brand_icons.py.
INK = (22, 22, 26)
PAPER = (250, 249, 247)

#: sha256 of `create-next-app`'s stock favicon.ico - the Vercel triangle - as actually
#: served by https://dbsearch.ai/favicon.ico on 2026-08-26, before this card. Pinned so the
#: specific regression that caused #961 cannot come back silently.
STOCK_NEXT_FAVICON_SHA256 = (
    "2b8ad2d33455a8f736fc3a8ebf8f0bdea8848ad4c0db48a2833bd0f9cd775932"
)

#: The frames a browser picks between: 16 for a tab, 32 for a retina tab and the bookmark
#: bar, 48 for Windows' shortcut, 256 for everything that scales up.
EXPECTED_ICO_FRAMES = {16, 32, 48, 256}

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" - {detail}" if detail else ""))
        failures.append(label)


def _ico_frames(payload: bytes) -> dict[int, bytes]:
    """Parse an .ico into {edge_px: frame_bytes} without PIL.

    Hand-parsed on purpose: the whole defect class here is "the file is present and
    wrong", and a decoder that silently picks one frame would hide exactly the case where
    the small ones went missing.
    """
    if payload[:4] != b"\x00\x00\x01\x00":
        raise ValueError("not an ICO")
    count = struct.unpack("<H", payload[4:6])[0]
    frames = {}
    for i in range(count):
        off = 6 + i * 16
        w, _h, _c, _r, _p, _bpp, size, data_off = struct.unpack(
            "<BBBBHHII", payload[off:off + 16]
        )
        frames[w or 256] = payload[data_off:data_off + size]
    return frames


def _png_size(payload: bytes) -> tuple[int, int]:
    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", payload[16:24])


def _corner_and_brightest(payload: bytes) -> tuple[tuple, tuple]:
    from PIL import Image
    im = Image.open(BytesIO(payload)).convert("RGB")
    w, h = im.size
    return im.getpixel((0, 0)), max(im.getdata(), key=sum)


def _near(a: tuple, b: tuple, tol: int = 6) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# ── 1. the icon is served, and it is OURS ───────────────────────────────────────────
print("\n[1] /favicon.ico is the DBSearch mark, not the stock one")

r = client.get("/favicon.ico")
check("200 with no site/out on disk (the self-hoster)", r.status_code == 200,
      f"got {r.status_code}")
check("declared as an icon type",
      "icon" in r.headers.get("content-type", ""), r.headers.get("content-type", ""))

ico = r.content
# Parsed defensively rather than optimistically: on the pre-#961 tree this route 404s and
# the body is a JSON error, and a test that raises there reports one crash instead of the
# dozen things that are actually wrong.
try:
    frames = _ico_frames(ico)
except ValueError as exc:
    check("served bytes are a real multi-frame .ico", False, f"{exc} (first 8: {ico[:8]!r})")
    frames = {}
else:
    check("served bytes are a real multi-frame .ico", True)

check(f"carries every frame a browser asks for {sorted(EXPECTED_ICO_FRAMES)}",
      EXPECTED_ICO_FRAMES.issubset(set(frames)), f"has {sorted(frames)}")

# The tile, at every size. A stock Vercel icon fails this on the corner alone: it is a
# disc on transparency, so its corner is not ink.
for edge in sorted(EXPECTED_ICO_FRAMES & set(frames)):
    corner, brightest = _corner_and_brightest(frames[edge])
    check(f"{edge}px tile is design-system ink {INK}", _near(corner, INK), str(corner))
    # And the mark actually reaches paper white rather than averaging to grey, which is
    # what an un-emboldened serif does when it is downsampled to a tab (#961's small
    # sizes render at a wider target width and with a synthetic stroke for this reason).
    check(f"{edge}px mark reaches paper", _near(brightest, PAPER, tol=12), str(brightest))

# The same regression, pinned at the FILE level as well as at the route.
#
# This is not redundant with the hash check on the response, which is vacuous exactly when
# it matters most: on the pre-#961 tree the route does not exist, so the bytes above are a
# JSON 404 that trivially "is not the stock favicon" and the assertion passes while the
# stock file sits in the tree. The committed files are where the defect actually lived.
committed = [p for p in (ROOT / "site").rglob("favicon.ico")
             if "out" not in p.parts and "node_modules" not in p.parts]
committed.append(STATIC / "favicon.ico")
committed = [p for p in committed if p.is_file()]

check("a favicon.ico is committed for the app to serve", (STATIC / "favicon.ico").is_file())
for path in committed:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    check(f"{path.relative_to(ROOT)} is not create-next-app's stock icon",
          digest != STOCK_NEXT_FAVICON_SHA256, "the Vercel triangle is back")
check("the served icon is one of the committed ones",
      any(p.read_bytes() == ico for p in committed), "route and files disagree")

# ── 2. the phone surfaces - the half that did not exist at all ──────────────────────
print("\n[2] apple-touch-icon and the web manifest")

for path, edge in (("/apple-touch-icon.png", 180),
                   ("/apple-touch-icon-precomposed.png", 180),
                   ("/icon-192.png", 192),
                   ("/icon-512.png", 512)):
    r = client.get(path)
    ok = r.status_code == 200
    check(f"{path} served", ok, f"got {r.status_code}")
    if not ok:
        continue
    check(f"{path} is image/png", r.headers.get("content-type") == "image/png",
          r.headers.get("content-type", ""))
    check(f"{path} is {edge}x{edge}", _png_size(r.content) == (edge, edge),
          str(_png_size(r.content)))
    corner, brightest = _corner_and_brightest(r.content)
    check(f"{path} is the same mark", _near(corner, INK) and _near(brightest, PAPER, 12),
          f"corner={corner} brightest={brightest}")

r = client.get("/site.webmanifest")
check("/site.webmanifest served", r.status_code == 200, f"got {r.status_code}")
check("manifest declares a manifest content type",
      "manifest" in r.headers.get("content-type", ""), r.headers.get("content-type", ""))

manifest = json.loads(r.content)
check("manifest names the product", manifest.get("name") == "DBSearch.AI",
      str(manifest.get("name")))
check("manifest has a start_url", bool(manifest.get("start_url")))
check("manifest background is the icon tile", manifest.get("background_color") == "#16161A",
      str(manifest.get("background_color")))

by_size = {i.get("sizes") for i in manifest.get("icons", [])}
check("manifest offers 192 and 512", {"192x192", "512x512"} <= by_size, str(by_size))
purposes = {i.get("purpose") for i in manifest.get("icons", [])}
check("manifest offers a maskable icon (Android crops to a circle)",
      "maskable" in purposes, str(purposes))

# The assertion that makes the rest mean something: an icon a manifest NAMES but that
# does not resolve is a home-screen tile that silently falls back to a letter.
for entry in manifest.get("icons", []):
    src = entry.get("src", "")
    got = client.get(src)
    check(f"manifest icon {src} ({entry.get('purpose')}) resolves",
          got.status_code == 200, f"got {got.status_code}")

# ── 3. every HTML surface actually declares them ────────────────────────────────────
print("\n[3] the shells declare the icons")

# link_gone.html is deliberately excluded from the manifest: it is a 404, and a manifest
# there invites an install prompt on an error page.
SHELLS_WITH_MANIFEST = ("index.html", "signin.html", "visitor.html")
SHELLS = SHELLS_WITH_MANIFEST + ("link_gone.html",)

seen_versions: set[str] = set()

for name in SHELLS:
    html = (STATIC / name).read_text(encoding="utf-8")
    check(f"{name} declares rel=icon",
          re.search(r'<link[^>]+rel="icon"[^>]+href="/favicon\.ico\?v=', html) is not None)
    check(f"{name} declares apple-touch-icon",
          re.search(r'rel="apple-touch-icon"[^>]+href="/apple-touch-icon\.png\?v=',
                    html) is not None)
    seen_versions.update(re.findall(r'/(?:favicon\.ico|apple-touch-icon\.png)\?v=([0-9a-f]+)',
                                    html))
    check(f"{name} tints the address bar for both schemes",
          html.count('name="theme-color"') == 2, str(html.count('name="theme-color"')))
    has_manifest = 'rel="manifest"' in html
    if name in SHELLS_WITH_MANIFEST:
        check(f"{name} links the manifest", has_manifest)
    else:
        check(f"{name} does NOT link the manifest (it is a 404 page)", not has_manifest)

# ── 3b. the cache-busting token ─────────────────────────────────────────────────────
print("\n[3b] the ?v= token that gets a returning visitor the new mark")

# This exists because the first prod deploy of #961 did NOT fix the reported symptom.
# The server was serving the right bytes - proven four ways - and every already-open tab
# still drew the Vercel triangle, because Chrome keys its favicon cache by icon URL and
# nothing had asked for a URL it had not already answered. The owner is a RETURNING
# visitor; a fix only new visitors can see is not the fix they asked for.
layout = (ROOT / "site" / "app" / "layout.tsx").read_text(encoding="utf-8")
seen_versions.update(
    re.findall(r'/(?:favicon\.ico|apple-touch-icon\.png)\?v=([0-9a-f]+)', layout))

check("every surface carries an icon version", bool(seen_versions))
check("every surface agrees on ONE version", len(seen_versions) == 1, str(seen_versions))
for v in seen_versions:
    check(f"version {v} is a real content hash, not the placeholder",
          v != "0" * len(v) and len(v) == 10, v)
# The token must track the BYTES - a stamp nobody re-derives is the "number in prose"
# failure. Section [5] re-runs the generator in --check mode, which recomputes this hash
# from the committed icons and reports the file stale if it disagrees.


# ── 4. the marketing export declares them too ───────────────────────────────────────
print("\n[4] the Next export")

export_index = ROOT / "site" / "out" / "index.html"
if not export_index.is_file():
    print("  skip site/out not built - run `npm run build` in site/")
    ledger = os.environ.get("DBS_SKIP_LEDGER")
    if ledger:
        Path(ledger).open("a").write(f"{Path(__file__).name}: site/out not built\n")
else:
    html = export_index.read_text(encoding="utf-8")
    check("export declares rel=icon with sizes=any (not a single 256 frame)",
          re.search(r'rel="icon"[^>]+sizes="any"', html) is not None,
          "Next's app/favicon.ico convention emits sizes=\"256x256\"; declare it by hand")
    export_versions = set(
        re.findall(r'/(?:favicon\.ico|apple-touch-icon\.png)\?v=([0-9a-f]+)', html))
    check("export carries the same icon version as the app shells",
          export_versions == seen_versions, f"export={export_versions} app={seen_versions}")
    check("export declares apple-touch-icon", 'rel="apple-touch-icon"' in html)
    check("export links the manifest", 'rel="manifest"' in html)
    check("export tints the address bar for both schemes",
          html.count('name="theme-color"') == 2)
    export_ico = ROOT / "site" / "out" / "favicon.ico"
    check("export ships a favicon", export_ico.is_file())
    if export_ico.is_file():
        check("export favicon is byte-identical to the one the app serves",
              export_ico.read_bytes() == (STATIC / "favicon.ico").read_bytes())
    # The stock Next starter assets were public at dbsearch.ai/vercel.svg.
    for stray in ("vercel.svg", "next.svg"):
        check(f"export no longer ships {stray}",
              not (ROOT / "site" / "out" / stray).is_file())

# ── 5. the committed icons match a fresh render ─────────────────────────────────────
print("\n[5] committed icons are what the generator produces")
# Cheap guard against someone hand-editing a PNG: the generator is the source of truth,
# and --check re-renders and compares without writing.
import subprocess  # noqa: E402
proc = subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "make_brand_icons.py"), "--check"],
    capture_output=True, text=True, cwd=str(ROOT),
)
if "Instrument Serif not found" in (proc.stdout + proc.stderr):
    print("  skip generator needs site/ built for the font")
else:
    check("committed icons match a fresh render", proc.returncode == 0,
          proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "")


print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
