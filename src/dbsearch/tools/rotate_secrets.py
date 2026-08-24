"""#832 - the secrets-store rotation step, runnable inside the api container.

Usage, from the runbook (docs + card #832):

    python -m dbsearch.tools.rotate_secrets            # re-encrypt every blob under the
                                                       # PRIMARY key (needs _OLD set for
                                                       # blobs written under the old key)
    python -m dbsearch.tools.rotate_secrets --verify   # read-only: prove every blob
                                                       # decrypts under the PRIMARY ALONE,
                                                       # i.e. the old key is safe to drop

Reads the same env the server reads (DBSEARCH_SECRET_KEY, DBSEARCH_SECRET_KEY_OLD,
DBSEARCH_SECRET_FILE), so what this tool proves is what the next boot will experience.
Exit codes: 0 = clean, 1 = at least one blob unreadable (named on stdout, by handle only -
values are never printed).
"""
from __future__ import annotations

import argparse
import os
import sys

from dbsearch.adapters.local import EncryptedFileSecrets


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verify", action="store_true",
                    help="read-only: check every blob decrypts under the PRIMARY key alone")
    ap.add_argument("--file", default=os.environ.get(
        "DBSEARCH_SECRET_FILE", "/var/lib/dbsearch/secrets.json"))
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"no store at {args.file} - nothing to rotate")
        return 0

    if args.verify:
        # PRIMARY alone: ignore any configured old key, because the question --verify
        # answers is "may the old key be removed?".
        store = EncryptedFileSecrets(args.file, old_keys=[])
        unreadable = []
        for handle in sorted(store._load()):
            try:
                store.get_secret(handle)
            except Exception:
                unreadable.append(handle)
        total = len(store._load())
        if unreadable:
            print(f"VERIFY FAILED: {len(unreadable)}/{total} blob(s) do NOT decrypt under "
                  "the primary key alone - do not drop DBSEARCH_SECRET_KEY_OLD yet:")
            for h in unreadable:
                print(f"  {h}")
            return 1
        print(f"VERIFY OK: all {total} blob(s) decrypt under the primary key alone. "
              "DBSEARCH_SECRET_KEY_OLD may be removed.")
        return 0

    store = EncryptedFileSecrets(args.file)
    report = store.reencrypt_all()
    print(f"{report['reencrypted']} re-encrypted, {len(report['unreadable'])} unreadable")
    for h in report["unreadable"]:
        print(f"  UNREADABLE (left untouched): {h}")
    return 1 if report["unreadable"] else 0


if __name__ == "__main__":
    sys.exit(main())
