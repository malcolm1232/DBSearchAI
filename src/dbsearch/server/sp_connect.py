"""In-app 'Add SharePoint' connector — the browser-driven OAuth flow (card #148).

Model: DBSearch is registered ONCE as a MULTI-TENANT Entra app (its client_id/secret + a web
redirect URI). A customer admin clicks 'Add SharePoint' → we send them to Microsoft's
*admin-consent* screen for OUR app in THEIR tenant. On Accept, Microsoft redirects back with
their tenantId; DBSearch then holds app-only Graph access to that tenant and can read
SharePoint content + ACLs (via GraphSharePointConnector, wired in S2). Query-time trimming
stays EntraIdentity group expansion (LAW 2). App-only matches the existing connector;
delegated/OBO is the SQL-federation path (#102), not this.

Config (env): SP_CONNECTOR_CLIENT_ID, SP_CONNECTOR_CLIENT_SECRET, SP_CONNECTOR_REDIRECT_URI.
Unconfigured → is_configured() is False and the endpoints return 503, so this ships and
unit-tests without a live multi-tenant app.

Testability: every network call goes through http_post_form / http_get_json, injectable so
tests never touch Microsoft.
"""
from __future__ import annotations

import base64
import hmac
import json as _json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

AUTHORITY = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"

# (owner_oid, tenant) -> {tenant, connected_at} for consents completed in THIS process.
# #431: keyed by owner, not by tenant alone. As a tenant-only dict this leaked one identity's
# connection to every other signed-in identity (proven on prod: a foreign-tenant stranger's node
# reported Malcolm's connection as "Connected"), and being in-process it was wiped by every
# restart so a real connection read as "Not connected yet" after each deploy. Memory is now just
# the front cache in front of `_STORE`.
_CONNECTED: dict[tuple[str, str], dict] = {}
_STORE = None                       # set by bind_store(); None => this process only
_STORE_LOCK = threading.Lock()


def _cfg(name: str) -> str:
    return os.environ.get(name, "")


def is_configured() -> bool:
    return bool(_cfg("SP_CONNECTOR_CLIENT_ID") and _cfg("SP_CONNECTOR_CLIENT_SECRET")
                and _cfg("SP_CONNECTOR_REDIRECT_URI"))


# ---- default network I/O (stdlib; injectable for tests) ----------------------------------
# Both NEVER raise on an HTTP error: they return the parsed JSON body (Graph/AAD errors are
# JSON: {"error": {...}}), so callers uniformly branch on `.error` instead of catching
# urllib.error.HTTPError (which previously escaped as an opaque 500).
def _body(fp) -> dict:
    try:
        return _json.loads(fp.read().decode() or "{}")
    except Exception:
        return {}


