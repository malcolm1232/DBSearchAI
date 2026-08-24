"""#431: SharePoint connection state must be PER OWNER and must survive a restart.

Observed live on prod during the #423 foreign-tenant proof: a stranger from a different Entra
tenant dropped a SharePoint node, was correctly 403'd on connect ("SharePoint connect ... aren't
available for external organizations yet"), and then Compose up flipped that same node to a green
"Connected - live probe ok" pill with the footer reading "1 connected" - reporting MALCOLM's
connection to a foreign user.

Cause: sp_connect._CONNECTED was a module-level dict keyed ONLY by tenant, and
GET /connectors/sharepoint/status returned connected_tenants() with no user dimension at all.
No document content leaked (the 403 stopped token issuance and the corpus is separately ACL'd),
but it disclosed that another organization had connected, and it lied to the user about their own
setup - a node that says "Connected" when nothing is connected is worse than one that says
nothing.

Second, quieter half of the same bug: being in-process, the registry was wiped by every restart,
so a genuine connection also read as "Not connected yet" after each deploy.

    PYTHONPATH=src python3 tests/selftest_sp_connection_per_owner.py
"""
import os
import sys
from pathlib import Path

os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from dbsearch.server import sp_connect

MALCOLM = "f0661235-1a5b-4c39-a8fe-cd8f2857bc29"
STRANGER = "d0c2a725-9545-455d-967f-c172f3d0c4eb"
HOME_TENANT = "11111111-2222-3333-4444-555555555555"


def _fresh_store():
    from dbsearch.server.connection_store import InMemoryConnectionStore
    return InMemoryConnectionStore()


def test_one_owners_connection_is_invisible_to_another():
    """THE LEAK. Malcolm connects; the stranger must see nothing."""
    store = _fresh_store()
    sp_connect.bind_store(store)
    sp_connect.reset_for_test()
    try:
        sp_connect.mark_connected(HOME_TENANT, owner=MALCOLM)
        mine = [c["tenant"] for c in sp_connect.connected_tenants(owner=MALCOLM)]
        theirs = [c["tenant"] for c in sp_connect.connected_tenants(owner=STRANGER)]
        assert mine == [HOME_TENANT], mine
        assert theirs == [], f"LEAK: a foreign owner sees {theirs}"
    finally:
        sp_connect.bind_store(None)
        sp_connect.reset_for_test()


def test_a_connection_survives_a_restart():
    """The quieter half: in-process state made every deploy read as 'Not connected yet'."""
    store = _fresh_store()
    sp_connect.bind_store(store)
    sp_connect.reset_for_test()
    try:
        sp_connect.mark_connected(HOME_TENANT, owner=MALCOLM)
        sp_connect.reset_for_test()          # simulate a fresh process (memory cache cleared)
        again = [c["tenant"] for c in sp_connect.connected_tenants(owner=MALCOLM)]
        assert again == [HOME_TENANT], f"connection did not survive the restart: {again}"
    finally:
        sp_connect.bind_store(None)
        sp_connect.reset_for_test()


def test_no_store_still_works_in_memory_for_this_process():
    """A rig with no Postgres keeps working - durability is an improvement, not a dependency."""
    sp_connect.bind_store(None)
    sp_connect.reset_for_test()
    try:
        sp_connect.mark_connected(HOME_TENANT, owner=MALCOLM)
        assert [c["tenant"] for c in sp_connect.connected_tenants(owner=MALCOLM)] == [HOME_TENANT]
        assert sp_connect.connected_tenants(owner=STRANGER) == [], \
            "the memory path must be per-owner too, not just the durable one"
    finally:
        sp_connect.reset_for_test()


def test_status_endpoint_reports_only_the_callers_connections():
    """End to end through the route, because that is where the leak was actually visible."""
    from fastapi.testclient import TestClient

    from dbsearch.server.app import app

    store = _fresh_store()
    sp_connect.bind_store(store)
    sp_connect.reset_for_test()
    try:
        sp_connect.mark_connected(HOME_TENANT, owner="alice")
        client = TestClient(app)
        mine = client.get("/connectors/sharepoint/status",
                          headers={"X-DBSearch-User": "alice"}).json()
        theirs = client.get("/connectors/sharepoint/status",
                            headers={"X-DBSearch-User": "bob"}).json()
        assert [c["tenant"] for c in mine.get("connected", [])] == [HOME_TENANT], mine
        assert theirs.get("connected") == [], f"LEAK through the route: {theirs}"
    finally:
        sp_connect.bind_store(None)
        sp_connect.reset_for_test()


if __name__ == "__main__":
    test_one_owners_connection_is_invisible_to_another()
    test_a_connection_survives_a_restart()
    test_no_store_still_works_in_memory_for_this_process()
    test_status_endpoint_reports_only_the_callers_connections()
    print("OK selftest_sp_connection_per_owner")
