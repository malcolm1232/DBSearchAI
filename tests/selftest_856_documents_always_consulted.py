"""#856: the caller's own documents are ASKED on every routed turn, never a losing candidate.

Found on prod, in Chrome, in the same session that proved #689: with routing on, "How many
days of annual leave do I get?" was answered from a composed folder's abbreviated copy of the
leave policy and lost the "30 days after five years of service" clause that the caller's OWN
uploaded document carries. A reader with five years of service was told the wrong number, with
a citation under it.

MEASURED, not guessed. `routing.candidates` showed the folder at 0.1333 ("analytical/exact")
and `documents` at 0.0556 ("semantic · shares: leave"), `routing.stores` held the folder
ALONE, and `retrieved_docs` was empty: the documents node was a candidate that LOST, not a
plane that was consulted and declined.

THE RULE THIS FILE PINS: a routed ask consults the caller's documents IN ADDITION to whatever
won the route, and merges the evidence into one answer. This is what /canvas has always done -
canvas.js fires the #255 document search on every ask, prints "also searched your documents"
when they come back empty, and retracts the router's abstention when they answer - so /ask
having documents merely compete was the same surface-to-surface divergence #689 exists to
remove. A score boost was the alternative and is refused in ask_router.py's own header: the
docs description is deliberately generic, and narrowing it makes the router prefer a database
for questions the documents answer.

    PYTHONPATH=src python3 tests/selftest_856_documents_always_consulted.py
"""
import json
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
os.environ["DBSEARCH_RATE_LIMIT"] = "0"
os.environ["DBSEARCH_ASK_ROUTES"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch import router  # noqa: E402
from dbsearch.adapters.local import ExtractiveLlm, HashingEmbedding, InMemoryIdentity  # noqa: E402

FULLER = ("Annual leave. Full-time employees accrue 26 days of paid annual leave per calendar "
          "year. Employees with more than five years of service accrue 30 days.")
SHORTER = ("HR Leave Policy 2026. Full-time employees accrue 26 days of paid annual leave per "
           "calendar year. Primary carers receive 18 weeks of parental leave.")

SPEC = {
    "tenant": "acme",
    "stores": [
        # The store that WINS the route: a folder of HR policy files, described the way a
        # connected source is described.
        {"id": "hr-folder", "kind": "local", "business_unit": "hr", "acl": ["alice"],
         "title": "HR policies folder",
         "description": "HR policy documents leave holidays parental benefits annual days",
         "config": {"seed": [{"external_id": "f1", "title": "leave-policy.txt", "uri": "",
                              "acl": ["alice"], "text": SHORTER}],
                    "user_groups": {"alice": ["alice"]}}},
        # The caller's own uploads, standing in for the ask overlay's documents node.
        {"id": "documents", "kind": "local", "business_unit": "uploads", "acl": ["alice"],
         "title": "Your documents",
         "description": "Documents this person has uploaded: policies, reports, contracts",
         "config": {"seed": [{"external_id": "d1", "title": "HR Leave and Benefits Policy",
                              "uri": "", "acl": ["alice"], "text": FULLER}],
                    "user_groups": {"alice": ["alice"]}}},
    ],
}


class _AlwaysConsult:
    """What `DocsOverlayCatalog` exposes: a node that is asked rather than ranked.

    A proxy rather than the real overlay, so these tests measure the ROUTER's half of the
    contract without an edition, an index or a request scope behind it."""

    def __init__(self, base, node_id):
        self._base, self._node_id = base, node_id

    def __getattr__(self, name):
        return getattr(self._base, name)

    def always_consulted(self, principals):
        return [n for n in self._base.visible_stores(principals) if n.id == self._node_id]


def _catalog():
    reg = router.ProviderRegistry()
    reg.register(router.LocalIndexProvider())
    return router.load_manifest(SPEC, registry=reg)


def _svc(always=True):
    cat = _catalog()
    if always:
        cat = _AlwaysConsult(cat, "documents")
    return router.RouterQueryService(cat, InMemoryIdentity({"alice": ["alice"]}),
                                     HashingEmbedding())


Q = "How many days of annual leave do I get?"


def test_PRECONDITION_the_folder_outranks_the_documents_node():
    """Without this, every assertion below could pass because documents won on merit, and the
    rule under test would never have been exercised at all."""
    d = _svc(always=False).route("alice", Q)
    chosen = [s.store_id for s in d.stores]
    assert chosen == ["hr-folder"], (
        f"PRECONDITION FAILED: the route did not go to the folder alone ({chosen}), so nothing "
        f"here measures what happens to a LOSING documents node. candidates="
        f"{[(c.store_id, round(c.score, 4)) for c in d.candidates]}")
    assert "documents" in {c.store_id for c in d.candidates}, \
        "the documents node was not even a candidate; this fixture is not the #856 shape"


def test_the_documents_plane_is_consulted_even_when_it_loses_the_route():
    """THE CARD. The folder wins and the documents are asked anyway."""
    result = _svc().ask("alice", Q, ExtractiveLlm())
    outs = {o["store_id"]: o["status"] for o in result.outcomes}
    assert "hr-folder" in outs, f"the routed store was not consulted at all: {outs}"
    assert "documents" in outs, (
        f"the caller's own documents were never asked - they lost the route and that ended it: "
        f"{outs}")
    texts = " ".join(str(e.get("content", "")) for e in result.evidence)
    assert "five years" in texts, (
        "the fuller clause never reached synthesis, so the answer cannot use it: " + texts[:300])


def test_a_manual_pin_is_not_widened():
    """E7 pins the ask to ONE store because the user said so. Adding a plane they did not ask
    for would overrule an explicit choice - the one case where 'always' must not mean always."""
    result = _svc().ask("alice", Q, ExtractiveLlm(), store_override="hr-folder")
    outs = {o["store_id"] for o in result.outcomes}
    assert outs == {"hr-folder"}, f"a manual pin was widened to {outs}"


def test_the_documents_node_is_not_consulted_twice_when_it_wins():
    """It is the same store either way: winning the route and being always-consulted must not
    produce two dispatches, two outcomes and two copies of the same evidence."""
    svc = _svc()
    result = svc.ask("alice", "what did I upload about contracts and reports", ExtractiveLlm())
    ids = [o["store_id"] for o in result.outcomes]
    assert ids.count("documents") <= 1, f"the documents plane was dispatched twice: {ids}"


def test_a_catalog_without_the_seam_is_untouched():
    """/router/ask and the canvas run on a plain StoreCatalog, which has no always_consulted.
    They must behave exactly as before - this is an ASK-surface rule, not a router-wide one."""
    result = _svc(always=False).ask("alice", Q, ExtractiveLlm())
    outs = {o["store_id"] for o in result.outcomes}
    assert outs == {"hr-folder"}, f"the plain catalog path changed: {outs}"


# ---------------------------------------------------------------- at the wire
def test_the_ask_surface_reads_the_callers_documents_on_a_routed_turn():
    """The prod shape end to end: a composed store that wins, an upload that answers better."""
    from fastapi.testclient import TestClient

    from dbsearch.server.app import app
    client = TestClient(app)
    A = {"X-DBSearch-User": "alice"}
    r = client.post("/router/compose", headers=A, json={"manifest": {"tenant": "acme", "stores": [
        {"id": "folder-856", "kind": "csv", "business_unit": "hr", "acl": ["alice"],
         "title": "HR policies folder",
         "description": "HR policy documents: leave, holidays, parental leave, benefits",
         "config": {"tables": {"policies": {"columns": ["file", "text"],
                                            "rows": [["leave-policy.txt", SHORTER]]}}}}]}})
    assert r.status_code == 200, r.text
    r = client.post("/ingest", headers=A, json={
        "external_id": "hr-leave-856", "title": "HR Leave and Benefits Policy",
        "text": FULLER, "acl": ["alice"], "uri": ""})
    assert r.status_code == 200, r.text

    r = client.post("/chat/stream", headers=A,
                    json={"conv_id": "c-856-wire", "question": Q})
    assert r.status_code == 200, r.text
    done = [json.loads(ln[6:]) for ln in r.text.splitlines() if ln.startswith("data: ")]
    done = [e for e in done if e.get("type") == "done"][0]
    routed = [s["store_id"] for s in (done.get("routing") or {}).get("stores", [])]
    assert "folder-856" in routed, (
        f"PRECONDITION FAILED: the composed store did not win the route ({routed}), so this "
        f"turn is not the #856 shape")
    assert done.get("retrieved_docs"), (
        "the caller's own uploaded document contributed nothing to a routed turn: "
        f"outcomes={done.get('outcomes')}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}\n        {exc}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