def http_post_form(url: str, form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:   # nosec B310 — fixed https authority
            return _body(r)
    except urllib.error.HTTPError as e:
        b = _body(e)
        return b if b else {"error": {"code": str(e.code), "message": e.reason}}
    except urllib.error.URLError as e:
        return {"error": {"code": "network", "message": str(e.reason)}}


def http_get_json(url: str, bearer: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:   # nosec B310
            return _body(r)
    except urllib.error.HTTPError as e:
        b = _body(e)
        return b if b else {"error": {"code": str(e.code), "message": e.reason}}
    except urllib.error.URLError as e:
        return {"error": {"code": "network", "message": str(e.reason)}}


# ---- CSRF state: a real per-browser token, not just a signed expiry -----------------------
# The OAuth `state` is the CSRF defense for every leg that lands a credential (SharePoint
# admin consent, Entra sign-in, Google link). A signed EXPIRY alone binds NOTHING: any
# browser's state validates in any other browser's callback, and the login endpoints are
# unauthenticated, so an attacker could simply fetch one and read a valid state out of the
# Location header. With account linking (#193) that is not a nuisance - it is a credential
# swap: a signed-in victim lured to a top-level GET callback carrying the ATTACKER's `code`
# (SameSite=Lax sends the victim's session cookie on a top-level navigation) would vault the
# attacker's refresh token under the VICTIM's oid, and every later query would then run as
# the attacker's identity.
#
# So the state is a RANDOM NONCE that is (a) also stored in an httpOnly, SameSite=Lax
# pre-auth cookie the attacker cannot read or forge, (b) HMAC'd with an expiry so a tampered
# state is rejected before any comparison, and (c) SINGLE-USE - the callback clears the
# cookie. A state minted in the attacker's browser therefore cannot validate in the victim's.
STATE_COOKIE = "dbs_oauth_state"
STATE_TTL = 600


def _secret() -> bytes:
    """HMAC key for the state. Mirrors user_auth._key()'s fallback chain: a deployment that
    configures ONLY Google (or only the AUTH_* tenant app) is first class now, and signing
    its state with the literal "dev-secret" would make every state forgeable offline."""
    return (_cfg("SP_CONNECTOR_CLIENT_SECRET") or _cfg("AUTH_CLIENT_SECRET")
            or _cfg("GOOGLE_CLIENT_SECRET") or "dev-secret").encode()


def make_state(nonce: str, ttl: int = STATE_TTL) -> str:
    """Sign (expiry, nonce). The nonce is what binds the state to ONE browser."""
    exp = str(int(time.time()) + ttl)
    payload = f"{exp}.{nonce}"
    sig = hmac.new(_secret(), payload.encode(), sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def start_state(ttl: int = STATE_TTL) -> tuple[str, str]:
    """Begin an OAuth leg -> (state, nonce). Put `state` in the authorize URL and `nonce` in
    the pre-auth cookie (set_state_cookie); the callback must see BOTH and matching."""
    nonce = secrets.token_urlsafe(24)
    return make_state(nonce, ttl), nonce


def check_state(state: str, cookie_nonce: str) -> bool:
    """True only if `state` is unexpired, untampered, AND carries the nonce this browser
    was issued. A missing cookie (or a missing/forged state) is a hard False - fail-closed."""
    if not state or not cookie_nonce:
        return False
    try:
        exp, nonce, sig = state.split(".", 2)
    except ValueError:
        return False
    good = hmac.new(_secret(), f"{exp}.{nonce}".encode(), sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good):
        return False
    try:
        if int(exp) < int(time.time()):
            return False
    except ValueError:
        return False
    return hmac.compare_digest(nonce, cookie_nonce)


def set_state_cookie(resp, nonce: str, ttl: int = STATE_TTL) -> None:
    """httpOnly so script cannot read it; SameSite=Lax so it IS sent on the IdP's top-level
    redirect back to the callback (the whole point) but not on a cross-site POST."""
    resp.set_cookie(STATE_COOKIE, nonce, httponly=True, samesite="lax",
                    max_age=ttl, path="/")


def clear_state_cookie(resp) -> None:
    """Single-use: a nonce that has reached a callback is spent, whatever the outcome."""
    resp.delete_cookie(STATE_COOKIE, path="/")


# ---- the flow ----------------------------------------------------------------------------
def consent_url(state: str) -> str:
    """Admin-consent URL for OUR multi-tenant app, to be granted in the customer's tenant."""
    q = urllib.parse.urlencode({
        "client_id": _cfg("SP_CONNECTOR_CLIENT_ID"),
        "scope": "https://graph.microsoft.com/.default",
        "redirect_uri": _cfg("SP_CONNECTOR_REDIRECT_URI"),
        "state": state,
    })
    return f"{AUTHORITY}/organizations/v2.0/adminconsent?{q}"


def parse_callback(params: dict) -> tuple[str, str]:
    """Read Microsoft's redirect. Returns (tenant_id, error) — exactly one is non-empty."""
    if params.get("error"):
        return "", str(params.get("error_description") or params.get("error"))
    if str(params.get("admin_consent", "")).lower() != "true":
        return "", "consent was not granted"
    tenant = params.get("tenant", "")
    return (tenant, "") if tenant else ("", "no tenant id in callback")


def app_token(tenant: str, post=http_post_form) -> str:
    """App-only Graph token for a CONSENTED customer tenant (client-credentials)."""
    body = post(f"{AUTHORITY}/{tenant}/oauth2/v2.0/token", {
        "client_id": _cfg("SP_CONNECTOR_CLIENT_ID"),
        "client_secret": _cfg("SP_CONNECTOR_CLIENT_SECRET"),
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    })
    return body.get("access_token", "")


def _errmsg(resp: dict) -> str:
    e = resp.get("error")
    if isinstance(e, dict):
        return f"{e.get('code', '?')}: {e.get('message', '')}"
    return str(e)


#: Sites SharePoint and Viva create for themselves. Nobody ingests them, and shown as peers of
#: real sites they are what made the owner ask "since when i have so many docs?" of his own
#: tenant - seven rows, four of them plumbing, all of them labelled "Documents". They are
#: TAGGED here rather than dropped: the surface de-emphasises them behind a disclosure, because
#: a list that silently omits rows gives nobody a way to tell filtering from absence.
#: Matched on the site's own name, which is what Graph gives us and what the tenant admin sees.
_SYSTEM_SITE_PREFIXES = ("contenttypehub", "appcatalog", "allcompany",
                         "groupforanswersinvivaengage", "portals", "search")


def _is_system_site(name: str, web_url: str = "") -> bool:
    n = (name or "").strip().lower()
    if any(n.startswith(p) for p in _SYSTEM_SITE_PREFIXES):
        return True
    # A personal OneDrive is a site too, and it is not a shared library either.
    return "-my.sharepoint.com" in (web_url or "").lower()


def list_drives(tenant: str, site_search: str = "*", post=http_post_form, get=http_get_json) -> list[dict]:
    """List document libraries (drives) across the tenant's SharePoint sites.

    Robust enumeration: `/sites?search=*` is flaky (some tenants 500 it), so we tolerate its
    failure and ALWAYS include the root site — which holds the default 'Documents' library —
    so the picker is never empty when SharePoint exists."""
    tok = app_token(tenant, post=post)
    if not tok:
        raise RuntimeError("could not obtain a Graph app token — is admin consent effective yet? (wait ~30s and retry)")

    sites: list[dict] = []
    seen: set[str] = set()
    search = get(f"{GRAPH}/sites?search={urllib.parse.quote(site_search)}", tok)
    for s in search.get("value", []):
        if s.get("id") and s["id"] not in seen:
            seen.add(s["id"]); sites.append(s)
    root = get(f"{GRAPH}/sites/root", tok)          # reliable; the main Communication site
    if root.get("id") and root["id"] not in seen:
        seen.add(root["id"]); sites.append(root)

    if not sites:
        raise RuntimeError(f"could not list SharePoint sites ({_errmsg(search)}). Consent may still be propagating — retry in ~30s.")

    # #879: one /drives call per site, CONCURRENTLY. Serially this was measured at 11.2s on the
    # owner's tenant (7 sites), every second of which the picker spent blank - and the whole
    # enumeration is optional to the folder-link path most people use. Threads rather than
    # asyncio because this module is deliberately stdlib-urllib throughout and the route that
    # calls it is a sync def, which FastAPI already runs in its threadpool; the work is pure
    # network wait, so the GIL costs nothing here. The `get` seam stays injectable for tests.
    # Bounded: a tenant with hundreds of sites must not open hundreds of sockets at once.
    def _drives_for(site: dict) -> list[dict]:
        try:
            drives = get(f"{GRAPH}/sites/{site['id']}/drives", tok)
        except Exception:
            return []          # one bad site never costs the user the whole picker
        name = site.get("name") or site.get("displayName", "")
        return [{"siteId": site["id"], "siteName": name,
                 "driveId": d["id"], "driveName": d.get("name", ""),
                 "web": site.get("webUrl", ""),
                 "system": _is_system_site(name, site.get("webUrl", ""))}
                for d in drives.get("value", [])]

    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(sites)))) as pool:
        # map, not as_completed: the order of `sites` is the order the picker shows, and a
        # list that reshuffles itself between two openings is a list nobody can learn.
        for rows in pool.map(_drives_for, sites):
            out.extend(rows)
    if not out:
        raise RuntimeError("consent is granted but no document libraries were found in this tenant.")
    return out


