"""Real per-user Microsoft/Entra sign-in (#171).

Confidential authorization-code flow: the code is exchanged server-side at the token endpoint
(TLS + client secret), so the returned id_token is *transport-trusted* — we read the verified
`oid`/`name` from its payload without a separate JWKS signature check (the token came directly
from Microsoft, not from the browser). Session identity is a signed cookie carrying the oid;
`current_user` prefers it over the dev-auth header (LAW 2: the client can never choose whose
results it gets — a real login is verified, the dev switcher is dev-only).

Per-user LAW 2 group expansion reuses the app-only Graph token (the connector app already holds
GroupMember.Read.All / User.Read.All *application* permissions), so a signed-in user sees exactly
the SharePoint content their real Entra group memberships allow.

Reuses sp_connect's OAuth I/O + CSRF state signing; adds only the delegated user-login leg.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from hashlib import sha256

from dbsearch.server.sp_connect import (
    AUTHORITY, GRAPH, STATE_COOKIE, app_token, check_state, clear_state_cookie,
    http_post_form, make_state, set_state_cookie, start_state,
)

COOKIE = "dbs_session"
_SESSION_TTL = 8 * 3600

DB_RESOURCE_SCOPE = "https://database.windows.net/user_impersonation"


class NotSignedIn(RuntimeError):
    """Raised when a delegated ask arrives for a user with no vaulted credential for the
    IdP the store's delegation names. The message is user-facing: it rides the executor's
    drop+disclose channel. `.idp` lets the caller say WHICH cloud to connect."""

    def __init__(self, message: str, idp: str = "entra") -> None:
        super().__init__(message)
        self.idp = idp


_NOT_LINKED = {
    "entra": ("sign in to query this source — queries run as you, and this "
              "session has no Microsoft sign-in for your identity"),
    "google": ("connect Google to query this source - queries run as you, and this "
               "session has no linked Google account"),
    # ADR 0024: AWS is linked by vaulting the user's own access keys (a form, not an OAuth
    # dance), so the remedy names the account menu rather than a sign-in.
    "aws": ("connect Amazon to query this source - queries run as your own AWS "
            "identity, and this account has no AWS credential. Add your AWS access "
            "keys from the account menu"),
}


def not_linked(idp: str) -> NotSignedIn:
    """The fail-closed error for 'this identity has no credential for that cloud' - whether
    because the user never linked it or because that cloud's login is not configured at all.
    Same user-facing remedy either way: connect that cloud (LAW 2 - never substitute
    another cloud's credential, never guess)."""
    return NotSignedIn(_NOT_LINKED.get(idp, _NOT_LINKED["entra"]), idp=idp)


#: Every cloud this vault can hold a credential for. The SecretsPort has no `list()` - by
#: design, since enumerable secrets are a liability - so `linked()` probes this fixed set
#: instead. Adding an IdP means adding it here, which is why it sits next to _NOT_LINKED.
#:
#: "aws" (ADR 0024) is not an OAuth IdP - the vaulted value is a JSON access-key document,
#: not a refresh token - but the vault's contract was never "refresh tokens": it is one
#: long-lived per-(account, cloud) credential that an exchange redeems into short-lived
#: per-request credentials, and AWS keys satisfy it exactly. Being here buys linked() in
#: /auth/me, the account-panel row, /auth/disconnect/aws and drop-on-logout for free.
KNOWN_IDPS = ("entra", "google", "aws")

#: Reserved namespace for vaulted refresh tokens inside the secrets store. THREE segments,
#: where every user-writable handle is exactly FOUR (tenant/owner/store/field, and no segment
#: may contain '/'). The mismatch is the security property: no /secrets caller can address a
#: vault entry - neither to inject a refresh token for a victim, nor to read its last-4 hint
#: back out of describe_secret. selftest_token_vault_durable pins it.
_VAULT_NS = "authrt"


def _vault_name(oid: str, idp: str) -> str:
    return f"{_VAULT_NS}/{idp}/{oid}"


class TokenVault:
    """Server-side (oid, idp) -> refresh_token store (#156 Entra, #193 multi-IdP).
    One sign-in per cloud vaults a refresh token; the broker redeems it per
    (user, resource). Account linking (#193): ONE session identity may hold N cloud
    credentials, so a single ask can fan out across clouds, each leg as the user.
    Never in the cookie, never logged, never returned over an API.

    #435: DURABLE when a SecretsPort is bound. It used to be a plain in-process dict, while the
    session cookie is an 8h signed token that survives anything - so every deploy left users
    apparently signed in but unable to query, their delegated credential silently gone. A
    refresh token IS a user credential, so it rests in the same Fernet-encrypted store as
    user-supplied database passwords (#319/#417): one at-rest story, no new infrastructure.

    Memory stays the front cache and the fallback. With no store bound (no DBSEARCH_SECRET_KEY)
    behaviour is exactly as before - durability is an improvement, never a new hard dependency
    on the sign-in path. Every durable operation is best-effort for the same reason: a store
    that is full, read-only, or holding ciphertext from a rotated key must cost the user a
    re-sign-in, never their ability to sign in.
    """

    def __init__(self, store=None) -> None:
        self._rt: dict[tuple[str, str], str] = {}     # (oid, idp) -> refresh_token
        self._lock = threading.Lock()
        self._store = store

    def bind_store(self, store) -> None:
        """Attach the secrets store after construction - VAULT is a module singleton created at
        import time, while the store is built later in app.py (lazily, so a box with no key still
        boots). Binding is idempotent and never migrates: tokens already in memory stay usable
        and land durably on their next put."""
        with self._lock:
            self._store = store

    # ---- durable side (best-effort by contract; see the class docstring) ------------------
    def _remember(self, oid: str, idp: str, refresh_token: str) -> None:
        if self._store is None:
            return
        try:
            self._store.put_secret(_vault_name(oid, idp), refresh_token)
        except Exception:
            # Deliberately silent about the VALUE and quiet about the failure: this runs inside
            # /auth/callback, and an exception here would turn a successful sign-in into a failed
            # one. The user keeps a working session; it just will not survive a restart.
            pass

    def _recall(self, oid: str, idp: str) -> str:
        if self._store is None:
            return ""
        try:
            return self._store.get_secret(_vault_name(oid, idp)) or ""
        except Exception:
            # Missing, or ciphertext under a rotated key. Both mean "no usable credential",
            # which the caller turns into "sign in again" - never a 500.
            return ""

    def _forget(self, oid: str, idp: str) -> None:
        if self._store is None:
            return
        try:
            self._store.delete_secret(_vault_name(oid, idp))
        except Exception:
            pass

    # ---- api -----------------------------------------------------------------------------
    def put(self, oid: str, refresh_token: str, idp: str = "entra") -> None:
        with self._lock:
            self._rt[(oid, idp)] = refresh_token
        self._remember(oid, idp, refresh_token)

    def get(self, oid: str, idp: str = "entra") -> str:
        with self._lock:
            rt = self._rt.get((oid, idp))
        if not rt:
            rt = self._recall(oid, idp)          # cold process after a restart
            if rt:
                with self._lock:
                    self._rt[(oid, idp)] = rt
        if not rt:
            raise not_linked(idp)
        return rt

    def drop(self, oid: str, idp: "str | None" = None) -> None:
        """idp=None drops EVERY cloud credential for this identity (logout).

        The durable copy goes too. A logout that cleared only memory would leave a redeemable
        credential on disk, and the next restart would resurrect the session the user just ended.
        """
        for cloud in ([idp] if idp else list(KNOWN_IDPS)):
            self._forget(oid, cloud)
        with self._lock:
            for key in [k for k in self._rt if k[0] == oid and (idp is None or k[1] == idp)]:
                self._rt.pop(key, None)

    def linked(self, oid: str) -> list[str]:
        """Which clouds this identity can be redeemed against - memory OR durable store, since
        after a restart the answer lives only on disk and /auth/me reports it to the user.

        A credential that cannot be DECRYPTED is not linked: reporting it would tell the user
        they are connected to a cloud that will fail on first use."""
        with self._lock:
            found = {k[1] for k in self._rt if k[0] == oid}
        for idp in KNOWN_IDPS:
            if idp not in found and self._recall(oid, idp):
                found.add(idp)
        return sorted(found)


VAULT = TokenVault()


def _cfg(name: str) -> str:
    return os.environ.get(name, "")


def auth_tenant() -> str:
    return _cfg("AUTH_TENANT_ID")


def _tenant_app_configured() -> bool:
    """Atomic predicate: all three AUTH_* vars must be set to use the tenant app."""
    return bool(_cfg("AUTH_TENANT_ID") and _cfg("AUTH_CLIENT_ID")
                and _cfg("AUTH_CLIENT_SECRET"))


def client_id() -> str:
    """OAuth client ID: tenant app (single-tenant, with delegated scopes) if fully configured,
    else SharePoint connector app (multi-tenant)."""
    return _cfg("AUTH_CLIENT_ID") if _tenant_app_configured() else _cfg("SP_CONNECTOR_CLIENT_ID")


def client_secret() -> str:
    """OAuth client secret: tenant app if fully configured, else SharePoint connector app."""
    return _cfg("AUTH_CLIENT_SECRET") if _tenant_app_configured() else _cfg("SP_CONNECTOR_CLIENT_SECRET")


def redirect_uri() -> str:
    return _cfg("AUTH_REDIRECT_URI") or "http://localhost:8080/auth/callback"


def multi_tenant_enabled() -> bool:
    """ADR 0011: the operator's explicit opt-in to /organizations. The tenant app stays
    the OAuth client either way, so data_scopes() (DB delegation) is unaffected."""
    return os.environ.get("DBSEARCH_MULTI_TENANT", "") == "1"


def _authority() -> str:
    """OAuth authority: tenant-specific for the single-tenant app; /organizations when the
    operator opts in (ADR 0011) or when no tenant app is configured (legacy fallback)."""
    if _tenant_app_configured() and not multi_tenant_enabled():
        return f"{AUTHORITY}/{auth_tenant()}"
    return f"{AUTHORITY}/organizations"


def data_scopes() -> str:
    """Extra delegated scopes for downstream data planes — only when the tenant
    app (which has them consented) is fully configured.

    This is what the BROKER redeems with; it is deliberately NOT what sign-in asks for.
    See identity_scopes() for why."""
    return f" offline_access {DB_RESOURCE_SCOPE}" if _tenant_app_configured() else ""


def identity_scopes() -> str:
    """What SIGN-IN asks for: identity, plus a refresh token when we have a tenant app to
    redeem it with. Never a resource scope (#429).

    Requesting `database.windows.net/user_impersonation` here made Microsoft reject the whole
    login with AADSTS650052 for any org that has never provisioned an Azure SQL service
    principal — which is most of them. The user never reached the app, so the failure was
    unattributable and unfixable from their side. Resource consent belongs at the moment the
    user connects that resource (db_consent_url), mirroring the Google leg's incremental
    per-channel scopes (#193). offline_access needs no resource principal, so it is safe for
    any tenant."""
    return "openid profile email" + (" offline_access" if _tenant_app_configured() else "")


def db_consent_scopes() -> str:
    """The incremental round's scopes: the DB resource itself. Empty without a tenant app —
    there is no consented delegation to ask for, and asking would 650052 for nothing."""
    return f"offline_access {DB_RESOURCE_SCOPE}" if _tenant_app_configured() else ""


def is_enabled() -> bool:
    """Real login is available only when the multi-tenant app is configured."""
    return bool(client_id() and client_secret())


def has_real_signing_key() -> bool:
    """True when `dbs_session` is signed with a REAL secret - never the literal
    "dev-secret" fallback in `_key()` below.

    #574 code review (CRITICAL): every session-minting path used to be reachable only
    when a real secret already existed - the Entra callback requires `is_enabled()`
    (a configured `AUTH_CLIENT_SECRET`), the Google callback requires `google_auth.
    is_enabled()` (`GOOGLE_CLIENT_SECRET`). Local auth is the first path that can mint a
    session from nothing but a password POST, so `local_auth.is_enabled()` gates itself
    on THIS function - a deployment that sets `DBSEARCH_LOCAL_AUTH=1` with no real
    secret anywhere gets local auth OFF, not a working-but-forgeable one."""
    return bool(os.environ.get("DBSEARCH_SESSION_KEY") or client_secret()
               or os.environ.get("GOOGLE_CLIENT_SECRET"))


def _key() -> bytes:
    # DO NOT let a new session-minting path rely on the "dev-secret" fallback below
    # without also gating itself on has_real_signing_key() (see #574 code review): that
    # string is public (it is sitting right here in the source), so a session signed
    # with it is forgeable by anyone who has read this file - mint any oid you like,
    # HMAC it with "dev-secret", and resolve_identity hands you that account's corpus.
    # It is safe ONLY because every real-login path is required to have a real secret
    # BEFORE it can mint a cookie at all; a path that mints without that guarantee must
    # refuse to enable itself instead (local_auth.is_enabled() is the precedent).
    return (os.environ.get("DBSEARCH_SESSION_KEY") or client_secret()
            or os.environ.get("GOOGLE_CLIENT_SECRET") or "dev-secret").encode()


# ---- signed session cookie (HMAC; same shape as sp_connect state) ------------------------
def sign_session(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    mac = hmac.new(_key(), body.encode(), sha256).hexdigest()[:32]
    return body + "." + mac


def read_session(token: str) -> dict | None:
    if not token or "." not in token:
        return None
    body, mac = token.split(".", 1)
    if not hmac.compare_digest(mac, hmac.new(_key(), body.encode(), sha256).hexdigest()[:32]):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return data


# ---- OIDC authorization-code flow --------------------------------------------------------
def _authorize_url(state: str, scope: str, prompt: str) -> str:
    q = urllib.parse.urlencode({
        "client_id": client_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "response_mode": "query",
        "scope": scope,
        "state": state,
        "prompt": prompt,
    })
    return f"{_authority()}/oauth2/v2.0/authorize?{q}"


def login_url(state: str) -> str:
    return _authorize_url(state, identity_scopes(), "select_account")


def db_consent_url(state: str) -> str:
    """Second, OPTIONAL round: ask this user's own tenant to consent to the DB delegation,
    at the moment they connect a database. `prompt=consent` because the point is to surface
    the consent screen (or the tenant's admin-approval path) rather than silently reuse a
    grant that does not exist yet."""
    return _authorize_url(state, db_consent_scopes() or identity_scopes(), "consent")


def _jwt_payload(jwt: str) -> dict:
    part = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def exchange_code(code: str, post=http_post_form, scope: str = "") -> dict:
    """Exchange the auth code for tokens; return the verified {oid, tid, name, email, refresh_token}.

    `scope` must match the round that produced the code (#429): the identity round for a
    normal sign-in, the DB round for the incremental grant. Asking the token endpoint for a
    resource the code was never consented for is how the 650052 class of failure gets
    reintroduced at exchange time."""
    r = post(f"{_authority()}/oauth2/v2.0/token", {
        "client_id": client_id(),
        "client_secret": client_secret(),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
        "scope": scope or identity_scopes(),
    })
    idt = r.get("id_token")
    if not idt:
        raise RuntimeError(r.get("error_description") or r.get("error") or "token exchange failed")
    c = _jwt_payload(idt)
    return {
        "oid": c.get("oid") or c.get("sub"),
        "tid": c.get("tid", ""),
        "name": c.get("name") or c.get("preferred_username") or "user",
        "email": c.get("preferred_username") or c.get("email") or "",
        "refresh_token": r.get("refresh_token", ""),
    }


# ---- per-user real principal expansion (app-only Graph; reused by LAW 2) ------------------
def fetch_member_principals(tenant: str, user_oid: str, token_fn=app_token) -> "list[str] | None":
    """Every transitive DIRECTORY PRINCIPAL a user belongs to, via the app-only Graph token.

    `getMemberObjects`, NOT `getMemberGroups` (#872). getMemberGroups returns groups and ONLY
    groups; getMemberObjects returns groups, DIRECTORY ROLES and administrative units. That is
    not a nicety — it was the difference between a customer's SharePoint library being readable
    and being invisible. Measured on prod 2026-08-20: every file in the owner's library carried
    the grantee `d99e09e2…`, which SharePoint reports under the identitySet's `group` key and
    which is really the DIRECTORY ROLE "Global Administrator" (roleTemplateId 62e90394-…).
    getMemberGroups returned three groups and never that id; getMemberObjects returned the same
    three PLUS that id, and the owner is the role's only member. So with getMemberGroups his own
    documents answered "I do not have that information" forever, and no amount of Graph consent
    could have changed it.

    LAW 2 — why this widening is faithful rather than permissive. It expands what a caller's
    principals are, so it deserves the suspicion. The ACL it is matched against is DERIVED FROM
    THE SOURCE'S OWN permission list (sharepoint_graph.fetch_acl reads drive-item permissions),
    and SharePoint hands out directory roles as grantees exactly as it hands out groups. Refusing
    to expand roles does not make the trim tighter — it makes it WRONG, denying a person access
    the source itself granted them. We match the source's model or we misrepresent it; there is
    no third option that is also honest. It never widens beyond what the source said, because
    every principal here must still appear in an ACL the connector copied verbatim.

    Returns None when the LOOKUP FAILED, [] when the user genuinely belongs to nothing (#266).
    Those were the same value until a caller needed to cache the result: caching a failed lookup
    as "no principals" turns a transient Graph blip into a silent denial that lasts until the
    process restarts — the user is told "I couldn't find anything you have access to" about a
    document they are perfectly entitled to. #875 found the sign-in path doing exactly that with
    an `or []`, so this distinction is only as good as every caller's respect for it.

    Still fail-closed either way: a caller that cannot resolve must expand to the user's own oid
    alone, never to more. None communicates "unknown, try again", not "allow"."""
    try:
        tok = token_fn(tenant)
        req = urllib.request.Request(
            f"{GRAPH}/users/{urllib.parse.quote(user_oid)}/getMemberObjects",
            data=json.dumps({"securityEnabledOnly": False}).encode(),
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:   # nosec B310 — fixed https authority
            return json.loads(resp.read().decode() or "{}").get("value", [])
    except Exception:
        return None                      # unknown — NOT "no principals". See the docstring.


#: The directory-object types the ACL picker must be able to NAME (#258, widened by #872).
#: `directoryRole` is here because a role is a principal an ACL can genuinely name — the whole
#: reason #872 moved expansion to getMemberObjects — and getByIds returns ONLY the types it is
#: asked for. Verified against live Graph 2026-08-20 rather than read off the docs: with
#: ["group","user"] the role id came back in a value array of ONE (silently absent, no error);
#: adding "directoryRole" returned it as '#microsoft.graph.directoryRole' "Global Administrator".
#: Without this, a role-ACL'd document would expand correctly and then sit in the picker with no
#: name, which `list_directory` drops — access granted, and invisible to the operator managing it.
_NAMEABLE_TYPES = ["group", "user", "directoryRole"]


def _kind_of(odata_type: str) -> str:
    """`#microsoft.graph.directoryRole` -> `directoryRole`. Unrecognised shapes fall back to
    `group`, which is what every non-user principal was called before #881 - a wrong LABEL is
    a cosmetic regression, an exception here would cost the caller their whole directory."""
    tail = str(odata_type or "").rsplit(".", 1)[-1].strip()
    return tail or "group"


def fetch_principal_facts(tenant: str, oids: list[str],
                          token_fn=app_token) -> dict[str, dict[str, str]]:
    """Display name AND directory kind per principal oid — users, groups, roles (#258, #872).

    Returns `{oid: {"name": str, "kind": "user"|"group"|"directoryRole"}}`.

    Both facts are COSMETIC — they label a picker. LAW 2 only ever compares oids, so neither
    a missing name nor a wrong kind may widen or narrow access. Fail-closed to {}: an unnamed
    principal simply stays out of the picker (see IdentityPort.list_directory) rather than
    appearing as a bare GUID the operator cannot verify.

    #881 is why `kind` is here at all, and it was a rename rather than a second call because
    this response is the ONLY place in the product where a principal's type is ever visible.
    `getMemberObjects` returns bare oids; a directory-role oid is a GUID indistinguishable
    from a group's. So the type was arriving on every sign-in and being dropped on the floor
    one line below where it landed, which is why the ACL picker called the tenant's
    "Global Administrator" role a group, and why filtering roles anywhere downstream was
    impossible without re-asking Graph."""
    if not oids:
        return {}
    try:
        tok = token_fn(tenant)
        req = urllib.request.Request(
            f"{GRAPH}/directoryObjects/getByIds",
            data=json.dumps({"ids": list(oids)[:1000],
                             "types": _NAMEABLE_TYPES}).encode(),
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:   # nosec B310 — fixed https authority
            body = json.loads(resp.read().decode() or "{}")
    except Exception:
        return {}
    out: dict[str, dict[str, str]] = {}
    for o in body.get("value", []):
        label = (o.get("displayName") or o.get("userPrincipalName") or "").strip()
        if o.get("id") and label:
            out[o["id"]] = {"name": label, "kind": _kind_of(o.get("@odata.type"))}
    return out


__all__ = ["COOKIE", "STATE_COOKIE", "is_enabled", "login_url", "exchange_code",
           "sign_session", "read_session", "fetch_member_principals", "fetch_principal_facts",
           "make_state", "start_state",
           "check_state", "set_state_cookie", "clear_state_cookie", "redirect_uri",
           "NotSignedIn", "not_linked", "TokenVault", "VAULT", "data_scopes", "client_id",
           "client_secret", "multi_tenant_enabled", "identity_scopes", "db_consent_scopes",
           "db_consent_url"]
