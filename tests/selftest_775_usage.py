"""#775 / ADR 0027 rule 3 - how many bytes has this account entrusted to us?

The number a quota is enforced against has to come from the SAME place the documents live,
because ADR 0027's consequence says so in as many words: quota accounting rides the
delete-together lifecycle rather than keeping a second ledger of what exists. A separate
usage table drifts the first time an ingest half-fails, and a drifted ledger either bills for
storage nobody has or hands out storage nobody paid for, silently, forever.

So the size travels with the document into the index, and usage is a SUM over the rows that
are already there. Delete a document and its bytes leave with it, for free, by construction.

What is metered is the RAW uploaded size, which is the number a person recognises as "the
file I uploaded". Our own storage overhead is real but is priced into the tier, exactly the
way consumer storage products count your file and not their replication.

    PYTHONPATH=src python3 tests/selftest_775_usage.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.ports.base import ReadScope  # noqa: E402
from dbsearch.server.edition import build_edition  # noqa: E402

ALICE, BOB = "oid-alice", "oid-bob"


def _upload(ed, doc_id, owner, body: bytes, name=None):
    return ed.ingest_file(external_id=doc_id, title=name or doc_id, data=body,
                          mime="text/plain", acl=["all-staff"],
                          uri=f"upload://{name or doc_id}.txt", owner_oid=owner)


def test_usage_is_the_sum_of_what_this_owner_uploaded():
    ed = build_edition()
    a1 = b"a" * 5000
    a2 = b"b" * 3000
    _upload(ed, "u-alice-1", ALICE, a1)
    _upload(ed, "u-alice-2", ALICE, a2)
    got = ed.index.usage_bytes(ed.tenant_id, ALICE)
    assert got == len(a1) + len(a2), f"expected {len(a1) + len(a2)}, got {got}"


def test_another_owners_documents_are_not_your_usage():
    """The whole point of metering PER ACCOUNT. On a shared partition every colleague's
    documents sit in the same table, so a query that forgets the owner bills one person for
    the whole company."""
    ed = build_edition()
    _upload(ed, "u-alice-only", ALICE, b"x" * 4000)
    _upload(ed, "u-bob-only", BOB, b"y" * 9000)
    assert ed.index.usage_bytes(ed.tenant_id, ALICE) == 4000
    assert ed.index.usage_bytes(ed.tenant_id, BOB) == 9000


def test_deleting_a_document_returns_its_bytes():
    """Rides delete-together: no separate ledger to update, so no way to forget."""
    ed = build_edition()
    _upload(ed, "u-keep", ALICE, b"k" * 1000)
    _upload(ed, "u-drop", ALICE, b"d" * 2500)
    assert ed.index.usage_bytes(ed.tenant_id, ALICE) == 3500
    ed.index.delete(ed.tenant_id, "u-drop")
    assert ed.index.usage_bytes(ed.tenant_id, ALICE) == 1000, "deleted bytes still counted"


def test_a_replaced_version_is_not_counted_twice():
    """#90's supersede path: re-uploading an edited file replaces the old version, so usage
    must reflect the NEW size only. Counting both would charge a customer for every draft
    they ever saved."""
    ed = build_edition()
    _upload(ed, "u-v1", ALICE, b"1" * 6000, name="Report")
    assert ed.index.usage_bytes(ed.tenant_id, ALICE) == 6000
    _upload(ed, "u-v2", ALICE, b"2" * 1500, name="Report")   # same uri -> supersedes v1
    docs = {d.doc_external_id for d in ed.index.list_doc_acls(ReadScope(ed.tenant_id))}
    assert docs == {"u-v2"}, docs
    assert ed.index.usage_bytes(ed.tenant_id, ALICE) == 1500, (
        "the superseded version's bytes are still being charged for")


def test_an_owner_with_nothing_uses_nothing():
    ed = build_edition()
    assert ed.index.usage_bytes(ed.tenant_id, "oid-nobody") == 0


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                failures.append(name)
                print(f"FAIL  {name}\n      {e}")
            except Exception as e:
                failures.append(name)
                print(f"FAIL  {name}\n      {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failures else 'PASSED'} - {len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
