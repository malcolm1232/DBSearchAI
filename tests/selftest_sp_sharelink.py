"""#300: paste a SharePoint sharing link → ingest just that folder.

Proves the resolve-link logic (Graph /shares encoding + folder_path derivation) and the
/connectors/sharepoint/finish share_link branch, WITHOUT touching Microsoft (injected HTTP /
monkeypatched resolve + ingest). The scoped ingest itself is the existing GraphSharePointConnector
folder filter (_in_scope), covered by its own tests — here we prove the link resolves to the right
(drive_id, folder_path) and that finish threads folder_path into the ingest.
"""
import base64
import os
import sys
import time
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
os.environ["SP_CONNECTOR_CLIENT_ID"] = "cid-123"
os.environ["SP_CONNECTOR_CLIENT_SECRET"] = "secret-xyz"
os.environ["SP_CONNECTOR_REDIRECT_URI"] = "http://localhost:8080/connectors/sharepoint/callback"
# #423: the doc-plane tenant gate fail-closes on a real-login session with no tid, and a
# real Azure AD id token always carries one - AUTH_TENANT_ID is this rig's home tenant so
# the minted cookie below can carry a matching tid, same as production.
os.environ["AUTH_TENANT_ID"] = "home-tid"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import sp_connect, user_auth  # noqa: E402
import dbsearch.server.app as appmod  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)
HDR = {"X-DBSearch-User": "alice"}
COOKIES = {user_auth.COOKIE: user_auth.sign_session(
    {"oid": "alice", "tid": "home-tid", "exp": int(time.time()) + 3600})}
fails = 0


def check(name, cond):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails += 1


print("SharePoint sharing-link ingest — #300 self-test:")

LINK = "https://contoso.sharepoint.com/:f:/g/ExAMPLEfakeTOKENfakeTOKENfakeTOKEN0?e=abcdef"

# --- Graph /shares encoding is exactly "u!" + urlsafe-b64(link) with padding stripped -------
enc = sp_connect.encode_share_url(LINK)
expected = "u!" + base64.urlsafe_b64encode(LINK.encode()).decode().rstrip("=")
check("encode_share_url matches Graph's u! rule", enc == expected)
check("encoded url has no '=' padding and no '/' or '+'", "=" not in enc and "/" not in enc[2:] and "+" not in enc)


# --- resolve_share_link: folder DIRECTLY under root → folder_path = its name -----------------
def post_ok(u, form):
    return {"access_token": "TOK"} if u.endswith("/oauth2/v2.0/token") else {}


def get_folder_root(u, bearer):
    # the shared item is a folder named "Books" sitting at the drive root
    return {"id": "item-1", "name": "Books", "folder": {"childCount": 3},
            "parentReference": {"driveId": "drive-XYZ", "path": "/drives/drive-XYZ/root:"},
            "webUrl": "https://quantifymeai.sharepoint.com/Shared%20Documents/Books"}


res = sp_connect.resolve_share_link("tenantA", LINK, post=post_ok, get=get_folder_root)
check("resolve → drive_id from parentReference", res["drive_id"] == "drive-XYZ")
check("resolve → folder_path is the folder's own name at root", res["folder_path"] == "Books")


# --- nested folder ("Books/Q3") → folder_path keeps the full path from root ------------------
def get_folder_nested(u, bearer):
    return {"id": "item-2", "name": "Q3", "folder": {"childCount": 1},
            "parentReference": {"driveId": "drive-XYZ", "path": "/drives/drive-XYZ/root:/Books"},
            "webUrl": "https://quantifymeai.sharepoint.com/Shared%20Documents/Books/Q3"}


res2 = sp_connect.resolve_share_link("tenantA", LINK, post=post_ok, get=get_folder_nested)
check("resolve → nested folder_path is 'Books/Q3'", res2["folder_path"] == "Books/Q3")


# --- a FILE link (no folder facet) is rejected with an actionable message (MVP = folders) ----
def get_file(u, bearer):
    return {"id": "f1", "name": "report.pdf", "file": {"mimeType": "application/pdf"},
            "parentReference": {"driveId": "drive-XYZ", "path": "/drives/drive-XYZ/root:/Books"}}


try:
    sp_connect.resolve_share_link("tenantA", LINK, post=post_ok, get=get_file)
    check("file link rejected", False)
except RuntimeError as e:
    check("file link rejected with 'folder' guidance", "folder" in str(e).lower())


# --- a Graph error surfaces as a RuntimeError (not an opaque 500) ----------------------------
def get_err(u, bearer):
    return {"error": {"code": "itemNotFound", "message": "the sharing link is invalid or expired"}}


try:
    sp_connect.resolve_share_link("tenantA", LINK, post=post_ok, get=get_err)
    check("invalid link raises", False)
except RuntimeError as e:
    check("invalid link surfaces the Graph message", "invalid or expired" in str(e))


# --- /finish with share_link: resolves, then ingests SCOPED to that folder -------------------
captured = {}


def fake_connect(az_tenant_id, drive_id, connector=None, folder_path=None, progress=None, **kw):
    captured["drive_id"] = drive_id
    captured["folder_path"] = folder_path
    return {"source_id": f"sharepoint:{az_tenant_id}", "tenant": az_tenant_id,
            "drive_id": drive_id, "docs_indexed": 4}


appmod._edition.connect_sharepoint = fake_connect
sp_connect.resolve_share_link = lambda tenant, link, **k: {"drive_id": "drive-XYZ", "folder_path": "Books",
                                                           "name": "Books", "web": ""}
r = client.post("/connectors/sharepoint/finish", headers=HDR, cookies=COOKIES,
                json={"tenant": "tenantB", "share_link": LINK})
check("/finish {share_link} -> 202 (#569: submits, does not crawl inline)", r.status_code == 202)
check("/finish {share_link} ingests the resolved drive", captured.get("drive_id") == "drive-XYZ")
check("/finish {share_link} scopes ingest to the resolved folder", captured.get("folder_path") == "Books")
check("/finish {share_link} returns docs_indexed", r.json().get("docs_indexed") == 4)

# the existing library-pick path (drive_id, no folder scope) still works and stays unscoped
captured.clear()
r = client.post("/connectors/sharepoint/finish", headers=HDR, cookies=COOKIES,
                json={"tenant": "tenantB", "drive_id": "drive-9"})
check("/finish {drive_id} still works", r.status_code == 202 and captured.get("drive_id") == "drive-9")
check("/finish {drive_id} leaves folder_path unset (whole library)", not captured.get("folder_path"))

# neither field → 400 (not a 500)
r = client.post("/connectors/sharepoint/finish", headers=HDR, cookies=COOKIES, json={"tenant": "tenantB"})
check("/finish with neither drive_id nor share_link → 400", r.status_code == 400)

print("\n" + ("ALL #300 SELF-TESTS PASSED — sharing-link resolve + scoped ingest verified (no live tenant)."
              if fails == 0 else f"{fails} #300 CHECK(S) FAILED"))
sys.exit(1 if fails else 0)
