#!/usr/bin/env python3
"""Render the DBSearch.AI brand mark into the favicon / PWA / apple-touch icon set.

#961. The site shipped with `create-next-app`'s stock favicon (a black circle holding
the Vercel triangle), so every tab and every phone home-screen tile carried somebody
else's logo. Nothing declared an `apple-touch-icon` or a web manifest either, so a
pinned tab on a phone got a generated letter tile rather than a mark.

THE MARK IS NOT INVENTED HERE. Its geometry and colours are lifted from the approved
LinkedIn asset (`brand_linkedin/dbs_logo_linkedin_400.png`), measured rather than
guessed:

    tile        #16161A   design-system "Ink"
    letters     #FAF9F7   design-system "Paper"
    "DB"        48% of the tile width, optically centred

so the tab, the phone tile and the LinkedIn page cannot drift apart.

OPTICAL SIZING. 48% is right for a 400px LinkedIn tile and wrong for a 16px tab, where
two serif letters at 48% collapse into a smudge. Small renders therefore scale the mark
up (`_TARGET_WIDTH`); the tile, the colours and the centring never change. This is the
same reason a typeface ships separate display and caption cuts.

Regenerating (only needed if the mark itself changes - the outputs are committed):

    python3 scripts/make_brand_icons.py

The display face is Instrument Serif (SIL OFL), the site's h1/h2 face - see
`site/lib/fonts.ts`. It is NOT vendored: Next.js already downloads it during
`npm run build`, so this script lifts it out of the build output and unpacks the
woff2 with fontTools. Build the site first, or pass `--font path/to.ttf`.
"""
from __future__ import annotations

import argparse
import glob
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent

#: Sampled from brand_linkedin/dbs_logo_linkedin_400.png, not typed from the design doc,
#: so the icon tracks the asset that actually shipped.
INK = (22, 22, 26)        # #16161A
PAPER = (250, 249, 247)   # #FAF9F7

MARK = "DB"

#: Fraction of the tile the mark's bounding box should span, by output size. The 0.48
#: entry is the measured LinkedIn geometry; the wider small sizes are the optical
#: correction described above.
_TARGET_WIDTH = {16: 0.86, 32: 0.74, 48: 0.62}
_DEFAULT_TARGET_WIDTH = 0.48

#: Synthetic emboldening, as a fraction of the tile, applied before downsampling.
#: A serif's hairlines are thinner than one pixel at tab sizes, so averaging renders
#: them mid-grey and the mark reads as a smudge rather than as letters: measured, an
#: unemboldened 16px tile peaks at 234/255 instead of reaching paper white. Widening
#: the strokes at supersampled resolution puts real ink under each pixel. Sizes big
#: enough to carry the hairlines get none, so the mark stays exactly the LinkedIn one.
_STROKE = {16: 0.006, 32: 0.003}


def _target_width(size: int) -> float:
    return _TARGET_WIDTH.get(size, _DEFAULT_TARGET_WIDTH)


def find_font() -> Path:
    """Locate Instrument Serif Regular inside the Next.js build output.

    Next fingerprints and subsets the file, so it cannot be named directly; every woff2
    in the media directory is opened and matched on its internal name table instead.
    """
    roots = [REPO / "site" / "out" / "_next" / "static" / "media",
             REPO / "site" / ".next" / "static" / "media"]
    from fontTools.ttLib import TTFont

    for root in roots:
        for path in sorted(glob.glob(str(root / "*.woff2"))):
            try:
                font = TTFont(path, lazy=True)
                names = {r.nameID: str(r) for r in font["name"].names}
                family = names.get(16) or names.get(1) or ""
                style = names.get(17) or names.get(2) or ""
                cmap = font.getBestCmap()
            except Exception:
                continue
            if family.strip() != "Instrument Serif" or style.strip() != "Regular":
                continue
            # A subset font can carry the right name and still be missing the two
            # letters we draw, which would render as .notdef boxes.
            if all(ord(ch) in cmap for ch in MARK):
                return Path(path)
    raise SystemExit(
        "Instrument Serif not found. Run `npm run build` in site/, or pass --font."
    )


def load_truetype(path: Path, tmpdir: Path) -> Path:
    """Return a .ttf path, unpacking a woff2 through fontTools when needed."""
    if path.suffix.lower() != ".woff2":
        return path
    from fontTools.ttLib import TTFont

    font = TTFont(str(path))
    font.flavor = None  # drop woff2 compression -> plain TrueType
    out = tmpdir / "InstrumentSerif-Regular.ttf"
    font.save(str(out))
    return out


def _fit_font(ttf: Path, size: int, target_px: float) -> tuple[ImageFont.FreeTypeFont,
                                                               tuple[int, int, int, int]]:
    """Pick the point size whose rendered mark is `target_px` wide.

    Measured by binary search on the real ink bbox rather than computed from font
    metrics: metrics describe the em box and advance widths, which include side
    bearings the eye does not see, so a metrics-derived size lands the mark visibly
    small and off-centre.
    """
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lo, hi = 1.0, size * 4.0
    best: tuple[ImageFont.FreeTypeFont, tuple[int, int, int, int]] | None = None
    for _ in range(40):
        mid = (lo + hi) / 2
        font = ImageFont.truetype(str(ttf), max(1, int(round(mid))))
        bbox = probe.textbbox((0, 0), MARK, font=font)
        width = bbox[2] - bbox[0]
        best = (font, bbox)
        if width > target_px:
            hi = mid
        else:
            lo = mid
    assert best is not None
    return best


