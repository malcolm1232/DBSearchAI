"""Self-test: a shared-index document store reads the OWNER's tenant partition (#439).

The gap this closes, found during #389. /search and /graphql derive the ADR 0012 partition
per request, but a document store that wraps the EDITION's shared QueryService (#304/#306)
inherited that service's DEPLOYMENT CONSTANT. So after #389 lifted the ingest gate, a foreign
org could ingest into its own partition and then get NOTHING back through /router/ask - the
documents were there, the retrieval was simply pointed at the home tenant.

Asserted here:
  1. the partition reaches retrieve/has_content/profile as a per-call override;
  2. it is SERVER-supplied - a manifest that tries to set it is overruled, never trusted;
  3. no override still means the QueryService's own tenant (unchanged single-tenant path).

    python3 tests/selftest_doc_store_owner_tenant.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _RecordingQS:
    """Stands in for the shared QueryService and records the tenant it was asked for. A fake
    is right here: the assertion is about which partition is REQUESTED, and a real service
    would answer from whichever partition it was given, hiding the very thing under test."""

    def __init__(self, own_tenant: str = "selfhost") -> None:
        self._tenant_id = own_tenant
        self.seen: list = []

    def retrieve(self, user_oid, question, tenant_id=None):
        self.seen.append(("retrieve", tenant_id))
        return []

    def has_visible_content(self, user_oid, tenant_id=None):
        self.seen.append(("has_visible_content", tenant_id))
        return False

    def content_titles(self, limit=40, tenant_id=None):
        self.seen.append(("content_titles", tenant_id))
        return []


class _Identity:
    def expand_groups(self, user_oid):
        return [user_oid]


def test_override_reaches_every_read_path():
    from dbsearch.router.indexed_store import IndexedStore
    from dbsearch.router.store import AccessContext

    qs = _RecordingQS(own_tenant="selfhost")
    store = IndexedStore("sp-1", "bu", "SharePoint", "", qs, _Identity(),
                         tenant_id="foreign-tid-123")
    access = AccessContext(user_oid="u-1", principals=["u-1"])
    store.retrieve(access, "anything")
    store.has_content(access)
    store.profile()                      # empty description -> content_titles path
    assert qs.seen == [("retrieve", "foreign-tid-123"),
                       ("has_visible_content", "foreign-tid-123"),
                       ("content_titles", "foreign-tid-123")], qs.seen
    print("  PASS  retrieve + has_content + profile all carry the owner's partition")


def test_no_override_keeps_the_services_own_tenant():
    """The single-tenant path must be untouched: no tenant_id -> the old call signature, so
    the QueryService uses its own constant."""
    from dbsearch.router.indexed_store import IndexedStore
    from dbsearch.router.store import AccessContext

    qs = _RecordingQS(own_tenant="selfhost")
    store = IndexedStore("sp-1", "bu", "SharePoint", "", qs, _Identity())
    store.retrieve(AccessContext(user_oid="u-1", principals=["u-1"]), "anything")
    assert qs.seen == [("retrieve", None)], qs.seen
    print("  PASS  no override -> QueryService keeps its own tenant (single-tenant unchanged)")


def test_provider_stamps_the_workspace_tenant_on_shared_stores():
    from dbsearch.router.providers.connector import ConnectorStoreProvider

    qs = _RecordingQS()
    prov = ConnectorStoreProvider("sharepoint",
                                  lambda cfg: _StubConnector(),
                                  identity=_Identity(), shared_query_service=qs)
    prov.set_doc_tenant("foreign-tid-123")
    store = prov.build({"id": "sp-1", "title": "SP", "acl": ["u-1"]})
    assert store._tenant_id == "foreign-tid-123", store._tenant_id
    # And clearing it returns to the shared service's own tenant rather than an empty string,
    # which would match no chunk and silently return nothing.
    prov.set_doc_tenant("")
    assert prov.build({"id": "sp-2", "title": "SP2", "acl": ["u-1"]})._tenant_id is None
    print("  PASS  ConnectorStoreProvider stamps the workspace partition on shared-index stores")


class _StubConnector:
    def authenticate(self, config):
        return None

    def list_changes(self, cursor):
        return []


def test_partition_is_server_supplied_not_client_supplied():
    """LAW 5 / ADR 0012: the client cannot choose the partition its documents are read from.
    A manifest that carries the internal key must have it overwritten, not honoured."""
    from dbsearch.server.router_api import OWNER_TENANT_KEY

    assert OWNER_TENANT_KEY.startswith("_"), \
        "the partition key must be internal-looking so it is never mistaken for user content"

    src = (Path(__file__).resolve().parents[1]
           / "src/dbsearch/server/router_api.py").read_text()
    # The compose path must STRIP the client's value before writing the verified one.
    assert f'if k != OWNER_TENANT_KEY' in src, \
        "#439: _persisting_compose no longer strips a client-supplied partition key"
    # And the endpoint must derive it from the resolver, never from the request body.
    assert "tenant_resolver(request)" in src, \
        "#439: compose no longer derives the partition from the ADR 0012 chokepoint"
    print("  PASS  the partition is stripped from client input and re-derived server-side")


def test_rebuild_replays_the_persisted_partition():
    """A rebuild has no request behind it, so a workspace warmed after a restart must still
    read the owner's partition - otherwise the bug returns the first time the pool evicts."""
    src = (Path(__file__).resolve().parents[1]
           / "src/dbsearch/server/router_api.py").read_text()
    assert "owner_tenant=manifest.get(OWNER_TENANT_KEY)" in src, \
        "#439: _rebuild no longer replays the stored partition - a warmed workspace would " \
        "silently fall back to the deployment constant"
    print("  PASS  _rebuild replays the persisted partition")


def main():
    print("Doc-store owner tenant partition (#439) self-test:")
    test_override_reaches_every_read_path()
    test_no_override_keeps_the_services_own_tenant()
    test_provider_stamps_the_workspace_tenant_on_shared_stores()
    test_partition_is_server_supplied_not_client_supplied()
    test_rebuild_replays_the_persisted_partition()
    print("\nDOC-STORE OWNER TENANT SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
