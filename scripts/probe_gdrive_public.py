"""#712 - LIVE probe of the public-Drive path. Not a test: tests/ stays offline-green.

Falsifies the three beliefs the FakeDrive rig merely ENCODES. A fake agrees with its
author by construction; only Google can disagree:

  1. an API key reads a public file at all
  2. it lists a folder's children, and recurses into subfolders
  3. files.export returns text for a native Google Doc under an API key   <- likeliest wrong

Belief 3 is the one the whole native-doc branch of fetch_content rests on. If it is false,
that branch is wrong and needs redesign (candidate fallbacks: the public
`export?format=` download endpoint, or treating native docs as unreadable-with-count in
slice 1). Better to learn it here than from a user.

    GOOGLE_API_KEY=... PYTHONPATH=src python3 scripts/probe_gdrive_public.py <folder link>
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.connectors.gdrive import (  # noqa: E402
    _EXPORT, GDriveConnector, _folder_id_from_link,
)
from dbsearch.ports.base import ItemUnreadable  # noqa: E402


def main() -> int:
    key = os.environ.get("GOOGLE_API_KEY", "")
    if not key:
        print("set GOOGLE_API_KEY (an API key restricted to the Drive API)")
        return 2
    link = sys.argv[1] if len(sys.argv) > 1 else input("public Drive folder link: ")

    folder_id = _folder_id_from_link(link)
    print(f"folder id parsed: {folder_id}")
    c = GDriveConnector("probe", folder_id, ["probe-oid"], api_key=key)

    # BELIEF 1 -------------------------------------------------------------------------
    try:
        c.authenticate({})
    except Exception as exc:                       # noqa: BLE001 - a probe reports, never hides
        print(f"BELIEF 1 FAILED: an API key could not reach the folder: {exc}")
        return 1
    print("BELIEF 1 OK: the folder is reachable with an API key alone (no OAuth)")

    # BELIEF 2 -------------------------------------------------------------------------
    items, cursor = c.list_changes(None)
    print(f"BELIEF 2: listed {len(items)} extractable item(s); cursor={cursor!r}")
    for i in items:
        kind = "native" if i["mime"] in _EXPORT else "binary"
        print(f"   [{kind:6s}] {i['mime']:62s} {i['title']}")
    if not items:
        print("BELIEF 2 INCONCLUSIVE: the folder listed nothing extractable. Put a PDF or "
              "a Google Doc in it and re-run - an empty folder proves nothing either way.")
        return 1
    # Recursion is only PROVEN if something came from below the top level. Say so honestly
    # rather than claiming it: a flat folder cannot demonstrate it.
    print("   (recursion is exercised only if this folder has subfolders; add one to prove it)")

    # BINARY DOWNLOAD - deliberately BEFORE the belief-3 gate.
    #
    # It used to sit after it, and that ordering told a lie: on a folder with no native
    # Doc the probe returned early, fetch_content never executed at all, and two runs
    # reported "beliefs 1 and 2 confirmed" while nothing had ever been DOWNLOADED. Listing
    # is not retrieval. A missing native Doc must never again hide whether the connector
    # can fetch bytes at all.
    binary = [i for i in items if i["mime"] not in _EXPORT]
    if binary:
        b = binary[0]
        try:
            raw_b, mime_b = c.fetch_content(b)
        except Exception as exc:                   # noqa: BLE001
            print(f"BINARY DOWNLOAD FAILED for {b['title']!r}: {exc}")
            return 1
        if not raw_b:
            print(f"BINARY DOWNLOAD returned ZERO bytes for {b['title']!r} - a document "
                  "that ingests as empty is a silently-partial store by another route.")
            return 1
        print(f"BINARY DOWNLOAD OK: {b['title']!r} -> {len(raw_b):,} bytes as {mime_b}, "
              f"starts {raw_b[:8]!r}")
    else:
        print("BINARY DOWNLOAD UNTESTED: no non-native file in this folder.")

    # BELIEF 3 - the one worth betting against ------------------------------------------
    native = [i for i in items if i["mime"] in _EXPORT]
    if not native:
        print("BELIEF 3 UNTESTED: no native Google Doc in the folder. This is THE belief "
              "the export branch rests on - add a Google Doc and re-run. Note an UPLOADED "
              ".txt or .docx does NOT count: Drive stores those with real bytes, so they "
              "ride alt=media (proven above). Only a Doc created IN Drive has no bytes.")
        return 1

    target = native[0]
    try:
        raw, mime = c.fetch_content(target)
    except ItemUnreadable as exc:
        print(f"BELIEF 3 FAILED for {target['title']!r}: {exc}")
        print("   The export branch of fetch_content is built on a false assumption. "
              "Record on card #712 and redesign that path before merging.")
        return 1
    except Exception as exc:                       # noqa: BLE001
        print(f"BELIEF 3 FAILED (unexpected) for {target['title']!r}: {exc}")
        return 1

    print(f"BELIEF 3 OK: exported {target['title']!r} -> {len(raw)} bytes as {mime}")
    print(f"   first 200 bytes: {raw[:200]!r}")
    if not raw.strip():
        print("   WARNING: the export succeeded but returned NO TEXT. A document that "
              "ingests as empty is a silently-partial store by another route.")
        return 1

    print("\nALL THREE BELIEFS CONFIRMED against live Drive.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
