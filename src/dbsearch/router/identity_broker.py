"""Identity broker — federated per-store authorization, gate #2 (Phase E E5, card #102).

ADR 0006: DBSearch never owns an identity→row-policy mapping. The broker resolves each
(user, store) into an AccessContext whose PREFERRED material is a delegated credential
obtained by OIDC token exchange (RFC 8693 / Entra OBO) — the query then runs AS THE USER
and the source's own IAM/RLS enforces. A row-policy predicate is the registered FALLBACK
for sources with no delegation path; a store with neither gets principals only (the
indexed-store trim). Credential isolation: one TokenExchangePort instance per source,
each with its own (user, resource) cache — a leaked cache never crosses sources.

Per-cloud exchanges (Entra OBO / GCP WIF / AWS STS) are this module's OboTokenExchange
with cloud-specific endpoints+params, or thin siblings behind the same TokenExchangePort.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Callable

from dbsearch.ports.base import IdentityPort
from dbsearch.router.store import AccessContext

GRANT_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
TOKEN_TYPE_ACCESS = "urn:ietf:params:oauth:token-type:access_token"

# post(url, form) -> parsed JSON dict — injected so tests/adapters never touch the network
PostForm = Callable[[str, dict], dict]
RowPolicyFn = Callable[[str, list], str]     # (user_oid, principals) -> SQL predicate


class TokenExchangePort(ABC):
    @abstractmethod
    def exchange(self, user_oid: str, resource: str) -> str:
        """Return a source-scoped bearer for this user (query-as-the-user)."""


class StaticTokenExchange(TokenExchangePort):
    """Dev/spike delegation: a fixed bearer, or a per-user map. Pairs with E3b's env
    spike; production uses OboTokenExchange."""

    def __init__(self, token: "str | dict") -> None:
        self._token = token

    def exchange(self, user_oid: str, resource: str) -> str:
        if isinstance(self._token, dict):
            try:
                return self._token[user_oid]
            except KeyError:
                raise RuntimeError(f"no static token for user {user_oid!r}")
        return self._token


class TokenExchangeError(RuntimeError):
    """A token endpoint refused the exchange, carrying the IdP's OWN reason.

    `expired_grant` is the one an operator can act on themselves: the vaulted refresh token
    is dead (expired, revoked, or already rotated), and the fix is simply to sign in again —
    as opposed to a misconfigured app, which it is not.

    `needs_db_consent` is the OTHER user-fixable one (#429). Since sign-in no longer requests
    the Azure SQL delegation, a user's first delegated query can arrive before their tenant
    has ever consented to it — an expected step, not a fault. It must route them to the
    incremental consent round instead of showing them an AADSTS code."""

    #: AADSTS codes that mean "this identity/tenant has not consented to the resource yet",
    #: as opposed to a broken deployment. 65001 = user or admin has not consented;
    #: 650052 = the tenant has no service principal for the resource at all (the #429
    #: failure, which now surfaces here — where it is actionable — instead of at sign-in).
    _CONSENT_CODES = ("AADSTS65001", "AADSTS650052", "AADSTS650057")

    def __init__(self, status: int, error: str, description: str, correlation_id: str = ""):
        self.status, self.error, self.description = status, error, description
        self.correlation_id = correlation_id
        detail = description or error or f"HTTP {status}"
        super().__init__(detail + (f" [correlation_id={correlation_id}]" if correlation_id else ""))

    @property
    def expired_grant(self) -> bool:
        """A dead grant — but NOT one that is really an un-consented resource. Entra returns
        `invalid_grant` for both, so the consent codes are checked first or every missing
        consent would be mislabelled 'your sign-in has expired' and the user would re-sign-in
        forever without ever being offered the grant they actually need."""
        return self.error == "invalid_grant" and not self.needs_db_consent

    @property
    def needs_db_consent(self) -> bool:
        return any(c in (self.description or "") for c in self._CONSENT_CODES)


def http_post_form(url: str, form: dict) -> dict:
    import json
    from urllib import error as urlerror
    from urllib import parse, request

    req = request.Request(url, data=parse.urlencode(form).encode(),
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          method="POST")
    try:
        with request.urlopen(req, timeout=15) as resp:  # noqa: S310 — token endpoint from config
            return json.loads(resp.read().decode())
    except urlerror.HTTPError as exc:
        # The BODY is the whole diagnostic — Microsoft returns error=invalid_grant with
        # "AADSTS700082: The refresh token has expired…" and a correlation id. urllib raises
        # before anyone reads it, so every delegated-auth failure used to collapse into an
        # identical, useless "HTTP Error 400: Bad Request" and an operator could not tell
        # "sign in again" from "grant consent" from "the app is misconfigured" (#205).
        try:
            body = json.loads(exc.read().decode())
        except Exception:
            body = {}
        raise TokenExchangeError(exc.code, body.get("error", ""),
                                 body.get("error_description", ""),
                                 body.get("correlation_id", "")) from exc


class OboTokenExchange(TokenExchangePort):
    """RFC 8693 token exchange (the Entra on-behalf-of shape). The user's inbound
    assertion comes from `subject_token_provider`; the exchanged, source-scoped token is
    cached per (user, resource) honoring expires_in minus a safety skew."""

    def __init__(self, token_url: str, client_id: str, client_secret: str,
                 subject_token_provider: Callable[[str], str],
                 *, post: PostForm | None = None, skew_s: float = 30.0) -> None:
        self._url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._subject = subject_token_provider
        self._post = post or http_post_form
        self._skew = skew_s
        self._cache: dict[tuple, tuple] = {}     # (user, resource) -> (token, expires_at)

    def exchange(self, user_oid: str, resource: str) -> str:
        key = (user_oid, resource)
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and hit[1] > now:
            return hit[0]
        body = self._post(self._url, {
            "grant_type": GRANT_TOKEN_EXCHANGE,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "subject_token": self._subject(user_oid),
            "subject_token_type": TOKEN_TYPE_ACCESS,
            "requested_token_type": TOKEN_TYPE_ACCESS,
            "resource": resource,
        })
        token = body["access_token"]
        expires_at = now + float(body.get("expires_in", 0)) - self._skew
        self._cache[key] = (token, expires_at)
        return token


class _CachedExchange(TokenExchangePort):
    """Shared per-(user, resource) credential cache honoring expiry minus skew —
    ADR 0006's 'one token exchange per (user, source), amortized by caching'."""

    def __init__(self, skew_s: float = 30.0) -> None:
        self._skew = skew_s
        self._cache: dict[tuple, tuple] = {}     # (user, resource) -> (cred, expires_at)

    def exchange(self, user_oid: str, resource: str) -> str:
        key = (user_oid, resource)
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and hit[1] > now:
            return hit[0]
        cred, ttl_s = self._exchange(user_oid, resource)
        self._cache[key] = (cred, now + ttl_s - self._skew)
        return cred

    def _exchange(self, user_oid: str, resource: str) -> "tuple[str, float]":
        raise NotImplementedError


class EntraOboExchange(_CachedExchange):
    """Azure — the Entra on-behalf-of flow. NOT RFC 8693: Entra's OBO is the
    `jwt-bearer` grant with `requested_token_use=on_behalf_of`, scoped to
    `<resource>/.default` (e.g. https://database.windows.net for Azure SQL, where
    the database's own Entra users + RLS then enforce). Requires the Entra app to
    have the delegated permission consented at onboarding (ADR 0006 setup cost)."""

    GRANT_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"

    def __init__(self, tenant_id: str, client_id: str, client_secret: str,
                 subject_token_provider: Callable[[str], str],
                 *, post: "PostForm | None" = None, skew_s: float = 30.0) -> None:
        super().__init__(skew_s)
        self._url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        self._client_id = client_id
        self._client_secret = client_secret
        self._subject = subject_token_provider
        self._post = post or http_post_form

    def _exchange(self, user_oid: str, resource: str) -> "tuple[str, float]":
        body = self._post(self._url, {
            "grant_type": self.GRANT_JWT_BEARER,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "assertion": self._subject(user_oid),
            "requested_token_use": "on_behalf_of",
            "scope": f"{resource}/.default",
        })
        return body["access_token"], float(body.get("expires_in", 0))


class EntraRefreshExchange(_CachedExchange):
    """Azure - auth-code+refresh delegation for the WEB-APP topology (ADR 0006
    addendum, #156). The canvas server IS the confidential client the user signed
    into, so it redeems the vaulted multi-resource refresh token for a
    source-scoped access token per (user, resource) - same invariant as OBO
    (query as the user; the source's IAM/RLS enforces), different grant.
    `on_rotate` writes a rotated refresh token back to the vault."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str,
                 refresh_provider: Callable[[str], str],
                 *, on_rotate: "Callable[[str, str], None] | None" = None,
                 post: "PostForm | None" = None, skew_s: float = 30.0) -> None:
        super().__init__(skew_s)
        self._url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh = refresh_provider
        self._on_rotate = on_rotate
        self._post = post or http_post_form

    def _exchange(self, user_oid: str, resource: str) -> "tuple[str, float]":
        try:
            body = self._post(self._url, {
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh(user_oid),
                "scope": f"{resource}/.default offline_access",
            })
        except TokenExchangeError as exc:
            # Two failures the USER can fix themselves get plain wording instead of an AADSTS
            # code (#205, #429). Anything else is an operator problem and keeps the IdP's own.
            if exc.needs_db_consent:
                raise TokenExchangeError(
                    exc.status, exc.error,
                    "your organization hasn't approved database access for DBSearch yet — "
                    "open Connect on this source to grant it "
                    f"({exc.description})", exc.correlation_id) from exc
            if exc.expired_grant:
                raise TokenExchangeError(
                    exc.status, exc.error,
                    "your sign-in has expired — sign in again to query this source "
                    f"({exc.description})", exc.correlation_id) from exc
            raise
        if body.get("refresh_token") and self._on_rotate is not None:
            self._on_rotate(user_oid, body["refresh_token"])
        return body["access_token"], float(body.get("expires_in", 0))


class GoogleRefreshExchange(_CachedExchange):
    """GCP - Google sign-in delegation for the WEB-APP topology (ADR 0006 addendum, #193).
    The canvas server is the confidential client the user linked, so it redeems the vaulted
    Google refresh token for an access token and every GCP client is built as the user;
    BigQuery IAM + row-access policies (and Cloud SQL IAM auth, Drive ACLs) then enforce.

    Unlike Entra, Google does NOT scope redemption per resource: the refresh token carries
    the scopes consented at link time and one multi-scope access token comes back, so
    `resource` is only the cache key. Least privilege comes from incremental authorization
    at link time (see server/google_auth.py). Google refresh tokens do not rotate on
    redemption, so there is no on_rotate leg."""

    _URL = "https://oauth2.googleapis.com/token"

    def __init__(self, client_id: str, client_secret: str,
                 refresh_provider: Callable[[str], str],
                 *, post: "PostForm | None" = None, skew_s: float = 30.0) -> None:
        super().__init__(skew_s)
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh = refresh_provider
        self._post = post or http_post_form

    def _exchange(self, user_oid: str, resource: str) -> "tuple[str, float]":
        body = self._post(self._URL, {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh(user_oid),
        })
        if "access_token" not in body:
            raise RuntimeError(
                "google token redemption failed: "
                + (body.get("error_description") or body.get("error") or "unknown error"))
        # A response with no expires_in must not buy cache time - default 0 (no caching),
        # matching EntraRefreshExchange. (The broader gap - _CachedExchange still serving a
        # cached token after VAULT.drop, so logout does not immediately fail closed - is
        # #194, out of scope here.)
        return body["access_token"], float(body.get("expires_in", 0))


# post_json(url, payload, headers) -> parsed JSON — injected (GCP impersonation hop)
PostJson = Callable[[str, dict, dict], dict]


def http_post_json(url: str, payload: dict, headers: dict) -> dict:
    import json as _json
    from urllib import request

    req = request.Request(url, data=_json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json", **headers},
                          method="POST")
    with request.urlopen(req, timeout=15) as resp:  # noqa: S310 — endpoint from config
        return _json.loads(resp.read().decode())


class GcpWifExchange(_CachedExchange):
    """GCP - Workload Identity Federation. sts.googleapis.com speaks real RFC 8693:
    the user's IdP jwt exchanges against the WIF pool `audience` for a federated
    token; with `service_account` set, a second hop impersonates that SA.

    WARNING (#193): the service-account hop COLLAPSES IDENTITY. Every user's query then
    executes as ONE service account - the source sees the SA, not the user - which silently
    breaks query-as-user (ADR 0006) even though the code looks delegated. Not wired live.
    Before shipping WIF: bind users to direct `principal://` IAM bindings (no SA hop), or
    treat the store as service-identity + row-policy fallback and say so. The shipped GCP
    path is `google_refresh` (Google sign-in), which never collapses."""

    _STS_URL = "https://sts.googleapis.com/v1/token"
    _CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
    _TOKEN_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"

    def __init__(self, audience: str, subject_token_provider: Callable[[str], str],
                 *, service_account: "str | None" = None,
                 post: "PostForm | None" = None, post_json: "PostJson | None" = None,
                 skew_s: float = 30.0) -> None:
        super().__init__(skew_s)
        self._audience = audience
        self._subject = subject_token_provider
        self._sa = service_account
        self._post = post or http_post_form
        self._post_json = post_json or http_post_json

    def _exchange(self, user_oid: str, resource: str) -> "tuple[str, float]":
        body = self._post(self._STS_URL, {
            "grant_type": GRANT_TOKEN_EXCHANGE,
            "audience": self._audience,
            "subject_token": self._subject(user_oid),
            "subject_token_type": self._TOKEN_TYPE_JWT,
            "requested_token_type": TOKEN_TYPE_ACCESS,
            "scope": self._CLOUD_SCOPE,
        })
        token, ttl = body["access_token"], float(body.get("expires_in", 3600))
        if self._sa is None:
            return token, ttl
        sa_body = self._post_json(
            f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
            f"{self._sa}:generateAccessToken",
            {"scope": [self._CLOUD_SCOPE]},
            {"Authorization": f"Bearer {token}"})
        return sa_body["accessToken"], ttl


class AwsStsExchange(_CachedExchange):
    """AWS — STS AssumeRoleWithWebIdentity. Returns temporary keys, not a bearer:
    serialized as a JSON string {access_key_id, secret_access_key, session_token} so
    TokenExchangePort stays str; the Redshift engine parses it into a boto3 session.
    RoleSessionName embeds the user so the source's CloudTrail attributes per user;
    Lake Formation / Redshift grants on the role then enforce (ADR 0006)."""

    def __init__(self, role_arn: str, subject_token_provider: Callable[[str], str],
                 *, sts_client_factory: "Callable[[], object] | None" = None,
                 duration_s: int = 900, skew_s: float = 30.0) -> None:
        super().__init__(skew_s)
        self._role_arn = role_arn
        self._subject = subject_token_provider
        self._duration = duration_s
        self._sts_factory = sts_client_factory or self._default_factory
        self._sts = None

    @staticmethod
    def _default_factory():
        import boto3   # lazy optional dep (LAW 7)

        return boto3.client("sts")

    def _exchange(self, user_oid: str, resource: str) -> "tuple[str, float]":
        import json as _json
        import re as _re

        if self._sts is None:
            self._sts = self._sts_factory()
        session = "dbsearch-" + _re.sub(r"[^\w+=,.@-]", "-", user_oid)[:53]
        creds = self._sts.assume_role_with_web_identity(
            RoleArn=self._role_arn, RoleSessionName=session,
            WebIdentityToken=self._subject(user_oid),
            DurationSeconds=self._duration)["Credentials"]
        cred = _json.dumps({"access_key_id": creds["AccessKeyId"],
                            "secret_access_key": creds["SecretAccessKey"],
                            "session_token": creds["SessionToken"]})
        return cred, float(self._duration)


class AwsKeysExchange(_CachedExchange):
    """AWS — the caller's OWN vaulted access keys (ADR 0024), redeemed via STS
    GetSessionToken into the same temporary {access_key_id, secret_access_key,
    session_token} JSON triple AwsStsExchange emits and the Redshift engine parses.

    This is the web-app-topology sibling of aws_sts, the same way entra_refresh sits
    beside entra_obo: aws_sts (AssumeRoleWithWebIdentity) needs a real OIDC JWT as its
    subject, which the hosted deployment's vault does not hold - it vaults refresh
    tokens - so on dbsearch.ai that kind can never redeem. Here the subject provider
    hands back the user's vaulted key JSON instead, and no role hop exists at all:
    the STS session inherits exactly the caller's IAM principal, so Redshift GRANTs /
    Lake Formation enforce per user and CloudTrail attributes per user. The
    identity-collapse trap GcpWifExchange warns about (every caller funneled through
    one service account) is structurally unreachable in this shape.

    GetSessionToken rather than passing the long-lived keys straight to the engine:
    it validates the keys at redemption (a revoked IAM key dies here with STS's own
    error, not deep inside a query), and it yields EXPIRING credentials that fit
    _CachedExchange's ttl contract. Session credentials cannot call IAM APIs, which
    is fine - the Redshift Data API is unaffected."""

    def __init__(self, subject_credential_provider: Callable[[str], str],
                 *, sts_client_factory: "Callable[[dict], object] | None" = None,
                 duration_s: int = 3600, skew_s: float = 30.0) -> None:
        super().__init__(skew_s)
        self._subject = subject_credential_provider
        self._duration = duration_s
        self._sts_factory = sts_client_factory or self._default_factory

    @staticmethod
    def _default_factory(keys: dict):
        import boto3   # lazy optional dep (LAW 7)

        return boto3.client("sts", aws_access_key_id=keys["access_key_id"],
                            aws_secret_access_key=keys["secret_access_key"])

    def _exchange(self, user_oid: str, resource: str) -> "tuple[str, float]":
        import json as _json

        keys = _json.loads(self._subject(user_oid))
        creds = self._sts_factory(keys).get_session_token(
            DurationSeconds=self._duration)["Credentials"]
        cred = _json.dumps({"access_key_id": creds["AccessKeyId"],
                            "secret_access_key": creds["SecretAccessKey"],
                            "session_token": creds["SessionToken"]})
        return cred, float(self._duration)


class IdentityBroker:
    """user + store → AccessContext, by ADR-0006 precedence:
    registered delegation → delegated_credential; else registered row policy →
    row_policy predicate; else principals only (indexed trim)."""

    def __init__(self, identity: IdentityPort) -> None:
        self._identity = identity
        self._delegations: dict[str, tuple] = {}      # store_id -> (exchange, resource)
        self._row_policies: dict[str, RowPolicyFn] = {}

    def register_delegation(self, store_id: str, exchange: TokenExchangePort,
                            resource: str) -> None:
        self._delegations[store_id] = (exchange, resource)

    def register_row_policy(self, store_id: str, policy: RowPolicyFn) -> None:
        self._row_policies[store_id] = policy

    def reset(self) -> None:
        """Drop all registrations — a recompose must never inherit a stale
        delegation from a store id the new manifest reuses differently.

        #805: compose no longer calls this on the LIVE broker (see staging/adopt below) —
        resetting a broker that concurrent asks are reading opens a window where a
        delegated store silently falls through to the principals-only path."""
        self._delegations.clear()
        self._row_policies.clear()

    def staging(self) -> "IdentityBroker":
        """#805: an empty broker over the same identity, for a recompose to register the
        NEW manifest's delegations into while the live one keeps serving the old ones."""
        return IdentityBroker(self._identity)

    def adopt(self, staged: "IdentityBroker") -> None:
        """#805: atomically take over a staging broker's registrations. Providers capture
        the broker OBJECT at construction, so the swap must replace this broker's internals
        rather than the reference — one tuple assignment, so a concurrent `access_for`
        sees either the old registrations or the new ones, never an empty broker."""
        self._delegations, self._row_policies = staged._delegations, staged._row_policies

    def access_for(self, user_oid: str, store_id: str) -> AccessContext:
        principals = self._identity.expand_groups(user_oid)
        delegation = self._delegations.get(store_id)
        if delegation is not None:
            exchange, resource = delegation
            return AccessContext(user_oid=user_oid, principals=principals,
                                 delegated_credential=exchange.exchange(user_oid, resource))
        policy = self._row_policies.get(store_id)
        if policy is not None:
            return AccessContext(user_oid=user_oid, principals=principals,
                                 row_policy=policy(user_oid, principals))
        return AccessContext(user_oid=user_oid, principals=principals)


def env_subject_token_provider(user_oid: str) -> str:
    """DEV subject-token seam (the E3b GRAPH_TOKEN pattern): the header-auth demo has
    no real user session jwt, so the inbound assertion comes from the environment —
    DBSEARCH_SUBJECT_TOKEN_<USER> per user, else DBSEARCH_SUBJECT_TOKEN shared.
    Production replaces this callable with the real session-token extractor; the
    exchanges never know the difference (same seam)."""
    import os
    import re as _re

    per_user = "DBSEARCH_SUBJECT_TOKEN_" + _re.sub(r"\W", "_", user_oid).upper()
    token = os.environ.get(per_user) or os.environ.get("DBSEARCH_SUBJECT_TOKEN")
    if not token:
        raise RuntimeError(
            f"no subject token for {user_oid!r}: set {per_user} or "
            "DBSEARCH_SUBJECT_TOKEN (dev seam), or wire a real session-token "
            "provider in production")
    return token


def _for_idp(provider: Callable, idp: str, *, allow_legacy: bool) -> Callable[[str], str]:
    """Bind a subject/refresh provider to ONE idp. Providers come in two shapes: the
    multi-IdP one the app passes ((oid, idp) -> token, backed by the vault) and the legacy
    1-arg env dev seam ((oid) -> token). Bind by PARAMETER NAME and pass idp as a keyword:
    an arity count would mis-attribute a provider whose 2nd parameter is not `idp`, and a
    mis-attributed binding redeems one cloud's refresh token against the OTHER cloud's token
    endpoint - a credential leak, not just a failed query. An unrecognizable shape raises:
    a security binding must never GUESS which cloud a credential belongs to (LAW 2).

    `allow_legacy` is the whole guard, and it lives HERE - at the binding site - rather than
    at one caller: the 1-arg seam is `env_subject_token_provider`, which hands back an ENTRA
    assertion whatever it is asked for, so it may only be bound to idp="entra". Callers pass
    allow_legacy=(idp == "entra"); `exchange_from_config` is public API, so any second caller
    inherits the refusal instead of having to re-derive which kinds bind to which cloud.

    A plain parameter-count check would also mis-bind a `lambda *a: ...` varargs shim (it
    has exactly one Parameter object, `*a`, so a naive `len(params) == 1` reads it as the
    legacy 1-arg seam) - so the legacy-seam match additionally requires that single
    parameter be an ordinary positional one, not *args."""
    import inspect

    try:
        params = inspect.signature(provider).parameters
    except (TypeError, ValueError):      # builtins / C callables: shape unknowable
        params = None
    if params is not None and "idp" in params:
        return lambda user_oid: provider(user_oid, idp=idp)
    legacy = False
    if params is not None and len(params) == 1:
        (only,) = params.values()
        legacy = only.kind in (inspect.Parameter.POSITIONAL_ONLY,
                               inspect.Parameter.POSITIONAL_OR_KEYWORD)
    if legacy:
        if allow_legacy:
            return provider      # legacy 1-arg env dev seam (entra only)
        raise ValueError(
            f"subject provider {provider!r} takes only (oid): the legacy 1-arg env dev seam "
            f"is an ENTRA seam by construction, so it cannot supply the {idp!r} credential. "
            "Pass a multi-IdP provider (oid, idp=...) instead - binding the legacy seam here "
            "would redeem one cloud's refresh token against another cloud's token endpoint "
            "(LAW 2)")
    raise ValueError(
        f"subject provider {provider!r} has no recognizable shape: expected (oid) or "
        "(oid, idp=...) - refusing to guess which cloud its credential belongs to")


def exchange_from_config(d: dict, subject_token_provider: Callable,
                         *, on_rotate: "Callable[[str, str], None] | None" = None
                         ) -> "tuple[TokenExchangePort, str]":
    """A manifest `delegation:` block (env-resolved) -> (exchange, resource).
    Each kind binds the subject provider to ITS OWN idp, so one session holding several
    cloud credentials hands each store the credential for the cloud it actually names
    (#193 account linking). Unknown kinds raise - a typo must never silently mean
    'no delegation' (LAW 2). A kind that binds to a NON-entra cloud refuses the legacy 1-arg
    env dev seam outright (see `_for_idp`), so it can never redeem an Entra assertion against
    that cloud's token endpoint - the refusal is here at the binding, not duplicated in each
    caller's routing table."""
    kind = d.get("kind", "")

    def bind(idp: str) -> Callable[[str], str]:
        # the env dev seam is an ENTRA seam: legacy 1-arg providers bind to entra and nothing else
        try:
            return _for_idp(subject_token_provider, idp, allow_legacy=(idp == "entra"))
        except ValueError as exc:
            raise ValueError(f"delegation kind {kind!r}: {exc}") from exc

    if kind == "entra_obo":
        return (EntraOboExchange(d["tenant_id"], d["client_id"], d["client_secret"],
                                 bind("entra")),
                d.get("resource", "https://database.windows.net"))
    if kind == "entra_refresh":
        return (EntraRefreshExchange(d["tenant_id"], d["client_id"], d["client_secret"],
                                     bind("entra"), on_rotate=on_rotate),
                d.get("resource", "https://database.windows.net"))
    if kind == "google_refresh":
        return (GoogleRefreshExchange(d["client_id"], d["client_secret"], bind("google")),
                d.get("resource", "bigquery"))
    if kind == "gcp_wif":
        # WIF federates FROM an Entra assertion (see the class docstring), so entra it is.
        return (GcpWifExchange(d["audience"], bind("entra"),
                               service_account=d.get("service_account")),
                d.get("resource", "bigquery"))
    if kind == "aws_sts":
        return (AwsStsExchange(d["role_arn"], bind("entra")),
                d.get("resource", "redshift"))
    if kind == "aws_keys":
        # ADR 0024: the subject is the caller's own vaulted AWS key JSON - per-caller, so
        # the block itself carries no credential fields (nothing for the plaintext guard
        # in secret_fields.py to protect, same as aws_sts).
        return (AwsKeysExchange(bind("aws")),
                d.get("resource", "redshift"))
    if kind == "static":
        # a fixed token/map: no subject provider is consulted at all, so nothing to bind
        return (StaticTokenExchange(d.get("tokens") or d.get("token", "")),
                d.get("resource", "dev"))
    raise ValueError(f"unknown delegation kind {kind!r} (known: entra_obo, entra_refresh, "
                     "google_refresh, gcp_wif, aws_sts, aws_keys, static)")
