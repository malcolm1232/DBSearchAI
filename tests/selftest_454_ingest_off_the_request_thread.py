"""#454 — a crawl must not run inside an HTTP request (LAW 4, ADR 0016 §1).

THE DEFECT, measured by #536 rather than argued: `router_api.sync_store` called
`provider.sync(store_id)` inline, and `ConnectorStoreProvider.build` ran the initial full
crawl during compose. A 40.2MB / 4884-document pack exceeded `/router/compose`'s 3600s
timeout, so connecting a real SharePoint library — the thing the product is FOR — could not
complete at all. Not slowly. At all.

WHAT THIS TEST PINS:

  1. The request RETURNS while the crawl is still going. Asserted against a connector that
     blocks, so a passing run cannot be "it was just fast".
  2. The job is observable: phase, documents done/total, and a terminal status.
  3. **A question asked DURING ingest does not crash.** Moving ingest onto a worker put a
     writer beside every reader of the in-memory index, and each read iterated the dict it
     was mutating — `RuntimeError: dictionary changed size during iteration`, raised exactly
     when someone queries their library while it indexes. That is not an edge case; for a
     library that takes an hour it is the normal case.
  4. Content becomes ROUTABLE, not merely indexed. The compose layer snapshots a store's
     profile for the router to rank on, and an async crawl means that snapshot is taken over
     an empty index (#306/#453's failure) unless the node re-derives it when content lands.
  5. LAW 2: a job belongs to a workspace. Another signed-in user cannot read it — #549 was
     exactly this defect on the metadata plane, and a job id would otherwise be an oracle
     for someone else's source names and document counts.

    PYTHONPATH=src python3 tests/selftest_454_ingest_off_the_request_thread.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.ports.base import ReadScope  # noqa: E402

from dbsearch.adapters.local import InMemoryIdentity  # noqa: E402
from dbsearch.core.models import Document, Principal  # noqa: E402
from dbsearch.ports.base import ConnectorPort  # noqa: E402
from dbsearch.router.providers.connector import ConnectorStoreProvider  # noqa: E402

N_DOCS = 40


class GatedConnector(ConnectorPort):
    """Emits N documents but holds the first one until released, so "the request returned
    before the crawl finished" is a fact rather than a timing coincidence."""

    def __init__(self, tenant_id: str) -> None:
        # The real factories stamp config["id"] as the tenant, and QueryService is built with
        # the same value - a connector that stamps anything else indexes into a partition the
        # store never reads (ADR 0012), which looks exactly like "ingest silently did nothing".
        self.tenant_id = tenant_id
        self.gate = threading.Event()
        self.reached_gate = threading.Event()
        self.emitted = 0

    def authenticate(self, config: dict) -> object:
        return object()

    def list_changes(self, cursor):
        return ([{"external_id": f"doc-{i}.txt"} for i in range(N_DOCS)], "cursor-1")

    def external_ids(self, item):
        return [item["external_id"]]

    def fetch_content(self, item: dict):
        if item["external_id"] == "doc-0.txt":
            self.reached_gate.set()
            self.gate.wait(timeout=30)
        self.emitted += 1
        return (f"Document {item['external_id']}. The travel policy allows economy flights "
                "under six hours and the probation period is six months. ").encode(), "text/plain"

    def fetch_acl(self, item: dict):
        return [Principal(oid="all-staff", kind="group")]

    def to_documents(self, item: dict):
        return [Document(tenant_id=self.tenant_id, source_id="gated", acl=self.fetch_acl(item),
                         external_id=item["external_id"], content_ref="",
                         title=item["external_id"])]


def _provider(connectors: dict):
    """One connector per store id, so a provider clone (health's isolated build) gets its own
    instance rather than sharing a gate with the live crawl."""
    def factory(cfg):
        return connectors.setdefault(cfg["id"], GatedConnector(cfg["id"]))
    return ConnectorStoreProvider("folder", factory,
                                  identity=InMemoryIdentity({"alice": ["all-staff"]}))


def test_build_returns_while_the_crawl_is_still_running() -> None:
    conns: dict = {}
    p = _provider(conns)
    t0 = time.perf_counter()
    store = p.build({"id": "gated", "business_unit": "bu", "title": "Gated",
                     "description": "policies", "path": "/unused"})
    elapsed = time.perf_counter() - t0

    conn = conns["gated"]
    assert conn.reached_gate.wait(timeout=10), "the crawl never started on a worker"
    assert elapsed < 5.0, (
        f"1. build() blocked for {elapsed:.1f}s on a crawl that is still gated — it is "
        "running the crawl inline, which is the #536 timeout")

    job_id = p.active_jobs()["gated"]
    running = p.jobs.get(job_id)
    assert running.status == "running", f"2. job should be running, got {running.status!r}"

    conn.gate.set()
    final = p.wait_for_ingest("gated", timeout=30)
    assert final.status == "succeeded", f"2. terminal status: {final.status} {final.error}"
    assert final.docs_total == N_DOCS and final.docs_done == N_DOCS, final
    assert final.phase == "done", final

    ev = store.retrieve(store.authorize("alice"), "probation period")
    assert any("probation" in e.content for e in ev), "content never became queryable"
    assert store.profile().freshness.startswith("ingested@"), store.profile().freshness
    p._runner.shutdown()


