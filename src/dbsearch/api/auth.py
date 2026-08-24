"""Trusted identity resolution for the API layer (LAW 2).

The client must NEVER be able to choose whose results it gets. Identity is derived from the
request's auth context only:
  - a verified session cookie from a real per-user login (Entra #171 / Google #193);
  - an `Authorization: Bearer dbk_...` API key, resolved to the user it is bound to;
  - dev mode (DBSEARCH_DEV_AUTH=1, OPT-IN, default OFF since #315): trust the
    `X-DBSearch-User` header;
  - prod mode: validate the `Authorization: Bearer <jwt>` token via a configured verifier
    (e.g. an Entra-issued token -> the `oid` claim). No verifier / no token -> AuthError.

A body field or GraphQL argument is NEVER a source of identity.

#184 - ONE chokepoint, no drift: `resolve_identity` is the ONLY identity resolver, and every
transport calls it (the REST `current_user` dependency AND the GraphQL ASGI context). The
previous split let /graphql keep honoring the dev header after the REST path had been coupled
to real login (#183), so an unauthenticated caller could send `X-DBSearch-User: <victim-oid>`
to /graphql and receive that victim's permission-trimmed documents. The coupling now lives
HERE, below both transports, so a future transport inherits it instead of forgetting it.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

HeaderGetter = Callable[[str], Optional[str]]
CookieGetter = Callable[[str], Optional[str]]

# Production verifier: (raw_jwt) -> trusted user oid. Set via set_bearer_verifier().
_bearer_verifier: Optional[Callable[[str], str]] = None


class AuthError(Exception):
    """No trusted user identity could be established from the request."""


# Demo scope (ADR 0009 / #279): a no-session/no-key request may act as one of these fixed
# principals over the LOCAL demo catalog only. The returned identity is NAMESPACED `demo:<name>`
# so it can never collide with a real oid, has no vault entry, and is de-namespaced to the bare
# principal ONLY inside the router's demo scope (never a live store). See resolve_identity.
DEMO_PREFIX = "demo:"
_DEMO_PRINCIPALS = ("alice", "bob")

# ADR 0018: the per-account document partition for verified sessions with no Entra tid
# (Google, local email/password). Never GUID-shaped and never equal to a deployment
# constant, so it cannot collide with any real tenant partition.
ACCT_TENANT_PREFIX = "acct:"


def set_bearer_verifier(fn: Callable[[str], str]) -> None:
    """Install a JWT verifier for production (validates the token, returns the oid claim)."""
    global _bearer_verifier
    _bearer_verifier = fn


# API-key resolver: (full_token) -> bound user oid, or raise AuthError. Set via set_api_key_resolver().
_api_key_resolver: Optional[Callable[[str], str]] = None


def set_api_key_resolver(fn: Optional[Callable[[str], str]]) -> None:
    global _api_key_resolver
    _api_key_resolver = fn


def dev_auth_enabled() -> bool:
    """Whether the `X-DBSearch-User` header may authenticate. OPT-IN, default OFF (#315).

    It used to default ON, which made every deployment that had never heard of this variable
    identity-spoofable. That is not a hypothetical: with no real login configured - the exact
    state the published `docker compose up -d --build` produces, since docker-compose.yml sets
    no auth variables at all - the branch below trusts whatever oid the caller writes into the
    header, so a request with no session, no cookie and no API key reads any user's private
    documents just by naming them. Reproduced end-to-end before this change: a victim uploaded
    a document ACL'd to themselves, and one header read it back verbatim.

    A protection someone has to remember to switch on does not protect the stranger who cloned
    the repo and ran the quickstart, so the switch is now thrown deliberately or not at all.
    `docker-compose.demo.yml` already sets DBSEARCH_DEV_AUTH=1 explicitly, which is why the
    seeded alice/bob demo keeps working across this change.

    Parsing is an allowlist rather than a denylist for the same reason: a typo'd or unrecognised
    value now means OFF. Under the old denylist, DBSEARCH_DEV_AUTH=flase enabled the dev header.
    """
    return os.environ.get("DBSEARCH_DEV_AUTH", "0").strip().lower() in ("1", "true", "yes", "on")


def real_login_enabled() -> bool:
    """True when a real per-user login is configured on this deployment - Microsoft/Entra
    (#171), Google (#193), or local email/password (#574). Imported lazily: the api layer
    must not hard-depend on the server package (and server.app imports THIS module at
    import time)."""
    try:
        from dbsearch.server import google_auth, local_auth, user_auth
    except Exception:                     # api used without the server extras
        return False
    return bool(user_auth.is_enabled() or google_auth.is_enabled() or local_auth.is_enabled())


def api_key_oid(get_header: HeaderGetter) -> "str | None":
    """The oid bound to this request's `Authorization: Bearer dbk_...` key, or None when the
    request carries no api key at all.

    THE api-key branch of `resolve_identity`, factored out so a route guard can ask "is this
    an api-key call, and whose?" without re-parsing the header - one parser, no drift. A
    PRESENT but invalid key still raises AuthError (hard fail), exactly as inside
    resolve_identity: an unusable key must never degrade into "no key was offered"."""
    auth = get_header("authorization") or ""
    if auth.lower().startswith("bearer ") and _api_key_resolver is not None:
        token = auth.split(" ", 1)[1]
        if token.startswith("dbk_"):
            return _api_key_resolver(token)        # bound_user, or AuthError (hard fail)
    return None


def _session_oid(get_cookie: CookieGetter) -> str:
    from dbsearch.server import user_auth

    sess = user_auth.read_session(get_cookie(user_auth.COOKIE) or "")
    return (sess or {}).get("oid") or ""


def canonical_partition(tid: str, oid: str, default_tenant: str) -> str:
    """THE partition rule (ADR 0012 + ADR 0018), in one place.

    #582 / ADR 0019 D2 extracted this out of `resolve_tenant` because share-time refusal
    needs the SAME rule that session-time resolution uses. Two copies that drift would let
    a grant open its doorway onto a partition the grantee never actually reads - the exact
    bug #582 exists to fix, wearing a different hat.
    `tests/selftest_582_partition_rule.py` pins that the two agree on every branch.

      home tid      -> the deployment constant (every chunk on the box carries it)
      foreign tid   -> itself
      no tid, oid   -> `acct:<oid>`, the ADR 0018 private per-account partition
      demo:, or neither -> "" , fail-closed
    """
    home = os.environ.get("AUTH_TENANT_ID", "")
    if tid and home and tid == home:
        return default_tenant           # home tid -> the deployment's partition value
    if tid:
        return tid                      # foreign tid as-is
    if oid and not oid.startswith(DEMO_PREFIX):
        return ACCT_TENANT_PREFIX + oid  # ADR 0018: private per-account partition
    return ""                           # no oid either -> fail-closed


def account_partitions(identities: list, account_id: str,
                       default_tenant: str) -> "set | None":
    """Every partition this account's sessions can resolve to (#582 / ADR 0019 D2).

    `identities` is what `AccountStore.identity_tenants` returns - storage rows. This
    function owns the RULE, because it is the layer that knows the deployment constant,
    and it applies that rule through `canonical_partition` so share-time resolution can
    never disagree with the session-time resolution in `resolve_tenant`.

    A SET, not a scalar: an account with a linked Entra identity AND a Google identity
    lands in a different partition depending on which route it signed in through. That
    falls out of ADR 0013 + 0018 and is surfaced here rather than papered over.

    Returns None - meaning UNKNOWABLE, not empty - when an ENTRA identity has no recorded
    tid, which is every Entra identity that has not signed in since #582 shipped. The
    share-time refusal fails closed on None and asks the person to sign in once.

    Reading "we have no record" as "no tenant" would be the tempting default and is
    exactly wrong: `canonical_partition("", oid, ...)` returns `acct:<oid>`, so a
    foreign-tenant Entra user would be handed a private partition they do not have, the
    share would be allowed, and it would silently retrieve nothing - the original #582
    failure, rebuilt inside its own fix. Only Entra identities can carry a tid, so a
    Google or local row without one is complete information rather than a gap.
    """
    out = set()
    for row in identities:
        tid = (row.get("tid") or "") if hasattr(row, "get") else ""
        if not tid and (row.get("idp") if hasattr(row, "get") else "") == "entra":
            return None
        part = canonical_partition(tid, account_id, default_tenant)
        if part:
            out.add(part)
    return out


def resolve_tenant(get_header: HeaderGetter,
                   get_cookie: "CookieGetter | None" = None,
                   default_tenant: str = "") -> str:
    """ADR 0012: the tenant partition this request's DOCUMENT retrieval must carry.

    Same chokepoint discipline as resolve_identity (#184): every transport derives the
    partition here, so REST and GraphQL cannot drift apart ON THE VALUE. #790 is the standing
    lesson that this is a narrower promise than it reads: the two transports agreed perfectly
    here and then diverged one call downstream, because REST handed `as_read_scope` a ReadScope
    and GraphQL handed it the bare `""` below, which `value or default` rewrote into the
    deployment constant. A chokepoint guarantees one DERIVATION, not one INTERPRETATION.
    Precedence mirrors identity:

      - no real login configured (dev/self-host rigs): the deployment constant — a
        single-tenant box has exactly one partition, and it is this one.
      - verified session cookie: the session's VERIFIED Entra tid, CANONICALIZED —
        the HOME tenant's partition value is the deployment constant (every chunk ever
        ingested on a box was stamped with it, so home sessions must land there; a
        foreign tid stays itself). One rule, one place; it also makes the prod
        backfill a structural no-op. The constant is never GUID-shaped, so a foreign
        tid can't collide with it. A session with NO tid (old cookie, Google/local
        identity - ADR 0018) partitions to its own PRIVATE `acct:<oid>` partition -
        tighter than per-tenant isolation, never the home corpus, and never "" (which
        made those logins unable to ingest or retrieve anything). Only a session with
        neither tid nor oid, or a `demo:` principal, falls back to "" - fail-closed.
      - api key: a key is a deployment credential only when its OWNER is an operator
        (the same owner-resolution rule as `_require_home_tenant`): operator keys get
        the deployment constant, any other key fails closed to "" (a foreign user can
        mint a key, and a key must never widen what its owner's session could see).
      - anything else (demo principal, unauthenticated): "" — fail-closed.

    tenant_id is a SERVER-supplied verified value. No header, body field, or GraphQL
    argument is ever consulted — the client cannot choose its partition."""
    if not real_login_enabled():
        return default_tenant
    if get_cookie is not None:
        from dbsearch.server import user_auth

        sess = user_auth.read_session(get_cookie(user_auth.COOKIE) or "")
        if sess:
            # #582 / ADR 0019 D2: the rule itself lives in `canonical_partition` so that
            # share-time refusal resolves partitions exactly the way session-time
            # resolution does. Behaviour here is unchanged by the extraction.
            return canonical_partition(sess.get("tid") or "", sess.get("oid") or "",
                                       default_tenant)
    try:
        key_oid = api_key_oid(get_header)
    except AuthError:
        key_oid = None
    if key_oid is not None:
        from dbsearch.server.operators import is_operator

        if is_operator(key_oid):
            return default_tenant
    return ""


def resolve_identity(get_header: HeaderGetter,
                     get_cookie: "CookieGetter | None" = None) -> str:
    """Return the trusted user oid for this request, or raise AuthError.

    THE identity chokepoint (#184): REST and GraphQL both call exactly this, so they cannot
    drift apart. Precedence: verified session cookie -> `Bearer dbk_...` API key -> dev
    header / verified bearer jwt.

    #183/#193 fail-closed coupling: when a real login is configured, the `X-DBSearch-User`
    dev switcher must NEVER authenticate - otherwise a caller with no session could claim a
    victim's oid and have the victim's vaulted refresh token redeemed as them
    (`_subject_provider` -> VAULT.get). Real login and the dev seam are mutually exclusive.

    #315 closed the other half of that hole. The coupling above only ever fired on a box that
    HAD a real login; a box with none - the published quickstart's own default - fell straight
    through to the dev header, which was trusted because DBSEARCH_DEV_AUTH defaulted on. Both
    halves are now needed: the header authenticates only when an operator has explicitly set
    DBSEARCH_DEV_AUTH=1 AND no real login is configured.

    `get_cookie` is optional only so the pure-function call sites (tests, resolvers with no
    cookie jar) still work; a transport that HAS cookies must pass it."""
    if get_cookie is not None:
        oid = _session_oid(get_cookie)
        if oid:
            return oid

    key_oid = api_key_oid(get_header)
    if key_oid is not None:
        return key_oid

    auth = get_header("authorization") or ""
    # Demo scope (ADR 0009 / #279): with NO session and NO api-key, honor a demo selector
    # (the `X-DBSearch-Demo-User` header) as a fixed demo principal. This NEVER authenticates
    # a real identity: only the allowlist passes, and the result is namespaced `demo:<name>`
    # (no real oid, no vault entry) so it reaches ONLY the local demo catalog. It sits ABOVE
    # the dev/real-login branch on purpose - the public hosted demo HAS a real login
    # configured (which refuses the bare `X-DBSearch-User` dev header, #183), yet an anonymous
    # visitor must still be able to play the demo. A present session already won above. A
    # present-but-non-allowlisted selector HARD-FAILS (like a bad `dbk_`), so a real oid can
    # never be smuggled in through it.
    # Ordering: this branch sits above the bearer-jwt verifier below on purpose (the dev/
    # real-login block between them hard-raises under a real login - the hosted-demo config).
    # A caller authenticating purely by `Authorization: Bearer <jwt>` (no session cookie) that
    # ALSO carries an injected demo header is therefore downgraded to the demo - but a demo
    # identity is strictly LESS access, so the worst case is a 403 denial, never a leak or an
    # escalation (a real session cookie is checked first, so a browser user is unaffected).
    demo = get_header("x-dbsearch-demo-user")
    if demo is not None:
        if demo in _DEMO_PRINCIPALS:
            return DEMO_PREFIX + demo
        raise AuthError("unknown demo user (the demo selector accepts only the fixed demo "
                        "principals, and never a real identity)")

    if dev_auth_enabled():
        if real_login_enabled():
            raise AuthError("sign in required (the X-DBSearch-User dev header never "
                            "authenticates while a real login is configured)")
        user = get_header("x-dbsearch-user")
        if user:
            return user
        raise AuthError("missing X-DBSearch-User header (dev auth)")

    if auth.lower().startswith("bearer ") and _bearer_verifier is not None:
        return _bearer_verifier(auth.split(" ", 1)[1])

    # #315: this is where a freshly-cloned self-host box now lands, so the message has to be
    # a next step rather than a diagnosis. Before the fix it could not be reached from the
    # published compose file at all - the dev header caught everything first, silently.
    if not real_login_enabled():
        raise AuthError(
            "this deployment has no way to identify you: no login is configured and the "
            "X-DBSearch-User dev header is off. Configure a real login "
            "(DBSEARCH_LOCAL_AUTH=1 with DBSEARCH_SESSION_KEY, or Entra, or Google), or - "
            "for a local rig ONLY, never a reachable host - set DBSEARCH_DEV_AUTH=1 to trust "
            "the X-DBSearch-User header. See docs/SELFHOST.md.")
    raise AuthError("no verified bearer token (production auth requires a configured verifier)")
