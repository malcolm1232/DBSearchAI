"""Phase E #111 — connector rail unified into the compose layer.

stores.yml `kind: folder|sharepoint, mode: index` must DRIVE the document-connector
rail: provider.build() -> ConnectorPort + SourceRegistry + run_ingestion (initial
crawl), provider.sync() -> delta re-crawl off the persisted cursor, freshness
reported in the store profile. Docs and databases plug into the SAME Lego surface.

Run: python3 tests/selftest_router_connector_rail.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import InMemoryIdentity  # noqa: E402
from dbsearch.router.provider import ProviderRegistry, StoreProviderPort  # noqa: E402
from dbsearch.router.providers.connector import (  # noqa: E402
    ConnectorStoreProvider, folder_connector_factory, sharepoint_connector_factory,
)
from dbsearch.router.providers.local import LocalIndexProvider  # noqa: E402
from dbsearch.router.provisioning import load_manifest  # noqa: E402
from dbsearch.router.store import INDEXED, SEMANTIC  # noqa: E402

IDENTITY = InMemoryIdentity({"alice": ["all-staff", "deal-team"], "bob": ["all-staff"]})


def _folder_provider(now=None):
    return ConnectorStoreProvider("folder", folder_connector_factory,
                                  identity=IDENTITY, now=now)


def _corpus(root: Path) -> None:
    (root / "all-staff").mkdir(parents=True)
    (root / "deal-team").mkdir(parents=True)
    (root / "all-staff" / "handbook.txt").write_text(
        "parental leave is sixteen weeks for all staff")
    (root / "deal-team" / "falcon.txt").write_text(
        "project falcon valuation is four point two billion")


# ---------------------------------------------------------------- mode registry

class _FakeIndexProvider(StoreProviderPort):
    kind = "sharepoint"
    modes = ("index",)

    def probe(self, config):  # pragma: no cover - never called
        raise AssertionError

    def build(self, config):  # pragma: no cover - never called
        raise AssertionError


class _FakeNativeProvider(_FakeIndexProvider):
    modes = ("native",)


def test_registry_is_mode_aware():
    r = ProviderRegistry()
    idx, nat = _FakeIndexProvider(), _FakeNativeProvider()
    r.register(idx)
    r.register(nat)
    assert r.get("sharepoint") is idx, "no mode -> first-registered default"
    assert r.get("sharepoint", "index") is idx
    assert r.get("sharepoint", "native") is nat
    try:
        r.get("sharepoint", "pushdown")
        assert False, "unsupported mode must raise"
    except KeyError as exc:
        assert "pushdown" in str(exc) and "native" in str(exc), exc
    try:
        r.get("nope")
        assert False, "unknown kind must raise"
    except KeyError:
        pass


def test_provider_must_declare_modes():
    class Undeclared(StoreProviderPort):
        kind = "mystery"

        def probe(self, config):  # pragma: no cover
            raise AssertionError

        def build(self, config):  # pragma: no cover
            raise AssertionError

    r = ProviderRegistry()
    try:
        r.register(Undeclared())
        assert False, "modes-less provider must be rejected (ADR 0008)"
    except ValueError:
        pass


# ------------------------------------------------------- connector-rail provider

def test_build_ingests_and_trims():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _corpus(root)
        p = _folder_provider()
        store = p.build({"id": "legal-archive", "business_unit": "legal",
                         "title": "Legal archive", "description": "contracts",
                         "path": str(root)})
        # #454: build() SUBMITS the initial crawl instead of running it inside the call, so a
        # 40MB library no longer has to finish inside a compose request. A test that asks a
        # question therefore has to wait for the job, exactly as the UI polls for it.
        p.wait_for_ingest("legal-archive", timeout=60)
        # LAW 2: alice (deal-team) sees falcon; bob does not.
        ev_a = store.retrieve(store.authorize("alice"), "falcon valuation")
        assert any("falcon" in e.content for e in ev_a), ev_a
        ev_b = store.retrieve(store.authorize("bob"), "falcon valuation")
        assert not any("falcon" in e.content for e in ev_b), ev_b
        # freshness is reported post-ingest
        prof = store.profile()
        assert prof.kind == INDEXED and SEMANTIC in prof.capabilities, prof
        assert prof.freshness.startswith("ingested@"), prof.freshness
        # the connector rail is REAL: a SourceRegistry descriptor with a cursor
        s = p.summary("legal-archive")
        assert s.doc_count == 2 and s.status == "idle" and s.last_sync_at, s


def test_probe_is_cheap_and_honest():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _corpus(root)
        p = _folder_provider()
        cfg = {"id": "legal-archive", "business_unit": "legal", "title": "Legal",
               "description": "", "path": str(root)}
        before = p.probe(cfg)
        assert before.freshness == "never-synced", before.freshness
        p.build(cfg)
        p.wait_for_ingest(cfg["id"], timeout=60)
        after = p.probe(cfg)
        assert after.freshness.startswith("ingested@"), after.freshness


def test_delta_sync_advances_cursor():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _corpus(root)
        p = _folder_provider()
        cfg = {"id": "legal-archive", "business_unit": "legal", "title": "Legal",
               "description": "", "path": str(root)}
        store = p.build(cfg)
        p.wait_for_ingest(cfg["id"], timeout=60)
        first = p.summary("legal-archive")

        # #910: doc_count means CORPUS SIZE, not the per-crawl counter. This assert used to
        # pin the defect itself (quiet delta -> 0 written over a real 2, durably once the
        # registry is store-backed).
        # nothing changed -> delta crawl sees nothing new -> corpus count unchanged
        s = p.sync("legal-archive")
        assert s.doc_count == 2, s

        # new doc, mtime bumped past the cursor -> picked up + retrievable -> corpus grows
        newf = root / "all-staff" / "merger.txt"
        newf.write_text("the acme merger closed in october")
        future = time.time() + 5
        os.utime(newf, (future, future))
        s2 = p.sync("legal-archive")
        assert s2.doc_count == 3, s2
        assert s2.last_sync_at >= first.last_sync_at, (s2, first)
        ev = store.retrieve(store.authorize("bob"), "acme merger")
        assert any("merger" in e.content for e in ev), ev
        # freshness moved with the sync
        assert store.profile().freshness == f"ingested@{s2.last_sync_at}"


def test_sharepoint_index_mode_via_seed():
    p = ConnectorStoreProvider("sharepoint", sharepoint_connector_factory,
                               identity=InMemoryIdentity(
                                   {"casey": ["grp-all-consultants"]}))
    store = p.build({"id": "contoso-sp", "business_unit": "consulting",
                     "title": "Contoso SharePoint", "description": ""})
    p.wait_for_ingest("contoso-sp", timeout=60)
    ev = store.retrieve(store.authorize("casey"), "retail bank proposal")
    assert any("retail bank" in e.content.lower() for e in ev), ev
    # Chinese wall: casey is not on grp-falcon-team
    ev2 = store.retrieve(store.authorize("casey"), "falcon valuation due diligence")
    assert not any("falcon" in e.content.lower() for e in ev2), ev2


# ----------------------------------------------------------- manifest + mode

def test_manifest_mode_routes_to_connector_rail():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _corpus(root)
        reg = ProviderRegistry()
        reg.register(LocalIndexProvider())
        reg.register(_folder_provider())
        spec = {
            "tenant": "acme",
            "stores": [
                {"id": "legal-archive", "kind": "folder", "mode": "index",
                 "business_unit": "legal", "acl": ["all-staff"],
                 "title": "Legal archive", "description": "contracts legal",
                 "config": {"path": str(root)}},
                {"id": "hr-wiki", "kind": "local", "business_unit": "hr",
                 "acl": ["all-staff"],
                 "config": {"seed": [{"external_id": "hb", "title": "HB", "uri": "u",
                                      "acl": ["all-staff"],
                                      "text": "holiday policy is 25 days"}],
                            "user_groups": {"bob": ["all-staff"]}}},
            ],
        }
        cat = load_manifest(spec, registry=reg)
        assert {n.id for n in cat.stores()} == {"legal-archive", "hr-wiki"}
        # #454: compose submits the crawl. The catalog node re-derives its routing profile
        # when the content lands (_refresh_profile_when_content_lands) - without that the
        # router would keep ranking this store on the empty index it was composed over.
        reg.get("folder", "index").wait_for_ingest("legal-archive", timeout=60)
        legal = cat.get("legal-archive")
        # the catalog profile reflects the BUILT store (probe-after-build): freshness real
        assert legal.profile.freshness.startswith("ingested@"), legal.profile
        ev = legal.store.retrieve(legal.store.authorize("alice"), "falcon valuation")
        assert any("falcon" in e.content for e in ev), ev


def test_manifest_unsupported_mode_fails_honestly():
    reg = ProviderRegistry()
    reg.register(_folder_provider())
    spec = {"tenant": "acme",
            "stores": [{"id": "x", "kind": "folder", "mode": "pushdown",
                        "business_unit": "bu", "acl": [], "config": {"path": "/nope"}}]}
    try:
        load_manifest(spec, registry=reg)
        assert False, "mode a provider doesn't support must raise"
    except KeyError as exc:
        assert "pushdown" in str(exc), exc


def main():
    print("Phase E #111 connector-rail self-test:")
    test_registry_is_mode_aware()
    test_provider_must_declare_modes()
    print("  PASS  mode-aware registry + mandatory mode declaration")
    test_build_ingests_and_trims()
    test_probe_is_cheap_and_honest()
    test_delta_sync_advances_cursor()
    test_sharepoint_index_mode_via_seed()
    print("  PASS  build->ingest->trim / honest probe / delta sync / sharepoint seed")
    test_manifest_mode_routes_to_connector_rail()
    test_manifest_unsupported_mode_fails_honestly()
    print("  PASS  manifest mode plumbing + honest unsupported-mode failure")
    print("\n#111 CONNECTOR-RAIL SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
