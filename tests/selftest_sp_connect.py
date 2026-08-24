"""S1 self-test for the in-app 'Add SharePoint' OAuth flow (card #148).

Proves the admin-consent flow end-to-end WITHOUT touching Microsoft: pure-function logic
(consent URL, signed CSRF state, callback parsing, drive listing via injected HTTP) plus the
HTTP endpoints (503 when unconfigured, 302 consent redirect, state-guarded callback, picker).
"""
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

os.environ["SELFHOST_BACKEND"] = "memory"   # before importing the app
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
from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)
HDR = {"X-DBSearch-User": "alice"}
# #183: the SharePoint connector app doubles as the multi-tenant login app (see
# user_auth.client_id/client_secret), so configuring SP_CONNECTOR_* above makes
# user_auth.is_enabled() True — current_user then requires a real session cookie,
# not the dev header. Mint one so these HTTP-endpoint checks keep exercising the
# connector wiring rather than the (now-refused) dev-header identity path. #423: carries
# the home tid so the doc-plane tenant gate admits it like production would.
COOKIES = {user_auth.COOKIE: user_auth.sign_session(
    {"oid": "alice", "tid": "home-tid", "exp": int(time.time()) + 3600})}
fails = 0


def check(name, cond):
    global fails
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        fails += 1


print("In-app SharePoint connector — S1 (OAuth admin-consent) self-test:")

# --- pure functions -----------------------------------------------------------------------
url = sp_connect.consent_url("st8")
check("consent_url targets adminconsent w/ our client_id + redirect",
      "/organizations/v2.0/adminconsent" in url and "client_id=cid-123" in url and "state=st8" in url)

# CSRF state is a real token now: a random nonce, carried in the state AND in an httpOnly
# pre-auth cookie, verified against each other at the callback (a signed expiry alone binds
# nothing - one browser's state validated in any other browser's callback).
st, nonce = sp_connect.start_state()
check("signed state round-trips against its own nonce cookie", sp_connect.check_state(st, nonce))
check("tampered state rejected",
      not sp_connect.check_state(st[:-1] + ("0" if st[-1] != "0" else "1"), nonce))
check("garbage state rejected", not sp_connect.check_state("nope", nonce))
check("expired state rejected",
      not sp_connect.check_state(sp_connect.make_state(nonce, ttl=-10), nonce))
check("missing state rejected", not sp_connect.check_state("", nonce))
check("missing nonce cookie rejected", not sp_connect.check_state(st, ""))
other_st, other_nonce = sp_connect.start_state()
check("a state minted for ANOTHER browser is rejected (the CSRF property)",
      not sp_connect.check_state(other_st, nonce) and not sp_connect.check_state(st, other_nonce))
check("each start_state is a fresh nonce", nonce != other_nonce)

t, e = sp_connect.parse_callback({"admin_consent": "True", "tenant": "tenantA", "state": st})
check("callback success → tenant, no error", t == "tenantA" and e == "")
t, e = sp_connect.parse_callback({"error": "access_denied", "error_description": "nope"})
check("callback error surfaced", t == "" and e == "nope")
t, e = sp_connect.parse_callback({"admin_consent": "False", "tenant": "x"})
check("callback without consent rejected", t == "" and e == "consent was not granted")

# list_drives with injected HTTP (no network)
def fake_post(u, form):
    return {"access_token": "TOK"} if u.endswith("/oauth2/v2.0/token") else {}

def fake_get(u, bearer):
    if "/sites?search=" in u:
        return {"value": [{"id": "site-1", "name": "Consulting", "webUrl": "https://c.sharepoint.com"}]}
    if "/sites/site-1/drives" in u:
        return {"value": [{"id": "drive-9", "name": "Proposals"}]}
    return {"value": []}

drives = sp_connect.list_drives("tenantA", post=fake_post, get=fake_get)
check("list_drives maps sites→drives", len(drives) == 1 and drives[0]["driveId"] == "drive-9"
      and drives[0]["driveName"] == "Proposals" and drives[0]["siteName"] == "Consulting")

def fake_get_err(u, bearer):
    return {"error": {"code": "Authorization_RequestDenied", "message": "consent needed"}}