def render(ttf: Path, size: int, *, supersample: int = 8) -> Image.Image:
    """Render one square tile.

    Drawn at `supersample`x and reduced with LANCZOS. Rendering a 16px glyph directly
    hands the whole job to the hinter, which snaps a serif's thin strokes onto the pixel
    grid and drops them; downsampling a large clean render keeps them as grey.
    """
    hi_res = size * supersample
    img = Image.new("RGB", (hi_res, hi_res), INK)
    draw = ImageDraw.Draw(img)

    font, bbox = _fit_font(ttf, hi_res, hi_res * _target_width(size))
    stroke = int(round(hi_res * _STROKE.get(size, 0.0)))
    if stroke:
        # The stroke grows the mark, so re-measure: fitting on the unstroked bbox and
        # then emboldening would overshoot the target width and clip at the tile edge.
        bbox = draw.textbbox((0, 0), MARK, font=font, stroke_width=stroke)
    # textbbox is relative to the anchor, so subtracting it places the INK, not the
    # em box, at the tile centre - the difference the LinkedIn asset shows as a
    # mark centred to within one pixel.
    mark_w, mark_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (hi_res - mark_w) / 2 - bbox[0]
    y = (hi_res - mark_h) / 2 - bbox[1]
    draw.text((x, y), MARK, font=font, fill=PAPER,
              stroke_width=stroke, stroke_fill=PAPER)

    return img.resize((size, size), Image.LANCZOS)


def write_ico(path: Path, images: list[Image.Image]) -> None:
    """Write a multi-resolution .ico.

    Hand-assembled rather than via PIL's `save(sizes=...)`, which re-scales from a
    single source; each entry here is its own optically-sized render.
    """
    images = sorted(images, key=lambda im: im.width)
    payloads = []
    for im in images:
        from io import BytesIO
        buf = BytesIO()
        # RGBA, not RGB. The tile is fully opaque either way, but Turbopack's ICO
        # decoder rejects a non-RGBA PNG outright ("The PNG is not in RGBA format!")
        # and fails the site build, so the channel is not optional here.
        im.convert("RGBA").save(buf, format="PNG")  # PNG-in-ICO: fine since IE11
        payloads.append(buf.getvalue())

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for im, payload in zip(images, payloads):
        dim = 0 if im.width >= 256 else im.width  # 0 means 256 in the ICO format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(payload), offset)
        offset += len(payload)
        blobs += payload
    path.write_bytes(header + entries + blobs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--font", type=Path, help="Instrument Serif .ttf/.otf/.woff2")
    ap.add_argument("--check", action="store_true",
                    help="verify committed icons match a fresh render; write nothing")
    args = ap.parse_args()

    tmpdir = REPO / "site" / ".icon-build"
    tmpdir.mkdir(parents=True, exist_ok=True)
    ttf = load_truetype(args.font or find_font(), tmpdir)

    site_public = REPO / "site" / "public"
    app_static = REPO / "src" / "dbsearch" / "server" / "static"
    for d in (site_public, app_static):
        d.mkdir(parents=True, exist_ok=True)

    ico_sizes = [16, 32, 48, 256]
    ico_images = [render(ttf, s) for s in ico_sizes]

    # The PNG set. 180 is apple-touch-icon; 192/512 are the manifest's two required
    # entries. Both manifest sizes double as `purpose: maskable` - the mark sits at 48%
    # of the tile, well inside the 80% safe circle Android crops to, on a full-bleed
    # tile, so no separate maskable asset is needed.
    #
    # Written ONCE, into the app's static dir. server/app.py routes them at the root, so
    # that single copy answers for the app shell, for the marketing site and for a
    # self-hoster alike - nothing is mirrored into site/public, because a second copy
    # that nothing compares is how a rebrand ends up half-applied.
    png_sizes = {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180}

    written: list[tuple[Path, bytes]] = []

    from io import BytesIO
    for name, size in png_sizes.items():
        buf = BytesIO()
        render(ttf, size).save(buf, format="PNG", optimize=True)
        written.append((app_static / name, buf.getvalue()))

    ico_tmp = tmpdir / "favicon.ico"
    write_ico(ico_tmp, ico_images)
    ico_bytes = ico_tmp.read_bytes()
    # The one deliberate duplicate: the export carries its own favicon so it is correct
    # standalone, and app_static holds what FastAPI actually serves.
    #
    # site/PUBLIC, not site/app. app/favicon.ico is Next's file convention, and it
    # generates the link tag itself - reading only the LARGEST frame out of the .ico and
    # emitting sizes="256x256". That advertises a 16px tab icon as a 256px one and throws
    # away the optically-sized small frames this script exists to produce. Kept in public/
    # (copied verbatim, no tag generated) and declared by hand in layout.tsx as
    # sizes="any", which is what a multi-resolution .ico is supposed to say.
    written.append((site_public / "favicon.ico", ico_bytes))
    written.append((app_static / "favicon.ico", ico_bytes))

    if args.check:
        stale = [p for p, payload in written
                 if not p.exists() or p.read_bytes() != payload]
        for p in stale:
            print(f"STALE {p.relative_to(REPO)}")
        print(f"{len(written) - len(stale)}/{len(written)} icons up to date")
        return 1 if stale else 0

    for path, payload in written:
        path.write_bytes(payload)
        print(f"wrote {path.relative_to(REPO)} ({len(payload)}b)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
