"""#791 - the PROD proof: two users, one filename, both documents survive.

Local tests run on the in-memory backend. Prod runs pgvector, where the owner comes back
through `max(owner_oid)` in a GROUP BY - a different code path that no local test touches.
"Done" is only ever proved on prod, so this drives the real site as two real Entra identities
(D2/#822 made bob available; #797 makes the signed-in browser cheap).

WHAT IT PROVES
  1. alice uploads `<name>.txt`; bob uploads a DIFFERENT `<name>.txt` to the SAME tenant
     partition (both are org-wide, so `_request_tenant` resolves to the shared partition and
     `uri` collides at `upload://<name>.txt` - the exact #791 condition).
  2. BOTH documents still exist afterwards. Before the fix, bob's upload deleted alice's.
  3. Neither listing carries an `owner_oid` field (the disclosure half of the fix: ownership
     reaches the client as `owned_by_you`, never as somebody else's identifier).

It cleans up after itself: both documents are deleted at the end, whatever the outcome.

    python scripts/prod_791_two_user_collision.py
    python scripts/prod_791_two_user_collision.py --base https://dbsearch.ai
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pw_dbs_auth import DEFAULT_BASE, DEFAULT_SSH_HOST, authed_pages  # noqa: E402


def _db_rows(ssh_host: str, docs: list) -> dict:
    """Ground truth for the collision precondition, straight from prod's index.

    Cross-VISIBILITY cannot stand in for one partition: `/admin/documents` is ACL-trimmed
    (#549), so two documents can share a partition and still be invisible to each other.
    The supersede loop is partition-scoped and ACL-blind, so `tenant_id` is the fact that
    decides whether the defect could fire. Ask the database.
    """
    ids = ", ".join("'" + d.replace("'", "''") + "'" for d in docs)
    sql = (f"SELECT doc_external_id, tenant_id, uri, owner_oid FROM chunks "
           f"WHERE doc_external_id IN ({ids}) GROUP BY 1,2,3,4;")
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", ssh_host,
         "docker compose -f /opt/dbsearch/docker-compose.yml "
         "-f /opt/dbsearch/docker-compose.prod.yml -p dbsearch exec -T db "
         f'psql -U postgres -d dbsearch -At -F "|" -c "{sql}"'],
        capture_output=True, text=True)
    rows = {}
    for line in out.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) == 4:
            rows[parts[0]] = {"tenant_id": parts[1], "uri": parts[2], "owner_oid": parts[3]}
    return rows


def _listing(page, base: str) -> dict:
    r = page.request.get(f"{base}/admin/documents")
    assert r.ok, f"/admin/documents failed: {r.status} {r.text()[:200]}"
    return {d["doc_external_id"]: d for d in r.json()}


def _upload(page, base: str, filename: str, body: bytes) -> str:
    """Upload as the page's identity, org-wide. Returns the new doc_external_id."""
    r = page.request.post(f"{base}/admin/upload", multipart={
        "file": {"name": filename, "mimeType": "text/plain", "buffer": body},
        "audience": "org",
        "title": filename,
    })
    assert r.ok, f"upload of {filename} failed: {r.status} {r.text()[:300]}"
    out = r.json()
    doc = out.get("external_id") or out.get("doc_external_id")
    assert doc, f"upload returned no document id: {out}"
    return doc


def _delete(page, base: str, doc: str) -> None:
    """#594's route is /documents/{id} (the listing lives at /admin/documents; the mutation
    does not). Owner-only, so each identity removes its own."""
    import urllib.parse
    page.request.delete(f"{base}/documents/{urllib.parse.quote(doc, safe='')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="#791 prod two-user collision drive")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    stamp = time.strftime("%H%M%S")
    filename = f"Report791-{stamp}.txt"
    alice_body = f"ALICE copy {stamp} - quarterly figures alpha".encode()
    bob_body = f"BOB copy {stamp} - completely different content beta".encode()

    print(f"[mint] cookies for alice and bob against {base}")
    alice_doc = bob_doc = None
    failures = []
    with authed_pages(("alice", "bob"), base=base, ssh_host=args.ssh_host) as pages:
        apage, bpage = pages["alice"], pages["bob"]
        try:
            alice_doc = _upload(apage, base, filename, alice_body)
            print(f"[alice] uploaded {filename} -> {alice_doc}")
            assert alice_doc in _listing(apage, base), "alice cannot see her own upload"

            bob_doc = _upload(bpage, base, filename, bob_body)
            print(f"[bob]   uploaded {filename} -> {bob_doc}")
            assert alice_doc != bob_doc, (
                "identical content hashes - the two uploads are the SAME document, so "
                "this run cannot show the bug (make the bodies differ)")

            # THE RIG HAS TO PROVE ITS OWN PRECONDITION. A drive that runs after the fix is
            # deployed passes whether or not it could ever have shown the bug, so assert the
            # two conditions the defect actually needed, measured through the product:
            #   same uri      - both rows report upload://<name>, so the loop's `doc.uri ==
            #                   uri` matched and the ids differ (visible above).
            #   same partition - each user can SEE the other's document. list_doc_acls is
            #                   partition-scoped, so cross-visibility is only possible when
            #                   both documents live in one partition, which is what let the
            #                   owner-blind loop reach across users at all.
            db = _db_rows(args.ssh_host, [alice_doc, bob_doc])
            assert len(db) == 2, (
                f"could not read both documents back from the index: {db}")
            parts = {db[alice_doc]["tenant_id"], db[bob_doc]["tenant_id"]}
            uris = {db[alice_doc]["uri"], db[bob_doc]["uri"]}
            owners = {db[alice_doc]["owner_oid"], db[bob_doc]["owner_oid"]}
            assert uris == {f"upload://{filename}"}, (
                f"the two uploads do not share one uri, so no collision was possible: {uris}")
            assert len(parts) == 1, (
                f"the two uploads landed in DIFFERENT partitions {parts}, so the "
                "partition-scoped supersede loop could never have reached across them and "
                "this run cannot show #791 either way")
            assert len(owners) == 2, (
                f"both documents carry the same owner_oid {owners}, so this is the #90 "
                "same-owner path, not the #791 cross-owner one")
            print(f"[precondition] one partition {parts.pop()[:24]}..., one uri "
                  f"upload://{filename}, two distinct owners: the defect COULD fire here")

            alice_rows = _listing(apage, base)
            if alice_doc not in alice_rows:
                failures.append(
                    f"#791 DATA LOSS: bob's upload of {filename} deleted alice's "
                    f"document {alice_doc}")
            else:
                print(f"[#791] alice's {alice_doc} SURVIVED bob's same-named upload")

            bob_rows = _listing(bpage, base)
            if bob_doc not in bob_rows:
                failures.append(f"bob's own upload {bob_doc} is missing from his listing")
            else:
                print(f"[#791] bob's {bob_doc} present")

            # The disclosure half, on prod.
            for who, rows in (("alice", alice_rows), ("bob", bob_rows)):
                leaked = [d for d, row in rows.items() if "owner_oid" in row]
                if leaked:
                    failures.append(
                        f"{who}'s listing exposes owner_oid on {len(leaked)} row(s), "
                        f"e.g. {leaked[0]}")
            if not failures:
                print("[wire] no owner_oid field in either listing")
        finally:
            if alice_doc:
                _delete(apage, base, alice_doc)
            if bob_doc:
                _delete(bpage, base, bob_doc)
            print("[cleanup] both documents deleted")

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("\nPASS - #791 proved on prod (pgvector): two owners, one filename, both survive")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