try:
    sp_connect.list_drives("tenantA", post=fake_post, get=fake_get_err)
    check("list_drives raises on Graph error", False)
except RuntimeError as ex:
    check("list_drives raises on Graph error", "consent needed" in str(ex))

# --- HTTP endpoints -----------------------------------------------------------------------
r = client.get("/connectors/sharepoint/consent", headers=HDR, cookies=COOKIES, follow_redirects=False)
check("/consent → 302 to Microsoft adminconsent", r.status_code == 302 and "adminconsent" in r.headers["location"])
nonce = r.cookies.get(sp_connect.STATE_COOKIE)          # the pre-auth CSRF cookie
check("/consent sets the single-use CSRF nonce cookie", bool(nonce))
issued = dict(parse_qsl(urlsplit(r.headers["location"]).query))["state"]
check("/consent's state carries THIS browser's nonce", sp_connect.check_state(issued, nonce))

# unconfigured → 503
os.environ.pop("SP_CONNECTOR_CLIENT_ID")
r = client.get("/connectors/sharepoint/consent", headers=HDR, cookies=COOKIES, follow_redirects=False)
check("/consent → 503 when unconfigured", r.status_code == 503)
os.environ["SP_CONNECTOR_CLIENT_ID"] = "cid-123"   # restore

# callback CSRF: missing / forged / another-browser's state must all be refused. The last is
# the one a signed expiry alone could never catch — an attacker can mint a valid-looking state
# in their OWN browser, and (with account linking) a callback accepted in the VICTIM's browser
# vaults the ATTACKER's credential under the victim's identity.
r = client.get("/connectors/sharepoint/callback?admin_consent=True&tenant=tX", follow_redirects=False)
check("/callback missing state → 400", r.status_code == 400)
r = client.get("/connectors/sharepoint/callback?admin_consent=True&tenant=tX&state=bad", follow_redirects=False)
check("/callback forged state → 400", r.status_code == 400)
foreign, _ = sp_connect.start_state()                   # minted in the attacker's browser
r = client.get(f"/connectors/sharepoint/callback?admin_consent=True&tenant=tX&state={foreign}",
               follow_redirects=False)
check("/callback state not matching this browser's cookie → 400", r.status_code == 400)

# callback: good state → 302 back to UI with tenant, and status shows it connected.
# #431: a connection is now recorded against an OWNER, so the callback needs to identify who
# consented. A real browser carries the session cookie on this redirect (that is how production
# works), so put it in the jar - the state nonce rides in the same jar and both must be sent.
client.cookies.set(user_auth.COOKIE, COOKIES[user_auth.COOKIE])
good = sp_connect.make_state(nonce)
r = client.get(f"/connectors/sharepoint/callback?admin_consent=True&tenant=tenantB&state={good}", follow_redirects=False)
check("/callback good → 302 with tenant", r.status_code == 302 and "tenant=tenantB" in r.headers["location"])
check("/callback clears the nonce cookie", not client.cookies.get(sp_connect.STATE_COOKIE))
r = client.get(f"/connectors/sharepoint/callback?admin_consent=True&tenant=tenantC&state={good}", follow_redirects=False)
check("the nonce is SINGLE-USE: replaying the same state → 400", r.status_code == 400)
stat = client.get("/connectors/sharepoint/status", headers=HDR, cookies=COOKIES).json()
check("/status lists the connected tenant", any(c["tenant"] == "tenantB" for c in stat["connected"]))
check("the replayed callback connected nothing", not any(c["tenant"] == "tenantC" for c in stat["connected"]))

# drives endpoint (monkeypatch the network-touching function)
sp_connect.list_drives = lambda tenant, **k: [{"siteId": "s", "siteName": "S", "driveId": "d", "driveName": "Docs", "web": ""}]
r = client.get("/connectors/sharepoint/drives?tenant=tenantB", headers=HDR, cookies=COOKIES)
check("/drives → 200 library list", r.status_code == 200 and r.json()[0]["driveId"] == "d")

print("\n" + ("ALL S1 SELF-TESTS PASSED — in-app SharePoint OAuth flow verified (no live tenant)."
              if fails == 0 else f"{fails} S1 CHECK(S) FAILED"))
sys.exit(1 if fails else 0)