def test_questions_during_ingest_do_not_crash() -> None:
    """3. The reader/writer race that moving ingest onto a worker created.

    Several readers scan the index while the crawl writes into it. Every read path iterates
    the chunk map, so an unsynchronised read raises `RuntimeError: dictionary changed size
    during iteration` - and it raises for the USER, mid-question, on the one code path this
    whole card exists to make usable (a library that takes an hour is a library you query
    while it indexes).

    Driven at the INDEX, not through retrieve(). A version of this that asked questions
    through the store passed against the unsynchronised index every time - the query path is
    slow enough per call that a scan rarely straddles a write, so it would have certified the
    bug as fixed. The hazard lives in the chunk map, so that is where it has to be provoked:
    verified to fail on the pre-fix code and pass after."""
    from dbsearch.adapters.local import InMemoryIndex, InMemoryObjectStore
    from dbsearch.core.models import Chunk

    obj = InMemoryObjectStore()
    emb_ref = obj.put("emb/probe", json.dumps([0.05] * 64).encode())
    text_ref = obj.put("chunk/probe", b"travel policy and probation period")
    index = InMemoryIndex(obj)

    def chunk(n: int) -> Chunk:
        return Chunk(tenant_id="t", doc_external_id=f"doc-{n // 8}", chunk_id=f"c-{n}",
                     text_ref=text_ref, allowed_principals=["all-staff"],
                     embedding_ref=emb_ref, title=f"Doc {n // 8}")

    for n in range(1500):
        index.upsert([chunk(n)])

    errors: list = []
    stop = threading.Event()

    def writer() -> None:
        """What the ingest worker does: upsert a document's chunks, and delete-before-index
        the previous set (#391)."""
        n = 10 ** 6
        while not stop.is_set():
            index.upsert([chunk(n)])
            n += 1
            if n % 200 == 0:
                index.delete("t", f"doc-{(n // 8) - 5}")

    def reader(fn) -> None:
        while not stop.is_set():
            try:
                fn()
            except Exception as exc:                     # noqa: BLE001
                errors.append(exc)
                return

    reads = [lambda: index.search([0.05] * 64, ["all-staff"], 5, ReadScope("t")),
             lambda: index.distinct_titles("t"),
             lambda: index.corpus_status(ReadScope("t"), ["all-staff"]),
             lambda: index.has_authorized(["all-staff"], ReadScope("t"))]
    threads = [threading.Thread(target=writer, daemon=True)]
    threads += [threading.Thread(target=reader, args=(fn,), daemon=True)
                for fn in reads for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(3.0)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert not errors, (
        f"3. a read of the index crashed while ingest wrote to it: "
        f"{type(errors[0]).__name__}: {errors[0]}")


def test_a_composed_store_becomes_routable_not_just_indexed() -> None:
    """4. The router ranks on the profile the COMPOSE layer snapshotted. Taken while the
    crawl is queued it describes an empty index, so without a refresh the store is correctly
    ingested and still unfindable."""
    from dbsearch.router.provisioning import load_manifest
    from dbsearch.router.provider import ProviderRegistry

    conns: dict = {}
    p = _provider(conns)
    reg = ProviderRegistry()
    reg.register(p)
    spec = {"tenant": "acme", "stores": [
        {"id": "gated", "kind": "folder", "mode": "index", "business_unit": "bu",
         "acl": ["all-staff"], "title": "Gated", "description": "",
         "config": {"path": "/unused"}}]}
    cat = load_manifest(spec, registry=reg)
    conns["gated"].gate.set()
    node = cat.get("gated")
    assert node.profile.freshness.startswith(("syncing", "never-synced")), (
        f"compose should not have waited for the crawl, got {node.profile.freshness!r}")

    p.wait_for_ingest("gated", timeout=30)
    assert node.profile.freshness.startswith("ingested@"), (
        "4. the catalog node never re-derived its profile, so the router is still ranking "
        f"this store on the empty index it was composed over: {node.profile.freshness!r}")
    assert node.profile.topics, (
        "4. no routing topics: a document store's topics come from its ingested doc titles "
        "(#306/#453), which is precisely what a compose-time snapshot cannot see")
    p._runner.shutdown()


def test_a_job_is_not_readable_by_another_workspace() -> None:
    """5. LAW 2 on the job surface. Two providers, two workspaces: neither owns the other's
    store, so neither may answer for the other's job."""
    ca: dict = {}
    cb: dict = {}
    a, b = _provider(ca), _provider(cb)
    a.build({"id": "store-a", "business_unit": "bu", "title": "A", "description": "",
             "path": "/unused"})
    b.build({"id": "store-b", "business_unit": "bu", "title": "B", "description": "",
             "path": "/unused"})
    ca["store-a"].gate.set()
    cb["store-b"].gate.set()
    a.wait_for_ingest("store-a", timeout=30)
    b.wait_for_ingest("store-b", timeout=30)

    job_a = a.active_jobs()["store-a"]
    assert b.jobs.get(job_a) is None, (
        "5. workspace B's job store answered for workspace A's job — the route's ownership "
        "check is the only thing standing between a job id and another tenant's source names")
    assert not b.owns("store-a"), "5. B must not claim A's store"
    a._runner.shutdown()
    b._runner.shutdown()


def main() -> int:
    test_build_returns_while_the_crawl_is_still_running()
    test_questions_during_ingest_do_not_crash()
    test_a_composed_store_becomes_routable_not_just_indexed()
    test_a_job_is_not_readable_by_another_workspace()
    print("PASS #454: the crawl runs off the request, the job is observable, questions asked "
          "mid-ingest do not crash, the store becomes routable, and a job is workspace-scoped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