# ---- sharing-link → (drive, folder) resolve (#300) ---------------------------------------
def encode_share_url(link: str) -> str:
    """Graph's sharing-URL encoding: 'u!' + base64url(link) with '=' padding stripped.
    https://learn.microsoft.com/graph/api/shares-get#encoding-sharing-urls"""
    return "u!" + base64.urlsafe_b64encode(link.strip().encode()).decode().rstrip("=")


def resolve_share_link(tenant: str, link: str, post=http_post_form, get=http_get_json) -> dict:
    """#300: resolve a SharePoint FOLDER sharing link to the (drive_id, folder_path) the
    connector ingests, so a user can ingest ONE folder instead of a whole library.

    App-only Graph read (the access already consented for the connector). The link only
    LOCATES the folder — it never widens visibility: GraphSharePointConnector still reads each
    item's ACL and the QueryService + EntraIdentity group-expansion trim every result at query
    time (LAW 2). MVP = folders; a file link is refused with actionable guidance."""
    tok = app_token(tenant, post=post)
    if not tok:
        raise RuntimeError("could not obtain a Graph app token — is admin consent effective yet? (wait ~30s and retry)")
    item = get(f"{GRAPH}/shares/{encode_share_url(link)}/driveItem"
               "?$select=id,name,folder,file,parentReference,webUrl", tok)
    if item.get("error"):
        raise RuntimeError(f"could not resolve the sharing link ({_errmsg(item)}).")
    if not item.get("folder"):
        raise RuntimeError("that link points to a file, not a folder — paste a link to a FOLDER "
                           "(or use the library picker above to ingest a whole library).")
    parent = item.get("parentReference") or {}
    drive_id = parent.get("driveId", "")
    if not drive_id:
        raise RuntimeError("the sharing link resolved without a drive id — it may be an external or guest share.")
    # parent.path is the FOLDER'S PARENT path from root (e.g. '' at root, or '/Books' when nested);
    # the folder's OWN path — what GraphSharePointConnector._in_scope matches — appends its name.
    parent_after_root = parent.get("path", "").split("root:", 1)[-1]
    folder_path = f"{parent_after_root}/{item.get('name', '')}".strip("/")
    return {"drive_id": drive_id, "folder_path": folder_path,
            "name": item.get("name", ""), "web": item.get("webUrl", "")}


# ---- connected-tenant registry, per owner and durable (#431) -----------------------------
def bind_store(store) -> None:
    """Attach the durable per-owner store (app.py wires it). None keeps this process-only, so a
    rig without Postgres still works - durability is an improvement, never a new dependency."""
    global _STORE
    with _STORE_LOCK:
        _STORE = store


def reset_for_test() -> None:
    """Clear the in-process cache. Also how a test simulates a restart: whatever a fresh process
    would know comes from the bound store, not from this dict."""
    _CONNECTED.clear()


def mark_connected(tenant: str, owner: str = "") -> None:
    """Record that `owner` completed admin consent for `tenant`.

    An empty owner is refused rather than recorded globally: a connection nobody owns is exactly
    the state that leaked, and "shared by default" is the shape #431 existed to remove."""
    if not owner:
        return
    at = int(time.time())
    _CONNECTED[(owner, tenant)] = {"tenant": tenant, "connected_at": at}
    store = _STORE
    if store is not None:
        store.put(owner, tenant, at)        # store swallows + logs its own failures


def connected_tenants(owner: str = "") -> list[dict]:
    """This owner's connections - never anyone else's. No owner means no connections, not all of
    them: the caller has to say who is asking, which is what makes a leak impossible to write."""
    if not owner:
        return []
    rows = {c["tenant"]: c for (o, _t), c in _CONNECTED.items() if o == owner}
    store = _STORE
    if store is not None:
        for row in store.list(owner):       # cold process after a restart
            rows.setdefault(row["tenant"], row)
    return [rows[t] for t in sorted(rows)]


# ---- ingest progress (#302) --------------------------------------------------------------
# The ingest /finish is one long synchronous request (~minutes for a big library). FastAPI runs
# it in a threadpool, so a concurrent GET /ingest-progress reads this store while /finish writes
# it (CPython dict ops are atomic; a stale read just shows slightly old progress — harmless).
_PROGRESS: dict[str, dict] = {}


def set_progress(tenant: str, phase: str, done: int, total: int) -> None:
    _PROGRESS[tenant] = {"phase": phase, "done": int(done), "total": int(total),
                         "at": int(time.time())}


def get_progress(tenant: str) -> dict | None:
    return _PROGRESS.get(tenant)


def clear_progress(tenant: str) -> None:
    _PROGRESS.pop(tenant, None)


# #365: /finish runs the whole crawl inside one long POST; a proxy (Cloudflare ~100s) can
# drop the response while the crawl keeps running. The outcome therefore must ALSO land in
# this store — as a terminal state the canvas poller can consume — or the client resets a
# UI whose ingest actually succeeded. Terminal states persist until ack'd (or overwritten
# by the next ingest's "starting"); ack only ever clears terminal states, never a live one.
def set_progress_complete(tenant: str, result: dict) -> None:
    _PROGRESS[tenant] = {"phase": "complete", "result": result, "at": int(time.time())}


def set_progress_error(tenant: str, detail: str) -> None:
    _PROGRESS[tenant] = {"phase": "error", "detail": detail, "at": int(time.time())}


def ack_progress(tenant: str) -> None:
    p = _PROGRESS.get(tenant)
    if p and p.get("phase") in ("complete", "error"):
        _PROGRESS.pop(tenant, None)
