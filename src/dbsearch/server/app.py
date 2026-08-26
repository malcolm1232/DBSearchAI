"""DBSearch.AI self-host edition — FastAPI app exposing REST + GraphQL.

Endpoints:
  GET  /health                      -> {status, backend}
  POST /ingest   {external_id,title,text,acl[],uri?}  -> index a document (acl = allowed groups)
  POST /search   {user, question}   -> permission-trimmed cited answer
  /graphql                          -> the GraphQL API (same QueryService)

Run:   uvicorn dbsearch.server.app:app --host 0.0.0.0 --port 8080

⚠ SECURITY (LAW 2): `user` is taken from the request body here for easy local testing. In a
real deployment, derive the user identity from a verified auth token (reverse proxy / JWT),
never trust a client-supplied user — otherwise a caller can impersonate anyone. The
permission trim itself is enforced server-side regardless.
"""
from __future__ import annotations

import functools
import hashlib
import io
import json
import logging
import os
import re
import time
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import urllib.parse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel

from dbsearch.core import headroom   # #843
from dbsearch.adapters.local import ParseProducedNoText, UnsupportedMedia
from dbsearch.adapters.local.secrets import EncryptedFileSecrets
from dbsearch.api.auth import (
    ACCT_TENANT_PREFIX, DEMO_PREFIX, AuthError, account_partitions, api_key_oid,
    dev_auth_enabled, real_login_enabled, resolve_identity, resolve_tenant,
)
from dbsearch.api.graphql_app import build_router, build_schema
from dbsearch.core.validation import is_safe_external_id
from dbsearch.ports.base import ReadScope, as_read_scope
from dbsearch.server import (billing, demo_requests, entitlements, google_auth,
                             local_auth, rate_limit, sp_connect, user_auth)
from dbsearch.server import tiers as tiers_mod
from dbsearch.server import edition as edition_mod
from dbsearch.server.edition import build_edition
from dbsearch.server.accounts import (AccountStoreUnavailable, InMemoryAccountStore,
                                      PgAccountStore)
from dbsearch.audit import AuditLogUnavailable
from dbsearch.server.connection_store import PgConnectionStore
from dbsearch.pipeline.jobs import PgJobStore
from dbsearch.server.conversation_shares import (AUDIENCE_LINK, AUDIENCE_PEOPLE,
                                                 ConversationShareStoreUnavailable)
from dbsearch.server.conversation_store import ConversationStoreUnavailable
from dbsearch.server.grant_store import GrantStoreUnavailable
from dbsearch.server.link_access import build_link_access_api, fork_key
from dbsearch.server.manifest_store import PgManifestStore
from dbsearch.server.operators import is_operator
from dbsearch.server.router_api import build_router_api
from dbsearch.server.secrets_api import build_secrets_api

app = FastAPI(title="DBSearch.AI — self-host edition", version="0.1.0")

# #332: per-visitor + global spend caps on the paths that cost money. The hosted demo is
# anonymous by design and drives Groq on a real key, so this is the "remaining deploy-time
# piece" edition.py refers to, and the precondition the public box's Caddyfile sets for
# lifting its basic-auth gate. ON unless DBSEARCH_RATE_LIMIT=0.
rate_limit.install(app)

_edition = build_edition()
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _log_identity_posture() -> None:
    """Say at BOOT how this deployment identifies people (#315).

    Before #315 the dev header was trusted by default, so a box with no login configured
    still "worked" and nothing ever mentioned that every request was self-asserted. Now that
    the header is opt-in, the same box refuses everything instead - which is safe but reads
    like a broken product if the operator has to discover it one 401 at a time. So the posture
    is stated once, at startup, in the operator's log rather than in a user's error.

    Deliberately a log line and not a hard refusal to boot: /health and the static shell must
    keep serving so a container orchestrator can tell "misconfigured" from "crashed".
    """
    log = logging.getLogger("dbsearch")
    from dbsearch.api.auth import dev_auth_enabled, real_login_enabled

    real, dev = real_login_enabled(), dev_auth_enabled()
    if real:
        log.info("identity: real login configured (Entra / Google / local) - the "
                 "X-DBSearch-User dev header is refused regardless of DBSEARCH_DEV_AUTH")
    elif dev:
        log.warning(
            "identity: DEV AUTH IS ON (DBSEARCH_DEV_AUTH=1) and no real login is configured - "
            "any caller who can reach this port may act as ANY user by setting the "
            "X-DBSearch-User header. Correct for a laptop rig or the seeded demo; never expose "
            "this deployment to a network you do not control.")
    else:
        log.error(
            "identity: NOT CONFIGURED - no login (Entra / Google / local) and "
            "DBSEARCH_DEV_AUTH is off, so every authenticated request will be refused with "
            "401. Set DBSEARCH_LOCAL_AUTH=1 together with DBSEARCH_SESSION_KEY for real "
            "email/password login, or DBSEARCH_DEV_AUTH=1 for a local-only rig. "
            "See docs/SELFHOST.md.")


_log_identity_posture()

# #319 (ADR 0010 s3): self-serve credential storage, wired LAZILY. A deployment with
# DBSEARCH_SECRET_KEY unset must still boot with every OTHER feature working - only the
# /secrets surface is unavailable, and it says why (503, not a server that refuses to start).
# `EncryptedFileSecrets` itself already refuses to run under a key it generated (adapters/
# local/secrets.py) - this try/except only covers "not configured at all", never conjures one.
try:
    _secrets = EncryptedFileSecrets(
        os.environ.get("DBSEARCH_SECRET_FILE", "/var/lib/dbsearch/secrets.json"))
    _secrets_unavailable_reason = None
except Exception as exc:
    _secrets = None
    _secrets_unavailable_reason = str(exc)

# #435: make the refresh-token vault DURABLE by giving it that same encrypted store. Without
# this the module-level VAULT stays memory-only and every deploy logs every user out of data
# access while their session cookie still says they are signed in. `bind_store(None)` is a
# no-op, so a box with no DBSEARCH_SECRET_KEY keeps today's behaviour rather than failing to
# boot - a deployment must never lose the ability to SIGN IN because it cannot persist.
user_auth.VAULT.bind_store(_secrets)

# #368: per-workspace manifest persistence. Reuses the deployment's own Postgres
# (PGVECTOR_DSN, the pgvector container in docker-compose) - no new infrastructure.
# DBSEARCH_MANIFEST_DSN overrides for a split deployment. Unset -> None -> the
# workspace pool runs memory-only (hermetic tests, memory-backend rigs): unconfigured
# is not an error; configured-but-broken raises ManifestStoreUnavailable at use time,
# which router_api maps to a 503 (fail closed, #200).
_manifest_dsn = os.environ.get("DBSEARCH_MANIFEST_DSN") or os.environ.get("PGVECTOR_DSN", "")
_manifest_store = PgManifestStore(_manifest_dsn) if _manifest_dsn else None

# #572 (ADR 0013): accounts and identities are rows, not an implication of the first
# manifest write. Same DSN sharing as everything else here - no new infrastructure.
# Unconfigured -> memory-only (hermetic tests); configured-but-broken is best-effort at
# every call site below, never a failed sign-in (same stance as VAULT.bind_store above).
_account_dsn = os.environ.get("DBSEARCH_ACCOUNT_DSN") or os.environ.get("PGVECTOR_DSN", "")
ACCOUNTS = PgAccountStore(_account_dsn) if _account_dsn else InMemoryAccountStore()

# #565 (ADR 0016 s3): ingest job records in the SAME Postgres, for the same reason - no new
# infrastructure. This is what makes a crawl survive a restart rather than merely a retry
# (#327): the checkpoint is only as durable as the store holding it, and the in-memory
# fallback goes down with the process it was protecting. Unset DSN -> None -> each provider
# keeps its own in-memory store, i.e. today's behaviour.
_job_store = PgJobStore(_manifest_dsn) if _manifest_dsn else None

# #962: "Book a demo" leads, in the same Postgres for the same reason - no new
# infrastructure. Unset DSN -> in-memory, which is honest for a self-hoster and for tests
# but does NOT survive a restart; that is why /admin/demo-requests says so out loud rather
# than rendering an empty list that looks like "no one has asked".
DEMO_REQUESTS = (demo_requests.PgDemoRequestStore(_manifest_dsn) if _manifest_dsn
                 else demo_requests.InMemoryDemoRequestStore())

# #431: per-owner, durable SharePoint connection state, in the same Postgres for the same reason.
# Unset DSN -> None -> this process only, i.e. today's behaviour minus the cross-user leak (the
# per-owner keying is in sp_connect itself, so isolation does NOT depend on having a database).
sp_connect.bind_store(PgConnectionStore(_manifest_dsn) if _manifest_dsn else None)


class IngestRequest(BaseModel):
    external_id: str
    title: str
    text: str
    acl: list[str]            # group ids allowed to read this document (required — LAW 2)
    uri: str = ""


class SearchRequest(BaseModel):
    question: str            # identity is NOT a body field — it comes from the auth header (LAW 2)
    model: str = ""          # optional generation-model name (#43); identity still from the header


class ChatRequest(BaseModel):
    conv_id: str
    question: str            # identity comes from the auth header (LAW 2), never the body
    model: str = ""          # optional generation-model name (#43)


class DraftRequest(BaseModel):
    brief: str               # identity comes from the auth header (LAW 2), never the body


class DraftTurnRequest(BaseModel):
    conv_id: str             # identity comes from the auth header (LAW 2), never the body
    message: str = ""        # the user's typed message (gather chat / final note)
    intent: str = "chat"     # chat | ready | confirm | cancel  (#57 two-phase draft)


class PermissionTestRequest(BaseModel):
    user_oid: str
    question: str = ""


class ResyncRequest(BaseModel):
    source_id: str


class CreateKeyRequest(BaseModel):
    label: str


class AwsConnectRequest(BaseModel):
    access_key_id: str
    secret_access_key: str


@functools.lru_cache(maxsize=1)
def _aws_supported() -> bool:
    """ADR 0024: whether this deployment can validate and redeem AWS keys at all - the
    account panel's "Not configured here" vs "Not connected" distinction for Amazon.
    Implementation presence (boto3 baked into the image), not an env var: AWS linking has
    no client id to configure, so the only way a box can lack the capability is a missing
    optional dependency (the #654 shape - never offer what cannot run)."""
    import importlib.util

    return importlib.util.find_spec("boto3") is not None


def _subject_provider(oid: str, idp: str = "entra") -> str:
    """Delegation subject seam (#156 Entra, #193 multi-IdP): the vaulted refresh token for
    the cloud the store's delegation names. An unlinked cloud raises NotSignedIn -> the
    executor's drop+disclose says which one to connect.

    The env dev seam (DBSEARCH_SUBJECT_TOKEN) is an ENTRA seam BY CONSTRUCTION - it holds an
    Entra assertion - so it is NEVER substituted for another cloud. Falling through to it for
    idp="google" (Google login simply not configured) would POST that Entra credential to
    https://oauth2.googleapis.com/token: one cloud's credential transmitted to another cloud,
    reachable by anyone who can compose a manifest. Fail closed instead (LAW 2)."""
    if idp == "aws":
        # ADR 0024: AWS is not an OAuth IdP - the vaulted value is the user's own access-key
        # JSON, written only by /auth/aws/connect, so there is no "login configured" gate to
        # consult and no env seam to fall through to. Absent -> not_linked("aws"), the
        # executor's drop+disclose says to add keys from the account menu.
        return user_auth.VAULT.get(oid, idp="aws")
    enabled = google_auth.is_enabled() if idp == "google" else user_auth.is_enabled()
    if enabled:
        return user_auth.VAULT.get(oid, idp=idp)
    if idp != "entra":
        raise user_auth.not_linked(idp)
    from dbsearch.router import env_subject_token_provider
    return env_subject_token_provider(oid)


def _resolve_groups_if_unknown(ident, oid: str, tenant: str, fetch=None,
                               session_tid: str = "", force: bool = False,
                               self_name: str = "") -> None:
    """Restore a signed-in user's Entra group memberships when this process has none (#266).

    #875: this is now the ONLY implementation of "what a failed membership lookup means", and
    /auth/callback delegates to it with `force=True` instead of registering groups itself. It
    used to have its own copy of the logic, ending in `or []` — which CACHED A FAILURE AS AN
    ANSWER, the precise thing `fetch_member_principals`' None-vs-[] distinction exists to make
    impossible, and then `set_user_groups` made `knows_groups` true so the retry below could
    never fire again. Measured on prod: the owner signed in during a Graph 403 window, his entry
    was written as "resolved, no groups", the 403 was then fixed, and his session kept expanding
    to nothing until he signed in a second time. Two paths disagreeing about what a failure
    means is the #184 one-chokepoint lesson; the one that ran first won, and it was the wrong one.

    `force` exists because a fresh sign-in is a legitimate moment to RE-resolve: the cached
    answer may predate a group change, or a Graph outage. It skips the `knows_groups` short
    circuit and nothing else — a failed lookup under `force` still registers NOTHING, so a
    sign-in during an outage leaves the user unresolved for the chokepoint to retry, rather
    than overwriting a good cached answer with an empty one.

    Memberships were registered ONLY at sign-in, and the identity adapter holds them in
    process. So a restart (or a scale-out to a fresh worker) left a valid session whose groups
    had silently vanished, and a document ACL'd to a GROUP the user really belongs to answered
    "I couldn't find anything you have access to about that." The vault dying is surfaced
    honestly by the header (#210); this was not surfaced at all.

    Resolvable without the user: getMemberObjects runs on the APP-ONLY Graph token, not their
    delegated one, so a valid session is enough — no re-authentication needed.

    Three properties this must have, in order of how badly they bite:
      - a FAILED lookup must not be cached. fetch returns None for failure and [] for genuinely
        no groups; caching the former as the latter denies an entitled user until restart.
      - a resolved-empty result IS cached, or every request from a user in no groups pays a
        Graph round trip.
      - no tenant configured means nothing to resolve against, and we must not stamp the user
        as "known, no groups" on that basis.

    FINAL WHOLE-BRANCH REVIEW (260807), Fix 3: this is the THIRD place groups get registered,
    and it was the only one `_with_tenant_principal` (#575) was not applied to - so it
    re-registered a signed-in user WITHOUT their `tenant:<tid>` principal and `knows_groups`
    then cached that omission for the life of the process. A restart does not sign anyone out
    (the session cookie lives 8h), so after EVERY deploy - including the one that ships #575 -
    every already-signed-in user silently lost every "My organization" document until their
    cookie expired. Fail-closed, but it makes the audience feature non-functional in
    production, which is the failure mode #266 exists to prevent, one layer up.

    `session_tid` is the caller's VERIFIED tid, read from their signed session cookie by
    `_verified_session_tid` - the same provenance `/auth/callback` mints the principal from,
    never a header or a body. It is separate from `tenant` on purpose: `tenant` is the
    directory the app-only Graph token can query (AUTH_TENANT_ID), while the tenant principal
    must be the SESSION's own tid, so a foreign-tenant session can never be handed the home
    tenant's principal. Empty (an api-key call, a dev-header call, a session with no tid) means
    no principal is added - fail-closed, exactly as before this fix."""
    if not oid or not tenant or not hasattr(ident, "knows_groups"):
        return
    if ident.knows_groups(oid) and not force:
        return
    groups = (fetch or user_auth.fetch_member_principals)(tenant, oid)
    if groups is None:                   # lookup failed — stay unresolved so the next try retries
        return
    if session_tid:
        groups = _with_tenant_principal(groups, session_tid)
    ident.set_user_groups(oid, groups)
    if hasattr(ident, "set_principal_name"):
        # #575 Finding 1 again: tenant:<tid> is not a GUID, and Graph 400s the WHOLE getByIds
        # batch on one bad id - so the synthetic principal is filtered out of the NAMES lookup
        # while set_user_groups above still gets the full, unfiltered list.
        lookup = [oid, *_names_lookup_safe(groups)]
        facts = user_auth.fetch_principal_facts(tenant, lookup) or {}
        # #875: the caller's OWN display name from the validated id token, used only where Graph
        # gave nothing. setdefault, never overwrite: the directory is the better source, and this
        # is the fallback that kept a signed-in user named on a tenant whose getByIds is refused.
        if self_name:
            facts.setdefault(oid, {"name": self_name, "kind": "user"})
        for poid, fact in facts.items():
            ident.set_principal_name(poid, fact["name"])
        # #881: the KIND rides the same lookup, on a separate capability check because it is a
        # newer one - an identity port that can name a principal need not be able to type it,
        # and a port that cannot must keep working rather than lose its names too. Cosmetic by
        # construction (LAW 2 compares oids), which is why a failure here degrades the label
        # and nothing else.
        if hasattr(ident, "set_principal_kind"):
            for poid, fact in facts.items():
                if fact.get("kind"):
                    ident.set_principal_kind(poid, fact["kind"])


def _with_tenant_principal(groups: list[str], tid: str) -> list[str]:
    """#575: every real-login session with a verified tid also carries its own tenant
    principal, `tenant:<tid>`, alongside its real transitive groups - so a "My organization"
    upload (ACL'd to that same principal, minted server-side in /admin/upload from this same
    verified tid) is readable by this session. Shared by /auth/callback and /auth/dev/seed,
    the two places a session's groups get registered, so both stay in the same shape."""
    return [*groups, f"tenant:{tid}"]


def _names_lookup_safe(principals: list[str]) -> list[str]:
    """#575 code review Finding 1: Graph's directoryObjects/getByIds requires real GUIDs. A
    synthetic principal like tenant:<tid> is not one, and Graph 400s the WHOLE batch on a
    single bad id - silently, because fetch_principal_facts swallows every exception into {}.
    Left unfiltered, the very first sign-in after this deploy would have zeroed out the
    directory for every user on the tenant, degrading the #258 ACL picker to "paste a raw
    oid" for everyone. A synthetic principal never needs a display name anyway - it is
    filtered ONLY from the names lookup; set_user_groups (the LAW 2 expansion path) still
    gets the real, un-filtered list."""
    return [p for p in principals if not p.startswith("tenant:")]


def _identity_can_hold_tenant_principal(ident) -> bool:
    """#575 review round 2 (new finding): a verified tid is NECESSARY for "My organization"
    but not SUFFICIENT - the bound identity port must also be able to register a synthetic
    `tenant:<tid>` principal. The cloud `EntraIdentity` port has no `set_user_groups` at all
    (its `expand_groups` is Graph-only), so `tenant:<tid>` can never enter anyone's
    expansion there, no matter how many times a session signs in.

    This is the ONE capability signal both `/auth/me` (`has_org`, which decides whether the
    UI even offers the option) and `/admin/upload` (the 400 that enforces it) must agree on -
    factored into a single function so they cannot drift apart again. A split between them is
    exactly the "tile that always fails" trap #551 exists to prevent, and it would be worse
    than #551's case: the option would render, the upload would 400, and the file would not
    be ingested at all - not even privately.

    `hasattr` sees through `GrantAwareIdentity.__getattr__`'s proxy to the wrapped port, the
    same way every other capability check in this file already relies on it (`knows_groups`,
    `set_principal_name`, ...) - a synthetic double must be wrapped in `GrantAwareIdentity` in
    tests that exercise this, or the wrapper's transparency itself goes unverified."""
    return hasattr(ident, "set_user_groups")


def _cookie_secure() -> bool:
    """Whether the session cookie is minted with `Secure` (browser: HTTPS transport only).

    Final review ledger item: no cookie-setting path set this. Pre-existing across all three
    IdPs, but this branch is what makes a PASSWORD mint one (#574), so a plaintext-http hop
    now hands over a session that was derived from a credential the user typed.

    Guarded rather than hardcoded, because a `Secure` cookie is simply DISCARDED over http -
    turning it on unconditionally would make every local dev rig (`http://localhost:8080`) and
    every self-host box on plain http unable to sign in at all, which is a worse failure than
    the one being fixed.

    The signal is the deployment's OWN configured OAuth redirect URI - the one place that
    already has to be truthful about the scheme users reach this box on, resolved through the
    same `_cfg` chain the IdPs use (env OR the encrypted secret file), never a request header
    (`X-Forwarded-Proto` is caller-controlled at the edge and is not a safe input here).
    `DBSEARCH_COOKIE_SECURE=1`/`0` overrides it explicitly for a box whose redirect URI does
    not tell the truth - e.g. TLS terminated upstream with an http redirect configured."""
    raw = os.environ.get("DBSEARCH_COOKIE_SECURE", "").strip().lower()
    if raw:
        return raw not in ("0", "false", "no")
    for resolve in (user_auth.redirect_uri, google_auth.redirect_uri):
        try:
            if (resolve() or "").strip().lower().startswith("https://"):
                return True
        except Exception:
            continue
    return False


def _verified_session_tid(request: Request, oid: str) -> str:
    """The VERIFIED Entra tid on this request's signed session cookie, or "".

    Final review Fix 3's input. Two properties it must have:
      - it reads the SIGNED cookie through `user_auth.read_session`, so a forged or expired
        cookie yields "" rather than a tid a caller chose. No header or body is consulted.
      - it returns "" unless the session's own oid IS the identity being registered. Identity
        can also come from an api key or the dev header (`resolve_identity`); binding one
        caller's session tid to a different oid's group registration would be a LAW 2 hole,
        and this is the one line that rules it out rather than relying on the resolver's
        precedence never changing."""
    try:
        sess = user_auth.read_session(request.cookies.get(user_auth.COOKIE) or "") or {}
    except Exception:
        return ""
    return sess.get("tid", "") if oid and sess.get("oid") == oid else ""


def _resolve_request_identity(request: Request) -> str:
    """Resolve the trusted identity for a request (the shared REST body of #184's chokepoint).
    Raises HTTP 401 if none can be established.

    Identity itself is derived in `dbsearch.api.auth.resolve_identity` (the ONE resolver REST
    and GraphQL share). It prefers a verified session cookie (#171/#193) and refuses the
    `X-DBSearch-User` dev switcher whenever a real login is configured (#183), so a caller with
    no session can never claim a victim's oid and redeem their vaulted refresh token.

    #279 (ADR 0009): a `?demo=` query param is surfaced as the demo header, so a hosted demo is
    a shareable link (`/canvas?demo=alice`) as well as a header. The allowlist enforcement lives
    in resolve_identity, so this convenience adds no security surface."""
    def _hdr(n: str):
        v = request.headers.get(n)
        if v is None and n == "x-dbsearch-demo-user":
            return request.query_params.get("demo")
        return v

    try:
        return resolve_identity(_hdr, request.cookies.get)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


def current_user(request: Request) -> str:
    """Trusted, LIVE identity for a request. 401 if none; **403 for a `demo:*` identity**.

    #279 (ADR 0009), default-deny: the demo scope is served ONLY by the explicitly demo-safe
    router read endpoints (which depend on `current_user_demo_ok`). Every OTHER endpoint - the
    live query/ingest/admin/developer/connector surface, and any future one - depends on THIS,
    so a demo visitor is refused by construction, never by an endpoint remembering to add a
    guard (the #184 one-chokepoint discipline: a per-endpoint check would inevitably miss one,
    and the miss would be an anonymous visitor reaching live customer data or mutating it)."""
    oid = _resolve_request_identity(request)
    if oid.startswith(DEMO_PREFIX):
        raise HTTPException(status_code=403,
                            detail="not available in the demo (sign in for the live product)")
    # #266: one chokepoint, so EVERY endpoint gets its groups back — doing this per-endpoint
    # would inevitably miss one, and the miss looks like "you have no access" rather than a bug.
    # Costs a Graph call only on the first request after a restart, then it is cached.
    _resolve_groups_if_unknown(getattr(_edition, "identity", None), oid,
                               os.environ.get("AUTH_TENANT_ID", ""),
                               session_tid=_verified_session_tid(request, oid))
    # #576: the "any access counts" activity clock. Every live request keeps ITS OWN
    # account alive. Best-effort: a retention bookkeeping failure must never turn into a
    # 500 on an otherwise-successful request.
    #
    # #576 code review Finding 2: this used to ALSO touch, here, the owner behind every
    # live grant the caller holds - regardless of whether the request retrieved anything
    # that grant covered. That was both too broad (an unrelated request kept a sharer's
    # workspace alive) and too narrow (a document shared org-wide via the #575 "My
    # organization" audience has NO grant row at all, so its owner was never touched by a
    # colleague reading it for weeks - the exact bug the owner's review ruling fixed). The
    # single, correct mechanism now lives at the point retrieval results are actually known
    # - `_touch_retrieved_owners`, called from /search, /chat and /chat/stream below - which
    # covers a grant-shared document too, since the grantee's expanded principals include
    # the grant and the chunk's `owner_oid` rides along in the search result regardless of
    # WHY the caller was authorized to see it.
    try:
        from dbsearch.server import retention
        retention.touch(oid)
    except Exception:
        logging.getLogger("dbsearch").exception("retention touch failed for a request")
    return oid


def current_user_demo_ok(request: Request) -> str:
    """Identity for the demo-safe router READ endpoints (/router/ask, /route, /rerun, /catalog,
    /demo) - the ONLY place a `demo:*` identity is accepted (#279 / ADR 0009). A live identity
    is resolved exactly as `current_user` (groups restored, #266); a `demo:*` identity is
    namespaced with no Entra presence, so its directory lookup is skipped (it would be a wasted,
    failing Graph round trip) - its demo groups come from the demo catalog compose, not a lookup."""
    oid = _resolve_request_identity(request)
    if not oid.startswith(DEMO_PREFIX):
        _resolve_groups_if_unknown(getattr(_edition, "identity", None), oid,
                                   os.environ.get("AUTH_TENANT_ID", ""),
                                   session_tid=_verified_session_tid(request, oid))
    return oid


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "backend": _edition.backend, "tenant": _edition.tenant_id}


# #320: which connection env vars THIS deployment can actually resolve. The canvas prefills a
# field with ${NAME} only when NAME is in this list, which is the honest signal - an operator
# rig (local, self-host) has them and prefills as it always did; a hosted box does not and
# leaves the field blank for the user to fill (ADR 0010).
#
# NAMES ONLY, never values (LAW 1). Existence is what the prefill decision needs; the value
# stays server-side and is resolved at compose time.
#
# #317 first gated this on `!realLoginConfigured()`, which was wrong: it conflated "has a real
# login" with "is a hosted multi-user deployment". The local rig is BOTH - real Entra login AND
# operator-provisioned AZURE_SQL_* - so that gate broke signed-in local connects.
CONNECTOR_ENV_NAMES = (
    "AZURE_SQL_SERVER", "AZURE_SQL_DATABASE", "AZURE_SQL_USER", "AZURE_SQL_PASSWORD",
    "AZURE_PG_HOST", "AZURE_PG_DATABASE", "AZURE_PG_USER", "AZURE_PG_PASSWORD",
    "AZURE_MYSQL_HOST", "AZURE_MYSQL_DATABASE", "AZURE_MYSQL_USER", "AZURE_MYSQL_PASSWORD",
    "SYNAPSE_SERVER", "SYNAPSE_POOL", "SYNAPSE_USER", "SYNAPSE_PASSWORD",
    "COSMOS_ENDPOINT", "COSMOS_DATABASE", "COSMOS_CONTAINER", "COSMOS_KEY",
    "GCP_PROJECT", "GCP_DATASET", "GBQ_PROJECT", "GBQ_KEY",
    "RS_CLUSTER", "AWS_SECRET", "HR_SP_URL", "GRAPH_TOKEN",
)


@app.get("/config")
def config(request: Request) -> dict:
    """Metadata the UI needs to render (LAW 1: flags/usernames/backend only, no content).

    #373: `signed_in` exists because the shell used to render a hardcoded "Signed in"
    label whenever dev_auth was off, without ever checking for a token. An anonymous
    visitor to /chat was told they were signed in and then got a bare 401 on their first
    message. The server is the only thing that knows, so it has to say. A `demo:*`
    identity reports False: it cannot reach the live surfaces, so calling it signed in
    would reproduce the same lie in a narrower form.

    Returning the caller their OWN oid is not a LAW 1 content leak - it is the identity
    they already authenticated as, and nothing about anyone else's data."""
    # #198: a real login being configured means the X-DBSearch-User dev switcher is REFUSED
    # (resolve_identity, #183). Advertising dev_auth anyway made the canvas paint an
    # alice/bob picker that can never authenticate: choose an identity, ask, get 401. The
    # server must not offer a capability it will always reject.
    dev = dev_auth_enabled() and not (user_auth.is_enabled() or google_auth.is_enabled()
                                      or local_auth.is_enabled())
    enabled = list(_edition.chat_models.keys())
    # Greyed-out, non-selectable placeholders for providers not yet wired (#67). Kimi K2 is
    # retired from Groq (#63); show it as "coming soon" until an OpenRouter/Moonshot key is set,
    # at which point it becomes a real enabled model and drops out of this list.
    disabled = [] if any("kimi" in m.lower() for m in enabled) else ["Kimi (OpenRouter) — coming soon"]
    # #170 interim identity: an authorized SharePoint principal (real Entra OID) so the canvas
    # can answer permission-trimmed SharePoint questions before real per-user login (#171).
    sp_owner = os.environ.get("SP_DEMO_IDENTITY", "").strip()
    users = list(_edition.users) if dev else []
    if dev and sp_owner and sp_owner not in users:
        users.append(sp_owner)
    try:
        oid = _resolve_request_identity(request)
        signed_in = not oid.startswith(DEMO_PREFIX)
    except Exception:
        oid, signed_in = "", False
    # ADR 0011 s3: env prefill + env_present are operator-only data. A non-real-login rig IS
    # the operator's own machine; under real login, only a session whose oid is on
    # DBSEARCH_OPERATOR_OIDS gets them. The oid list itself must never appear in a response.
    # Read the SESSION oid, not _resolve_request_identity's - the latter also honors the dev
    # header and api keys, neither of which should confer operator affordances under real login.
    sess = user_auth.read_session(request.cookies.get(user_auth.COOKIE) or "")
    # ONE definition of "operator" (server/operators.py), shared with the compose gate - an
    # affordance the canvas hides but /router/compose still honors would be a hint, not a gate.
    operator = is_operator((sess or {}).get("oid", ""))
    return {
        "dev_auth": dev,
        "users": users,
        "signed_in": signed_in,
        "user": oid if signed_in else "",
        "operator": operator,
        "env_present": ([n for n in CONNECTOR_ENV_NAMES if os.environ.get(n, "").strip()]
                        if operator else []),
        # #423 M2: SP_DEMO_IDENTITY is a real Entra OID (the operator's own authorized
        # SharePoint principal). It is deployment identity, not product metadata, so it is
        # gated exactly like env_present - a signed-in stranger has no business learning it.
        "sp_owner": sp_owner if operator else "",
        "backend": _edition.backend,
        "tenant": _edition.tenant_id,
        "edition": "self-host",
        "chat_models": enabled,
        "chat_model": _edition.chat_model_default,
        "disabled_models": disabled,
    }


# maxsize must stay ABOVE the number of shells this box serves, or the cache thrashes and a
# MISS re-reads and re-hashes every js/css file under static/ on a page load. There are four
# now (index, signin, visitor, link_gone) - #605 task 12 added the last two and turned a cache
# that always hit into one that evicted on every alternating request; #643 folded canvas.html
# into index.html and took the count back down to four.
@functools.lru_cache(maxsize=8)
def _build_id(name: str = "index.html") -> str:
    """Content hash of a shell AND the code it loads (#415).

    It used to hash the shell alone, which meant editing app.css or main.js left the
    id unchanged - so a versioned asset URL built from it would not have changed
    either, and a browser holding a stale module would keep serving it. The whole
    point is to change whenever anything the page EXECUTES changes.

    Cached for the process lifetime: assets cannot change without a redeploy, and a
    redeploy restarts the container."""
    h = hashlib.sha256()
    h.update((_STATIC_DIR / name).read_bytes())
    for f in sorted(_STATIC_DIR.rglob("*")):
        if f.is_file() and f.suffix in (".js", ".css"):
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


def _html(name: str) -> Response:
    """Serve an HTML shell with revalidation forced AND its own build id embedded (#261, #265).

    #261 set Cache-Control: no-cache. That was necessary and insufficient. A directive only
    governs entries stored AFTER it — a browser already holding a header-less copy keeps its
    heuristic freshness window and serves it without asking, which we watched happen
    repeatedly (navigation transferSize 0 on a pre-fix build while the server had the new one).
    Nothing a server sends can retract a copy the client already has.

    So the page is given the build it was rendered from, and checks that against /version on
    boot. A stale copy discovers it is stale and replaces itself — the one path that works
    even when the browser never asks us. Assets under /static get the same directive from
    _RevalidatingStatic (#313) — StaticFiles sends the validators but nothing makes a
    browser USE them, which let a stale login.js survive a deploy."""
    build = _build_id(name)
    text = (_STATIC_DIR / name).read_text(encoding="utf-8").replace("__DBS_BUILD__", build)
    return Response(content=text, media_type="text/html",
                    headers={"Cache-Control": "no-cache, must-revalidate",
                             "ETag": f'"{build}"'})


@app.get("/version")
def version(page: str = "index.html") -> Response:
    """The build a shell SHOULD be on (#265). Never cached: a staleness check served from
    cache is exactly as stale as the thing it is checking.

    #643: the default was canvas.html, which no longer exists. A still-cached copy of the OLD
    canvas.html asking `/version?page=canvas.html` is exactly the case this endpoint is for,
    so an unknown page falls back to index.html rather than 404ing - it gets a build id that
    cannot match, self-heals, and lands on the merged shell."""
    safe = page if page == "index.html" else "index.html"
    return JSONResponse({"build": _build_id(safe)},
                        headers={"Cache-Control": "no-store"})


# ── Marketing site (#401) ───────────────────────────────────────────────────
# The Next.js marketing site is statically exported (`output: "export"`) and served
# by THIS box, so no third party sits between a visitor and dbsearch.ai — which is
# the same promise the product makes about customer documents.
#
# The export is served by a StaticFiles mount registered LAST (see the bottom of
# this file), so every API route above still wins — a marketing page can never
# shadow an endpoint. An explicit per-page route list was tried first and is the
# wrong shape: Next's client router also fetches RSC payloads (`__next.*.txt`) and
# prefetches with HEAD, so anything narrower than "serve the export directory"
# produces 404s and 405s on every client-side navigation.
#
# Checked at the time of writing: the only path the site and the app both wanted
# was "/" itself. The app's demo lives at /router/demo, not /demo.
_SITE_DIR = Path(os.environ.get("DBSEARCH_SITE_DIR", "").strip()
                 or (Path(__file__).resolve().parents[3] / "site" / "out"))


def _site_file(rel: str) -> "Path | None":
    f = _SITE_DIR / rel
    return f if f.is_file() else None


@app.get("/")
def index() -> Response:
    """The marketing site's home page, when one has been built into site/out.

    Falls back to the app shell's own landing so a SELF-HOSTER — who runs this
    same code with no exported site on disk — still gets a working front door
    rather than a 404."""
    f = _site_file("index.html")
    if f is None:
        return _html("index.html")
    return Response(content=f.read_bytes(), media_type="text/html",
                    headers={"Cache-Control": "no-cache, must-revalidate"})


# ── Brand icons (#961) ──────────────────────────────────────────────────────
# The site shipped with create-next-app's stock favicon, so every tab and every phone
# home-screen tile carried the Vercel triangle. Generated by
# scripts/make_brand_icons.py from the approved LinkedIn mark.
#
# Served by the APP, for the /signin reason: a self-hoster has no site/out on disk, and
# these are paths a browser fetches on its OWN initiative rather than because a document
# linked them — /favicon.ico and /apple-touch-icon.png are conventions, so there is no
# link tag to point somewhere else. Routes are declared above the site mount, so the one
# copy under static/ answers on both surfaces and the two cannot drift apart.
#
# Cached for a day, unlike the shells: an icon is immutable in a way a shell is not, and
# a browser that re-fetches the favicon on every navigation is the #313 problem inverted.
# The day is short enough that a rebrand lands without anyone clearing anything.
_ICON_FILES = {
    "favicon.ico": "image/x-icon",
    "apple-touch-icon.png": "image/png",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "site.webmanifest": "application/manifest+json",
}


def _icon(name: str) -> Response:
    f = _STATIC_DIR / name
    if not f.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return Response(content=f.read_bytes(), media_type=_ICON_FILES[name],
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico")
def favicon_ico() -> Response:
    return _icon("favicon.ico")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def apple_touch_icon() -> Response:
    """Both spellings: iOS probes the -precomposed name first and logs a 404 for it
    even when a link tag names the other one."""
    return _icon("apple-touch-icon.png")


@app.get("/icon-192.png")
def icon_192() -> Response:
    return _icon("icon-192.png")


@app.get("/icon-512.png")
def icon_512() -> Response:
    return _icon("icon-512.png")


# ── Book a demo (#962) ──────────────────────────────────────────────────────
# The form used to POST to a third-party endpoint read from NEXT_PUBLIC_FORM_ENDPOINT.
# That variable was set on no build, so the placeholder constant shipped and every lead
# this site has ever taken 404'd into nothing - see demo_requests.py for the full note.
#
# Unauthenticated by construction: a prospect has no account, and requiring one to ask for
# a demo is a contradiction. What bounds it is that it is WRITE-ONLY and gives a stranger
# nothing back but `{"received": true}` - no read, no lookup, no field of any other
# submission, and the same answer whether or not the honeypot tripped.
#
# STORAGE FIRST, THEN MAIL, and the order is the whole point (demo_requests.py). The lead
# is durable before any attempt to send, and a mail failure never reaches the visitor:
# turning a captured lead into "something went wrong" is what made people submit twice
# into the same void.
@app.post("/demo-request", status_code=202)
def demo_request(body: dict, request: Request) -> dict:
    ip = rate_limit.client_ip(request)
    # A public write needs its own budget. 5/minute is far above any human filling in a
    # form and far below anything worth doing with a script.
    if not _local_rate_ok("demo", ip):
        raise HTTPException(status_code=429, detail="too many requests - wait a minute")

    # Answered exactly like a real submission. An error here would tell a scraper which
    # field to leave alone next time.
    if demo_requests.is_bot(body):
        logging.getLogger("dbsearch").info("demo request honeypot tripped; not recorded")
        return {"received": True}

    try:
        fields = demo_requests.clean(body)
    except demo_requests.DemoRequestRejected as exc:
        # The FIELD name, never the value - the value is a named person's work email.
        raise HTTPException(status_code=400, detail=f"invalid {exc}")

    try:
        DEMO_REQUESTS.record(fields, source_ip=ip)
    except Exception:
        # The one failure the visitor must hear about: nothing was kept, so "we'll be in
        # touch" would be a lie. Logged with the payload NOT included.
        logging.getLogger("dbsearch").exception("demo request could not be stored")
        raise HTTPException(status_code=503, detail="could not record this request")

    demo_requests.notify(fields)   # best-effort by contract; never raises
    return {"received": True}


@app.get("/site.webmanifest")
def site_webmanifest() -> Response:
    """The PWA manifest, single-sourced here rather than copied into site/public too:
    two copies of a file nothing compares is a drift waiting to happen, and this route
    already wins over the export for both surfaces."""
    return _icon("site.webmanifest")




@app.get("/robots.txt")
def robots() -> Response:
    """The crawler policy, defaulting to DISALLOW (#334).

    Serving nothing is not neutral. Before this route existed the app answered 404, and a
    crawler reads a missing robots.txt as "crawl everything" — so lifting the basic-auth
    gate would have invited indexing by omission. Indexing is the one step in going public
    that does not cleanly reverse: pages stay cached and surfaced long after a later
    disallow, so the default has to be the closed one and opening up has to be deliberate.

    Policy is env, not code: flipping to public is a prod.env line plus a restart, not an
    edit and an image rebuild. An unrecognized value falls back to disallow rather than
    guessing — a typo in prod must cost a crawl that did not happen, never an index that
    cannot be undone.

    NOTE this is the app's own policy. site/app/robots.ts belongs to the Next.js marketing
    site, which is NOT what the box serves; it has no effect here."""
    policy = os.environ.get("DBSEARCH_ROBOTS", "disallow").strip().lower()
    rule = "Allow: /" if policy == "allow" else "Disallow: /"
    return Response(content=f"User-agent: *\n{rule}\n", media_type="text/plain",
                    headers={"Cache-Control": "no-cache, must-revalidate"})


# #309: the app shell (Draft/Admin/Developer, plus the legacy Ask/Chat) keeps real,
# linkable URLs instead of being a second product competing with the canvas at "/".
# All six serve the same shell; the client router reads the path when there is no hash.
# No collision: GET /chat is distinct from POST /chat, and GET /admin from /admin/index.
# These return static HTML with no content — every data API behind them still enforces
# current_user, so this adds no unauthenticated read path (LAW 2).
#
# THIS LIST MEANS "paths which serve the APP SHELL", and `/c` is deliberately not one of them
# (#605 task 12). The link doorway serves its OWN document - server/link_access.py's
# VISITOR_PAGE - because a visitor has no account and the shell is a workspace: a rail, a
# model picker, Connectors/Admin/Developer, "New conversation", "Your data". Adding "/c" here
# would also register a bare `GET /c` handing the shell to anybody with no token at all.
# The mirrored copy in static/js/login.js carries the same note; selftest_nav_shell.py asserts
# the two lists are EQUAL, in both directions, so neither can grow a path the other lacks.
#
# #643: `/canvas` JOINED THIS LIST, and that is the whole fix for "Connectors is a hard
# refresh". It used to serve canvas.html, a second front-end with its own stylesheet, topbar
# and identity chip, so moving to Connectors was a real document load - 56KB, a new
# `document.title`, and every bit of shell state discarded - while Ask to Draft was a
# pushState. There is one document now; canvas.html is gone and the canvas is
# static/js/surfaces/canvas.js, mounted by the client router like any other surface.
SHELL_PATHS = ("/app", "/ask", "/draft", "/admin", "/developer", "/canvas")


def shell() -> Response:
    return _html("index.html")


for _shell_path in SHELL_PATHS:
    app.add_api_route(_shell_path, shell, methods=["GET"])


@app.get("/chat", include_in_schema=False)
def chat_shell_redirect() -> RedirectResponse:
    """#632: Ask and Chat were one backend behind two skins, and this is the doorway closing.

    They both POSTed this same `/chat/stream` with a conv_id, through the same
    ConversationService - so a thread started on Chat was durable and reachable ONLY from
    Ask's "Your conversations" list. The owner could not say what the difference between the
    two surfaces was, which is the defect rather than a documentation gap.

    308, not 302: the method survives, so a client that follows redirects blindly cannot have
    its POST quietly turned into a GET. That matters here more than usual, because `POST /chat`
    and `POST /chat/stream` are API routes on this very path and they do NOT move - only the
    HTML shell that used to be served at GET /chat does. A 302 here would mean a POST from an
    older client arriving at GET /ask and receiving a page instead of an answer."""
    return RedirectResponse("/ask", status_code=308)


@app.get("/signin")
def signin() -> Response:
    """The branded sign-in page (#446).

    Served by the APP rather than the marketing export, deliberately. A self-hoster has no
    site/out on disk - which is why "/" already falls back to the shell - so a /signin that
    lived only in site/ would 404 for exactly the people who most need to sign in. It also
    has to read /auth/me at runtime, because which providers exist is a property of THIS
    box's env, not of a build.

    Unauthenticated by design: it is the page you visit in order to authenticate. It
    exposes nothing - the provider list it renders is already public in /auth/me."""
    return _html("signin.html")


# The standalone `GET /canvas` handler is gone (#643). It served canvas.html, which no longer
# exists: `/canvas` is in SHELL_PATHS above and serves index.html like every other surface, so
# the URL is unchanged and every bookmark, OAuth callback and "Connect" link still lands on the
# canvas - it is now painted by the client router instead of by a second document.


def _request_tenant(request: Request) -> str:
    """ADR 0012: the tenant partition every document retrieval in this request carries.
    Thin wrapper over the `resolve_tenant` chokepoint with this deployment's constant."""
    return resolve_tenant(request.headers.get, request.cookies.get, _edition.tenant_id)


def _caller_owns_home_directory(request: Request) -> bool:
    """#550: may THIS caller see the deployment's directory + source registry?

    Only a caller whose partition IS the deployment's home tenant. The directory
    (`identity.list_directory`) enumerates the deployment's OWN Graph tenant, and the source
    registry holds that tenant's connected sources, so both are meaningless - and a disclosure -
    to anyone else. A SOLO account (a Google/email signup, partition `acct:<oid>`, e.g. the
    owner's own 123@gmail.com) and a FOREIGN Entra tenant each see NEITHER, which is also the
    honest answer: this deployment cannot enumerate their directory. The ACL picker's "Only you
    / paste an oid" fallback covers the solo case, so nothing an ordinary user does breaks.

    A dev rig is unaffected: dev-auth flattens every caller to the deployment constant, so this
    is True for everyone there, exactly as `is_operator` is (ADR 0011 s3)."""
    partition = _request_tenant(request)
    return bool(partition) and partition == _edition.tenant_id


def _is_foreign_partition(partition: str) -> bool:
    """A partition belonging to an Entra tenant that is NOT this deployment's home
    (#582 / ADR 0019 D1). `acct:` partitions and the deployment constant are local; "" is
    the fail-closed value and is nobody's tenant."""
    return (bool(partition) and partition != _edition.tenant_id
            and not partition.startswith(ACCT_TENANT_PREFIX))


def _request_scope(request: Request, user: str,
                   active_conv_id: "str | None" = None) -> ReadScope:
    """WHERE this request may look (#582 / ADR 0019 D3): the caller's own partition, plus
    one doorway pair per live grant they hold.

    Server-derived end to end - the partition comes from the `resolve_tenant` chokepoint
    and both halves of every pair come from a grant RECORD. Nothing a client sends WIDENS
    this: `active_conv_id` (from `req.conv_id`, #600) is the one client-supplied value that
    reaches this function, and it can only SELECT among grants that already name the caller
    as `grantee_oid` - it cannot manufacture a pair, cannot pick a different grantee's grant,
    and cannot make a foreign-partition grant contribute. At most a caller can ask "open the
    conv-scoped grants for MY conversation c1" and get exactly that grant's own pair back, or
    ask for a conversation nothing was shared into and get nothing. This is the same
    discipline the partition alone has carried since ADR 0012, sharpened rather than
    abandoned: both halves of every pair still come from a grant record, and the value that
    varies with the client can only narrow which of the caller's OWN live records are read.

    Rebuilt per request, so revocation and expiry need no sweep: a grant that stopped
    being live simply stops contributing a pair.

    THIS is where ADR 0019 D1 is actually ENFORCED, in both directions, because this is the
    only place both partitions are known as verified fact:

      - A caller reading from a FOREIGN partition gets NO doorway at all. Share-time
        refusal cannot cover this on its own: a grant may name an account that has no
        identity rows yet (a colleague who has not signed in), and if that person later
        signed in from a foreign tenant, the doorway would hand them a home-tenant document
        by a route the share-time check never saw. Deciding it here, against the session's
        own verified partition, closes that by construction.
      - A grant whose GRANTOR partition is foreign contributes nothing. Nothing in the API
        can create one any more, but a grant made before #582 shipped can still be
        rehydrated from Postgres on restart, and a stale row must not become the one
        exception (pinned by test_a_legacy_grant_naming_a_foreign_partition_is_inert).

    Share-time refusal remains worth having, but as honest EARLY feedback rather than the
    guarantee. The guarantee is here.

    #600 / #601 (conversation sharing): a grant can carry a `conv_id`, meaning it exists
    BECAUSE one conversation was shared rather than one document, and it must authorize
    only inside that conversation.

    WHERE THAT IS ENFORCED, and this paragraph is a CORRECTION (ADR 0020). It used to claim,
    by name, that `/documents/*`, `/admin/documents`, downloads, segments and corpus counts
    "can never see a conversation grant's pair", and concluded that a conversation share was
    therefore structurally incapable of widening into general document access. The first
    half was true and the conclusion did not follow, because the doorway is partition
    ROUTING and `ReadScope.allows` returns True on its partition-equality arm BEFORE it
    consults the doorway. Grantor and grantee in the same partition - every self-host box
    and every single-organization Entra tenant - never reach the doorway at all, so the
    scoping expressed here was simply never applied. Reproduced end to end: /search with no
    conversation, the "Your data" row with its title and uri, the segment preview text, the
    566 bytes of the original file, and /chat in an unrelated thread.

    THE GUARANTEE IS NOW ON THE ACL SIDE: `GrantRegistry.live_principals_for` expands a
    conv-scoped grant's principal only when that conversation is active, and drops it by
    default (ADR 0020). Since the ACL overlap is the single enforcement point every read
    already passes through, that covers every surface at once, including ones nobody
    remembered to list here.

    THE DOORWAY FILTER BELOW IS THE BACKSTOP, and is kept deliberately. It is the half that
    still does real work ACROSS partitions, where routing is not a short-circuit: a grantee
    in her own `acct:` partition reaches a grantor's document only through a pair, so
    withholding the pair outside its conversation is a second, independent refusal. Defence
    in depth, one layer either of which would have to fail. What it must not be mistaken for
    again is the guarantee."""
    partition = _request_tenant(request)
    if _is_foreign_partition(partition):
        # No doorway crosses into a foreign tenant. `active_conv_id` still rides along:
        # the ACL side reads it, and this early return must not silently widen expansion.
        return ReadScope(partition=partition, active_conv_id=active_conv_id)
    pairs = {(g.tenant_id, g.doc_external_id)
             for g in _edition.grant_registry.live_grants_for(user)
             if not _is_foreign_partition(g.tenant_id)
             and (g.conv_id is None or g.conv_id == active_conv_id)}
    return ReadScope(partition=partition, doorway=frozenset(pairs),
                     active_conv_id=active_conv_id)


def _corpus_block(user: str, request: Request,
                  scope: "ReadScope | None" = None) -> "dict | None":
    """#393: the honest denominator that ships WITH every answer.

    The answer surfaces need two different numbers and had only one. Retrieval (how many
    documents this question drew on) rides on the QueryResult; entitlement (how many this
    caller may see at all) comes from here. Printing the first as the second is the bug -
    it made the count move with the question, and made an empty index read as a permissions
    refusal.

    Computed through `Edition.corpus_status`, so it uses the caller's own expanded
    principals (LAW 2 - it can never exceed what a query would return) and the SAME
    per-request tenant partition as the retrieval it accompanies (LAW 5; passing the
    deployment constant here is the #439 bug class). Counts only, never titles or content
    (LAW 1).

    Returns None when the backend cannot count, which the surfaces must render as silence
    rather than as "empty" - claiming an empty corpus we did not measure would be a fresh
    honesty bug inside the fix for an honesty bug (#392).

    #600 review Finding B: inside a shared conversation, `/chat` could cite a document
    (retrieval used the conv-scoped scope) while this footer said `authorized_docs: 0` (this
    call built its own, conversation-blind scope) - the exact "the answer surfaces need two
    numbers and had only one" failure #392 exists to prevent. LAW 5 already required the
    denominator to use the SAME per-request scope as the retrieval it accompanies; it simply
    had no way to say "which conversation".

    #600 review Finding F: the fix is `scope`. A caller that already built one for the
    retrieval it ran (`/chat`, `/chat/stream`) passes it straight through and it is used
    AS-IS rather than independently re-derived here. Two derivations of "the same" scope
    agree only because `_request_scope` is deterministic within a request - true today, but a
    revoke landing between the two calls would desync the denominator from the citations it
    describes, which is the class of bug #393 exists about.

    #601 round 4: there used to be an `active_conv_id` parameter here as well, threaded into
    a fallback `_request_scope(request, user, active_conv_id)`. It is GONE, and it was DEAD
    when it was removed - since Finding F landed, all five call sites passed two positionals
    or `scope=`, nothing in src/ or tests/ ever passed it, and `_request_scope(request, user,
    None)` is byte-identical to the two-argument call. Nothing leaked through it.

    Removed anyway, and the reason is worth stating precisely rather than dramatically: it
    was one edit away from live, on a helper five routes call, sitting on the single seam ADR
    0020's guarantee rests on. A dead parameter that only has to be PASSED to widen principal
    expansion is a footgun whether or not anybody has pulled it. The tripwire
    (`test_only_the_two_chat_routes_may_declare_an_active_conversation`) flagged it on its
    first run, which is what a tripwire is for - not because it caught an exploit.

    Callers with no scope in hand still get `scope=None` and a conversation-blind scope built
    below, which is the correct denominator for a surface with no conversation."""
    if scope is None:
        scope = _request_scope(request, user)
    block = _corpus_for_scope(user, scope)
    # #937 round 2: carry the OTHER plane's count alongside the document one, for the same
    # reason /ask/suggestions does. Without it the answer surfaces can distinguish "you have
    # connected nothing" from "your question matched nothing" only when something was
    # retrieved - so a connected caller whose question found nothing was still told to go and
    # connect a source, underneath an answer that had just said "the source is there and
    # readable". Measured on prod after this card's first deploy; the first fix covered only
    # the retrieved > 0 half.
    #
    # Added HERE and not in `_corpus_for_scope`, which is deliberate: that split exists for the
    # anonymous link visitor (ADR 0021), who has no workspace and whose `user` is synthesized
    # from a share record. Asking for their composed sources would be asking a question with no
    # answer, and the field's absence is already read as "unknown".
    if block is not None:
        block["connected_sources"] = _composed_sources(user)
    return block


def _corpus_for_scope(user: str, scope: ReadScope) -> "dict | None":
    """The denominator for a scope that is ALREADY BUILT - `_corpus_block` minus the request.

    Split out for the one caller that has no `request` to derive a scope FROM: the anonymous
    link visitor (server/link_access.py, ADR 0021), whose scope is synthesized from a share
    record rather than resolved from a session. Passing that caller through `_corpus_block`
    would mean handing it a `request` it must never let reach `_request_scope` - a parameter
    whose only remaining job is to be ignored, which is the shape `_corpus_block`'s own
    docstring records as a footgun (the dead `active_conv_id` parameter, #601 round 4).

    Everything the denominator has to be is here rather than in either caller, so the two
    cannot drift: it is computed through `Edition.corpus_status`, so it uses the caller's own
    expanded principals (LAW 2 - it can never exceed what a query would return) and the SAME
    scope as the retrieval it accompanies (LAW 5). None means the backend cannot count, which
    every surface must render as silence rather than as "empty" (#392)."""
    status = _edition.corpus_status(user, scope)
    if status is None:
        return None
    return {"indexed": status.indexed, "authorized_docs": status.authorized_docs}


def _composed_sources(user: str) -> "int | None":
    """#937: how many sources this caller has composed, or None if we could not measure.

    A thin pass-through to the router API's own counter, which owns the workspace key and the
    manifest store. Defined here rather than inlined at the one call site so the forward
    reference to `_router_api` (built at the bottom of this module, resolved at request time)
    is stated once, with the reason: duplicating the workspace-key derivation in this file is
    how the two rails would start disagreeing about whose workspace a caller is in.

    Absent seam -> None -> the surface says nothing. An older router build must not be
    rendered as "this caller has connected nothing"."""
    counter = getattr(_router_api, "_composed_source_count", None)
    return counter(user) if counter is not None else None


def _require_partitioned_tenant(request: Request) -> None:
    """ADR 0012 (#389): an ingest must land in a REAL tenant partition.

    This replaces `_require_home_tenant`, which 403'd every foreign organization from the
    document surfaces. That gate was never a policy - it stood in for a partition that did
    not exist, and now it does: `tenant_id` is a mandatory predicate in every retrieval
    query, so a foreign org ingesting into *its own* partition is the product working.

    What survives is the narrower check the partition genuinely requires: the caller's
    resolved partition must be NON-EMPTY. As of ADR 0018 (#573), a session carrying no
    Entra tid but a real oid (Google, local email/password) no longer resolves to `""` -
    `resolve_tenant` gives it its own PRIVATE `acct:<oid>` partition, non-empty, so it
    passes this check and can ingest into its own workspace. Only a session with NEITHER
    tid NOR oid (or a `demo:` principal) still resolves to `""` here, and that write is
    still refused - `""` would be a bucket every such identity shares, so refusing it is
    still the fail-closed direction; re-signing-in is cheap, a corpus co-mingled across
    identities is not.

    Operator api-key calls pass because `resolve_tenant` maps an operator key to the
    deployment constant (a non-empty partition), which is what keeps the e2edbs/CLI rigs
    and the operator's own scripted ingest working. Dev rigs with no real login configured
    get the deployment constant too, so they are unaffected."""
    if not _request_tenant(request):
        raise HTTPException(
            status_code=403,
            detail="sign in with your organization account to add documents (this session "
                   "carries no tenant, so there is no workspace to ingest into)")


def _touch_retrieved_owners(owner_oids: "list[str]") -> None:
    """#576 code review Finding 2 (owner's ruling): "any access counts" means any
    RETRIEVAL, not merely holding a live grant. Called at every point a query's results
    are known - /search, /chat, /chat/stream below - so a colleague reading a document
    they did not upload (an org-audience upload with no Grant row at all, or a document
    shared via an explicit grant) keeps THAT DOCUMENT'S OWNER's workspace alive, exactly
    the "HR policy read by colleagues for weeks" case the feature exists for.

    `owner_oids` already came back on the QueryResult at no extra cost (`owner_oid` rides
    on every Chunk, ADR 0012 attribution) - this is a pure touch, no new lookup, no new
    round trip. Best-effort and wrapped whole, same as the current_user hook: retention
    bookkeeping must never turn a successful query into a 500."""
    if not owner_oids:
        return
    try:
        from dbsearch.server import retention
        for owner in owner_oids:
            retention.touch(owner)
    except Exception:
        logging.getLogger("dbsearch").exception("retention touch (retrieval) failed")


@app.post("/ingest")
def ingest(req: IngestRequest, request: Request, user: str = Depends(current_user)) -> dict:
    _require_partitioned_tenant(request)
    # #576 review Finding A (CRITICAL), layer 2: external_id becomes a raw path segment in
    # the object store's blob keys (raw/{tenant}/{external_id}, chunk/.../{n}, ...) - refuse
    # anything that could ever traverse out of its own document's key space BEFORE it is
    # accepted at all, rather than relying only on the adapter's own defenses (layer 1,
    # FilesystemObjectStore._safe_path). This is the ONLY caller-typed external_id in the
    # product (/admin/upload derives its id from `_slug()`, already safe by construction;
    # connector-sourced ids get the same check inside pipeline/runner.py's run_ingestion,
    # which every OTHER ingestion path - SharePoint, folder connector, resync - funnels
    # through) - see is_safe_external_id's docstring for the exact rule.
    if not is_safe_external_id(req.external_id):
        raise HTTPException(status_code=400,
                            detail="external_id must not be empty, contain a path "
                                   "separator, or start with '.'")
    # #842: this endpoint stores bytes exactly as /admin/upload does - the runner writes the
    # raw content to the blob volume either way - so it must pass the same two gates. It
    # passed NEITHER: a signed-in caller could POST unbounded text repeatedly, have it
    # indexed and its bytes written, and the account's usage still read ~0, so the 402 never
    # fired and the 507 that protects the disk for everybody never ran. Same order as the
    # upload path (#831 before #775): the operator condition outranks the billing one, so a
    # full disk is never misreported as "upgrade your plan".
    _incoming = len(req.text.encode("utf-8"))
    _enforce_disk_headroom(_incoming)
    # #844: this path does not supersede by uri, but run_ingestion deletes-before-indexing
    # for the id it is writing, so re-POSTing an external_id REPLACES it - the same
    # double-count, keyed differently.
    _enforce_storage_quota(request, user, _incoming, replaces_doc_id=req.external_id)
    # ADR 0012: chunks land in the CALLER's partition, attributed to the caller.
    _edition.ingest_document(req.external_id, req.title, req.text, req.acl, req.uri,
                             tenant_id=_request_tenant(request), owner_oid=user)
    return {"indexed": req.external_id, "acl": req.acl}


@app.post("/search")
def search(req: SearchRequest, request: Request, user: str = Depends(current_user)) -> dict:
    r = _edition.query_service.answer(user, req.question, llm=_edition.resolve_chat_llm(req.model),
                                      tenant_id=_request_scope(request, user))
    _edition.record_query(user, req.question, r, surface="search")
    _touch_retrieved_owners(r.retrieved_owners)   # #576 Finding 2
    return {"answer": r.answer, "citations": r.citations,
            "referenced": r.referenced,               # #724: the set the answer POINTS AT
            "retrieved_docs": r.retrieved_docs,
            "authorized_docs": r.retrieved_docs,      # deprecated alias (#393)
            "corpus": _corpus_block(user, request)}


@app.post("/chat")
def chat(req: ChatRequest, request: Request, user: str = Depends(current_user)) -> dict:
    # #600 review Finding F: built ONCE and reused for the corpus footer below, rather than
    # letting _corpus_block re-derive it - see _corpus_block's own docstring for why a
    # second derivation is a seam, not merely a redundancy.
    scope = _request_scope(request, user, active_conv_id=req.conv_id)
    gen_llm = _edition.resolve_chat_llm(req.model)
    # #689: the SAME decision /chat/stream makes, from the same helper. A routed stream
    # beside a document-only /chat is a divergence a client trips over by choosing an
    # endpoint - the shape of bug this card is about.
    producer = _bind_ask_producer(user, req.conv_id, scope, gen_llm)
    if producer is not None:
        # Drained rather than given its own seam on `ask()`: ONE producer contract, so the
        # two routes cannot drift on what a routed turn contains or on how it is recorded.
        from dbsearch.query.service import QueryResult
        final = {}
        for ev in _edition.conversation_service.ask_stream(
                user, req.conv_id, req.question, llm=gen_llm, tenant_id=scope,
                answer_producer=producer):
            if ev.get("type") == "done":
                final = ev
        _edition.record_query(
            user, req.question,
            QueryResult(final.get("answer", ""), final.get("citations", []),
                        final.get("retrieved_docs", []), final.get("retrieved_owners", [])),
            surface="chat")
        _touch_retrieved_owners(final.get("retrieved_owners", []))   # #576 Finding 2
        docs = final.get("retrieved_docs", [])
        # `retrieved_owners` is NOT in this dict, deliberately - the same #549/#576 rule the
        # stream's `wire_ev` strip keeps, applied by building the body from named keys rather
        # than by subtracting from the internal one.
        return {"answer": final.get("answer", ""), "citations": final.get("citations", []),
                "retrieved_docs": docs,
                "authorized_docs": docs,              # deprecated alias (#393)
                "footnotes": final.get("footnotes", []),
                # #859: the document path has returned this since #724 and the routed path
                # must too - it is what the Sources rail renders, and a client that had to
                # re-derive it from the prose would be a second home for the rule.
                "referenced": final.get("referenced", []),
                "outcomes": final.get("outcomes", []),
                "routing": final.get("routing", {}),
                "disclosure": final.get("disclosure", ""),
                "corpus": _corpus_block(user, request, scope=scope),
                "conv_id": req.conv_id}
    r = _edition.conversation_service.ask(user, req.conv_id, req.question,
                                          llm=gen_llm,
                                          tenant_id=scope)
    _edition.record_query(user, req.question, r, surface="chat")
    _touch_retrieved_owners(r.retrieved_owners)   # #576 Finding 2
    return {"answer": r.answer, "citations": r.citations,
            "referenced": r.referenced,               # #724: the set the answer POINTS AT
            "retrieved_docs": r.retrieved_docs,
            "authorized_docs": r.retrieved_docs,      # deprecated alias (#393)
            "corpus": _corpus_block(user, request, scope=scope),
            "conv_id": req.conv_id}


def ask_routes_enabled() -> bool:
    """#689 / ADR 0025: whether a conversational turn's ANSWER is produced by the caller's
    router scope instead of the document plane alone.

    OFF by default and read PER REQUEST, not captured at import: this changes what the
    product's most-used surface answers from, so it ships dark, gets turned on deliberately
    on a deployment somebody is watching, and can be turned off again without a rebuild.

    Allowlist parsing, the #315 rule: a typo'd value means OFF. `DBSEARCH_ASK_ROUTES=flase`
    must not enable a feature - the same reasoning that stopped a denylist leaving the dev
    header on."""
    return os.environ.get("DBSEARCH_ASK_ROUTES", "0").strip().lower() in (
        "1", "true", "yes", "on")


def _bind_ask_producer(user: str, conv_id: str, scope, gen_llm):
    """The routed answer producer for THIS turn, or None for today's document path.

    ONE decision, used by both chat routes: a routed /chat/stream beside a document-only
    /chat is a divergence a client trips over by choosing an endpoint, which is the same
    shape of bug as #689 itself.

    None in three cases:
      - the flag is off. Byte-identical to before this card.
      - THIS IS A SHARED THREAD'S CONTINUATION. A share widens the reader's document scope
        for one conversation (ADR 0020's conv-scoped grants) and the whole machinery around
        it - `_readable_prefix`, the per-turn live-grant re-check, `turns_withheld` - is
        defined over DOCUMENTS. The router workspace is the caller's own and knows nothing
        about any of it, so a recipient asking inside a shared thread stays on the plane the
        share was built for. Widening what a share can reach is a product decision, not a
        side effect of turning a flag on.
      - the delegate itself declines (nothing composed, workspace store down) - see
        `router_api.ask_delegate`.
    """
    if not ask_routes_enabled():
        return None
    if _edition.conversation_shares.live_share_for(conv_id, user) is not None:
        return None
    return _router_api.ask_delegate(user, scope, gen_llm)


@app.post("/chat/stream")
def chat_stream(req: ChatRequest, request: Request,
                user: str = Depends(current_user)) -> StreamingResponse:
    """SSE stream of the answer (#50): retrieval/trim happens first, then tokens stream as the
    local model generates — first words appear in ~2-3s instead of a full-answer wait."""
    from dbsearch.query.service import QueryResult

    gen_llm = _edition.resolve_chat_llm(req.model)
    tenant = _request_scope(request, user, active_conv_id=req.conv_id)
    # Measured BEFORE the generator runs: the streaming body is produced after the request
    # scope closes, and `request` is not safe to touch from inside it (#393).
    # #600 review Finding F: `tenant` is the SAME ReadScope the retrieval below uses, passed
    # through rather than rebuilt - see _corpus_block's own docstring for why a second
    # derivation is a seam, not merely a redundancy.
    corpus = _corpus_block(user, request, scope=tenant)
    # #689: bound HERE, outside the generator, for the same reason `tenant` and `corpus` are -
    # the streaming body runs after the request scope closes and `request` is not safe to
    # touch from inside it (#393). The producer closes over the scope measured now.
    producer = _bind_ask_producer(user, req.conv_id, tenant, gen_llm)

    def sse():
        final = None
        # #952: this generator runs AFTER the 200 is on the wire, so an exception here does
        # not become a 500 - it kills the stream mid-flight with no terminal event, and the
        # Ask box shows typing dots forever (measured on prod: a Groq 429 raised by the
        # synthesis call; "Exception in ASGI application"; the owner: "the chat cant type").
        # So the body is wrapped, and a failure becomes ONE terminal error event - unless
        # `done` already went out, in which case the reader has their answer and the failure
        # is only logged. The message is OURS, never the provider's: the real 429 carried the
        # Groq org id and a billing URL, and provider errors can quote api keys (LAW 1).
        # Keyed on the class NAME - the local edition does not ship the provider SDK.
        try:
            yield from _sse_events()
        except Exception as exc:
            logging.getLogger("dbsearch").warning(
                "chat stream failed after headers were sent", exc_info=True)
            if _sent_done[0]:
                return
            if type(exc).__name__ == "RateLimitError":
                msg = ("The model is rate-limited right now - wait a few seconds and ask "
                       "again.")
            else:
                msg = ("Answer generation failed mid-stream. Your documents are fine - "
                       "ask the question again.")
            yield f"data: {json.dumps({'type': 'error', 'message': msg})}\n\n"

    _sent_done = [False]

    def _sse_events():
        final = None
        for ev in _edition.conversation_service.ask_stream(user, req.conv_id, req.question,
                                                           llm=gen_llm, tenant_id=tenant,
                                                           answer_producer=producer):
            if ev.get("type") == "done":
                ev = {**ev, "corpus": corpus}    # the honest denominator, alongside retrieval
                final = ev
                # #576 review round 2, Finding B (Important): `retrieved_owners` is an
                # ACCOUNT ID - the document owner's oid (ADR 0012 attribution), server-
                # internal input to the touch call below. /search and /chat never put it in
                # their response bodies; this stream event was built from the SAME internal
                # dict `ask_stream` yields and forwarded verbatim, so it rode along onto the
                # wire - a colleague reading an org-audience document received the
                # uploader's account id in their own browser's network tab. Same class as
                # #549. `wire_ev` is what actually reaches the client; `final` (with
                # `retrieved_owners` intact) is used ONLY after the generator finishes,
                # server-side, for record_query + the touch call - never re-serialized out.
                #
                # #689: this covers the ROUTED done event too, and has to. That event is
                # built by a different producer entirely (router_api.ask_delegate) and
                # carries the same key for the same reason - the retention touch below is
                # its only consumer - so a strip that named the document path would have
                # reopened #576's leak on the new plane the day the flag went on.
                wire_ev = {k: v for k, v in ev.items() if k != "retrieved_owners"}
            else:
                wire_ev = ev
            yield f"data: {json.dumps(wire_ev)}\n\n"
            if ev.get("type") == "done":
                _sent_done[0] = True     # #952: past this point a failure is log-only
        if final:
            _edition.record_query(
                user, req.question,
                QueryResult(final["answer"], final["citations"], final["retrieved_docs"],
                           final.get("retrieved_owners", [])),
                surface="chat",
            )
            _touch_retrieved_owners(final.get("retrieved_owners", []))   # #576 Finding 2

    return StreamingResponse(sse(), media_type="text/event-stream")


@app.post("/draft")
def draft(req: DraftRequest, request: Request, user: str = Depends(current_user)) -> dict:
    brief = (req.brief or "").strip()
    if not brief or len(brief) > 4000:
        raise HTTPException(status_code=400, detail="brief must be 1–4000 characters")
    d = _edition.draft_proposal(user, brief)
    return {
        "brief": d.brief,
        "plan": d.plan,
        "corpus": _corpus_block(user, request),      # #393: entitlement, once per caller
        "sections": [
            {"title": s.title, "prose": s.prose, "citations": s.citations,
             "retrieved_docs": s.retrieved_docs,
             "authorized_docs": s.retrieved_docs}    # deprecated alias (#393)
            for s in d.sections
        ],
    }


@app.post("/draft/turn")
def draft_turn(req: DraftTurnRequest, user: str = Depends(current_user)) -> dict:
    """One turn of the two-phase conversational draft (#57): Haiku gather chat -> requirements
    confirmation -> Sonnet proposal. `intent` drives the state machine; identity is header-derived
    (LAW 2). The Sonnet draft's retrieval is permission-trimmed by the same QueryService core."""
    conv_id = (req.conv_id or "").strip()
    intent = (req.intent or "chat").strip().lower()
    if not conv_id:
        raise HTTPException(status_code=400, detail="conv_id is required")
    if intent not in ("chat", "ready", "confirm", "cancel", "edit"):
        raise HTTPException(status_code=400, detail="intent must be chat|ready|confirm|cancel")
    if len(req.message or "") > 4000:
        raise HTTPException(status_code=400, detail="message must be <= 4000 characters")
    t = _edition.draft_turn(user, conv_id, req.message or "", intent)
    return {"state": t.state, "reply": t.reply, "requirements": t.requirements, "draft": t.draft}


@app.post("/draft/stream")
def draft_stream(req: DraftTurnRequest, request: Request,
                 user: str = Depends(current_user)) -> StreamingResponse:
    """SSE stream of the CONFIRM step (#61): the Sonnet proposal streams as plan/section/token/
    section_done/done events (the gather chat stays on the JSON /draft/turn). Identity is
    header-derived (LAW 2); retrieval is the permission-trimmed QueryService core."""
    conv_id = (req.conv_id or "").strip()
    if not conv_id:
        raise HTTPException(status_code=400, detail="conv_id is required")
    # Measured before the generator runs - `request` is out of scope inside the stream body.
    corpus = _corpus_block(user, request)

    def sse():
        # #952: same wrap as /chat/stream, same reason - this body runs after the 200 is on
        # the wire, so a raise (the strong model runs per section; a Groq 429 is MORE likely
        # here) kills the stream with no terminal event and the draft surface waits forever.
        # draft.js has rendered `error` events since #61; the server just never sent one for
        # a raise. The message is ours, never the provider's (LAW 1).
        produced = False
        try:
            for ev in _edition.draft_session.confirm_stream(user, conv_id):
                produced = produced or ev.get("type") == "done"
                if ev.get("type") == "section_done":
                    ev = {**ev, "corpus": corpus}       # #393: the entitlement denominator
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as exc:
            logging.getLogger("dbsearch").warning(
                "draft stream failed after headers were sent", exc_info=True)
            if not produced:
                if type(exc).__name__ == "RateLimitError":
                    msg = ("The model is rate-limited right now - wait a few seconds and "
                           "confirm again.")
                else:
                    msg = ("Draft generation failed mid-stream. Your documents are fine - "
                           "confirm again.")
                yield f"data: {json.dumps({'type': 'error', 'message': msg})}\n\n"
            return
        if produced:
            _edition.agent.emit("proposal.drafted", counts={"proposals_drafted": 1}, ts=_edition._now())

    return StreamingResponse(sse(), media_type="text/event-stream")


@app.exception_handler(NotImplementedError)
def _not_implemented(request: Request, exc: NotImplementedError) -> JSONResponse:
    # a backend that hasn't wired an admin read method yet (e.g. pg/Azure) -> 501, not 500
    return JSONResponse(status_code=501, content={"detail": str(exc)})


@app.exception_handler(StarletteHTTPException)
async def _http_exception(request: Request, exc: StarletteHTTPException):
    """GraphQL callers get a GraphQL-shaped refusal; everyone else gets FastAPI's default.

    #432 moved /graphql behind `Depends(current_user)` so default-deny lives in the route table
    where the demo-scope sweep can see it. The side effect was a REST-shaped `{"detail": ...}`
    body, which a GraphQL client cannot read - it looks for `errors[]`.

    Both matter, so both are kept: the HTTP STATUS still says 401/403 (so proxies, logs and
    monitoring see a refusal, and a 200 never carries a failure), and the BODY is the GraphQL
    envelope with an `extensions.code` a client can branch on. The refusal itself happens in the
    dependency, before any resolver runs - this only changes how it is spoken.
    """
    if request.url.path.rstrip("/") == "/graphql":
        code = ("UNAUTHENTICATED" if exc.status_code == 401 else
                "FORBIDDEN" if exc.status_code == 403 else "REQUEST_REFUSED")
        return JSONResponse(
            status_code=exc.status_code,
            content={"data": None,
                     "errors": [{"message": str(exc.detail),
                                 "extensions": {"code": code}}]})
    return await http_exception_handler(request, exc)


def _require_operator(request: Request) -> None:
    """#549: gate the routes that report on the WHOLE deployment rather than on the caller.

    `current_user` answers "are you signed in", never "may you operate this deployment", and
    these routes were declared with nothing else — so any signed-in colleague could read the
    audit trail (other people's QUESTION TEXT, attributed by oid), the directory, and the
    corpus inventory. Being a tenant's user is not being its operator.

    Operator status is read from the SESSION oid, deliberately not from `current_user`: the
    dev header and api keys must not confer operator affordances under a real login, which is
    the same rule /config already applies (ADR 0011 s3). `is_operator` returns True for
    everyone when no real login is configured, so dev rigs and self-host boxes are untouched.

    The refusal is a fixed string — naming the operators would make a 403 an oracle for the
    very list it protects."""
    sess = user_auth.read_session(request.cookies.get(user_auth.COOKIE, ""))
    if not is_operator((sess or {}).get("oid", "")):
        raise HTTPException(status_code=403, detail="operator-only on this deployment")


@app.get("/admin/index", dependencies=[Depends(_require_operator)])
def admin_index(user: str = Depends(current_user)) -> dict:
    return asdict(_edition.admin_service.index_health())


@app.get("/admin/identities", dependencies=[Depends(_require_operator)])
def admin_identities(user: str = Depends(current_user)) -> dict:
    """Who this deployment has resolved, and what they hold. OPERATOR-ONLY since #881.

    It was left ungated by #549 -> #550 for one stated reason: the upload form populated a
    "visible to groups" selector from here and refused to submit empty, so a gate would have
    stopped an ordinary user ingesting their own file. **That selector no longer exists.**
    #539 deleted it - an upload is private to the uploader, its ACL resolved server-side from
    the session - and the only consumer left is the admin "Users and groups" panel, which
    surfaces/admin.js already renders inside `if (operator)`. The exemption outlived the
    journey it protected, and a route nobody ungated calls is just an ungated route.

    #881 is what made that stale exemption expensive. #872 registered directory ROLES as
    principals, so the response stopped describing group membership and started naming who
    holds "Global Administrator" - the tenant's administrator list, with `member_count`
    saying how few of them there are. Gating is both smaller than filtering the new class and
    strictly more complete: the group oids, the user-to-group mapping and the per-principal
    document counts were already a disclosure to every signed-in caller (#580/#550), and one
    dependency closes all of it rather than the one class that happened to be noticed.

    Directory roles are deliberately NOT filtered out of the body. A role is a principal an
    ACL can genuinely name (#872) and the operator managing those ACLs is exactly who is
    reading this. The leak was the audience, not the row.

    The `tenant:<tid>` filter below stays, though its #575 Finding 3 rationale ("must not
    ride along on this UNGATED response") no longer applies. It is now disclosure hygiene
    rather than a boundary: a synthetic principal is not a directory fact, and the operator
    has no use for it. Cheap, guarded, and removing it would be a change nobody asked for.
    """
    d = asdict(_edition.admin_service.identities())
    d["groups"] = [g for g in d["groups"] if not str(g["group_oid"]).startswith("tenant:")]
    for u in d["users"]:
        u["group_oids"] = [g for g in u["group_oids"] if not str(g).startswith("tenant:")]
    return d


@app.get("/admin/documents")
def admin_documents(request: Request, user: str = Depends(current_user)) -> list[dict]:
    """What's indexed, with each doc's ACL (identities only, never body text) — #51.

    NOT operator-only, unlike its neighbours: a person who uploads their own document must be
    able to see it listed — that is the "talk to your own data" journey (#548/#539). It is
    ACL-trimmed to the caller instead, through the same identity port retrieval uses, so it
    can never name a document a query would not return (#549). The operator still sees all.

    The tenant comes from the REQUEST, not the deployment constant: passing the constant here
    is the #439 bug class, and on a hosted multi-tenant deployment it read across tenants."""
    sess = user_auth.read_session(request.cookies.get(user_auth.COOKIE, ""))
    scope = _request_scope(request, user)
    # #791: this response names its OWN fields instead of serializing whatever the domain
    # object happens to carry. `DocACL` gained `owner_oid` so the supersede-by-uri loop could
    # be owner-scoped server-side (one user's upload must never delete another's); an
    # asdict() here would have published every uploader's OID to each colleague who can read
    # the document. Ownership reaches the client as the `owned_by_you` boolean below - about
    # the CALLER, never a third party's identifier - which is #549's rule and the reason this
    # listing is ACL-trimmed at all.
    rows = [{"doc_external_id": d.doc_external_id, "title": d.title, "uri": d.uri,
             "allowed_principals": list(d.allowed_principals or [])}
            for d in _edition.list_documents(
                user, scope, unrestricted=is_operator((sess or {}).get("oid", "")))]

    # #594: which of these are YOURS to delete. Readability is not ownership - on an org-wide
    # document every colleague reads it and none of them may destroy it - so the listing has
    # to carry the distinction or the UI will draw a Delete button that can only 404 (#551).
    # A backend that cannot answer leaves the field off entirely: unknown is not "yes", and an
    # absent field draws no button, which is the safe direction for an operation with no undo.
    # #582: OWNERSHIP is asked of the caller's OWN partition (`scope.partition`), never the
    # doorway. A document reached through a share is by definition not yours to delete, so
    # it must fall out of this set - which is also what stops the UI drawing a Delete
    # button on somebody else's document.
    # #582: which of these arrived through a SHARE rather than your own upload. Without this
    # the panel calls a document you were given "everything you have added", and offers you a
    # Share button on it that can only 404 (ADR 0017 s2 - a share cannot be re-shared). Found
    # by driving the page in a browser after the doorway landed; the API was right and the
    # page was lying about whose document it was.
    shared_docs = {g.doc_external_id for g in
                   _edition.grant_registry.live_grants_for(user)}
    for row in rows:
        row["shared_with_you"] = row.get("doc_external_id") in shared_docs

    try:
        owned = set(_edition.index.docs_owned_by(scope.partition, user))
    except NotImplementedError:
        owned = None
    if owned is not None:
        for row in rows:
            row["owned_by_you"] = row.get("doc_external_id") in owned

    # #948: MERGE THE SECOND PLANE. Everything above is the uploaded-document index; a
    # connector store (gdrive, sharepoint_link, folder) indexes into its own in-process index
    # and is invisible here, so a caller whose only source is a Drive folder saw an empty
    # Admin while the node showed a doc count and /ask answered from it (#937's split). The
    # router seam is WARM-ONLY - it never materializes a cold workspace to render this list -
    # and ACL-trims through each store's own authorize(), so it can only ADD documents this
    # caller may already see. None means the seam is absent or the workspace is not warm:
    # leave the upload listing untouched rather than claim the connector plane is empty.
    connector_docs = None
    _seam = getattr(_router_api, "_composed_documents", None)
    if _seam is not None:
        try:
            connector_docs = _seam(user)
        except Exception:
            connector_docs = None
    if connector_docs:
        seen = {r.get("doc_external_id") for r in rows}
        for d in connector_docs:
            if d.get("doc_external_id") not in seen:
                rows.append(d)
    return rows


@app.get("/ask/suggestions")
def ask_suggestions(request: Request, user: str = Depends(current_user)) -> dict:
    """What the Ask surface may honestly offer this caller before they type (#392).

    The Ask page used to ship two hardcoded example chips naming the DEMO SEED's two
    documents. On prod the seed is off and the index holds zero rows, so the most likely
    first click a new user makes was guaranteed to return nothing - and the generic
    "I couldn't find anything you have access to" made an empty index look like a
    permissions refusal. The examples are now server-supplied and only exist when the corpus
    that answers them does.

    `examples` is non-empty ONLY when the demo seed actually ran. LAW 1: counts and
    static prompt strings, never document titles, ids or content. LAW 2: the count comes
    from the caller's own expanded principals via the ADR 0012 tenant partition, so it can
    never exceed what a real query would return for them."""
    status = _edition.corpus_status(user, _request_scope(request, user))
    examples = list(edition_mod.DEMO_EXAMPLE_PROMPTS) if edition_mod.demo_seed_enabled() else []
    # #937: the SECOND plane. Everything above counts uploaded documents, and a connector store
    # indexes outside that count entirely (router/providers/connector.py), so `indexed: False`
    # alone can never distinguish "you have connected nothing" from "your Drive folder is not
    # in this index". Both readings produced the same banner, and the surface picked the wrong
    # one for every connector-only caller on prod. None = unknown, and the surface stays silent
    # on unknown rather than guessing empty.
    composed = _composed_sources(user)
    if status is None:
        # Backend cannot count. Say nothing rather than guess "empty" (see Edition.corpus_status).
        return {"known": False, "indexed": None, "authorized_docs": None,
                "connected_sources": composed, "examples": examples}
    return {"known": True, "indexed": status.indexed,
            "authorized_docs": status.authorized_docs,
            "connected_sources": composed, "examples": examples}


@app.post("/admin/permission-test", dependencies=[Depends(_require_operator)])
def admin_permission_test(req: PermissionTestRequest, user: str = Depends(current_user)) -> dict:
    # Answers "what would ANOTHER named user see?" — an entitlement oracle, operator-only (#549).
    return asdict(_edition.admin_service.permission_test(req.user_oid, req.question))


#: #623. Actionable and honest in one sentence: it says the record still exists, because the
#: user's next question is "have I lost my history?" and the answer is no.
_AUDIT_UNAVAILABLE = ("cannot read your question history right now - it is stored, not lost, "
                      "try again shortly")


@app.get("/admin/demo-requests", dependencies=[Depends(_require_operator)])
def admin_demo_requests(limit: int = 100,
                        user: str = Depends(current_user)) -> dict:
    """The "Book a demo" leads (#962), for an operator. This is the DURABLE record; the
    email in demo_requests.notify() is only the ping, and pings get lost.

    `durable` and `email_configured` are reported rather than assumed. With no DSN the
    store is in-memory and dies with the process; with no mail key nothing is ever sent.
    Either way an empty list would otherwise read as "nobody has asked" - the #200
    affirmative-looking failure, pointed at a sales pipeline. This endpoint says which of
    the two you are looking at.

    Operator-gated for the /admin/audit reason: these rows are named people's work email
    addresses and what they said they wanted, so "signed in" was never a sufficient gate.

    `user` is unused in the body and is NOT decoration: current_user is the live-only
    identity resolver, so depending on it is what stops a `demo:*` principal reaching this
    at all (ADR 0009). _require_operator alone gates on the oid it is handed; this is what
    guarantees the oid is a real one. Same shape as admin_audit above.
    """
    return {
        "durable": isinstance(DEMO_REQUESTS, demo_requests.PgDemoRequestStore),
        "email_configured": demo_requests.mail_config() is not None,
        "requests": DEMO_REQUESTS.recent(max(1, min(int(limit), 500))),
    }


@app.get("/admin/telemetry", dependencies=[Depends(_require_operator)])
def admin_telemetry(user: str = Depends(current_user)) -> dict:
    return asdict(_edition.admin_service.telemetry())


@app.get("/admin/audit", dependencies=[Depends(_require_operator)])
def admin_audit(limit: int = 50, user: str = Depends(current_user)) -> list[dict]:
    """Query/access audit trail (#45) — OPERATOR-gated (#549). Metadata only (LAW 1), but the
    metadata includes other users' question text attributed to their oid, which is exactly why
    "signed in" was never a sufficient gate here."""
    try:
        return [e.to_dict() for e in _edition.audit_log.recent(limit)]
    except AuditLogUnavailable:
        raise HTTPException(status_code=503, detail=_AUDIT_UNAVAILABLE)


@app.get("/me/questions")
def my_questions(limit: int = 25, user: str = Depends(current_user)) -> list[dict]:
    """The caller's OWN question history (#593). Not operator-gated, and never widened.

    "Questions you have asked" lives in the owner's half of Your data, but it called
    /admin/audit - operator-only since #549, because those rows carry other users' question
    text attributed to their oid. So the panel 403'd for every ordinary user, i.e. for exactly
    the people whose questions they were.

    The gate above is right and stays. This route is the other half of it: the filter is the
    SERVER's `user`, taken from the verified session, so there is no parameter a caller could
    pass to see somebody else's history and no browser-side filter to trust (LAW 2). An
    operator reading this route gets their own rows too - /admin/audit is where the
    deployment-wide view lives, and one route with two meanings is how a gate rots.

    #623: A STORE OUTAGE IS A 503 AND NEVER AN EMPTY LIST. `[]` renders as "No questions
    yet", which is a statement about the CALLER'S HISTORY; the store being unreachable is a
    statement about the store, and showing the second as the first tells a user their record
    is gone when it is sitting in Postgres. This is the same failure shape that let #623 hide
    in plain sight on prod - the panel said "No questions yet" after a restart and looked,
    for all the world, like a user who had not asked anything yet.
    """
    try:
        return [e.to_dict()
                for e in _edition.audit_log.recent(max(1, min(limit, 200)), user=user)]
    except AuditLogUnavailable:
        raise HTTPException(status_code=503, detail=_AUDIT_UNAVAILABLE)


@app.get("/admin/sources")
def admin_sources(request: Request, user: str = Depends(current_user)) -> list[dict]:
    # NOT operator-gated, deliberately (#549): the canvas calls this to restore a signed-in
    # user's own ingested SharePoint state across a reload (syncSharePointNodes). #550: but the
    # registry holds the HOME tenant's sources, so a solo account or a foreign tenant would read
    # the deployment's source names + counts + tenant metadata. Scope it to the home tenant; a
    # non-home caller owns nothing here (their own connector stores live in the per-oid router
    # workspace, not this registry), so an empty list is correct AND non-leaking.
    if not _caller_owns_home_directory(request):
        return []
    return [asdict(s) for s in _edition.admin_service.sources()]


@app.get("/admin/principals")
def admin_principals(request: Request, user: str = Depends(current_user)) -> dict:
    """Named principals for the ACL picker (#258).

    NOT operator-gated, deliberately (#549 -> #550): this backs the canvas ACL picker, and an
    ordinary user choosing who may see the store they just created needs to resolve NAMES.
    Gating it left them pasting raw GUIDs behind an "advanced" link — which is precisely the
    silent-typo failure this picker exists to prevent. The residual exposure is that
    `list_directory()` is not tenant-scoped in either adapter, so on a multi-tenant deployment
    one org's user can see another's principals. That is a real defect, it PREDATES this gate,
    and a role check is the wrong instrument for it: it needs tenant scoping in the adapter (#550).

    Returns {available, principals, reason}. `available` is the honest bit: a backend that
    cannot enumerate its directory reports available=false WITH a reason, so the canvas can
    say "directory unavailable — paste an oid" instead of rendering an empty dropdown that
    reads as "this tenant has no groups". Distinguishing those two is the whole point;
    conflating them is the #255 failure mode in a new place."""
    # #550: the directory is the HOME tenant's. A solo account (acct:<oid>) or a foreign tenant
    # must not enumerate it - not a role gate (that broke the picker, #549), a TENANT scope. The
    # unavailable shape below is the SAME one a no-directory backend returns, so the ACL picker
    # keeps its "Only you / paste an oid" fallback for a solo user rather than breaking.
    if not _caller_owns_home_directory(request):
        return {"available": False, "principals": [],
                "reason": "sign in with your organization account to resolve names here, "
                          "or paste an oid — a personal account shares only with itself"}
    try:
        entries = _edition.identity.list_directory()
    except NotImplementedError as exc:
        return {"available": False, "principals": [],
                "reason": f"this identity backend cannot list a directory ({exc})"}
    except Exception as exc:                      # Graph down, token expired, throttled…
        return {"available": False, "principals": [],
                "reason": f"directory lookup failed: {exc}"}
    if not entries:
        # An authoritative-looking empty picker is the trap this endpoint exists to avoid:
        # "no principals we can NAME" is a capability gap, not a statement that the tenant
        # has no groups. Report it as unavailable so the canvas keeps the oid escape hatch.
        return {"available": False, "principals": [],
                "reason": "no named principals are known on this backend yet — "
                          "sign in so the directory can be resolved, or paste an oid"}
    return {"available": True, "principals": [asdict(p) for p in entries], "reason": ""}


# ---- In-app 'Add SharePoint' connector (card #148, S1: OAuth admin-consent) ---------------
def _oauth_start(build_url) -> RedirectResponse | JSONResponse:
    """Begin an OAuth leg: mint a single-use CSRF nonce, hand the matching signed `state` to
    `build_url`, and set the nonce in the pre-auth cookie on the response. Every leg
    (SharePoint consent, Entra sign-in, Google link) goes through here, so none of them can
    be the one that forgets to bind its state to the browser."""
    state, nonce = sp_connect.start_state()
    resp = RedirectResponse(build_url(state), status_code=302)
    sp_connect.set_state_cookie(resp, nonce)
    return resp


def _state_ok(request: Request, params: dict) -> bool:
    return sp_connect.check_state(params.get("state", ""),
                                  request.cookies.get(sp_connect.STATE_COOKIE, ""))


@app.get("/connectors/sharepoint/consent")
def sp_consent(request: Request, user: str = Depends(current_user)):
    """Kick off the flow: redirect the admin to Microsoft's admin-consent screen for our app."""
    _require_partitioned_tenant(request)
    if not sp_connect.is_configured():
        raise HTTPException(status_code=503, detail="SharePoint connector not configured (set SP_CONNECTOR_CLIENT_ID/SECRET/REDIRECT_URI)")
    return _oauth_start(sp_connect.consent_url)


@app.get("/connectors/sharepoint/consent-url")
def sp_consent_url(request: Request, user: str = Depends(current_user)) -> JSONResponse:
    """JSON variant so the SPA (which carries the dev-auth header on fetch, not on a plain
    navigation) can get the URL and then window.location to Microsoft. The CSRF nonce rides
    this response's Set-Cookie, exactly as on the redirect variant."""
    _require_partitioned_tenant(request)
    if not sp_connect.is_configured():
        raise HTTPException(status_code=503, detail="SharePoint connector not configured (set SP_CONNECTOR_CLIENT_ID/SECRET/REDIRECT_URI)")
    state, nonce = sp_connect.start_state()
    resp = JSONResponse({"url": sp_connect.consent_url(state)})
    sp_connect.set_state_cookie(resp, nonce)
    return resp


@app.get("/connectors/sharepoint/callback")
def sp_callback(request: Request):
    """Microsoft redirects the browser here after consent. CSRF-guarded by the single-use
    nonce cookie + signed state (no dev-auth header rides an external redirect)."""
    params = dict(request.query_params)
    if not _state_ok(request, params):
        raise HTTPException(status_code=400, detail="invalid or expired state")
    tenant, err = sp_connect.parse_callback(params)
    # #431: a connection is recorded AGAINST AN OWNER, so the callback has to know who consented.
    # A browser redirect carries no dev header, but it does carry the session cookie, and reaching
    # here means /consent already required a session. If the identity cannot be resolved we say so
    # instead of recording nothing: a consent that appears to succeed and then leaves the node
    # unconnected is the most confusing outcome available.
    try:
        owner = resolve_identity(lambda n: request.headers.get(n), request.cookies.get)
    except AuthError:
        owner = ""
    if not err and not owner:
        err = ("your sign-in wasn't carried back from Microsoft — sign in, then connect "
               "SharePoint again")
    if err:
        resp = RedirectResponse(f"/canvas?connector=sharepoint&error={urllib.parse.quote(err)}", status_code=302)
    else:
        sp_connect.mark_connected(tenant, owner=owner)   # the picker then ingests (S2)
        # canvas-first: always return to the canvas (not the legacy /#/connectors dashboard)
        resp = RedirectResponse(f"/canvas?connector=sharepoint&tenant={urllib.parse.quote(tenant)}", status_code=302)
    sp_connect.clear_state_cookie(resp)      # single-use
    return resp


@app.get("/connectors/sharepoint/drives")
def sp_drives(tenant: str, request: Request, user: str = Depends(current_user)) -> list[dict]:
    """List the consented tenant's SharePoint document libraries for the picker."""
    _require_partitioned_tenant(request)
    if not sp_connect.is_configured():
        raise HTTPException(status_code=503, detail="SharePoint connector not configured")
    try:
        return sp_connect.list_drives(tenant)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


class SharePointFinishRequest(BaseModel):
    tenant: str
    drive_id: Optional[str] = None      # pick a whole library, OR…
    share_link: Optional[str] = None    # …#300: paste a folder sharing link → ingest just that folder


@app.post("/connectors/sharepoint/finish")
def sp_finish(req: SharePointFinishRequest, request: Request, user: str = Depends(current_user)) -> dict:
    """Ingest into the index → register as a queryable source (S2). Either a whole library
    (`drive_id`) or, #300, one folder resolved from a `share_link`."""
    _require_partitioned_tenant(request)
    if not sp_connect.is_configured():
        raise HTTPException(status_code=503, detail="SharePoint connector not configured")
    drive_id, folder_path = req.drive_id, None
    if req.share_link:
        # #300: a bad/expired/file link is the USER's input, not a server fault → 400, not 502.
        try:
            resolved = sp_connect.resolve_share_link(req.tenant, req.share_link)
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        drive_id, folder_path = resolved["drive_id"], resolved["folder_path"]
    if not drive_id:
        raise HTTPException(status_code=400,
                            detail="provide a library to ingest (drive_id) or a folder sharing link")
    # #302: publish live indexing progress (discover → fetch → extract → embed → index) to a
    # per-tenant store the canvas polls, so a multi-minute ingest isn't a silent 'Ingesting…'.
    sp_connect.set_progress(req.tenant, "starting", 0, 0)

    def _publish_terminal(result, exc):
        """#569: the crawl no longer ends inside this request, so the REQUEST can no longer
        report the outcome - the worker does, on the same progress store the canvas already
        polls (#302/#365). Publishing "complete" from the request, as this used to, would now
        announce a crawl that had merely been submitted."""
        if exc is not None:
            logging.getLogger("dbsearch").exception(
                "sharepoint ingest failed for tenant %s", req.tenant, exc_info=exc)
            sp_connect.set_progress_error(req.tenant, "the ingest failed — see server logs")
            return
        sp_connect.set_progress_complete(req.tenant, {
            "status": "connected", "tenant": req.tenant, "drive_id": drive_id,
            "docs_indexed": result.doc_count})

    try:
        submitted = _edition.connect_sharepoint(
            req.tenant, drive_id, folder_path=folder_path,
            progress=lambda phase, done, total: sp_connect.set_progress(req.tenant, phase, done, total),
            tenant_id=_request_tenant(request), owner_oid=user,   # ADR 0012
            on_done=_publish_terminal)
    except RuntimeError as e:
        # #365: publish the terminal state instead of clearing — if the proxy dropped this
        # response mid-crawl, the canvas poller is the only way the client learns the outcome.
        sp_connect.set_progress_error(req.tenant, str(e))
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        # #367: ANY other failure (a storage/driver error deep in the index stage) must land
        # here too. Leaving a LIVE phase in the store hangs the canvas forever: the poller
        # keeps reading "indexing" from a crawl that is already dead. Detail stays generic —
        # a driver message can quote row content (LAW 1) — the traceback goes to the log.
        logging.getLogger("dbsearch").exception(
            "sharepoint ingest failed for tenant %s", req.tenant)
        sp_connect.set_progress_error(req.tenant, "the ingest failed — see server logs")
        raise
    # The CONNECTION is real the moment consent resolved and the crawl was accepted - that is
    # what the user did, and it is true before the library finishes indexing. The crawl's own
    # outcome is published by `_publish_terminal` on the worker.
    sp_connect.mark_connected(req.tenant, owner=user)
    # 202, not 200: accepted and running, with a handle to follow it. `docs_indexed` is
    # deliberately absent rather than 0 - a zero here would render "0 documents indexed" for a
    # crawl that had not started, which is the "broken or working?" confusion this card exists
    # to remove. Poll /connectors/sharepoint/ingest-progress or GET /ingest/jobs/{job_id}.
    return JSONResponse(status_code=202, content={"status": "ingesting", **submitted})


@app.get("/ingest/jobs/{job_id}")
def edition_ingest_job(job_id: str, request: Request,
                       user: str = Depends(current_user)) -> dict:
    """#569: follow an edition-rail crawl. The router rail's twin is `/router/jobs/{id}`.

    Reports phase, documents done out of total, how many a resume skipped, and the terminal
    reason. `error` is an exception CLASS NAME by construction (jobs.JobCheckpoint.failed) -
    never a driver or connector message, which can quote document content or a credential
    (LAW 1).

    LAW 2 / ADR 0012: a job is visible only to a caller in the partition that owns it. 404,
    never 403, for anything else - a job id must not become an oracle for another workspace's
    source names and document counts, which is #549's defect on a new surface.
    """
    job = _edition.ingest_job(job_id)
    if job is None or job.tenant_id != _request_tenant(request):
        raise HTTPException(status_code=404, detail="no such job")
    return {"job_id": job.job_id, "source_id": job.source_id, "status": job.status,
            "phase": job.phase, "docs_done": job.docs_done, "docs_total": job.docs_total,
            "docs_skipped": job.docs_skipped, "error": job.error}


@app.get("/connectors/sharepoint/ingest-progress")
def sp_ingest_progress(tenant: str, ack: bool = False, user: str = Depends(current_user)) -> dict:
    """#302: latest indexing progress for an in-flight ingest, polled by the canvas. Returns
    {phase, done, total}, a #365 terminal state ({phase:'complete', result} / {phase:'error',
    detail}), or {phase:'idle'} when nothing is running. `ack=1` clears a terminal state
    (and ONLY a terminal state) — the canvas acks before starting a fresh ingest."""
    if ack:
        sp_connect.ack_progress(tenant)
    return sp_connect.get_progress(tenant) or {"phase": "idle", "done": 0, "total": 0}


@app.get("/connectors/sharepoint/status")
def sp_status(user: str = Depends(current_user)) -> dict:
    # #431: scoped to the CALLER. This used to return every connection on the box, so a stranger
    # from another Entra tenant saw (and their canvas node claimed) somebody else's connection.
    return {"configured": sp_connect.is_configured(),
            "connected": sp_connect.connected_tenants(owner=user)}


# ---- Real per-user Microsoft/Entra sign-in (#171) ----------------------------------------
def _login_error(msg: str) -> RedirectResponse:
    """Any OAuth leg that ends badly returns the user to the canvas with a message - never a
    raw 400 JSON page (LAW 1: the message is an error string, never a token)."""
    resp = RedirectResponse(f"/canvas?login=error&msg={urllib.parse.quote(msg)}", status_code=302)
    sp_connect.clear_state_cookie(resp)       # the nonce is spent whatever the outcome
    return resp


@app.get("/auth/login")
def auth_login():
    if not user_auth.is_enabled():
        raise HTTPException(status_code=503, detail="sign-in not configured (SP_CONNECTOR_* env)")
    return _oauth_start(user_auth.login_url)


@app.get("/auth/grant/db")
def auth_grant_db(request: Request, user: str = Depends(current_user)):
    """#429 incremental round: ask THIS user's own tenant to consent to the Azure SQL
    delegation, at the moment they connect a database rather than at sign-in.

    Requires an existing session - it upgrades an identity we already trust, so an
    unauthenticated caller has nothing to upgrade. Deliberately NOT gated on
    `_require_partitioned_tenant`: a foreign org consenting to its OWN database delegation is the
    product working, not a boundary being crossed."""
    if not user_auth.is_enabled():
        raise HTTPException(status_code=503, detail="sign-in not configured")
    if not user_auth.db_consent_scopes():
        raise HTTPException(status_code=503,
                            detail="no database delegation is configured on this deployment")
    return _oauth_start(user_auth.db_consent_url)


@app.get("/auth/grant/db-url")
def auth_grant_db_url(request: Request, user: str = Depends(current_user)) -> JSONResponse:
    """JSON variant, same reason as the SharePoint consent-url twin: the SPA fetches rather
    than navigates, and the single-use CSRF nonce rides this response's Set-Cookie."""
    if not user_auth.db_consent_scopes():
        raise HTTPException(status_code=503,
                            detail="no database delegation is configured on this deployment")
    state, nonce = sp_connect.start_state()
    resp = JSONResponse({"url": user_auth.db_consent_url(state)})
    sp_connect.set_state_cookie(resp, nonce)
    return resp


def _entra_link(u: dict, session_oid: str) -> RedirectResponse:
    """#646 (ADR 0023): hang a verified Entra credential off the identity the caller is
    ALREADY signed in as, and leave that identity alone.

    CREDENTIAL ONLY. Nothing here writes the session: no tid, no group registration, no
    partition change. "Connected" therefore means "DBSearch can redeem Microsoft as you", not
    "you are in your org's workspace" - the same thing a Google link has always meant, so the
    account panel keeps ONE meaning across providers instead of two. Promotion to the org
    partition is a separate decision with its own LAW 2 analysis (ADR 0023 "What a link buys").

    The two refusals are Google's, learned there first and inherited rather than re-derived.
    """
    # No refresh token = nothing to redeem. Reporting success here would be a lie the user
    # only discovers when their first delegated ask fails, long after the consent screen.
    if not u.get("refresh_token"):
        return _login_error("Microsoft returned no refresh token - nothing was linked; "
                            "try connecting again")
    linked_to = None
    try:
        linked_to = ACCOUNTS.link("entra", u["oid"], session_oid)
    except Exception:
        logging.getLogger("dbsearch").warning("account row not recorded for this link")
    # `link()` refuses to re-point an identity that already maps elsewhere and hands back that
    # PRE-EXISTING owner. Vaulting anyway would say "Connected" while the account graph says
    # this Entra identity belongs to someone else - and a later Microsoft sign-in would resolve
    # to THAT account, with the token orphaned here. Names neither account: a refusal must not
    # become an oracle for who owns what.
    if linked_to is not None and linked_to != session_oid:
        return _login_error(
            "that Microsoft account is already connected to a different DBSearch account - "
            "sign in as that account and disconnect Microsoft from the account menu, or sign "
            "in with it directly")
    # LAW 1: server-side only, never the cookie, never a response body. Keyed by the SESSION's
    # oid - that is the whole point of the link.
    user_auth.VAULT.put(session_oid, u["refresh_token"], idp="entra")
    resp = RedirectResponse(
        f"/canvas?linked=microsoft&name={urllib.parse.quote(u.get('name') or 'Microsoft')}",
        status_code=302)
    sp_connect.clear_state_cookie(resp)      # single-use CSRF nonce, spent either way
    return resp                              # NB: no set_cookie - the session is untouched


@app.get("/auth/entra/link")
def auth_entra_link(request: Request, _caller: str = Depends(current_user)):
    """#646: start the Microsoft leg in LINK mode - i.e. while already signed in as somebody.

    Requires a session, because linking upgrades an identity we already trust and an
    anonymous caller has nothing to upgrade (the same stance /auth/grant/db takes). It is
    NOT public infra: /auth/login is the bootstrap hop that mints a session from nothing,
    whereas this one only makes sense when a session already exists - so it carries
    `Depends(current_user)` like any other authenticated route, and selftest_demo_scope_boundary
    sweeps it as one. What
    actually decides link-vs-mint is the callback reading the session, so this route is the
    honest entry point rather than the enforcement point; the enforcement is at
    /auth/callback, where the verified oid is finally known.

    Deliberately reuses AUTH_REDIRECT_URI. A dedicated /auth/entra/link/callback would read
    more clearly, but it needs a second redirect URI registered in the Entra app registration
    on EVERY deployment - turning a code fix into an operations task for every self-host
    operator. `prompt=select_account` is already what login_url asks for, which is exactly
    right here: the user is picking WHICH Microsoft account to attach."""
    if not user_auth.is_enabled():
        raise HTTPException(status_code=503,
                            detail="sign-in not configured (SP_CONNECTOR_* env)")
    sess = user_auth.read_session(request.cookies.get(user_auth.COOKIE, ""))
    if not (sess or {}).get("oid"):
        raise HTTPException(status_code=401, detail="sign in first")
    return _oauth_start(user_auth.login_url)


@app.get("/auth/callback")
def auth_callback(request: Request):
    params = dict(request.query_params)
    if params.get("error"):
        return _login_error(params.get("error_description") or params["error"])
    if not _state_ok(request, params):
        raise HTTPException(status_code=400, detail="invalid or expired state")
    if not params.get("code"):
        raise HTTPException(status_code=400, detail="missing authorization code")
    try:
        u = user_auth.exchange_code(params["code"])
    except RuntimeError as e:
        return _login_error(str(e))
    # #646 (ADR 0023): LINK vs MINT, decided by reading the session - exactly as the Google
    # callback below has done since #193.
    #
    # This route used to mint unconditionally. So a user signed in as their email account who
    # reached Microsoft sign-in was not connecting Microsoft to that account: they were
    # silently re-principaled to the Entra oid. Different account, different partition, their
    # workspace and conversations simply absent. Same human, two accounts, no warning.
    #
    # Only the THIRD case is new. A signed-out user still becomes their Entra identity, and a
    # signed-in user re-authenticating AS THEMSELVES (the "Sign in again" pill, whose whole job
    # is to re-mint a credential for the same oid) still takes the mint path untouched -
    # `oid == u["oid"]` is a re-auth, not a swap, and treating it as a link would leave that
    # pill unable to do the one thing it exists for.
    sess = user_auth.read_session(request.cookies.get(user_auth.COOKIE, ""))
    session_oid = (sess or {}).get("oid", "")
    if session_oid and session_oid != u["oid"]:
        return _entra_link(u, session_oid)
    # #572 (ADR 0013): record the account row. Best-effort - a down account store must
    # never turn a working sign-in into a 500 (same stance as TokenVault._remember).
    try:
        # #582 (ADR 0019 D2): record the VERIFIED tid and email on the identity row. This
        # is what lets a share compute the grantee's partition instead of guessing from an
        # identifier's shape, and it is why the last silently-broken share case can now be
        # refused honestly. Both values come from the validated id token (user_auth).
        ACCOUNTS.resolve("entra", u["oid"], preferred_account_id=u["oid"],
                         tid=u.get("tid") or "", email=u.get("email") or "")
    except Exception:
        logging.getLogger("dbsearch").warning("account row not recorded for this sign-in")
    # per-user LAW 2: register the signed-in user's REAL transitive Entra groups so retrieval
    # trims to their actual memberships (self-host identity; azure backend expands via Graph).
    ident = getattr(_edition, "identity", None)
    if u.get("oid") and u.get("tid") and hasattr(ident, "set_user_groups"):
        # #875: ONE implementation, shared with the current_user chokepoint. This block used to
        # be a second copy ending in `or []`, which cached a FAILED Graph lookup as "resolved,
        # no groups" - permanent for the process, because set_user_groups then made knows_groups
        # true and the chokepoint's retry could never fire. #266 introduced the None-vs-[]
        # distinction precisely to stop that, and this path threw it away. It is delegated now,
        # so a failure means the same thing on both paths by construction rather than by two
        # authors agreeing.
        #
        # `force=True`: a fresh sign-in is a legitimate moment to RE-resolve (memberships may
        # have changed since the cached answer, and this is the one moment we know the user is
        # present). It skips only the cache check - a failed lookup still registers nothing.
        #
        # Resolved against u["tid"], the session's OWN verified tenant, not the deployment
        # constant (#439 / ADR 0012). #258 principal names ride along inside, with the id
        # token's display name as the fallback where Graph names nothing.
        # #575: still in-memory, so a restart drops this - which is exactly what the chokepoint
        # exists to repair, and now genuinely can.
        _resolve_groups_if_unknown(ident, u["oid"], u["tid"], session_tid=u["tid"],
                                   force=True, self_name=u.get("name", ""))
    # #156: vault the multi-resource refresh token server-side ONLY — never in the
    # cookie, never logged, never in a response body — so the broker can later
    # redeem source-scoped access tokens as this user (LAW 1/LAW 2).
    if u.get("oid") and u.get("refresh_token"):
        user_auth.VAULT.put(u["oid"], u["refresh_token"])
    # ADR 0011 s5 / ADR 0018: the verified `tid` from the code exchange is part of the
    # session, not a detail of the exchange. Dropping it here used to 403 EVERY real
    # sign-in at `_require_partitioned_tenant` - including the operator's own - because a
    # session with no tid was treated as foreign with no partition. As of ADR 0018 the
    # failure mode is quieter and worse: a dropped tid now silently lands the operator in
    # their own private `acct:<oid>` partition instead of the home tenant's, rather than
    # refusing outright. The tenant gate is only as good as this line either way.
    # #630: which provider this session came through. The account control says "Signed in
    # with Microsoft" and it must be reading a fact, not inferring one from an email domain -
    # a gmail address proves nothing about how somebody authenticated. Sessions minted before
    # this field existed simply lack it, and the control falls back to a plain "Signed in"
    # rather than guessing.
    token = user_auth.sign_session({"oid": u["oid"], "name": u["name"], "email": u["email"],
                                    "tid": u.get("tid", ""), "idp": "entra",
                                    "exp": int(time.time()) + 8 * 3600})
    resp = RedirectResponse(f"/canvas?login=ok&name={urllib.parse.quote(u['name'])}", status_code=302)
    resp.set_cookie(user_auth.COOKIE, token, httponly=True, samesite="lax",
                    secure=_cookie_secure(), max_age=8 * 3600, path="/")
    sp_connect.clear_state_cookie(resp)       # single-use CSRF nonce
    return resp


@app.post("/auth/logout")
def auth_logout(request: Request):
    sess = user_auth.read_session(request.cookies.get(user_auth.COOKIE, ""))
    if sess and sess.get("oid"):
        user_auth.VAULT.drop(sess["oid"])
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(user_auth.COOKIE, path="/")
    return resp


@app.post("/auth/disconnect/{idp}")
def auth_disconnect(idp: str, request: Request, _caller: str = Depends(current_user)):
    """#652: forget ONE cloud's credential for the calling identity.

    `TokenVault.drop` has taken a per-cloud `idp` since #193 and nothing ever called it that
    way - the only call site was /auth/logout, with no idp, which drops every cloud AND ends
    the session. So the account panel could say "Connected" with no way back, and
    /auth/google/callback's refusal told a stuck user to "disconnect it there first" about a
    control that did not exist.

    CREDENTIAL ONLY (owner's ruling). This revokes what DBSearch may redeem; it does NOT
    unlink the identity. Signing in with that provider again re-links to the SAME account,
    which is ADR 0013 decision 4's existing promise and not this route's to change.

    TWO DIFFERENT JOBS, deliberately split. `Depends(current_user)` is the DEFAULT-DENY GATE -
    it is what selftest_demo_scope_boundary sweeps for, and every route owes it. The vault key
    is then read from the SESSION COOKIE and nowhere else, because the dev-auth header can
    assert any identity (#183): a caller who can name a victim's oid must never be able to drop
    that victim's credential. Gate says "you are someone"; the cookie says "who". Using
    `_caller` for the key would be the bug.

    Unknown idp is a 400, not a shrug - a typo must never report success having dropped
    nothing, which is the same stance `scopes_for` takes on an unknown channel."""
    if idp not in user_auth.KNOWN_IDPS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown provider {idp!r} (known: {', '.join(user_auth.KNOWN_IDPS)})")
    sess = user_auth.read_session(request.cookies.get(user_auth.COOKIE, ""))
    oid = (sess or {}).get("oid", "")
    if not oid:
        raise HTTPException(status_code=401, detail="sign in first")
    user_auth.VAULT.drop(oid, idp)
    # Report what is left, from the vault itself rather than from what we think we just did -
    # `linked()` refuses to report a credential it cannot decrypt, so this is the same answer
    # /auth/me would give and the panel cannot drift from it.
    return JSONResponse({"ok": True, "idp": idp, "linked": user_auth.VAULT.linked(oid)})


@app.post("/auth/aws/connect")
def auth_aws_connect(req: AwsConnectRequest, request: Request,
                     _caller: str = Depends(current_user)):
    """ADR 0024: link AWS by vaulting the caller's OWN access keys - a form, not an OAuth
    dance, because AWS delegated data access is IAM and there is no consumer OAuth with a
    path to the data plane (owner ruling under #650).

    Same two-job split as /auth/disconnect (#652): `Depends(current_user)` is the
    default-deny gate, and the vault key comes from the SESSION COOKIE and nowhere else.
    Here that stance guards the WRITE side: the dev header can assert any identity (#183),
    and a caller who could name a victim's oid must never be able to plant keys under it -
    every later query-as-the-user would silently run as the PLANTER's AWS identity while
    the panel reads Connected, a poisoned-credential channel rather than a leak.

    FALSIFIED BEFORE BELIEVED: the keys must answer sts:GetCallerIdentity before they are
    vaulted. An unvalidated put would report Connected and die at first query - the
    green-surface-over-hollow-offer shape (#646/#652/#654/#656). The validated identity
    (account id + arn) is returned: it is the caller's own identity, not a secret, and it
    lets the UI say WHAT was connected rather than merely that something was.

    Write-only, per ADR 0010's asymmetry: the keys go in once and never come back out over
    any API - vault entries have no read affordance by construction (three-segment
    namespace, user_auth._VAULT_NS)."""
    sess = user_auth.read_session(request.cookies.get(user_auth.COOKIE, ""))
    oid = (sess or {}).get("oid", "")
    if not oid:
        raise HTTPException(status_code=401, detail="sign in first")
    akid = req.access_key_id.strip()
    secret = req.secret_access_key.strip()
    if not akid or not secret:
        raise HTTPException(status_code=400,
                            detail="access_key_id and secret_access_key are required")
    try:
        import boto3
        from botocore.config import Config as _BotoConfig
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        # 501, not 500: the capability is absent, not broken. /auth/me's aws_enabled makes
        # the UI never offer this on such a box, so reaching here means a direct call.
        raise HTTPException(status_code=501, detail=(
            "this deployment cannot hold AWS credentials - boto3 is not installed"))
    try:
        ident = boto3.client(
            "sts", aws_access_key_id=akid, aws_secret_access_key=secret,
            config=_BotoConfig(connect_timeout=5, read_timeout=10,
                               retries={"max_attempts": 1}),
        ).get_caller_identity()
    except (ClientError, BotoCoreError) as exc:
        # AWS's own reason (InvalidClientTokenId, SignatureDoesNotMatch, a network hole) -
        # never the keys themselves, which botocore does not echo.
        raise HTTPException(status_code=400, detail=f"AWS rejected these keys: {exc}")
    user_auth.VAULT.put(oid, json.dumps({"access_key_id": akid,
                                         "secret_access_key": secret}), idp="aws")
    return JSONResponse({"ok": True, "idp": "aws",
                         "linked": user_auth.VAULT.linked(oid),
                         "identity": {"account": ident.get("Account", ""),
                                      "arn": ident.get("Arn", "")}})


@app.get("/auth/google/login")
def auth_google_login(channels: str = "", incremental: bool = False):
    """Start the Google leg. With an existing session this LINKS a Google credential to
    that identity (#193 account linking); with none it signs the user in as their Google
    identity. `channels` (comma-separated, e.g. "bigquery,drive") selects the delegated
    scopes to request - least privilege is incremental, not up-front.

    An unknown channel is a 400, not a shrug: silently dropping it would consent to the base
    scopes only, vault a refresh token with NO data scopes, report success - and then every
    query against that source would fail opaquely at Google, long after the typo."""
    if not google_auth.is_enabled():
        raise HTTPException(status_code=404, detail="google login not configured")
    chans = [c.strip() for c in channels.split(",") if c.strip()] or None
    try:
        return _oauth_start(lambda state: google_auth.login_url(
            state, channels=chans, incremental=incremental))
    except ValueError as e:                    # unknown channel name
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/auth/google/callback")
def auth_google_callback(request: Request):
    params = dict(request.query_params)
    if params.get("error"):                    # e.g. the user declined consent
        return _login_error(params.get("error_description") or params["error"])
    if not _state_ok(request, params):
        raise HTTPException(status_code=400, detail="invalid or expired state")
    if not params.get("code"):
        raise HTTPException(status_code=400, detail="missing authorization code")
    try:
        u = google_auth.exchange_code(params["code"])
    except RuntimeError as e:
        return _login_error(str(e))
    # No refresh token = nothing to vault. Saying "linked" here would be a lie the user only
    # discovers when their first BigQuery ask fails; surface it now instead.
    if not u.get("refresh_token"):
        return _login_error("google returned no refresh token - nothing was linked; "
                            "try connecting again")
    # Account linking (#193): keep the identity the user is ALREADY signed in as, and hang
    # the Google credential off it. Only a signed-out user becomes their Google identity.
    sess = user_auth.read_session(request.cookies.get(user_auth.COOKIE, ""))
    oid = (sess or {}).get("oid") or u["email"]
    # #572 (ADR 0013): record the account row, and - for a signed-out caller - resolve to
    # a PREVIOUSLY LINKED account rather than always minting the bare-email identity. This
    # is the actual #442 fix: a Google identity linked to an Entra account on a prior visit
    # now signs in to THAT account (the Entra oid), not a second orphaned one keyed on the
    # email. Best-effort, same stance as the Entra callback above - a down account store
    # must never turn a working sign-in into a 500. This is the one deliberate exception to
    # "never reassign the session principal": ADR 0013 decision 4 is that a linked identity
    # signs in to the SAME workspace, which by construction means `oid` (the workspace key
    # and doc-ACL principal) becomes the linked account's id here. The unlinked case is
    # unaffected - `oid` stays the verified email, exactly as LAW 2 requires today.
    linked_to = None
    try:
        if sess:   # linking: caller is authenticated as the account AND completed OAuth (ADR 0013 d3)
            linked_to = ACCOUNTS.link("google", u.get("sub") or u["email"], oid)
        else:      # sign-in: find the linked account, or auto-provision (ADR 0013 d4)
            # #582: a Google session has no Entra tenant, so no tid - but the verified
            # email is how a colleague will name them in a share.
            oid = ACCOUNTS.resolve("google", u.get("sub") or u["email"],
                                   preferred_account_id=u["email"],
                                   email=u.get("email") or "")
    except Exception:
        logging.getLogger("dbsearch").warning("account row not recorded for this sign-in")
    # Review finding #1: `link()` refuses to re-point an identity that already maps
    # elsewhere and returns that PRE-EXISTING owner instead of `oid`. If we vaulted the
    # fresh refresh token under `oid` anyway, the UI would say "linked" while the account
    # graph still says this Google identity belongs to someone else - a later Google
    # sign-in would resolve to THAT account, not this one, with the token sitting orphaned
    # under `oid`. Refuse instead of lying about success. The message names neither account
    # (no id, no email) - it must not become an oracle for who owns what.
    if sess and linked_to is not None and linked_to != oid:
        return _login_error(
            "that Google account is already connected to a different DBSearch account - "
            "sign in as that account and disconnect Google from the account menu, or sign "
            "in with it directly")
    # LAW 1: the refresh token is vaulted server-side ONLY - never the cookie, never a body.
    user_auth.VAULT.put(oid, u["refresh_token"], idp="google")
    resp = RedirectResponse(f"/canvas?linked=google&name={urllib.parse.quote(u['name'])}",
                            status_code=302)
    if not sess:
        # ADR 0011 s5: a Google identity has no Entra tenant, so its `tid` is empty by
        # construction - which the doc-plane gate reads as "not the home tenant". As of
        # ADR 0018 that no longer refuses this session outright: it gets its own private
        # `acct:<oid>` partition and can upload/retrieve there. SharePoint ingest still
        # will not work for it, and that is correct, not an oversight: SharePoint is an
        # Entra-side capability gated by a real tenant consent flow, independent of
        # `resolve_tenant`, and a Google identity has no Entra tenant to consent with. The
        # field is written explicitly so the shape of a session never depends on which
        # callback minted it.
        token = user_auth.sign_session({"oid": oid, "name": u["name"], "email": u["email"],
                                        "tid": "", "idp": "google",      # #630
                                        "exp": int(time.time()) + 8 * 3600})
        resp.set_cookie(user_auth.COOKIE, token, httponly=True, samesite="lax",
                        secure=_cookie_secure(), max_age=8 * 3600, path="/")
    sp_connect.clear_state_cookie(resp)        # single-use CSRF nonce
    return resp


# #574: per-process rate limiting only - a plain module-level dict is NOT durable (a
# restart, or a second worker process, resets it). Acceptable for this slice; a real
# limiter (Redis, or a DB counter) is a later hardening pass, not this one.
#
# Keyed on (bucket, ip) - "bucket" separates login attempts from signup attempts so a
# visitor who signs up and then immediately signs in does not spend the same budget
# twice, and "ip" is rate_limit.client_ip(request), NOT request.client.host: behind
# Caddy (and Cloudflare in front of it) every request arrives from 127.0.0.1, so keying
# on the socket peer would put the entire internet in one bucket - code review Finding 2.
_LOCAL_RATE_ATTEMPTS: dict[tuple[str, str], list[float]] = {}
_LOCAL_RATE_LAST_SWEEP = [0.0]        # single-element box so the closure can mutate it


def _local_rate_ok(bucket: str, ip: str, limit: int = 5, window: float = 60.0) -> bool:
    now = time.time()
    key = (bucket, ip)
    hits = [t for t in _LOCAL_RATE_ATTEMPTS.get(key, []) if now - t < window]
    hits.append(now)
    _LOCAL_RATE_ATTEMPTS[key] = hits
    # Stale-key eviction (code review Finding 2): client_ip is the REAL visitor address
    # and varies per caller, unlike the request.client.host bug this replaced - without
    # this the dict grows one entry per distinct caller forever. Sweeping at most once
    # per window keeps the hot path O(1) amortized.
    if now - _LOCAL_RATE_LAST_SWEEP[0] >= window:
        _LOCAL_RATE_LAST_SWEEP[0] = now
        for stale in [k for k, v in _LOCAL_RATE_ATTEMPTS.items()
                     if not v or now - v[-1] >= window]:
            _LOCAL_RATE_ATTEMPTS.pop(stale, None)
    return len(hits) <= limit


def _local_session_response(account_id: str, email: str) -> JSONResponse:
    name = email.split("@", 1)[0]
    # tid="" is what makes a local account partition to its own acct:<oid> corpus
    # (ADR 0018 / #573) instead of the fail-closed "" that made these logins decorative.
    token = user_auth.sign_session({"oid": account_id, "name": name, "email": email,
                                    "tid": "", "idp": "local",           # #630
                                    "exp": int(time.time()) + 8 * 3600})
    resp = JSONResponse({"ok": True, "name": name})
    resp.set_cookie(user_auth.COOKIE, token, httponly=True, samesite="lax",
                    secure=_cookie_secure(), max_age=8 * 3600, path="/")
    return resp


@app.post("/auth/local/signup")
def auth_local_signup(body: dict, request: Request):
    if not local_auth.is_enabled():
        raise HTTPException(status_code=404, detail="not found")
    # code review Finding 3: signup was unrate-limited, and paid scrypt's ~65ms / 32MB
    # cost BEFORE checking for a duplicate email - a few hundred concurrent anonymous
    # signup POSTs could pin single-digit GB of RSS. Rate-limited the same as login.
    if not _local_rate_ok("signup", rate_limit.client_ip(request)):
        raise HTTPException(status_code=429, detail="too many attempts - wait a minute")
    email = local_auth.normalize_email(str(body.get("email", "")))
    password = str(body.get("password", ""))
    if not local_auth.valid_email(email):
        raise HTTPException(status_code=400, detail="a valid email address is required")
    if len(password) < local_auth.MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400,
                            detail=f"password must be at least {local_auth.MIN_PASSWORD_LEN} characters")
    # Duplicate check BEFORE the hash: this does not reintroduce a timing oracle, because
    # the 409 below already discloses "this email exists" explicitly, in the response
    # BODY - unlike /auth/local/login (which deliberately withholds that fact and so
    # equalizes timing with a burn hash), there is nothing left here for timing to leak
    # that the status/body has not already said outright. Checking first just means an
    # attacker who already knows (or is probing) a taken email does not also cost us a
    # scrypt call to be told so.
    if ACCOUNTS.get_local_user(email) is not None:
        # #574 accepted trade-off: this 409 is an account-enumeration oracle (a caller can
        # learn whether an email is registered). Accepted at this stage - there is no email
        # infra yet to do a "check your inbox either way" flow instead. Revisit with #-TBD
        # once verification email exists. Do NOT silently return success here; that would
        # let a signup "succeed" against an email whose password the caller never set.
        raise HTTPException(status_code=409, detail="an account with this email already exists")
    salt, pwhash = local_auth.hash_password(password)
    try:
        account_id = ACCOUNTS.create_local_user(email, salt, pwhash)
    except ValueError:
        # The pre-check above is a DoS mitigation, not the authority - a concurrent signup
        # for the same email can still win the race between the check and this insert.
        # create_local_user (both stores) is the atomic source of truth; this is that
        # same 409, reached the rare way instead of the common way.
        raise HTTPException(status_code=409, detail="an account with this email already exists")
    return _local_session_response(account_id, email)


@app.post("/auth/local/login")
def auth_local_login(body: dict, request: Request):
    if not local_auth.is_enabled():
        raise HTTPException(status_code=404, detail="not found")
    if not _local_rate_ok("login", rate_limit.client_ip(request)):
        raise HTTPException(status_code=429, detail="too many attempts - wait a minute")
    email = local_auth.normalize_email(str(body.get("email", "")))
    row = ACCOUNTS.get_local_user(email)
    # verify against a burn hash even when the email is unknown, so response time does not
    # distinguish "no such account" from "wrong password" - both hex strings below are the
    # same LENGTH as a real salt/hash, so verify_password takes the identical scrypt path
    # (the scrypt cost depends only on n/r/p, never on salt/hash content) rather than
    # short-circuiting on a malformed value.
    salt = row["salt"] if row else "00" * 16
    good = local_auth.verify_password(str(body.get("password", "")), salt,
                                      row["pwhash"] if row else "00" * 32)
    if not (row and good):
        raise HTTPException(status_code=401, detail="invalid email or password")
    # #582: the subject IS the email here; recording it in the email column too is what
    # makes `account_for_email` a single indexed lookup across every idp.
    ACCOUNTS.resolve("local", email, email=email)          # touch last_seen
    return _local_session_response(row["account_id"], email)


@app.get("/auth/me")
def auth_me(request: Request) -> dict:
    sess = user_auth.read_session(request.cookies.get(user_auth.COOKIE, ""))
    oid = (sess or {}).get("oid", "")
    return {"enabled": user_auth.is_enabled(),
            "google_enabled": google_auth.is_enabled(),
            "local_enabled": local_auth.is_enabled(),
            # ADR 0024: can this box validate + redeem AWS keys (boto3 present)? Implementation
            # presence, not an env var - AWS linking has no client id to configure.
            "aws_enabled": _aws_supported(),
            "signed_in": bool(oid),
            "name": (sess or {}).get("name", ""),
            "email": (sess or {}).get("email", ""),
            "oid": oid,
            # #630: which provider minted this session, so the account control can say so
            # instead of inferring it. Empty for a session predating the field - the control
            # then says a plain "Signed in", which is true of every session.
            "idp": (sess or {}).get("idp", ""),
            "linked": user_auth.VAULT.linked(oid) if oid else [],
            # #575: only a session with a verified Entra tid, on an identity backend that can
            # actually hold a tenant principal, can be offered "My organization" at upload
            # time - Google/local-account sessions (ADR 0018) have no tenant to publish into,
            # and #575 review round 2 found a cloud-backend gap the tid check alone missed.
            # Same predicate /admin/upload's 400 uses (_identity_can_hold_tenant_principal),
            # so this UI signal and that enforcement can never disagree.
            "has_org": bool((sess or {}).get("tid")) and
                       _identity_can_hold_tenant_principal(getattr(_edition, "identity", None))}


@app.post("/auth/dev/seed")
def auth_dev_seed(body: dict):
    """TEST-ONLY seam for /e2edbs --live-entra: vault a ROPC-obtained refresh token
    and mint the matching session cookie. Hidden (404) unless DBSEARCH_DEV_SEED=1 —
    never enable on a deployment."""
    if os.environ.get("DBSEARCH_DEV_SEED") != "1":
        raise HTTPException(status_code=404, detail="not found")
    oid, rt = body.get("oid", ""), body.get("refresh_token", "")
    if not oid or not rt:
        raise HTTPException(status_code=400, detail="oid and refresh_token required")
    user_auth.VAULT.put(oid, rt)
    # #258: mirror the REAL callback and register transitive groups + names. Without this a
    # seeded session expands to its bare oid, so every GROUP-based ACL is invisible to it —
    # the seam would silently disagree with the sign-in path it exists to stand in for, and
    # a tiering bug would look like a permission bug (or vice versa).
    ident = getattr(_edition, "identity", None)
    tid = body.get("tenant_id") or os.environ.get("AUTH_TENANT_ID", "")
    if tid and hasattr(ident, "set_user_groups"):
        # #575 review minor-b: mirror the callback exactly, tenant principal included, or
        # this seam disagrees with the sign-in path it exists to stand in for and
        # /e2edbs --live-entra can never exercise org sharing at all.
        # #875: "mirror the callback exactly" now means CALLING WHAT THE CALLBACK CALLS. Left as
        # its own copy, this seam would keep the `or []` failure-caching bug after the real path
        # was fixed - and the test seam quietly disagreeing with production is worse than the
        # original bug, because it is the thing that would have to catch it.
        _resolve_groups_if_unknown(ident, oid, tid, session_tid=tid,
                                   force=True, self_name=body.get("name", ""))
    # ADR 0011 s5: the seam already resolved the tenant above (body, else AUTH_TENANT_ID);
    # putting it in the session too is what keeps this stand-in honest - a seeded session
    # must reach the doc-plane gate with the same shape the real callback produces, or an
    # /e2edbs user-mode rig would 403 on ingest where production succeeds.
    token = user_auth.sign_session({"oid": oid, "name": body.get("name", oid),
                                    "email": body.get("email", ""),
                                    "tid": tid,
                                    "exp": int(time.time()) + 8 * 3600})
    resp = JSONResponse({"ok": True})
    resp.set_cookie(user_auth.COOKIE, token, httponly=True, samesite="lax",
                    secure=_cookie_secure(), max_age=8 * 3600, path="/")
    return resp


@app.get("/admin/documents/{doc_id}/segments")
def admin_document_segments(doc_id: str, request: Request,
                            user: str = Depends(current_user)) -> list[dict]:
    # #582: this took no `request` at all, so it could only ever read the deployment
    # constant - the #439 bug class, on a path nobody carried the fix across to. It now
    # derives the caller's scope like every other read, which also lets a grantee open the
    # segments of a document shared with them instead of seeing an empty list.
    try:
        return _edition.document_segments(doc_id, user, _request_scope(request, user))
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="segment preview not available on this backend")


@app.get("/admin/documents/{doc_id}/download")
def admin_document_download(doc_id: str, request: Request, form: str = "bundle",
                            user: str = Depends(current_user)) -> Response:
    """#562: download one document as it was ingested.

    `form=text` is the extracted text (what the answer engine actually sees), `form=original`
    the bytes as supplied, `form=bundle` a zip of both. The text is the half that answers
    "did this ingest correctly" - a PDF that parsed to garbage looks perfect in a listing.

    404 for a document that does not exist AND for one this caller holds no grant on: the
    same answer, so this cannot be used to discover that a document exists. A title is
    routinely the whole secret.

    LAW 2: trimmed for EVERYONE, operator included - `document_bundle` takes no unrestricted
    flag. The operator's untrimmed view (#549) is of metadata; serving content to someone
    outside the ACL would break the one promise the product is built on. LAW 1: the bytes go
    to the caller's own browser inside the data plane and never to the control plane.

    The tenant comes from the REQUEST, not the deployment constant (#439 / ADR 0012).
    """
    if form not in ("bundle", "text", "original"):
        raise HTTPException(status_code=400, detail="form must be bundle, text or original")
    b = _edition.document_bundle(doc_id, user, _request_scope(request, user))
    if b is None:
        raise HTTPException(status_code=404, detail="no such document")
    stem = _slug(b["title"] or doc_id) or "document"
    if form == "text":
        if b["text"] is None:
            raise HTTPException(status_code=404, detail="no extracted text retained for this document")
        return Response(b["text"].encode("utf-8"), media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{stem}.txt"'})
    if form == "original":
        if not b["original"]:
            raise HTTPException(status_code=404, detail="no original retained for this document")
        return Response(b["original"], media_type=_sniff_mime(b["uri"] or stem, None),
                        headers={"Content-Disposition": f'attachment; filename="{stem}"'})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # Named so the pair is obvious on disk. A half-empty bundle is still useful and says
        # so by omission rather than by failing the whole download.
        if b["original"]:
            z.writestr(f"{stem}/original", b["original"])
        if b["text"] is not None:
            z.writestr(f"{stem}/extracted.txt", b["text"])
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{stem}.zip"'})


@app.post("/admin/resync")
def admin_resync(req: ResyncRequest, user: str = Depends(current_user)):
    """#569: submits and returns 202 with a job handle. It used to run the whole re-crawl
    inside this request - the same LAW 4 violation #454 removed from the router rail, on a
    route any operator can hit against a real library. Follow it at /ingest/jobs/{job_id}."""
    try:
        handle = _edition.resync_source(req.source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown source: {req.source_id}")
    return JSONResponse(status_code=202,
                        content={**handle, "poll": f"/ingest/jobs/{handle['job_id']}"})


@app.post("/admin/retention/sweep")
def admin_retention_sweep(request: Request, dry_run: bool = False,
                          user: str = Depends(current_user)) -> dict:
    """#576: operator-only. An irreversible, whole-workspace delete has no business running
    inline in a request (LAW 4) - this starts the REAL sweep on a detached daemon thread and
    returns immediately.

    Final review Fix 4: this is now the ONLY path that runs a real sweep. The cron entrypoint
    (`python3 -m dbsearch.server.retention`) POSTs HERE instead of sweeping in its own
    process. Originally because a fresh process held a fresh in-memory ConversationService
    and so could not delete the chat history of the account it was deleting the documents
    of; #596 made that store durable when PGVECTOR_DSN is set, which removes that specific
    failure, but not the reason to route through here - the memory-only fallback
    (unconfigured, demo/self-host) still has a fresh-process-means-empty-store problem, and
    even where the store is durable this stays the one real-sweep implementation instead of
    two that can drift apart on what a sweep means. See `retention.run_cron_sweep`.

    `?dry_run=true` (#576 review Finding 10) is the one exception: it is READ-ONLY by
    construction (`retention.sweep(..., dry_run=True)` performs every read - candidate
    selection, the TOCTOU re-check, document enumeration - and skips every write), so it is
    cheap and safe to answer SYNCHRONOUSLY, 200, with the same report shape a real sweep
    would produce. An irreversible delete-everything button with no way to preview what it
    is about to do is not a luxury omission.

    404, not 403, on refusal: this route DELETES data, so confirming its existence to a
    caller who cannot use it is its own small leak (#549's "a 403 would confirm the route"
    reasoning, same as `_may_share`)."""
    if not is_operator(user):
        raise HTTPException(status_code=404, detail="not found")

    from dbsearch.server import retention

    if dry_run:
        return retention.sweep(_edition, ACCOUNTS, _manifest_store, dry_run=True)

    import threading
    t = threading.Thread(target=retention.sweep_and_log,
                         args=(_edition, ACCOUNTS, _manifest_store), daemon=True)
    t.start()                                  # LAW 4: never inline in the request
    return JSONResponse({"started": True}, status_code=202)


_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_EXT_MIME = {
    ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".json": "application/json",
}


def _sniff_mime(filename: str, content_type: str | None) -> str:
    if content_type and content_type != "application/octet-stream":
        return content_type
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXT_MIME.get(ext, "application/octet-stream")


def _slug(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "doc"


# ---- #775 billing ------------------------------------------------------------------------
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request) -> dict:
    """Stripe tells us what somebody is paying for. NO AUTHENTICATION, by design: Stripe
    cannot hold a session cookie, so the signature IS the authentication, and it is checked
    against the raw body before a single field is read.

    Always answers 200 once the signature is good, even when the event changes nothing.
    Stripe retries any non-2xx, so returning an error for "I ignored that one" would turn a
    no-op into an infinite redelivery loop.
    """
    payload = await request.body()
    try:
        billing.verify_signature(payload, request.headers.get("Stripe-Signature", ""))
        event = billing.parse_event(payload)
    except billing.WebhookRefused:
        # Deliberately uninformative: a caller must not learn WHICH part failed (#549's rule,
        # applied to a signature instead of a document).
        raise HTTPException(status_code=400, detail="could not verify this request")
    try:
        outcome = billing.handle_event(event, ACCOUNTS)
    except Exception:
        # A 500 here makes Stripe retry, which is right for a transient fault.
        logging.getLogger("dbsearch").exception("stripe webhook handling failed")
        raise HTTPException(status_code=500, detail="could not apply this event")
    return {"received": True, "outcome": outcome}


@app.post("/billing/checkout")
def billing_checkout(req: dict, request: Request, user: str = Depends(current_user)) -> dict:
    """Start a subscription. Signed-in only: the whole point is to attach a payment to an
    ACCOUNT, and an anonymous checkout would produce a subscription belonging to nobody."""
    if not billing.configured():
        raise HTTPException(status_code=501, detail="billing is not enabled on this deployment")
    tier_name = str(req.get("tier") or "").strip()
    try:
        tier = tiers_mod.tier(tier_name)
    except tiers_mod.UnknownTier:
        raise HTTPException(status_code=400, detail="unknown plan")
    if not tier.sellable:
        raise HTTPException(status_code=400, detail=f"the {tier.name} plan is not for sale")

    base = str(request.base_url).rstrip("/")
    row = ACCOUNTS.get_entitlement(user) or {}
    try:
        url = billing.checkout_url(
            tier_name=tier.name, account_id=user,
            success_url=f"{base}/canvas?upgraded={tier.name}",
            cancel_url=f"{base}/canvas",
            stripe_customer_id=row.get("stripe_customer_id"))
    except Exception:
        logging.getLogger("dbsearch").exception("stripe checkout session failed")
        raise HTTPException(status_code=502, detail="could not reach the payment provider")
    return {"url": url}


@app.post("/billing/portal")
def billing_portal(request: Request, user: str = Depends(current_user)) -> dict:
    """The Stripe-hosted portal: change plan, update a card, cancel. Only a customer who
    already has a Stripe customer id has anything to manage."""
    if not billing.configured():
        raise HTTPException(status_code=501, detail="billing is not enabled on this deployment")
    row = ACCOUNTS.get_entitlement(user) or {}
    customer = row.get("stripe_customer_id")
    if not customer:
        raise HTTPException(status_code=404, detail="no subscription to manage")
    try:
        url = billing.portal_url(stripe_customer_id=customer,
                                 return_url=f"{str(request.base_url).rstrip('/')}/canvas")
    except Exception:
        logging.getLogger("dbsearch").exception("stripe portal session failed")
        raise HTTPException(status_code=502, detail="could not reach the payment provider")
    return {"url": url}


@app.get("/billing/status")
def billing_status(request: Request, user: str = Depends(current_user)) -> dict:
    """What this account may store, what it is using, and what it could buy.

    LAW 1: counts and plan names, never document titles. The numbers are the CALLER's own,
    which is the same rule the over-quota refusal follows.
    """
    tier = entitlements.effective_tier(ACCOUNTS, user)
    try:
        used = _edition.index.usage_bytes(_request_tenant(request), user)
    except Exception:
        # NotImplementedError included: a backend that cannot meter reports `metered: false`
        # and a null usage, never 0. Zero would draw an empty progress bar on an account that
        # might be full, which is the "unknown is not empty" rule this codebase already
        # follows for corpus_status.
        used = None
    quota = None if tier is None else tier.quota_bytes
    return {
        "tier": None if tier is None else tier.name,
        "quota_bytes": quota,
        "used_bytes": used,
        "metered": used is not None,
        "sellable": [{"name": t.name, "quota_gb": t.quota_gb, "price_cents": t.price_cents}
                     for t in tiers_mod.sellable_tiers()] if billing.configured() else [],
    }


def _human_bytes(n: int) -> str:
    """A size a person can act on. 'You have used 9.8 GB of 10 GB' is actionable;
    '10522669056 bytes' is not."""
    for unit, size in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            v = n / size
            return f"{v:.0f} {unit}" if v >= 100 else f"{v:.1f} {unit}"
    return f"{n} bytes"


def _enforce_disk_headroom(incoming: int) -> None:
    """#831: refuse an upload that would push the blob volume under its free-space floor,
    whatever the caller's tier says.

    The quota above protects revenue; this protects AVAILABILITY, and the two must never be
    confused: a per-account quota counts one account, so two accounts inside their quotas
    can still fill the disk, and on self-host there is no quota at all - infinite
    entitlement, very finite disk. If the disk fills, prod goes down for everybody rather
    than for the uploader.

    So: 507 Insufficient Storage, never 402 - this is an operator condition, and the
    message must not send an innocent user to the upgrade button. The floor is absolute
    bytes (DBSEARCH_DISK_FLOOR_BYTES, default 2 GiB), not a percentage, because 20% of a
    small disk is a real number and 20% of a big one is waste. The check counts the
    INCOMING bytes, so the upload that would itself cross the floor is the one refused.

    Fail-open mirrors the quota check, and here it is correct rather than convenient: only
    the filesystem store's writes land on the local disk, so a store that raises
    NotImplementedError from free_bytes() (in-memory does not persist; a cloud store
    writes remotely) has nothing this guard could protect. Scope honestly stated: this
    watches the BLOB volume only - the Postgres index lives on its own volume (pgdata)
    and is not covered here."""
    # #843: the floor, its env override and the arithmetic live in core.headroom, which the
    # ingest runner uses too - one definition of "is there room", two presentations of it.
    # This path owes the user a 507 with an explanation; a crawl owes its job an error it can
    # record. #839: a malformed floor logs and falls back there rather than raising here.
    low = headroom.shortfall(_edition.store, incoming)
    if low is None:
        return
    free, floor = low
    raise HTTPException(status_code=507, detail=(
        f"Server storage is nearly full: this file needs {_human_bytes(incoming)} and the "
        f"server must keep {_human_bytes(floor)} of headroom to stay healthy. The upload "
        "was refused to protect existing data. Please try again later, or contact the "
        "operator."))


def _enforce_storage_quota(request: Request, user: str, incoming: int,
                           replaces_uri: "str | None" = None,
                           replaces_doc_id: "str | None" = None) -> None:
    """#775 / ADR 0027: refuse an upload that would take this account past its tier.

    Three things this deliberately does NOT do.

    It does not enforce when the deployment cannot meter. A self-hosted box holds its own
    storage and is free forever (ADR 0027 rule 6), so a quota there would be enforcing a bill
    nobody sends. Same for a tier the ladder no longer has: `quota_bytes` returns None and
    says so loudly, because a broken config of ours must not refuse a paying customer.

    It does not 500. Every failure inside billing is caught and lets the upload through: a
    storage limit that takes the product down when the billing lookup hiccups is worse than
    no limit at all, and this is money, not LAW 2 - nothing here decides what anyone may READ.

    It does not name anybody else's numbers. The message carries only this caller's own usage,
    because a billing message that reported deployment-wide storage would be a metadata leak
    about colleagues wearing a friendly hat.
    """
    log = logging.getLogger("dbsearch")
    try:
        quota = entitlements.quota_bytes(ACCOUNTS, user)
        if quota is None:
            return                                   # cannot say -> do not enforce
        # #844: measure what the account will hold AFTER this write. A replacement upload
        # supersedes the version sharing its uri (#90), so counting the old bytes AND the new
        # ones refused uploads that in fact fit - and refused them with 402 "upgrade your
        # plan", which is the worst-flavoured false refusal this surface can produce. Older
        # index adapters that predate the parameters keep working via the TypeError fallback.
        try:
            used = _edition.index.usage_bytes(_request_tenant(request), user,
                                              exclude_uri=replaces_uri,
                                              exclude_doc_id=replaces_doc_id)
        except TypeError:
            used = _edition.index.usage_bytes(_request_tenant(request), user)
    except NotImplementedError:
        return                                       # self-host / unmetered backend
    except HTTPException:
        raise
    except Exception:
        log.exception("storage quota check failed; allowing the upload")
        return

    if used + incoming <= quota:
        return
    raise HTTPException(status_code=402, detail=(
        f"Storage full. This file needs {_human_bytes(incoming)} and you have "
        f"{_human_bytes(max(0, quota - used))} left of {_human_bytes(quota)}. "
        "Upgrade your plan for more room, or delete something you no longer need."))


@app.post("/admin/upload")
async def admin_upload(
    request: Request,
    file: UploadFile = File(...),
    acl: list[str] = Form(default=[]),
    title: str = Form(""),
    audience: str = Form(""),
    user: str = Depends(current_user),
) -> dict:
    # Pre-read guard: reject oversized requests before buffering the body.
    # Content-Length may be absent or spoofed, so the post-read check below
    # is kept as the authoritative backstop.
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="file too large (max 10MB)")
        except ValueError:
            pass  # malformed header — fall through to post-read check

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 10MB)")

    # #831 before #775: the operator condition outranks the billing one, so a full disk is
    # never misreported as "upgrade your plan".
    _enforce_disk_headroom(len(data))
    # #844: this upload supersedes any prior version sharing its uri (#90, owner-scoped),
    # so those bytes are about to be returned and must not be counted against the caller.
    _enforce_storage_quota(request, user, len(data),
                           replaces_uri=f"upload://{file.filename or 'document'}")

    # #539: an omitted ACL means PRIVATE TO THE UPLOADER, not a 400.
    #
    # This was measured on the live site, not reasoned about: a signed-in user uploaded an HR
    # policy, the form made them choose a group, the only groups on offer were the DEMO ones
    # (all-staff / deal-team) rather than principals they hold, and /ask then told them "no
    # documents you are permitted to see have been indexed yet". They could not read the file
    # they had just uploaded. Nothing was broken - the trim did its job on an ACL that was
    # nonsense, because the person had no way to supply a true one.
    #
    # Defaulting to the caller cannot widen access: it grants the bytes back to the one person
    # who already had them. The alternatives are worse in both directions - defaulting to a
    # GROUP over-shares to people the uploader never chose, and refusing (today's behaviour) is
    # the live bug. Sharing stays a separate, deliberate act (#538's grant operation).
    acl = [p for p in acl if p and p.strip()]      # an untouched form control posts ""
    if not acl:
        acl = [user]

    # #575 (second half): the third audience choice, "My organization" - one step to make an
    # HR policy readable by everyone in the uploader's tenant, instead of a private upload
    # followed by naming colleagues one at a time through the separate share flow (ADR 0017).
    # "Specific people" is deliberately NOT an upload-time value - it stays that separate,
    # deliberate act; this endpoint only ever grows two audiences, private or org.
    #
    # (Review Finding minor-e) An explicit `acl` posted alongside audience="org" is discarded
    # in favour of the tenant principal below, on purpose and not silently: this only ever
    # NARROWS what #539's own default would have produced for an unrelated acl value, never
    # widens it, so there is no security question here - only which of two ACLs a caller who
    # posted both actually meant. audience wins because it is the field this task added
    # specifically to express "everyone in my org," and honouring both would mean silently
    # unioning a caller-chosen acl with the tenant principal, which is a second, harder-to-
    # reason-about way to reach the same ACL this branch already sets deliberately.
    if audience == "org":
        sess = user_auth.read_session(request.cookies.get(user_auth.COOKIE, "")) or {}
        tid = sess.get("tid", "")
        if not tid:
            raise HTTPException(status_code=400, detail=(
                "this account has no organization - org-wide sharing needs a "
                "Microsoft organizational sign-in"))
        # (Review Finding 4) Some identity backends (the cloud EntraIdentity port, which has
        # no set_user_groups - expand_groups is Graph-only) can NEVER be made to expand
        # tenant:<tid> for anyone. Succeeding here would return 200 and tell the uploader
        # their document is org-readable while it structurally is not - under-visible is the
        # safe direction, but a dishonest success message is worse than an honest refusal.
        # Same predicate /auth/me's has_org uses, so the UI never offers what this refuses.
        ident = getattr(_edition, "identity", None)
        if not _identity_can_hold_tenant_principal(ident):
            raise HTTPException(status_code=400, detail=(
                "org-wide sharing is not available on this identity backend - "
                "share the document with named people instead"))
        # LAW 2: the tenant principal is minted from the session's VERIFIED tid, server-side,
        # never from a client-supplied value (no form field, header, or body can influence
        # it) - the ACL-overlap test in the identity port stays the one enforcement point.
        acl = sorted({user, f"tenant:{tid}"})

    filename = file.filename or "document"
    mime = _sniff_mime(filename, file.content_type)
    external_id = f"upload-{_slug(filename)}-{hashlib.sha256(data).hexdigest()[:8]}"
    doc_title = title.strip() or filename

    # #917: the honest refusal STAYS at click time (#551's rule) - the mime allowlist is
    # knowable without parsing, so an unsupported type is a synchronous 415 exactly as
    # before. What moved to the job is the WORK: parse/chunk/embed/index used to run
    # inside this request (the LAW 4 "do it all in one request" path), which is also why
    # the picker could never show the stage progress the SharePoint flow has. The submit
    # returns 202 + a job handle; ParseProducedNoText now surfaces as a FAILED job
    # carrying the error class (the async home of the old 422), which the picker renders
    # as loudly as the SharePoint failure panel.
    from dbsearch.adapters.local.extract import segment_for
    if segment_for(mime) is None:
        raise HTTPException(status_code=415, detail=f"unsupported file type: {mime}")

    job = _edition.submit_file_ingest(
        external_id, doc_title, data, mime, acl, uri=f"upload://{filename}",
        tenant_id=_request_tenant(request), owner_oid=user,   # ADR 0012
    )
    return JSONResponse(status_code=202, content={
        "external_id": external_id, "title": doc_title, "acl": acl,
        "job_id": job.job_id, "job_status": job.status,
        "poll": f"/ingest/jobs/{job.job_id}"})


# ---- Removing a document you put in (#594) ----------------------------------------------
@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str, request: Request, user: str = Depends(current_user)) -> dict:
    """Delete a document you OWN, and everything that belonged to it.

    "Your data" could add and share but never remove, and with the retention sweep
    deliberately disabled (#576) nothing in the product removed an upload at all.

    OWNERSHIP, not readability. `_may_share` below deliberately grants its operation to
    anyone whose principals intersect the ACL; copying that rule here would let one colleague
    on an org-wide HR policy destroy it for everyone. The test is ADR 0012's `owner_oid`, read
    back through `docs_owned_by` - the same seam the retention sweep scopes itself with.

    404, never 403 (the #549 rule): a 403 confirms the document exists to somebody who cannot
    see it, which is the metadata leak #549 closed, reopened through a new door. A document
    that exists but is not yours and one that never existed answer identically.

    ORDER: the index row goes FIRST and a failure there refuses the whole call. If blobs went
    first and the index delete then failed, the document would still be listed and searchable
    with its bytes gone - a live document that cannot be read. The other way round, a blob that
    outlives its index row is unreachable residue: nothing lists it, retrieval cannot reach it,
    and /admin/documents/{id}/download 404s on the missing row. Residue is reported, not
    hidden, so an operator can see it happened.
    """
    # Layer 2 behind ownership, not instead of it. `doc_id` is interpolated straight into
    # `raw/{partition}/{doc_id}` by _blob_prefixes, which is the exact shape that made the
    # #576 sweep delete the blob root, and external_id is still unvalidated at other entry
    # points (#581). Same predicate /ingest uses, and the same refusal shape as an unknown
    # document so it cannot be used to probe.
    if not is_safe_external_id(doc_id):
        raise HTTPException(status_code=404, detail="no such document")

    partition = _request_tenant(request)
    try:
        owned = set(_edition.index.docs_owned_by(partition, user))
    except NotImplementedError:
        raise HTTPException(status_code=501,
                            detail="this deployment cannot tell who owns a document, so it "
                                   "will not delete one")
    if doc_id not in owned:
        raise HTTPException(status_code=404, detail="no such document")

    try:
        _edition.index.delete(partition, doc_id)
    except Exception:
        logging.getLogger("dbsearch").error("delete: index delete failed for one document")
        raise HTTPException(status_code=500, detail="could not delete the document")

    # Imported here, like every other retention use in this file: the module imports back
    # into the server package, so a top-level import is a cycle.
    from dbsearch.server import retention

    blob_residue = 0
    for prefix in retention.blob_prefixes(partition, doc_id):
        try:
            _edition.store.delete_prefix(prefix)
        except NotImplementedError:
            blob_residue += 1
        except Exception:
            blob_residue += 1
            logging.getLogger("dbsearch").error("delete: blob delete failed for one prefix")

    grants_dropped = _edition.grant_registry.drop_for_documents({doc_id})
    return {"deleted": doc_id, "grants_dropped": grants_dropped,
            "blob_prefixes_left": blob_residue}


# ---- Sharing a document with a named person (ADR 0017, #538) ----------------------------
def _shareable_docs(doc_ids, user: str, request: Request) -> dict:
    """Of `doc_ids`, the ones this caller may SHARE, as {doc_external_id: acl row}.

    THE RULE (ADR 0017 s2), and it is the only implementation of it: "may share" = the
    caller's DIRECT principals intersect the acl. Direct means their own oid and real
    groups, EXCLUDING grant principals - so a share cannot be re-shared and the audience
    stays the bounded set the owner chose.

    #582: deliberately the caller's OWN partition, with NO doorway. A doorway IS the grant
    machinery, so letting one in here would make a shared document re-shareable, which is
    exactly what s2 forbids.

    Fix round 1, CRITICAL-1: this used to be the body of `_may_share`, reachable only one
    document at a time and only from the document surface. `share_conversation` mints a
    grant per document a thread CITED, and a thread cites documents the sharer may be
    reading through somebody else's grant - so with no s2 test on that path, a conversation
    share re-shared them. Reproduced end to end: carol grants bob a document, bob is
    correctly refused when re-sharing the DOCUMENT and succeeds when sharing the
    CONVERSATION, alice retrieves carol's document, alice re-shares onward to dave, and
    carol can see all three grants on her own document and revoke none of them (`revoke_grant`
    requires `granted_by == requester`). The partition check is NOT a backstop for this: it
    only bites across `acct:` partitions, and a normal single-org Entra deployment resolves
    every home-tenant user to the SAME partition. So the rule lives here, in a set-returning
    form both share surfaces call, precisely so the two cannot drift.
    """
    wanted = set(doc_ids)
    if not wanted:
        return {}
    tenant = _request_tenant(request)
    direct = set(getattr(_edition.identity, "direct_principals", _edition.identity.expand_groups)(user))
    return {doc.doc_external_id: doc
            for doc in _edition.index.list_doc_acls(as_read_scope(tenant))
            if doc.doc_external_id in wanted and direct & set(doc.allowed_principals or [])}


def _may_share(doc_id: str, user: str, request: Request):
    """The document, if this caller may share it. Otherwise 404 - never 403.

    A 403 would confirm the document exists to somebody who cannot see it, which is the
    metadata leak #549 just closed, re-opened through a new door.

    The "may share" rule itself is `_shareable_docs` above - one implementation, two share
    surfaces (document and conversation)."""
    doc = _shareable_docs({doc_id}, user, request).get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="no such document")
    return doc


_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# The user-facing half of the refusal below. One sentence the sharer can act on, with no
# card number or partition value in it.
#
# #582 rewrote this. It used to say "sharing works today between people signed in to the
# same Microsoft organization", which was true when the ONLY working case was
# home-tenant-to-home-tenant. Cross-account sharing now works, so the only thing left that
# a share cannot cross is another organization's tenant - and the advice has to say that
# instead, or it sends people to do the one thing that will not help.
_CROSS_PARTITION_ADVICE = ("a document can only be shared with people on this deployment - "
                           "ask them to sign up here, or send them the file directly")

# Fix round 1, MINOR-4: the same refusal now serves the conversation surface, where the
# document copy above read as a bug ("this document belongs to another organization" about a
# thread). ONE code path, parameterized by noun - two copies of this reasoning is how the
# two surfaces start telling a user different things about the same rule. The tail differs
# because the fallback differs: you can email somebody a file, you cannot email them a
# conversation.
_CROSS_PARTITION_ADVICE_CONV = ("a conversation can only be shared with people on this "
                                "deployment - ask them to sign up here")


def _refuse_cross_partition_share(partition: str, grantee: str,
                                  noun: str = "document",
                                  advice: "str | None" = None) -> None:
    """Refuse a share ADR 0019 D1 does not allow - now from RECORDED FACTS.

    THE SEAM this guards is unchanged and still worth stating: a grant adds `grant:<id>` to
    the document's ACL inside the GRANTOR's partition, the grantee retrieves through THEIR
    OWN, and the partition filter runs BEFORE the ACL-overlap test. What changed is that
    #582 gave the grantee's partition a doorway (ADR 0019 D3), so most of what this used to
    refuse now WORKS and must not be refused any more.

    WHAT THIS USED TO BE, and why it is gone: with no tid on record (ADR 0013 stored
    `(idp, subject)` rows and nothing else) the grantee's partition was uncomputable, so
    this guessed from the SHAPE of an identifier - `acct:` grantor refuses everyone, non-GUID
    grantee refuses - and deliberately let ONE case through silently: grantor in a tenant
    partition, grantee GUID-shaped but living in a FOREIGN tenant. Task 4 records the tid,
    so that case is now decidable and the whole heuristic is replaced by one rule.

    THE RULE (ADR 0019 D1): a grant may cross ACCOUNT boundaries on this deployment
    (acct: <-> acct:, acct: <-> home) and may NEVER cross a foreign Entra tenant boundary,
    in either direction. Everything else is allowed, because the doorway now makes it work.

    Fail-closed on UNKNOWABLE: an account whose Entra identity predates #582 has no
    recorded tid, and `account_partitions` returns None rather than guess. Guessing would
    mean handing a foreign-tenant user an `acct:` partition they do not have - the original
    silent failure, rebuilt inside its own fix. One sign-in fixes it, and the message says so.
    """
    advice = advice or _CROSS_PARTITION_ADVICE
    if not real_login_enabled():
        # A rig with no real login has exactly ONE partition: `resolve_tenant` returns the
        # deployment constant for every identity, so no two callers can be in different
        # partitions and there is nothing here to refuse. Checking this first is what keeps
        # self-host and every dev/test rig working.
        return
    if _is_foreign_partition(partition):
        # Out of a foreign tenant. The document belongs to another organization and its
        # partition is not ours to open (ADR 0019 D1, and CLAUDE.md's locked "never let
        # customer document content leave the customer tenant").
        raise HTTPException(status_code=400, detail=(
            f"this {noun} belongs to another organization's workspace, so it cannot be "
            f"shared from here - {advice}"))
    try:
        identities = ACCOUNTS.identity_tenants(grantee)
    except AccountStoreUnavailable:
        # We could not CHECK. That is not the sharer's fault and must not read as one -
        # a 400 here would tell them their colleague's address is wrong.
        raise HTTPException(status_code=503,
                            detail="cannot verify who that is right now, try again shortly")
    parts = account_partitions(identities, grantee, _edition.tenant_id)
    if parts is None:
        # An Entra identity with no recorded tid: UNKNOWABLE, so refuse rather than guess.
        # (An account with NO identity rows at all is a different case and is allowed here -
        # nothing is known to be wrong with it, and `_request_scope` enforces D1 at read
        # time when that person's real partition finally exists. Refusing it instead would
        # break sharing with a colleague who simply has not signed in yet.)
        raise HTTPException(status_code=400, detail=(
            "ask them to sign in once so their workspace is known, then share again"))
    if any(_is_foreign_partition(p) for p in parts):
        # Into a foreign tenant.
        raise HTTPException(status_code=400, detail=(
            "that person signs in with another organization's Microsoft account, so this "
            f"{noun} cannot reach them - {advice}"))


@app.post("/documents/{doc_id}/grants")
def grant_document(doc_id: str, body: dict, request: Request,
                   user: str = Depends(current_user)) -> dict:
    """Share this document with one named person, who signs in as themselves.

    #582 / ADR 0019 D5: `grantee_email` is the product surface - nobody knows a colleague's
    `acct_<hex>`. It is a LOOKUP KEY only; what gets stored on the grant is still the
    account id, so email never becomes an authorization value. `grantee_oid` stays accepted
    for existing API callers."""
    _may_share(doc_id, user, request)
    grantee = str(body.get("grantee_oid", "")).strip()
    email = str(body.get("grantee_email", "")).strip()
    if email:
        try:
            resolved = ACCOUNTS.account_for_email(email)
        except AccountStoreUnavailable:
            raise HTTPException(status_code=503,
                                detail="cannot verify who that is right now, try again shortly")
        if not resolved:
            # Honest, and actionable. The cost is stated in ADR 0019 D5 rather than hidden:
            # this tells an authenticated user sharing their own document whether an address
            # has an account here. Accepted - a pending-invite scheme that avoided the
            # oracle would store an authorization decision against an unverified address,
            # which is a worse trade and a separate feature.
            raise HTTPException(status_code=400, detail=(
                "nobody has signed in with that address yet - ask them to sign up here, "
                "then share again"))
        grantee = resolved
    # Refused BEFORE the grant record is created, so a refused share leaves no phantom row
    # behind to be listed by GET /documents/{id}/grants (#575 Fix 2 - keep this ordering).
    _refuse_cross_partition_share(_request_tenant(request), grantee)
    days = body.get("expires_in_days")
    try:
        grant = _edition.grant_registry.create(
            doc_external_id=doc_id, tenant_id=_request_tenant(request),
            grantee_oid=grantee, granted_by=user,
            expires_in_days=int(days) if days else None)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # The ACL gains the grant principal ONCE. It is never removed on revoke: the principal
    # simply stops being expanded, so a dangling grant:<id> matches nobody (ADR 0017 s1).
    touched = _edition.index.add_doc_principals(
        _request_tenant(request), doc_id, [grant.principal])
    if not touched:
        # #575 review, Finding A: `discard_unconfirmed`, not `revoke` - the share never took
        # effect (nothing was shared, so there is no live authorization decision to fail
        # closed on), and this call runs inside an already-failed request. `revoke` fails
        # closed on a store outage by design (grants.py); calling it here would let a
        # transient store outage turn this honest 404 into a 500 and mask it, while leaving
        # the dead grant behind in memory besides. discard_unconfirmed always clears the
        # in-process state and never raises.
        _edition.grant_registry.discard_unconfirmed(grant.grant_id)
        raise HTTPException(status_code=404, detail="no such document")
    return grant.to_dict()


@app.get("/documents/{doc_id}/grants")
def list_document_grants(doc_id: str, request: Request,
                         user: str = Depends(current_user)) -> list[dict]:
    _may_share(doc_id, user, request)
    return [g.to_dict() for g in _edition.grant_registry.list_for_document(doc_id)]


@app.delete("/grants/{grant_id}")
def revoke_grant(grant_id: str, user: str = Depends(current_user)) -> dict:
    """Only the person who made the grant may take it back. Revocation is immediate: the
    grantee's next request expands without this principal."""
    try:
        _edition.grant_registry.revoke(grant_id, user)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such grant")
    return {"revoked": grant_id}


# --- #600: sharing a CONVERSATION ---------------------------------------------------------
#
# The five routes below are the product surface over four mechanisms that already exist and
# are deliberately NOT re-implemented here:
#
#   durable conversations   `conversation_service.history(user, conv_id)` (#596)
#   citations per turn      `Turn.cited_docs` (#600 task 2)
#   conv-scoped grants      `Grant.conv_id` + `_request_scope(..., active_conv_id=)` (task 3)
#   the transcript record   `_edition.conversation_shares` (task 4)
#
# WHAT THESE ROUTES AUTHORIZE, and what they deliberately do not: they decide who may SHARE a
# conversation, who may REVOKE that share, and who may READ a transcript. They add no second
# authorization check for RETRIEVAL - the doorway pair is routing and the ACL overlap stays
# the single enforcement point (LAW 2, `_request_scope`'s docstring). A recipient's next
# question is answered by exactly the same trim core as everybody else's; all a share does is
# make her own live grants contribute a pair while that one conversation is active.


# Fix round 1, MINOR-5: this used to read "every document this conversation cited has been
# deleted", stated as fact. Once ADR 0017 s2 is enforced on this surface that branch has TWO
# causes and the route cannot tell the caller which without saying something about a document
# she may not be allowed to know exists. So the copy states the disjunction honestly, and is
# true whichever cause fired.
_NOTHING_TO_SHARE = ("nothing in this conversation can be shared - every turn draws on "
                     "documents that are not yours to pass on, or on documents that have "
                     "since been deleted")


def _undo_share(share_id: str, minted_grant_ids) -> None:
    """Undo a `share_conversation` that is about to fail: every grant it minted, then the
    share row. #601 round 5.

    ONE implementation because the duplication was not cosmetic - it produced two Criticals in
    this task. Round 1's IMPORTANT-3 was a mint loop with no rollback at all; round 3 then
    added a SECOND refusal branch beside the fixed one and did not copy its cleanup, which is
    round 4's NEW-1. Both times the failure mode was identical: a new exit written next to a
    correct one, one statement short. There are exactly two exits that must undo the whole
    request, and they now cannot diverge because there is nothing left to copy.

    Order is load-bearing and is the order both callers already used: GRANTS FIRST, share row
    second. A share row without its grants opens a transcript backed by nothing; grants
    without their share row are access with no surface to revoke it from, which is strictly
    worse - so if only one of the two lands, it must be the grants that went.

    `discard_unconfirmed` throughout, never `revoke` (#575 Finding A). Neither of these
    records ever became a live authorization decision - the request that would have published
    them is failing - so there is nothing here for a store outage to put at risk, and
    `discard_unconfirmed` never raises, which is what keeps a cleanup from burying the error
    it is cleaning up after.

    Deliberately NOT a `finally`: the success path must not run this, and the per-document
    discard inside the loop (a cited document that vanished before `add_doc_principals`) is a
    different operation with a different meaning - that one drops ONE grant and lets the
    request continue."""
    for grant_id in minted_grant_ids:
        _edition.grant_registry.discard_unconfirmed(grant_id)
    _edition.conversation_shares.discard_unconfirmed(share_id)


def _conv_id(raw: str) -> str:
    """Normalize a conversation id at the ROUTE boundary, once.

    #600 review carry-forward: `ConversationShare.create` and `Grant.create` strip (and
    refuse/normalize blank), while `_request_scope` and `GrantRegistry.drop_for_conversation`
    compare `conv_id` RAW. So a padded id arriving in a path segment ("%20c1") would be
    stored stripped and then never match anything a later comparison does - a share that
    silently opens nothing, with nothing anywhere raising. Stripping here means every value
    these routes hand to any of those four places is already in the one canonical form."""
    return (raw or "").strip()


def _owned_thread_citations(conv_id: str, user: str) -> "tuple[list, list[str]]":
    """The prologue BOTH conversation-scope routes need: this caller's own turns under this
    conv_id, and every document they cited, in first-citation order and deduplicated.

    ONE implementation because the two routes must answer the same question about the same
    thread. `GET .../shareable` exists to tell the owner, before a share exists, exactly what
    the share POST is about to expose; if the two collected citations differently - a
    different order, a different dedup, a different reading of an empty history - the modal
    would show a set the mint then did not use, and the owner's decision to uncheck a document
    would be taken against a list that was never the real one.

    THE 404 IS THE WHOLE OWNERSHIP CHECK, for both routes, and it is 404 rather than 403 on
    purpose (the #549 rule): history is keyed by (conv_id, user_oid), so somebody else's
    thread is simply not there to read, and a thread that is not yours answers exactly as one
    that never existed. A 403 would confirm the thread exists to somebody who may not see it.

    `history()` is left UNGUARDED against a store outage (#596) and becomes a 503: a caller
    deciding what a conversation CONTAINS must never have an outage read back as an empty
    conversation - on the POST that would mint a share of nothing and report success, and on
    the GET it would render a modal saying this thread cites no documents, which is a lie the
    owner would then act on."""
    try:
        turns = _edition.conversation_service.history(user, conv_id)
    except ConversationStoreUnavailable:
        raise HTTPException(status_code=503,
                            detail="cannot read the conversation right now, try again shortly")
    if not turns:
        raise HTTPException(status_code=404, detail="no such conversation")
    all_cited, seen = [], set()
    for t in turns:
        for d in t.cited_docs:
            if d not in seen:
                seen.add(d)
                all_cited.append(d)
    return turns, all_cited


_THREAD_NAME_CHARS = 80
# How wide a thread's name may be wherever one is rendered. ONE number and ONE cut, because
# there are now two lists that name a conversation by its opening question - the owner's own
# conversations (#602) and the shares she has granted (#607) - and two widths would be two
# different strings for the same thread on two screens of the same product.


def _thread_name(question: str) -> str:
    """A conversation's human name: its opening question, truncated, with the cut made VISIBLE.

    The ellipsis is not decoration. The label is the owner's own sentence and she is about to
    act on it (reopen this thread, revoke that share); a silent cut reads as a question she
    does not remember asking. `rstrip` keeps the cut off a trailing space, so the ellipsis
    attaches to a word rather than floating."""
    name = question or ""
    if len(name) > _THREAD_NAME_CHARS:
        name = name[:_THREAD_NAME_CHARS - 1].rstrip() + "…"
    return name


@app.get("/conversations/mine")
def my_conversations(user: str = Depends(current_user)) -> dict:
    """#602: THE OWNER'S DOOR BACK TO HER OWN THREADS. Newest first.

    Conversations became durable in #596 and shareable in #600, and then the Ask surface minted
    a fresh conv_id on every page load - so after a reload the owner could not reopen her own
    thread, could not see its shares, and could not revoke one. The recipient of a share had a
    door (`/conversations/shared-with-me`); the person whose data it was did not. During the
    260808 acceptance run the tester had to call the DELETE route by hand to finish the script.
    This is the missing half of that pair, and it is deliberately its MIRROR: the same shape of
    listing, feeding the same transcript route, so the owner and the grantee reopen a thread
    through one read path rather than two.

    LAW 1, and the one place on this surface where returning content is right: `first_question`
    is the CALLER'S OWN question, returned to the caller alone. Nothing else in the row is
    content - `turns` and `last_asked_at` are counts and times, and the ANSWER text is never in
    this response because `conversations_for` does not select it. The question is what makes a
    row nameable at all; without it the owner is picking between opaque uuids.

    SCOPED BY EQUALITY ON user_oid, INSIDE THE STORE. Both stores match `user_oid` exactly and
    their docstrings carry the argument; the two failures a looser match would cause are worth
    naming here as well, because this route is where they would be SEEN. A link share stores
    every anonymous visitor's turns under `link:<share_id>:<visitor_id>` in the owner's own
    conv_id (ADR 0021), so a prefix or conv-id-only match would show the owner a stranger's
    typed question as the name of her own thread - and account ids are not prefix-free, so it
    would also hand one account another's threads.

    NO `active_conv_id` AND NO GRANT EXPANSION: this reads the conversation store directly and
    touches no document, so there is nothing here for a conv-scoped principal to widen.

    A THREAD SOMEBODY SHARED WITH THIS CALLER *CAN* APPEAR HERE, and `own` is how the row says
    so. This paragraph used to claim the opposite ("not in this list either, and should not
    be"), and that claim was FALSE - fix round 1. The moment a grantee asks a follow-up inside a
    received thread her turn keys `(conv_id, HER oid)`, which is exactly what makes the
    conv-scoped doorway work at all (`_request_scope`, ADR 0020), so the store legitimately has
    a row for her under that conversation. Nothing of the grantor's is in it: the store matched
    her oid, so `first_question` is HER question and `turns` counts HER turns - the grantor's
    half of the thread is not reachable from this response at all. There was never a disclosure
    here; there was a docstring telling the next reader not to look.

    WHAT THE FALSE CLAIM WAS HIDING is why `own` now exists as a field rather than being left
    implicit. The client sets `sharedConv` from it, and `sharedConv` is what arms #600's
    revoke detection on the next question asked inside an open thread. Without the flag,
    reopening a RECEIVED thread from the owner's own list armed nothing, so a grantee whose
    share had since been revoked was told "This conversation is no longer here" - owner-data
    language, aimed at someone whose SHARE ended. It told her she had lost her own data when
    somebody else had ended a share. `own` is computed with `live_share_for`, the SAME
    resolution `conversation_transcript` uses for its own top-level `own`, so the two answers
    about one thread cannot disagree.

    A STORE OUTAGE IS A 503, never an empty list. #596's rule, and this surface is exactly
    where it bites: an owner told she has no conversations concludes her data is gone, which is
    the fear this whole card exists to answer."""
    store = _edition.conversation_service._store
    try:
        rows = store.conversations_for(user)
    except ConversationStoreUnavailable:
        raise HTTPException(status_code=503,
                            detail="cannot read your conversations right now, try again shortly")
    out = []
    for r in rows:
        # `live_share_for` applies expiry per read, so a lapsed share answers None here and the
        # row correctly becomes hers again - the same moment it stops being listed by
        # /conversations/shared-with-me. One resolution, so the two lists cannot disagree.
        share = _edition.conversation_shares.live_share_for(r["conv_id"], user)
        out.append({"conv_id": r["conv_id"],
                    "first_question": _thread_name(r["first_question"]),
                    "turns": r["turns"],
                    "last_asked_at": r["last_asked_at"],
                    "own": share is None,
                    # Who to name when the transcript renders the grantor's half of a received
                    # thread - without it the surface would label his turns with HER name, which
                    # is the one mistake `transcriptTurn`'s `grantorLabel` exists to prevent. No
                    # new disclosure: she holds a LIVE share from him, and
                    # /conversations/shared-with-me already returns this same oid to this same
                    # caller. `None` on her own threads, so there is nothing to name there.
                    "grantor_oid": share.grantor_oid if share else None})
    return {"conversations": out}


@app.get("/conversations/shared-with-me")
def conversations_shared_with_me(user: str = Depends(current_user)) -> dict:
    """The threads other people have opened to this caller, live ones only (expiry is
    applied per read by `list_shared_with`, which is why no sweeper exists). LAW 1: ids and
    timestamps, never a word of what was said inside any of them."""
    return {"shares": [s.to_dict() for s in _edition.conversation_shares.list_shared_with(user)]}


@app.delete("/conversations/shares/{share_id}")
def revoke_conversation_share(share_id: str, user: str = Depends(current_user)) -> dict:
    """End one conversation share. Only the person who made it may do that.

    THE CALLER CHECK IS THIS ROUTE'S JOB, and it is `find(share_id, user)`.
    `GrantRegistry.drop_for_conversation` takes a `conv_id` and a `grantee_oid` and
    deliberately does NOT check who is asking - its docstring names authorizing the caller as
    a REQUIREMENT ON WHOEVER WIRES THE FIRST ROUTE, which is this one. So the two values it
    receives are never taken from the request: `find` returns the share only if this caller is
    its `grantor_oid` (one KeyError for both "no such share" and "not yours", so the 404 is not
    an oracle), and `conv_id`/`grantee_oid` are then read off that owned RECORD. A route that
    forwarded a client-supplied conv_id here would let anybody un-share anybody's conversation.

    ORDER, and it is load-bearing: the document grants are dropped FIRST and the transcript row
    second, both fail-closed. If the store is down we fail with the grantee still able to ask
    (and the caller told so, honestly, by a 503) rather than reporting "revoked" while the
    grants stay live. The reverse order would leave the safe-looking failure that is actually
    the dangerous one. Between the two calls the grantee can read old answers but ask nothing
    new, which is the direction this design is willing to fail in."""
    try:
        share = _edition.conversation_shares.find(share_id, user)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such share")
    try:
        # #601 round 4, NEW-2: scoped to the grants THIS share's grantor made. Round 3 fixed
        # the identical bug on the share route and left this one, on the reasoning that
        # having resolved an owned share row made the unscoped call safe. It does not:
        # `conv_id` is client-chosen, so somebody else's thread can carry the same id, and
        # revoking your own share then destroyed their grants to the same person while their
        # share row stayed live and listed - their recipient keeping a share that opens
        # nothing, and neither party told. `share.grantor_oid` is `user`, checked by `find`
        # above; naming it here says WHOSE grants are being ended rather than leaving it to
        # a conv_id that is not an identity.
        dropped = _edition.grant_registry.drop_for_conversation(
            share.conv_id, share.grantee_oid, granted_by=share.grantor_oid)
        _edition.conversation_shares.revoke(share_id, user)
    except (GrantStoreUnavailable, ConversationShareStoreUnavailable):
        raise HTTPException(status_code=503,
                            detail="cannot revoke right now - the share is still active, "
                                   "try again")
    return {"revoked": share_id, "grants_dropped": dropped}


_LINK_DEFAULT_EXPIRY_DAYS = 7
# ADR 0021 invariant 3: EVERY link is bounded in time, default 7 days, owner-settable at share
# time. The default belongs HERE, in the route, and not in `create_link`: `create_link` refuses
# 0 and negatives (a link that never expires cannot be minted) but deliberately accepts None,
# meaning "the caller set no expiry", precisely so this default has somewhere to arrive from.
# A link minted with no expiry at all is the one shape invariant 3 forbids, so a request that
# names no expiry gets seven days, never None. The PEOPLE path is untouched by this: ADR 0020
# never bounded a named share in time, no expiry stays no expiry there, and
# tests/selftest_600_conversation_shares.py pins that.

# The exclusion emptied the share. Distinct from `_NOTHING_TO_SHARE`, which is about documents
# the SERVER refused (not yours to pass on, or since deleted) and would read as a system
# failure to somebody who has just unchecked every box herself.
_EXCLUDED_EVERYTHING = ("nothing is left to share - every document this conversation can pass "
                        "on was unchecked")


def _exclusions(body: dict) -> "set[str]":
    """The documents the owner unchecked in the share modal (#610).

    `exclude_docs` CAN ONLY NARROW, and that is a security property rather than a convenience:
    it is subtracted from the SERVER-COMPUTED shareable set, so a value in it can remove a
    document from the share and can never add one. Nothing here is ever consulted to decide
    that something MAY be shared.

    UNKNOWN IDS SUBTRACT NOTHING AND ARE NOT AN ERROR. This is the reason this is a set
    difference and not a lookup: an id that is not in the thread, not in the corpus, or not
    this caller's must behave identically to one that simply was not cited - namely, it does
    nothing at all. If an unknown id errored, the parameter would become a PROBE: a caller
    could pass a document id and learn, from which status came back, whether it exists inside
    somebody's thread. The thread is the caller's own here, but the same route is the one a
    later surface will reuse, and an oracle built into a request parameter is not a thing to
    leave standing on the reasoning that today's only caller happens to be the owner.

    The TYPE is refused, though, because that is a client bug and not a probe: a bare string
    would iterate as characters and silently exclude nothing, which is a share wider than the
    caller asked for, arrived at silently."""
    raw = body.get("exclude_docs") or []
    if not isinstance(raw, (list, tuple, set)):
        raise HTTPException(status_code=400,
                            detail="exclude_docs must be a list of document ids")
    return {str(d).strip() for d in raw if str(d).strip()}


def _store_exclusions(body: dict) -> "set[str]":
    """The SOURCES the owner unchecked in the same modal (#851).

    Every word of `_exclusions` above applies here unchanged - narrow-only, unknown ids
    subtract nothing rather than erroring (an id that errored would be a probe for which
    stores exist inside somebody's thread, which is gate #1 leaking through a request
    parameter), and a bare string is refused because it would iterate as characters and
    silently share MORE than the caller asked for.

    It is a second parameter rather than one merged list because the two name different
    kinds of thing and are checked against different server-computed sets. Merging them would
    mean an id that is a document to one check and a store to the other, and the failure would
    be silent in the widening direction."""
    raw = body.get("exclude_stores") or []
    if not isinstance(raw, (list, tuple, set)):
        raise HTTPException(status_code=400,
                            detail="exclude_stores must be a list of store ids")
    return {str(d).strip() for d in raw if str(d).strip()}


@app.post("/conversations/{conv_id}/shares")
def share_conversation(conv_id: str, body: dict, request: Request,
                       user: str = Depends(current_user)) -> dict:
    """#600 / GOAL_ACCEPTANCE step 4: share this conversation with one named person.

    The share is TWO records: one `ConversationShare` (which opens the transcript) plus one
    conv-scoped grant per document the thread has CITED so far. Grantee resolution, the
    cross-partition refusal and the 404-not-403 stance are `grant_document`'s, verbatim -
    the two share surfaces must never drift apart in what they tell a user.

    SNAPSHOT SEMANTICS, and they are the owner's decision, not a limitation to be quietly
    improved: the share grants the documents cited AT THIS MOMENT. A document the thread
    cites afterwards is NOT included, and nothing here refreshes - sharing a conversation is
    a deliberate act over a transcript the sharer has just read, and a share that silently
    widened itself as the thread grew would grant documents the sharer never saw cited.
    A fresh share is how new citations get included.

    `history()` is left UNGUARDED against a store outage on purpose (#596): this caller is
    deciding what a conversation CONTAINS, and a store outage read as an empty conversation
    would mint a share of nothing and report success. It becomes a 503, never a 404.

    #606 / ADR 0021: the SAME route now serves TWO AUDIENCES. `audience="people"` is
    everything above, unchanged and still the default, so a body that never heard of this
    field keeps meaning exactly what it meant before. `audience="link"` mints a share for
    anyone holding an unguessable token, who signs in to nothing: there is no grantee to
    resolve, no email lookup, and NO cross-partition refusal - that rule is about which
    partition a GRANTEE reads from (ADR 0019 D1) and a link visitor has no partition at all
    (ADR 0021 gives them a synthetic one that matches nothing).

    `audience` IS NOT AN AUTHORIZATION FACT and nothing here treats it as one. It is a
    client-supplied string that SELECTS A CODE PATH; what actually gates a link visitor is the
    token hash on the row and the row's liveness, neither of which this value can influence.
    An unrecognised value is refused rather than defaulted, because the two paths hand over
    different things and a typo should not silently pick one of them.

    #610: `exclude_docs` lets the owner NARROW the share before it exists - see `_exclusions`
    for why it can only ever subtract. What it subtracts from is the shareable set, BEFORE the
    prefix cut, so an excluded document is treated exactly like an unshareable one: the shared
    transcript stops at the first turn that cited it. Filtering only the grants would close
    the grant channel and leave the CONTENT channel open beside it - the recipient would still
    read the answer text synthesized out of the excluded document - which is the identical
    shape as this branch's earlier `turns_withheld` defect and #601's post-share boundary."""
    conv_id = _conv_id(conv_id)
    turns, all_cited = _owned_thread_citations(conv_id, user)
    audience = str(body.get("audience") or AUDIENCE_PEOPLE).strip()
    if audience not in (AUDIENCE_PEOPLE, AUDIENCE_LINK):
        raise HTTPException(status_code=400,
                            detail="audience must be 'people' or 'link'")
    is_link = audience == AUDIENCE_LINK
    grantee = ""
    if not is_link:
        grantee = str(body.get("grantee_oid", "")).strip()
        email = str(body.get("grantee_email", "")).strip()
        if email:
            # #582 / ADR 0019 D5, copied from `grant_document`: `grantee_email` is the product
            # surface - nobody knows a colleague's `acct_<hex>`. It is a LOOKUP KEY only; what
            # gets stored is the account id, so email never becomes an authorization value.
            try:
                resolved = ACCOUNTS.account_for_email(email)
            except AccountStoreUnavailable:
                raise HTTPException(
                    status_code=503,
                    detail="cannot verify who that is right now, try again shortly")
            if not resolved:
                # Same honest, actionable copy as `grant_document`, word for word. The oracle
                # it accepts (an authenticated user learns whether an address has an account
                # here) is stated in ADR 0019 D5 rather than hidden, and the two share surfaces
                # must not answer the same question two different ways.
                raise HTTPException(status_code=400, detail=(
                    "nobody has signed in with that address yet - ask them to sign up here, "
                    "then share again"))
            grantee = resolved
        # Refused BEFORE any record is created, so a refused share leaves no phantom row behind
        # (#575 Fix 2 - keep this ordering).
        #
        # NOT run for a link (#606): this refusal compares the grantor's partition with the
        # GRANTEE's, and a link share has no grantee - the sentinel `link:<share_id>` is a name
        # for a principal, not a person, and `identity_tenants` on it would be a lookup of
        # nobody. The rule it enforces is not thereby skipped: a link visitor's read scope
        # carries the synthetic `linkvisitor:<share_id>` partition, which equals no real
        # partition anywhere, so every document a visitor reaches goes through the doorway and
        # the ACL rather than a partition-equality short-circuit (ADR 0021, "Mechanism").
        _refuse_cross_partition_share(_request_tenant(request), grantee, noun="conversation",
                                      advice=_CROSS_PARTITION_ADVICE_CONV)
    raw_days = body.get("expires_in_days")
    if raw_days is None or raw_days == "":
        # ADR 0021 invariant 3 - see `_LINK_DEFAULT_EXPIRY_DAYS`. A link with no expiry named
        # gets the default; a people share with none stays unbounded, as it always was.
        expires_in_days = _LINK_DEFAULT_EXPIRY_DAYS if is_link else None
    else:
        try:
            expires_in_days = int(raw_days)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400,
                                detail="expires_in_days must be a number of days")
        if not is_link and not expires_in_days:
            # The people path's pre-#606 reading of a falsy value, kept verbatim: 0 means "no
            # expiry". It is NOT carried over to links - `create_link` refuses 0 outright,
            # because there the same value would mint the one thing invariant 3 forbids, and
            # quietly rewriting it to the default here would hide a caller error rather than
            # answer it.
            expires_in_days = None
    excluded = _exclusions(body)
    # #851: a thread whose answers all came from a connected warehouse cites no DOCUMENT, and
    # this early refusal used to end the request there. That was correct while a share could
    # only ever hand over documents; the moment the grantor can consent to a SOURCE it became
    # a refusal to share the very threads routing exists to produce. The question this guard
    # asks is "is there anything here at all", so it has to ask about both planes.
    if not all_cited and not any(_turn_stores(t) for t in turns):
        raise HTTPException(status_code=400,
                            detail="this conversation cites no documents or sources - "
                                   "nothing to share")
    # ADR 0017 s2, the SAME rule and the same implementation the document surface runs
    # (`_shareable_docs`): a thread cites whatever the sharer could READ, which includes
    # documents she is reading through somebody else's grant, and those are not hers to pass
    # on. Applied HERE, before anything is minted, so a refused document never becomes a
    # grant that has to be un-made.
    shareable = _shareable_docs(all_cited, user, request)
    # #610: the owner's own narrowing, applied to the SAME set and one line after the server's
    # own rule, so everything downstream cannot tell the two apart - and that is the entire
    # point. An excluded document is not "granted less", it is UNSHAREABLE for this share, so
    # the prefix cut below withholds the turn that cited it exactly as it withholds a turn
    # citing somebody else's document, the rollback treats it the same, and the final re-cut
    # sees the same set. A filter applied to `cited` after the cut instead would have left the
    # answer text of the excluded document's turn travelling to the recipient with no grant
    # anywhere to show for it - the same grant-channel-closed / content-channel-open split
    # that produced NEW-1 below and #601's post-share boundary.
    #
    # `unchecked` is what the exclusion ACTUALLY removed, and it is kept rather than recomputed
    # because the refusal below reports a CAUSE to the owner. Branching that message on the raw
    # `excluded` input instead reported a fact that had not happened: a thread citing only a
    # document somebody else shared with her, plus an `exclude_docs` naming an id that is not in
    # the thread at all, answered "every document this conversation can pass on was unchecked"
    # when she had unchecked nothing - and the same request without the field answered, rightly,
    # that the documents were not hers to pass on. A wrong explanation is not a cosmetic defect:
    # it sends her to un-tick boxes that are already ticked instead of to the document's owner.
    unchecked = excluded & set(shareable)
    shareable = {d: acl for d, acl in shareable.items() if d not in unchecked}
    # Fix round 2, NEW-1: WITHHOLD THE TURN, not merely the grant.
    #
    # Filtering only the grants closed the retrieval channel and left the content channel
    # wide open: a thread citing one of the sharer's own documents AND one somebody shared
    # with her shared "successfully" with `documents: 1`, and the transcript then handed the
    # recipient BOTH turns - including the answer synthesized verbatim out of the document the
    # filter had just refused to grant. Strictly worse for that document's owner than the bug
    # it replaced, because no grant is minted, so she sees no trace of it at all. Same
    # grant-channel-versus-content-channel split as the post-share boundary, one turn earlier.
    #
    # A turn travels only if EVERY document it drew on is shareable. A turn that cited nothing
    # drew on nothing and travels. Dropped rather than refused, matching what the grants
    # already do - one received document must not poison an otherwise legitimate share - but
    # NOT silently: `turns_withheld` goes back in the response so the sharer's own UI, where
    # she already knows which documents are hers, can say how much did not travel. The
    # recipient is never told, because the count of what she cannot see is itself a fact about
    # documents she may not know exist.
    #
    # PROPAGATING, not per-turn (#601 IMPORTANT-C). Answers are not independent across
    # turns: `ConversationService.ask` condenses each follow-up against the history window
    # INCLUDING prior answers (the real adapters fold answers into the condense prompt -
    # adapters/azure/aoai.py, adapters/anthropic, adapters/llama), so a turn that cites only
    # shareable documents can have been generated from a question synthesized out of a
    # withheld turn's content, and a model that restates the question in its answer leaks it.
    # Once a turn is withheld, everything after it in the thread is downstream of content the
    # recipient may not have, so the shared half is the PREFIX up to the first withheld turn.
    # That also makes the shared half contiguous, which is what lets the boundary below be a
    # single count and lets the transcript stay in chronological order with no gaps.
    # #851: the SOURCES this thread drew on, minus the ones the owner unticked. Computed from
    # the thread itself and then narrowed, exactly as `shareable` is for documents - so a
    # store id the client sends can only ever remove one, never add one, and a store the
    # thread never touched cannot be consented to at all.
    thread_stores = []
    for t in turns:
        for sid in _turn_stores(t):
            if sid not in thread_stores:
                thread_stores.append(sid)
    unchecked_stores = _store_exclusions(body)
    consented_stores = [s for s in thread_stores if s not in unchecked_stores]
    cut = len(turns)
    for i, t in enumerate(turns):
        # #689/#851: the SAME predicate the read path applies (`_turn_blocks_share`), against
        # the SAME consent list that is about to be recorded on the row - so the sharer is TOLD
        # in `turns_withheld` how much did not travel, instead of discovering it by reading a
        # transcript that stops early. The two sides agreeing is the invariant the whole share
        # rests on; a boundary computed one way and enforced another is #601's post-share
        # defect with different inputs.
        if (not set(t.cited_docs) <= set(shareable)
                or _turn_blocks_share(t, consented_stores)):
            cut = i
            break
    shared_turns = turns[:cut]
    # A ZERO-CITATION turn (#601 IMPORTANT-D) drew on no document, so it carries no document
    # content and travels - `set() <= anything` is True and that is the correct answer, not an
    # accident. Including a leading one ("I could not find anything about that") is harmless
    # and keeps the thread readable. What must NOT happen is a share serving a transcript
    # with no live document behind it, and that takes TWO rules, not one - the round-3
    # comment here claimed this refusal covered it and was wrong, because it only covers
    # create time. `if not cited` below refuses to MINT such a share; the read path refuses
    # to SERVE one whose grants have since gone (see `conversation_transcript`).
    # The grants are minted for exactly what the SHARED turns cited - not for what the whole
    # thread cited. Keeping the two identical is the invariant the transcript read depends on
    # (see `conversation_transcript`): you may read a turn precisely when you may retrieve
    # everything it drew on.
    cited = [d for d in all_cited if any(d in t.cited_docs for t in shared_turns)]
    # #851: what the shared half rests on is now documents OR consented sources. A thread whose
    # answers all came from a connected warehouse cites no DOCUMENT and would have been refused
    # as "nothing to share" - which was true when a share could only ever hand over documents,
    # and became false the moment the grantor could consent to a source. The refusal below is
    # about an EMPTY share, so it has to ask about everything a share can now carry.
    shared_stores_in_prefix = any(_turn_stores(t) for t in shared_turns)
    if not cited and not shared_stores_in_prefix:
        # #610: an exclusion that empties the share is refused, never minted as an empty one -
        # and it is told apart from the server's own refusal, because "every document was
        # unchecked" and "none of these were yours to pass on" are different facts and the
        # owner can only act on one of them.
        #
        # The branch is on `unchecked` - what the exclusion REMOVED - and never on whether the
        # field was present. See its assignment above: an id that matched nothing removes
        # nothing, so it must leave this message exactly as it would have been with no
        # `exclude_docs` at all, which is the same rule that makes an unknown id harmless
        # everywhere else on this path.
        raise HTTPException(status_code=400,
                            detail=_EXCLUDED_EVERYTHING if unchecked else _NOTHING_TO_SHARE)
    token = None
    try:
        # ONE number does both jobs now that the shared half is a prefix: turns after it are
        # either later than the share (CRITICAL-2 - freeze what was handed over, so a turn
        # added afterwards never reaches the recipient) or downstream of a withheld one
        # (IMPORTANT-C). Corrected once more after the mint loop, see below.
        if is_link:
            # #606: `create_link` sits exactly where `create` sits, and for a structural
            # reason rather than a tidy one - the sentinel grantee is DERIVED from the
            # share_id, so the row has to exist before anything can be granted to it. That is
            # also why `grantee` is only read off the record below: the mint loop, the
            # pre-mint drop and the rollback all name the same principal for both audiences,
            # so neither can be given a link-shaped special case to drift in.
            share, token = _edition.conversation_shares.create_link(
                conv_id=conv_id, grantor_oid=user, expires_in_days=expires_in_days,
                turn_cutoff=cut, shared_stores=consented_stores)
        else:
            share = _edition.conversation_shares.create(
                conv_id=conv_id, grantor_oid=user, grantee_oid=grantee,
                expires_in_days=expires_in_days, turn_cutoff=cut,
                shared_stores=consented_stores)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # From here down there is ONE path. A people share's grantee is the person resolved above;
    # a link share's is the `link:<share_id>` sentinel the row just minted - and reading BOTH
    # off the record means the grant loop below cannot be looking at a different principal
    # from the one the share row names.
    grantee = share.grantee_oid
    backed, minted, backed_docs = 0, [], set(cited)
    tenant = _request_tenant(request)
    try:
        # Fix round 2, NEW-2: clear this (conversation, person) pair's OLD conversation grants
        # before minting the new set. A re-share updates one share row rather than adding a
        # second, so without this the previous share's grants would survive beside the new
        # ones - and since the transcript reads "which documents does this share actually
        # grant" to decide which turns may be read, a stale grant would make MORE turns
        # readable than the share the sharer just made was willing to hand over. On a first
        # share this drops nothing.
        #
        # #601 IMPORTANT-B: scoped to THIS caller's own grants. `history` proved the caller
        # owns HER OWN thread under this conv_id and nothing whatever about anybody else's
        # grants under the same id - and conv_id is client-chosen, so a collision is a
        # collision, not a coincidence to be trusted. Unscoped, sharing a thread whose id
        # matched one somebody else had already shared destroyed that person's grants while
        # their share row stayed live, leaving their recipient a listed share backed by
        # nothing, and told nobody.
        _edition.grant_registry.drop_for_conversation(conv_id, grantee, granted_by=user)
        for doc_id in cited:
            g = _edition.grant_registry.create(
                doc_external_id=doc_id, tenant_id=tenant, grantee_oid=grantee,
                granted_by=user, expires_in_days=expires_in_days, conv_id=conv_id)
            minted.append(g.grant_id)
            if _edition.index.add_doc_principals(tenant, doc_id, [g.principal]):
                backed += 1
            else:
                # It passed the s2 test a moment ago and is not reachable now: deleted
                # between the two reads. The grant backs nothing, so it must not survive as a
                # row that looks like access. `discard_unconfirmed`, not `revoke` (#575
                # Finding A): a grant that never took effect is not a live authorization
                # decision, and revoke's fail-closed store call would mask this request's
                # honest error behind a 503.
                _edition.grant_registry.discard_unconfirmed(g.grant_id)
                minted.pop()
                backed_docs.discard(doc_id)
    except Exception:
        # Fix round 1, IMPORTANT-3. `add_doc_principals` can raise (a live index round trip),
        # and an unrolled-back failure here returned 500 to the sharer while leaving the share
        # row LIVE and some grants standing - her UI says the share failed and the recipient
        # can read and ask. That is "looks revoked, isn't" inverted, and it is the one failure
        # direction this whole feature's docstrings exist to rule out. So the request undoes
        # itself: every grant minted so far, then the share row, all through
        # `discard_unconfirmed` (never `revoke`), which never raises and so cannot bury the
        # original failure. Then 503 - a partial share is not a 200, and not a silent one.
        #
        # On a RE-share this undoes the previous share too, since one row is all there is.
        # Deliberate: the fail-closed direction for an authorization operation is less access
        # than the sharer intended, never more, and a re-share that failed halfway would
        # otherwise leave a share whose boundary and grants nobody chose.
        _undo_share(share.share_id, minted)
        logging.getLogger("dbsearch").error(
            "conversation share %s rolled back - the index refused a grant mid-share",
            share.share_id)
        raise HTTPException(status_code=503,
                            detail="could not share right now - nothing was shared, try again")
    # #601 MINOR-F: the boundary is recomputed against the documents that ACTUALLY landed,
    # not the ones that passed the s2 test a moment earlier. A document deleted between the
    # two would otherwise leave its turn withheld from the recipient (the transcript checks
    # live grants) but counted as shared to the sharer - a number she would act on that was
    # never true. The correction is one narrowing call on the row that already exists - see
    # `set_turn_cutoff` below for why it is not a second `create` any more.
    final_cut = len(shared_turns)
    for i, t in enumerate(shared_turns):
        if (not set(t.cited_docs) <= backed_docs                  # #689/#851
                or _turn_blocks_share(t, share.shared_stores)):
            final_cut = i
            break
    if final_cut < cut:
        # #606: `set_turn_cutoff`, not a second `create`. Re-calling `create` corrected the
        # boundary by upserting the same row, which works only because the people path's
        # one-live-row rule resolves back to the same share_id. A link row has no such key -
        # `create_link` deliberately does not deduplicate, since two links are two independently
        # revocable credentials - so a second call there would have minted a SECOND share with a
        # SECOND token, leaving the first row live at the wrong boundary and the token this
        # response returns pointing at neither. One narrowing update, one row, both audiences.
        share = _edition.conversation_shares.set_turn_cutoff(share.share_id, final_cut)
    # #851: `backed` counts the DOCUMENT grants that actually landed, and "no grants" meant
    # "this share opens nothing" while documents were the only thing a share could open. A
    # share whose shared half rests on consented SOURCES opens something real without minting
    # a single grant - there is nothing to grant, which is the whole reason consent exists
    # here - so a zero here is only fatal when the prefix has no sources either. `final_cut`
    # stays as it was: a share of zero turns opens nothing under any reading.
    if (not backed and not any(_turn_stores(t) for t in shared_turns[:final_cut])) \
            or not final_cut:
        # Every shareable document went missing between the s2 test and the mint, or what
        # survived leaves no turn readable. A share of nothing that returns 200 is the exact
        # lie `add_doc_principals` returns a count to prevent, one layer up.
        #
        # #601 round 4, NEW-1: THE GRANTS GO TOO. `not backed` alone was vacuous - backed 0
        # implies `minted` is empty - but `not final_cut` is reachable with backed >= 1, and
        # that path is new in round 3: it discarded the share row and 400'd while leaving
        # every grant the loop had just minted and confirmed LIVE. Reproduced: the sharer is
        # told nothing was shared, the recipient holds a live grant on a document that was
        # never in any successful share, and retrieves it inside a conv_id an earlier share
        # taught her - with nothing on the conversation surface for the sharer to revoke it
        # with, because the share row is gone. Round 5 made it literally the same rollback as
        # the `except` handler above rather than a copy of it, because a copy is what went
        # missing here in the first place.
        #
        # What is NOT restored is whatever `drop_for_conversation` removed on the way in.
        # That is deliberate and is the fail-closed direction: the recipient ends with LESS
        # access than before a failed re-share, never more.
        _undo_share(share.share_id, minted)
        raise HTTPException(status_code=400, detail=_NOTHING_TO_SHARE)
    # LAW 1: ids, counts and a timestamp, never WHICH documents or turns - naming them would
    # name documents.
    #
    # #601 MINOR-E, and this is the honest version of a comment that used to be wrong. There
    # are two numbers and they have different audiences. `turns_withheld` is SHARER-ONLY: it
    # rides on this response, which only the sharer receives, and it is the count she needs
    # to say "some of this thread did not travel". `turn_cutoff` is on the share RECORD and
    # therefore does reach the grantee through /conversations/shared-with-me - which is fine,
    # and the previous claim that she is "never told" was simply false. It is fine because
    # after the propagation rule the shared half is a contiguous prefix, so `turn_cutoff` is
    # exactly the number of turns she is already reading and can count for herself. It
    # discloses nothing she does not already hold. The number she is NOT given is how much
    # was held back, and that one stays here.
    #
    # THE TOKEN LEAVES THE SERVER HERE AND NOWHERE ELSE (#606, ADR 0021). It is not on the
    # share record, not in the store, not in any log line - including the rollback logs above,
    # which name the share_id and must keep naming only that - and it cannot be recovered
    # afterwards by any route: the row keeps a SHA-256 digest, so losing this response means
    # minting a new link. `share.to_dict()` deliberately omits the digest as well, so this
    # response is the only place in the product where anything token-shaped is rendered.
    out = {**share.to_dict(), "documents": backed, "turns_withheld": len(turns) - final_cut}
    if token is not None:
        out["url"] = f"/c/{token}"
    return out


@app.get("/conversations/{conv_id}/shareable")
def conversation_shareable(conv_id: str, request: Request,
                           user: str = Depends(current_user)) -> dict:
    """#610: what a share of this thread WOULD expose, before one exists.

    This is the pre-share scope confirmation the owner asked for: the modal renders this list
    so she can see exactly which documents the share is about to hand over and uncheck the ones
    she does not mean to (`exclude_docs` on the POST). Every value here is computed by the same
    two calls the POST makes - `_owned_thread_citations` for the thread's citations and
    `_shareable_docs` for ADR 0017 s2 - so the modal cannot show a set the mint would not use.

    OWNER-ONLY, and 404 rather than 403, through exactly the mechanism the share POST uses:
    `_owned_thread_citations` reads history keyed by (conv_id, user_oid), so somebody else's
    thread is not there to read and answers identically to one that never existed (the #549
    rule - existence is the secret).

    `shareable: false` rows are LISTED rather than hidden, and that discloses nothing new: the
    caller cited these documents in her own thread and can already see their titles on "Your
    data". Hiding them would be worse than useless - she would count fewer documents than the
    thread visibly drew on and have no way to learn why part of the transcript will not travel.

    LAW 1 holds: ids, titles and a turn COUNT. A title is metadata this caller can already
    read; not one word of what was said inside the conversation is here.

    Titles come from the caller's OWN readable listing (`list_documents`, ACL-trimmed by the
    same identity port retrieval uses), never from an unscoped index read - so a title can only
    appear here if she may already see it. A document whose metadata this caller cannot read
    falls back to its id rather than being dropped, because the SHARE still turns on it and a
    row missing from this list is a document she cannot uncheck.

    The scope is the caller's plain request scope, WITH NO `active_conv_id`, and that is a rule
    rather than an omission: declaring an active conversation expands conv-scoped grant
    principals into the read (ADR 0020), and only the two chat routes may do that - pinned by
    tests/selftest_600_conv_scoped_grants.py, which caught this route doing it. The cost is
    that a document reached only through somebody else's conversation share shows its id in
    place of a title. That is the right direction: such a document is `shareable: false`
    anyway, so the row exists to explain a gap in the transcript, not to be checked."""
    conv_id = _conv_id(conv_id)
    turns, all_cited = _owned_thread_citations(conv_id, user)
    shareable = _shareable_docs(all_cited, user, request)
    titles = {d.doc_external_id: d.title
              for d in _edition.list_documents(user, _request_scope(request, user))}
    # #851: the SOURCES the thread drew on, so the modal can offer them beside the documents
    # and the owner decides once, in one place, what this share hands over. `shareable` is
    # True for all of them and is present rather than assumed: it keeps the row shape
    # identical to a document's, so the checklist renders one kind of row, and it leaves a
    # place for a future "not yours to pass on" rule to say so instead of the row vanishing.
    #
    # LAW 1 holds exactly as it does for documents above: a store id and its human origin are
    # names this caller connected and typed herself. Not one row of what the query returned.
    stores = []
    for t in turns:
        for c in (t.citations or []):
            sid = c.get("store_id")
            if sid and not c.get("doc") and not any(x["id"] == sid for x in stores):
                stores.append({"id": sid, "title": c.get("origin") or sid,
                               "shareable": True})
    return {"documents": [{"id": d, "title": titles.get(d) or d,
                           "shareable": d in shareable} for d in all_cited],
            "stores": stores,
            "turns": len(turns)}


@app.get("/conversations/{conv_id}/shares")
def list_conversation_shares(conv_id: str, user: str = Depends(current_user)) -> dict:
    """Who this caller has shared this thread with. Scoped to the caller inside
    `list_for_conversation`, so it can never list a share somebody else made - and an
    unknown conversation is an empty list rather than a refusal, which is the same
    "existence is the secret" stance the 404s take."""
    return {"shares": [s.to_dict() for s in
                       _edition.conversation_shares.list_for_conversation(_conv_id(conv_id),
                                                                          user)]}


@app.get("/conversations/{conv_id}/shares/{share_id}/questions")
def link_share_questions(conv_id: str, share_id: str,
                         user: str = Depends(current_user)) -> dict:
    """What strangers asked through this link. #611, ADR 0021's accepted consequences.

    The owner asked for it in those words ("yes, want to see what strangers asked", spec s5),
    and ADR 0021 records it as a DISCLOSURE OBLIGATION rather than only a feature: a visitor
    types into the share page without knowing the owner reads it, so the page must say so at
    the point of asking.

    THE DISCLOSURE HALF IS NOW BUILT, and this route no longer runs ahead of it. It did for
    one card: the sentence was a constant in static/js/surfaces/ask.js, `/c` was absent from
    SHELL_PATHS, so `/c/{token}` rendered the LANDING view and the disclosure node was built
    inside a container that was never shown - four green tests, all of them grepping that
    asset, and no visitor told anything. `/c/{token}` now serves the visitor's own document
    (static/visitor.html via server/link_access.py) with the sentence as static markup
    directly above the question input, and tests/selftest_605_visitor_surface.py plus the
    disclosure tests in tests/selftest_611_visitor_question_log.py assert on what
    `GET /c/{token}` RETURNS TO A COOKIE-LESS CALLER rather than on any file. Deleting the
    sentence from that page fails them; deleting it from a module does not, because no module
    carries it any more.

    WHAT IS STILL OWED, so this paragraph does not overclaim in the other direction: a real
    browser. Those tests prove the sentence is in the served bytes and in the DOM above the
    input. They do not prove it is painted where a visitor looks.

    QUESTIONS ONLY, NEVER THE ANSWERS, and nothing here should later be "completed" by adding
    them. The owner can re-ask anything she likes through her own account, so the answer text
    buys nothing; what it would cost is a second content channel out of the one surface whose
    entire justification is "the owner wants to know what people ask" - and it would reproduce
    document content into a new place besides. The refusal is structural, not a promise made
    in prose: `questions_for` selects `question, asked_at` and never touches the answer column
    (conversation_store.py), so this route is not handed the text it must not return.

    A VISITOR IS AN ORDINAL, NEVER THEIR COOKIE. `visitor: 1, 2, 3` are positions in
    first-seen order within THIS share. The fork-key cookie is a tracking key: handed to an
    owner it would become a stable identifier she could correlate, which is worse than content
    under LAW 1 rather than better - ids, counts and timestamps are the metadata class this
    product returns, and a person-tracker is not one of them.

    THE KNOWN RESIDUAL, stated rather than papered over: `dbsearch_visitor` is scoped to the
    `/c` DOORWAY, not to one share, so a single browser carries the SAME value to every
    owner's link. Ordinals are computed per share, over this share's own forks, so nothing
    cross-share-stable reaches either owner and two owners comparing logs have only a
    timestamp to join on - selftest_611 pins exactly that with one browser across two owners'
    links. Narrowing the cookie to one share is deferred work (it would give one visitor a
    fork per link, which is a behaviour change on the doorway, not on this route).

    OWNER ONLY, via `find(share_id, user)` - the same ownership check `revoke` uses, and the
    same single KeyError for "no such share" and "not yours" so this 404 is not an oracle. The
    `conv_id` in the path is CHECKED AGAINST THE RECORD and then never used again: it is
    client-chosen, so a share id paired with somebody else's conversation id must read
    nothing rather than that conversation.

    A PEOPLE SHARE ANSWERS AN EMPTY LIST, not an error. Its grantee signs in and asks under
    her OWN account key (ADR 0020) - there is no anonymous traffic to log, and her follow-up
    questions are hers, not the grantor's to read. The audience check is what keeps that true:
    widening this scan to keys that are not this share's forks would turn an owner's link log
    into a way to read a named colleague's private questions."""
    try:
        share = _edition.conversation_shares.find(share_id, user)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such share")
    if share.conv_id != _conv_id(conv_id):
        raise HTTPException(status_code=404, detail="no such share")
    return _link_question_log(share)


def _link_question_log(share) -> dict:
    """The body of the route above, extracted so `GET /shares/mine` (#607) can COUNT the same
    rows it will later show.

    ONE implementation, because the count on the management row and the list the [View] button
    opens are the same claim made twice. A count derived some cheaper way - rows in the store
    under the prefix, questions the rate limiter saw - would drift the first time either
    definition moved, and the visible symptom would be a row promising "asked 4 questions" over
    a log with three in it, which reads as the log hiding one.

    CALLER-CHECKED, NOT SELF-CHECKED, and stated because this function has no `user` argument:
    it takes a share RECORD, and every caller must have resolved that record through
    `find(share_id, requester_oid)` first. Both do."""
    if share.audience != AUDIENCE_LINK:
        return {"questions": [], "visitors": 0}
    # `fork_key(share, "")` rather than a prefix spelled out here: the fork key format is
    # link_access.py's to define, and a second copy of it in this file is a copy that keeps
    # matching until the day it does not - at which point this log silently reports that
    # nobody ever asked anything.
    prefix = fork_key(share, "")
    store = _edition.conversation_service._store
    try:
        forks = store.users_for_conv(share.conv_id, prefix)
        ordinal = {oid: n for n, oid in enumerate(forks, start=1)}
        rows = []
        for oid in forks:
            for i, (question, asked_at) in enumerate(store.questions_for(share.conv_id, oid)):
                rows.append(((asked_at or "", ordinal[oid], i),
                             {"question": question, "asked_at": asked_at,
                              "visitor": ordinal[oid]}))
    except ConversationStoreUnavailable:
        # The same honest 503 every other conversation read gives. An empty log read from a
        # down store would tell an owner that nobody has used her link, which is a statement
        # about strangers' behaviour she would have no reason to doubt.
        raise HTTPException(status_code=503,
                            detail="cannot read the question log right now, try again shortly")
    # Chronological across visitors, so the owner reads one conversation-shaped stream rather
    # than one block per fork. The (ordinal, position) tail is a deterministic tie-break, not
    # a second ordering: two visitors' questions can carry the same timestamp, and a log whose
    # order changed between two reads of the same data would be read as new activity.
    rows.sort(key=lambda r: r[0])
    return {"questions": [r[1] for r in rows], "visitors": len(forks)}


# --- #607 / #608: the owner's management surface over every share she has made -------------
#
# Everything above answers questions about ONE conversation, because that is where a share is
# born. The owner's question afterwards is the other way round - "what have I given away, and
# to whom?" - and she has no list of conv_ids to iterate to get there. These two routes are
# that surface's whole API: one listing, and one narrowing.

# The row's human name is the thread's OPENING QUESTION, truncated. It is the only string in
# this feature that names a conversation, and it is the owner's own words about her own thread,
# returned to her alone - LAW 1 is about not handing conversation content to somebody it was
# not addressed to, and this response has exactly one reader, checked by `grantor_oid`.
#
# #602: the cut itself moved to `_thread_name` up beside `/conversations/mine`, which names the
# SAME threads on a different screen. It was inlined here when this was the only such list; a
# second copy would have let the two lists disagree about what a conversation is called.

_NARROWED_TO_NOTHING = ("removing those documents would leave nothing in this share readable - "
                        "revoke the share instead")
# Requirement 3, and it is refused rather than performed. A share row that grants nothing but
# still lists as live is the "looks revoked, isn't" failure inverted: the owner sees a live row
# with a Revoke button beside it and believes the recipient still has what it says, while the
# recipient sees a 404. Whichever of the two acts on that belief acts wrongly.


def _share_scope(share) -> "set[str]":
    """The documents THIS share actually grants, right now.

    Read off grant RECORDS, never off anything stored beside the share, and narrowed the same
    two ways `link_access.granted_docs` and `conversation_transcript` narrow theirs - to this
    share's conversation, and to grants its own grantor made, because conv_id is client-chosen
    and somebody else's grant under a colliding id is not part of this share. Three readers of
    the same fact, all deriving it the same way, is the invariant that keeps "you may read a
    turn exactly when you may retrieve everything it drew on" true."""
    return {g.doc_external_id
            for g in _edition.grant_registry.live_grants_for(share.grantee_oid)
            if g.conv_id == share.conv_id and g.granted_by == share.grantor_oid}


def _readable_cut(turns, granted: "set[str]", ceiling: int) -> int:
    """How many of the grantor's turns a share hands over, given a set of granted documents.

    THE SAME PREFIX RULE `_readable_prefix` APPLIES AT READ TIME, and deliberately the same
    shape: bounded by what was already handed over, stopping (never skipping) at the first turn
    that draws on a document outside `granted`. `_readable_prefix` is the enforcement; this is
    the DECISION the narrowing route has to take BEFORE it changes anything, in order to answer
    "would this leave anything readable at all?" - and if the two computed different numbers,
    the route would either refuse a narrowing that was fine or persist a boundary the read path
    then contradicts.

    An empty `granted` yields 0 for the same reason `_readable_prefix` returns [] on one: a
    zero-citation turn passes `set() <= granted` however empty `granted` is, so without this the
    answer would be "the leading turn is still readable" for a share behind which nothing is
    left - and the grantor's typed QUESTION is itself content."""
    if not granted:
        return 0
    cut = max(0, min(int(ceiling), len(turns)))
    for i, t in enumerate(turns[:cut]):
        if not set(t.cited_docs) <= granted:
            return i
    return cut


@app.get("/shares/mine")
def my_shares(request: Request, user: str = Depends(current_user)) -> dict:
    """Every live share this caller has granted, both audiences, with what each one opens.

    THE ROW IS THE PRODUCT and each field on it answers a question the owner asked in her own
    words ("who it is shared to, when etc. or can revoke"): `first_question` names the thread,
    `audience` + `grantee_oid` name the recipient (or say there is nobody to name),
    `expires_at` says when it lapses, `opens`/`last_open_at` say how much use a link has had,
    `scope` says exactly which documents it opens, and `questions_asked` says how much strangers
    have asked through it. Nothing here is computed twice or stored twice - every one of them is
    read off the share record or derived from live grants at this moment.

    LIVE ONLY, decided inside `list_granted_by`. See its docstring: a row here carries Revoke
    and Edit, and offering both on a share that has already lapsed is offering two operations
    that change nothing about a thing the owner would read as still exposing her documents.

    `scope` IS THE SHARE'S OWN LIVE GRANTS, not the thread's citations. Those two were the same
    set at share time and stop being the same the moment anything narrows - a PATCH below, a
    deleted document, a revoke. Rendering citations here would show the owner a scope her share
    no longer has, which is the exact direction this surface exists to make visible.

    TITLES come from the caller's OWN readable listing, the same rule and the same reason
    `conversation_shareable` gives: a title can only appear here if she may already read it, and
    a document whose metadata she cannot read falls back to its id rather than vanishing from a
    list she is about to make decisions against. The scope is her plain request scope with NO
    `active_conv_id`, because declaring an active conversation expands conv-scoped grant
    principals into the read (ADR 0020) and only the two chat routes may do that.

    `history()` is UNGUARDED against a store outage and becomes a 503 (#596), not an empty name.
    A management surface that renders every share as an untitled row during an outage invites
    the owner to revoke the wrong one.

    LAW 1: ids, titles, counts, timestamps - and ONE sentence of the owner's own conversation,
    returned to the owner alone. `token_hash` is absent because `to_dict` never emits it."""
    shares = _edition.conversation_shares.list_granted_by(user)
    titles = ({d.doc_external_id: d.title
               for d in _edition.list_documents(user, _request_scope(request, user))}
              if shares else {})
    out = []
    for s in shares:
        try:
            turns = _edition.conversation_service.history(user, s.conv_id)
        except ConversationStoreUnavailable:
            raise HTTPException(
                status_code=503,
                detail="cannot read your conversations right now, try again shortly")
        name = _thread_name(turns[0].question if turns else "")
        scope = sorted(_share_scope(s))
        out.append({**s.to_dict(),
                    "first_question": name,
                    "scope": [{"id": d, "title": titles.get(d) or d} for d in scope],
                    "questions_asked": len(_link_question_log(s)["questions"])})
    return {"shares": out}


@app.patch("/shares/{share_id}/scope")
def narrow_conversation_share(share_id: str, body: dict,
                              user: str = Depends(current_user)) -> dict:
    """#608: take a document back out of a share that is already live. REMOVE ONLY.

    THERE IS NO ADD KEY, and that is a structural property of the API rather than a validation
    rule: `remove_docs` is subtracted from a set this route computed out of grant records, so
    nothing a caller sends can put a document INTO a share. The share modal keeps the same
    promise by having no control that could build one (static/js/surfaces/ask.js). A widening
    operation is a fresh deliberate share, made by the grantor from the thread she is reading -
    it is not an edit to a row on a management screen, where "which documents did this already
    open?" is a question she is answering from a list rather than from the conversation.

    BOTH CHANNELS CLOSE, and they close in that order. The document's conv-scoped grants are
    revoked FIRST - that is the retrieval channel, and the one a link-holder is actively using -
    and the share's `turn_cutoff` is narrowed second, which is the boundary the transcript is
    served against. Closing only the grants would leave the share RECORD claiming to hand over
    more turns than it can, a number the grantee reads through /conversations/shared-with-me and
    the owner reads here; closing only the boundary would leave the removed document retrievable
    by anyone still asking questions through the share. This branch has produced four separate
    defects of the shape "the grant channel looked shut while the content channel stayed open",
    which is why the order is grants-first: if only one half lands, it must be the half that
    stops retrieval.

    (Stated precisely, because overclaiming here is worse than saying less: `_readable_prefix`
    RE-DERIVES the readable prefix from live grants on every read, so the grant revocation on
    its own already stops the transcript at the removed document's turn. The persisted
    `turn_cutoff` is not a second lock on that door - it is the RECORD agreeing with the door.
    tests/selftest_607_shared_surface.py pins both, and says which assertion catches which.)

    IT TAKES EFFECT ON THE NEXT REQUEST, ON THIS WORKER, for everyone that worker serves -
    including a visitor with a cookie and an open tab. Both registries are in-process and read
    per request, so there is no cache to invalidate and no session to re-establish: the
    visitor's very next `/c/{token}/transcript` and `/c/{token}/chat` resolve against the grants
    this call just removed.

    "ON THIS WORKER" IS LOAD-BEARING AND IS NOT A HEDGE (review round 1, Finding 2). `revoke`
    and `set_turn_cutoff` mutate process-local `_by_id`; every read path behind a link
    (`live_grants_for`, `granted_docs`, `visitor_scope`, `find_by_token`) reads that same map
    and NEVER re-reads the store. So with `--workers` this route answers 200, and
    `GET /shares/mine` then renders a narrowed row confirming it, while a visitor pinned to a
    different worker keeps retrieving the removed document until that process restarts. THIS IS
    THE FIRST SURFACE IN THE PRODUCT WHERE AN OWNER TAKES A REVOCATION ACTION AND IS TOLD IT
    SUCCEEDED, which is what turns ADR 0020's single-worker deploy constraint from an operator
    fact into a user-visible false success - recorded in ADR 0021's consequences.

    NOT fixed here, and a local fix would be the wrong shape: the remedy is the cross-process
    invalidation ADR 0020 already names as the precondition for running this deployment with
    more than one worker. Until then the deploy constraint is the mitigation. Do not paper over
    it by making this route pessimistic about its own success - a 202 or a hedged message would
    be wrong on the single-worker deployment this product actually ships.

    NARROWING TO NOTHING IS REFUSED, whole, before anything is applied - see
    `_NARROWED_TO_NOTHING`. The computation therefore happens entirely before the first
    mutation: a route that revoked as it went and then discovered the share was empty would have
    destroyed it while answering 400.

    UNKNOWN IDS SUBTRACT NOTHING AND ARE NOT AN ERROR, exactly as `exclude_docs` does not error
    (`_exclusions`), and for the same reason: an id that errored would make this parameter a
    PROBE for whether a document exists inside somebody's share. The TYPE is still refused,
    because a bare string would iterate as characters and remove nothing while reporting
    success - a narrowing the owner believes happened and did not.

    OWNER ONLY, and 404 rather than 403, through `find(share_id, user)` - one KeyError for both
    "no such share" and "not yours", so this 404 is not an oracle for other people's shares."""
    try:
        share = _edition.conversation_shares.find(share_id, user)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such share")
    for widening in ("add_docs", "add", "include_docs", "grant_docs"):
        if widening in body:
            # Refused rather than ignored. Unlike an unknown document id this names no document
            # and so is no oracle, and it is unambiguously a client bug: a caller sending it
            # believes a share can be widened from here, and silently dropping the key would let
            # her believe the widening happened.
            raise HTTPException(status_code=400, detail=(
                "a share can only be narrowed - remove documents here, or share the "
                "conversation again to hand over more"))
    raw = body.get("remove_docs") or []
    if not isinstance(raw, (list, tuple, set)):
        raise HTTPException(status_code=400,
                            detail="remove_docs must be a list of document ids")
    remove = {str(d).strip() for d in raw if str(d).strip()}
    live = [g for g in _edition.grant_registry.live_grants_for(share.grantee_oid)
            if g.conv_id == share.conv_id and g.granted_by == share.grantor_oid]
    doomed = [g for g in live if g.doc_external_id in remove]
    surviving = {g.doc_external_id for g in live if g.doc_external_id not in remove}
    try:
        turns = _edition.conversation_service.history(user, share.conv_id)
    except ConversationStoreUnavailable:
        # Unguarded for #596's reason: an outage read as an empty thread would make every
        # narrowing look like it empties the share, and this route's answer to that is a 400
        # telling the owner to revoke something that is perfectly healthy.
        raise HTTPException(status_code=503,
                            detail="cannot read the conversation right now, try again shortly")
    final_cut = _readable_cut(turns, surviving, share.turn_cutoff)
    if not final_cut:
        raise HTTPException(status_code=400, detail=_NARROWED_TO_NOTHING)
    try:
        for g in doomed:
            # `revoke`, not `discard_unconfirmed`: these grants ARE live authorization decisions
            # and this is the operation that ends them, so the fail-closed store ordering is the
            # one that belongs here (grants.py's `revoke` docstring is the full account). It
            # also re-checks `granted_by == user`, which `find` above already established - one
            # more refusal in the way of a route that ever stopped resolving the share first.
            _edition.grant_registry.revoke(g.grant_id, user)
        share = _edition.conversation_shares.set_turn_cutoff(share.share_id, final_cut)
    except KeyError:
        # The share or a grant went between the read above and here - a concurrent revoke won.
        # That is the revoke working, and answering 404 says so with the same word every other
        # "this share is not there" answers with.
        raise HTTPException(status_code=404, detail="no such share")
    except (GrantStoreUnavailable, ConversationShareStoreUnavailable):
        raise HTTPException(status_code=503,
                            detail="cannot change this share right now, try again")
    # Counts, never which documents - the same LAW 1 line the share response draws. The owner
    # sent the ids and can see the new scope by re-reading /shares/mine; echoing them back would
    # put a document list on a response for no reason at all.
    return {**share.to_dict(), "removed": len(doomed), "documents": len(surviving)}


def _citation_rows(cited_docs, docmeta: dict) -> list[dict]:
    """#620: rebuild a turn's citation rows, in stored order, from the docs it cited.

    `docmeta` maps doc_external_id -> (title, uri), resolved through the READER's own scope,
    so a transcript can never name a document its reader could not retrieve (a title is
    routinely the whole secret - #549).

    A document the scope cannot resolve - deleted since, or reachable only through somebody
    else's grant - DEGRADES to title=id, uri=None. It is never dropped. The answer text's
    [n] markers index into this list POSITIONALLY, so removing row n silently renumbers every
    later marker, which is exactly what `_drop_dangling_markers` refuses to do: renumbering
    attaches a claim to a source the model never pointed at. A row that says less is honest;
    a row that has moved is a lie."""
    rows = []
    for d in cited_docs or []:
        title, uri = docmeta.get(d, (None, None))
        rows.append({"doc": d, "title": title or d, "uri": uri})
    return rows


def _turn_stores(turn) -> list:
    """The composed stores this turn drew on, in first-citation order, deduplicated.

    Only proof rows count: a routed turn that answered from DOCUMENTS persists document
    citations (keyed on `doc`), which the conv-scoped grants already carry, so it is not a
    store-plane fact at all and must not appear in a consent list the sharer has to read."""
    out, seen = [], set()
    for c in getattr(turn, "citations", None) or []:
        sid = c.get("store_id")
        if sid and not c.get("doc") and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _turn_blocks_share(turn, consented) -> bool:
    """#689/#851: does this turn draw on a source the GRANTOR did not agree to pass on?

    THE MODEL IS CONSENT, NOT CAPABILITY, and that is the owner's ruling on #850.

    A conversation share's invariant is that you may READ a turn exactly when you may RETRIEVE
    everything it drew on. Document citations satisfy it through conv-scoped grants, which
    DBSearch can mint because it owns its own index's ACLs. A ROUTER PROOF has nothing to
    grant: DBSearch does not own the customer's warehouse permissions.

    THE FIRST VERSION OF THIS STOPPED ON EVERY ROUTED TURN, and it was wrong for the reason
    the owner gave: sharing exists to reach people who do NOT have access - an HR thread
    handed to an onboarding hire is the canonical case - so any rule keyed on the reader's
    access denies precisely the person the feature is for. The same objection kills "the
    reader must be able to see the store".

    So the grantor decides, in the checklist she already uses for documents, and this predicate
    asks only whether she agreed. What travels is a FROZEN RECORD of evidence she already saw,
    because she said it could - not a capability, not live access, and never a re-run token
    (`_turn_citation_rows` signs only for the caller's own half).

    `consented` is the share row's `shared_stores`. EMPTY MEANS NOTHING TRAVELS, which is the
    fail-closed reading a row written before #851 requires: that grantor was never asked.

    ONE predicate, used by the read side and by both share-creation counts, so they cannot
    disagree about which turns travel."""
    allowed = set(consented or ())
    return any(sid not in allowed for sid in _turn_stores(turn))


def _turn_citation_rows(turn, docmeta: dict, *, rerun_user: "str | None" = None) -> list[dict]:
    """#633 over #620: a turn's citation rows, preferring what was STORED with it.

    A stored row carries the passage the answer was actually built from, so a reopened
    thread quotes what the reader saw live. Recomputing at read time was the alternative and
    is worse: retrieval is not stable, so the same question asked again can surface a
    different passage, and a reader would be shown evidence the answer above it never used.

    Turns written before the citations column fall back to `cited_docs` - the same rows #620
    already built, just without quotes. An older turn showing no quote is a thinner record;
    an older turn showing an invented one would be a false one.

    #689: a stored ROUTER PROOF row passes through with its query and origin, and is re-signed
    for `rerun_user` so a reopened thread's Verify data works. The token is minted HERE and
    never stored, because it binds (store, sql, user) - a stored one would be a token issued
    to somebody else, or one outliving the identity it was bound to.

    `rerun_user` IS ONLY EVER THIS CALLER, and only for turns that are theirs. It is a
    parameter rather than read off the request precisely so that the one place that renders
    somebody ELSE's half of a thread cannot pass it by accident: signing a rerun token for a
    grantor's turn would hand the reader a credential to execute a query against a store they
    cannot see, which is gate #1 defeated by a transcript.

    THIS IS LOAD-BEARING, and it was not always. Under #689's first design a routed turn
    stopped a shared transcript outright, so the grantor's half could never contain a proof row
    and this omission was belt to that rule's braces - a note here said exactly that, and said
    that no test could honestly claim to catch a mutation of it. #851 (the owner's ruling on
    #850) replaced the stop with the grantor's CONSENT, so a consented routed turn now travels
    WITH its proof rows, and this line is the whole of the difference between handing a
    recipient a record of what was run and handing them a credential to run it. That is the
    line the consent model draws: a share passes on evidence, never access."""
    stored = getattr(turn, "citations", None) or []
    if not stored:
        return _citation_rows(turn.cited_docs, docmeta)
    rows = []
    for c in stored:
        if not c.get("doc") and c.get("store_id"):
            row = {k: v for k, v in c.items()}
            if rerun_user and row.get("kind") == "sql" and row.get("sql"):
                from dbsearch.router.provenance import sign_rerun
                row["rerun_token"] = sign_rerun(row["store_id"], row["sql"], rerun_user)
            rows.append(row)
            continue
        d = c.get("doc") or ""
        # Title and uri are ALWAYS re-resolved through this reader's scope, never read from
        # the stored row - which is why they are not persisted. A stored title goes stale on
        # a rename and, worse, would survive a document leaving this reader's scope entirely.
        title, uri = docmeta.get(d, (None, None))
        row = {"doc": d, "title": title or d, "uri": uri}
        if c.get("quote"):
            row["quote"] = c["quote"]
            row["quote_kind"] = c.get("quote_kind") or "retrieved"
        if c.get("locator"):
            row["locator"] = c["locator"]
        rows.append(row)
    return rows


def _readable_prefix(share, grantor_turns, granted: "set[str]",
                     docmeta: "dict | None" = None) -> list[dict]:
    """WHICH of the grantor's turns a share hands over, right now: the first `turn_cutoff`,
    stopping at the first one that cites a document the share does not CURRENTLY grant.

    ONE implementation, because a share now has TWO readers and they must answer the same
    question about the same thread: the signed-in grantee (`conversation_transcript` below,
    ADR 0020) and the anonymous link visitor (`/c/{token}/transcript`, server/link_access.py,
    ADR 0021). If the two read the boundary differently, one audience gets turns the other's
    identical share withholds - and the difference would be answer TEXT, which is the channel
    three separate defects on this feature travelled down while the grant channel looked shut.

    BOTH rules are here, and both are load-bearing:

      `turn_cutoff` (fix round 1, CRITICAL-2) freezes what was handed over. Reading the
      grantor's history live handed over every turn she added AFTER sharing, including answers
      synthesized from documents the reader was never granted and provably cannot retrieve -
      the grant channel was a snapshot and the content channel was not, so the snapshot decided
      nothing. A COUNT is exactly right because history is oldest-first and append-only per
      (conv_id, user_oid), so "the first N turns" is "the turns that existed at share time".

      `granted` (fix round 2, NEW-1) is re-checked against the grants that are LIVE at THIS
      read, so a turn drawing on a document the share was not allowed to pass on - or one whose
      document has since been deleted or narrowed away - stops the transcript. The grants ARE
      the record, deliberately, rather than a second stored list: one invariant then carries
      both channels - you may READ a turn exactly when you may RETRIEVE everything it drew on.

    IT STOPS, it does not skip (#601 IMPORTANT-C). Every later turn was condensed against a
    history window that included this one's answer, so it is downstream of content this reader
    may not have, and a model that restates the question in its answer carries it forward.

    AN EMPTY `granted` YIELDS NOTHING AT ALL (#601 round 4, NEW-3), and that is a rule rather
    than an accident of the loop: a ZERO-CITATION turn cites nothing, so `set() <= granted` is
    True however empty `granted` is, and a leading zero-citation turn was therefore still
    served after every document behind the share was gone. No document CONTENT travels that
    way, but the grantor's typed QUESTION does, and a question can be the whole secret ("why
    was the Krakow office headcount cut in half?"). A conversation share is a transcript AND
    the documents it rests on; when the last grant goes, the share opens nothing - which is
    exactly what a revoke already produces, by the same mechanism rather than a separate one.
    Callers skip the history read entirely in that case; this guard is the floor under them,
    so a future third reader cannot lose the rule by forgetting to check first.

    #851 QUALIFIES THAT FLOOR WITHOUT LIFTING IT. "No live grants" meant "this share opens
    nothing" while documents were the only thing a share could rest on. A share can now also
    rest on SOURCES the grantor consented to, and such a share legitimately mints no grant at
    all - there is nothing to grant, which is the whole reason consent exists here. So the
    floor now asks whether the share opens NOTHING AT ALL, and the revoke property it was
    written for is untouched: a document turn still needs `set(t.cited_docs) <= granted`, so
    revoking the documents still stops every turn that drew on one, and a leading
    zero-citation turn on a share with no consented source still travels nowhere."""
    if not granted and not getattr(share, "shared_stores", None):
        return []
    out: list[dict] = []
    for i, t in enumerate(grantor_turns[:share.turn_cutoff]):
        if not set(t.cited_docs) <= granted:
            break
        # #689/#851: a turn drawing on a store the grantor did NOT tick stops the share, for
        # the same reason an ungranted document does and by the same mechanism rather than a
        # separate one. Read from the share ROW, so the consent that is checked is the one
        # recorded at mint time - not whatever the workspace happens to hold now.
        if _turn_blocks_share(t, share.shared_stores):
            break
        turn = {"seq": i, "question": t.question, "answer": t.answer, "own": False}
        # #620: citations ride along ONLY for a caller who brought a scope to resolve them
        # with. `docmeta is None` is the anonymous link reader (link_access.py, ADR 0021),
        # which has no account and therefore no scope - it keeps the response it had, byte
        # for byte, rather than gaining a field this card never designed for that audience.
        if docmeta is not None:
            turn["citations"] = _turn_citation_rows(t, docmeta)
        out.append(turn)
    return out


@app.get("/conversations/{conv_id}/transcript")
def conversation_transcript(conv_id: str, request: Request,
                            user: str = Depends(current_user)) -> dict:
    """Read a conversation: the owner reads her own thread, a live grantee reads the
    GRANTOR's thread and then her own continuation of it.

    WHY BOTH HALVES, and why "own" cannot simply mean "I have turns here": conversation
    history is keyed by (conv_id, user_oid), and the recipient of a share keeps asking her own
    questions INSIDE THE SAME conv_id - that is what makes the conv-scoped doorway open at all
    (`_request_scope`). So the moment she asks her first question she has turns of her own
    under this id too. Returning only the grantor's turns would hide her own half of the
    thread; returning only hers would make the shared transcript vanish the instant she used
    it. Each turn carries its own `own` flag so the surface can render the grantor's half
    read-only, and the top-level `own` says whether this caller is reading through a share.

    AFTER A REVOKE the grantor's half is gone from this response - `live_share_for` stops
    finding a share, so nothing looks it up any more - while the caller's OWN turns remain.
    That is deliberate and is the same direction `revoke_conversation_share` documents: the
    answers she was legitimately given do not get retracted, only her ability to see the
    grantor's thread and to keep asking from its documents.

    404 for everyone else - existence is the secret (the #549 rule), so a thread that is not
    yours and one that never existed answer identically.

    `history()` is unguarded here too (#596): a reader must never be told a thread is empty
    because the store is down."""
    conv_id = _conv_id(conv_id)
    turns: list[dict] = []
    share = _edition.conversation_shares.live_share_for(conv_id, user)
    # #620: ONE metadata map for both halves, built from the caller's PLAIN scope.
    #
    # NO `active_conv_id` HERE, DELIBERATELY, and this is the second thing tried rather than
    # the first. Passing it would fold this conversation's grants into principal expansion
    # (ADR 0020) so a grantee could resolve titles for the grantor's half - and that is
    # exactly CRITICAL-A: `_request_scope(request, user, <third arg>)` is the one greppable
    # seam through which a conv-scoped grant escapes its conversation, and only the two chat
    # routes may cross it (selftest_600_conv_scoped_grants pins the set, and says in words not
    # to relax it). A transcript is a read path, not a third conversational surface.
    #
    # The cost is that a document reached ONLY through somebody else's grant shows its id
    # instead of its title. That is already this product's rule, not a new degradation:
    # `/shareable` says the same thing about the same documents, for the same reason.
    scope = _request_scope(request, user)
    docmeta = {d.doc_external_id: (d.title, d.uri)
               for d in _edition.list_documents(user, scope)}
    try:
        if share is not None:
            # The documents this share ACTUALLY grants, read live. Scoped to grants this
            # share's grantor made, because conv_id is client-chosen and somebody else's
            # grant under a colliding id is not part of this share.
            granted = {g.doc_external_id
                       for g in _edition.grant_registry.live_grants_for(user)
                       if g.conv_id == conv_id and g.granted_by == share.grantor_oid}
            # `if granted else []` skips the history read entirely when the share opens
            # nothing - `_readable_prefix` re-states that rule as its own floor (#601 round 4,
            # NEW-3), so the two cannot disagree; this half only avoids a pointless store
            # round trip. Everything about WHICH turns travel - the `turn_cutoff` boundary and
            # the per-turn live-grant re-check, and why it stops rather than skips - lives in
            # `_readable_prefix`, which the anonymous link reader calls too (ADR 0021).
            # #851: read the grantor's half when the share opens ANYTHING - live document
            # grants, or sources she consented to. `_readable_prefix` re-states the same
            # condition as its own floor, so the two cannot disagree; this half only avoids a
            # pointless store round trip when the share opens nothing at all.
            theirs = (_edition.conversation_service.history(share.grantor_oid, conv_id)
                      if (granted or share.shared_stores) else [])
            turns.extend(_readable_prefix(share, theirs, granted, docmeta))
        # Read second and appended. With the boundary above this concatenation is also
        # genuinely CHRONOLOGICAL, which is worth stating because `Turn` carries no timestamp
        # and nothing else here could establish it: every grantor turn shown predates the
        # share, and the recipient could not have asked anything in this thread before the
        # share existed, so everything of hers follows all of it. `seq` is the position
        # within each half (the same order PgConversationStore's dense `seq` column stores),
        # not a global clock - the two halves are not comparable, they are consecutive.
        # (If this caller happened to own an unrelated conversation under the same
        # client-chosen id, the two would merge here - both halves are hers to read, so this
        # is confusing at worst and never a leak.)
        # #689: `rerun_user` HERE and only here - this is the caller's OWN half, so a proof
        # they saw live comes back re-runnable. The grantor's half above passes none: signing
        # a token for somebody else's turn would hand this reader a credential to execute a
        # query against a store they cannot see, which is gate #1 defeated by a transcript.
        turns += [{"seq": i, "question": t.question, "answer": t.answer, "own": True,
                   "citations": _turn_citation_rows(t, docmeta, rerun_user=user)}
                  for i, t in enumerate(_edition.conversation_service.history(user, conv_id))]
    except ConversationStoreUnavailable:
        raise HTTPException(status_code=503,
                            detail="cannot read the conversation right now, try again shortly")
    if not turns:
        raise HTTPException(status_code=404, detail="no such conversation")
    # #620: the corpus block the live answer already ships with (#393), so a reopened thread
    # renders the same honest footer instead of silently dropping it. Built from the scope
    # above, not a second derivation of it.
    #
    # NULL ON A SHARED THREAD, and that is the honest answer rather than a missing feature.
    # The footer's denominator is "documents YOU can access", and on a thread read through
    # somebody else's share the answers above were produced under a conv-scoped expansion
    # this route may not perform (see the scope comment). Counting under the narrower scope
    # would print a number that disagrees with the answers it sits under. `_corpus_block`
    # returning None is already the defined "cannot count" state: `provenanceNote` then
    # reports retrieval only and makes no entitlement claim at all, which is precisely true
    # here. An unmeasured number stated confidently is the #392/#393 bug class.
    return {"turns": turns, "own": share is None,
            "corpus": _corpus_block(user, request, scope=scope) if share is None else None}


@app.post("/developer/keys")
def create_key(req: CreateKeyRequest, user: str = Depends(current_user)) -> dict:
    label = req.label.strip()
    if not label or len(label) > 200:
        raise HTTPException(status_code=400, detail="label must be 1-200 chars")
    rec, token = _edition.create_api_key(user, label)
    return {"record": asdict(rec), "token": token}   # token shown once


@app.get("/developer/keys")
def list_keys(user: str = Depends(current_user)) -> list[dict]:
    return [asdict(r) for r in _edition.list_api_keys(user)]


@app.delete("/developer/keys/{key_id}")
def revoke_key(key_id: str, user: str = Depends(current_user)) -> dict:
    try:
        _edition.revoke_api_key(key_id, user)
    except KeyError:
        raise HTTPException(status_code=404, detail="key not found")
    return {"revoked": key_id}


@app.get("/developer/graphql-schema")
def graphql_schema() -> dict:
    return {"sdl": str(build_schema(_edition.query_service))}


# /router — Phase E live seam for the DB-canvas (#109); auth via the same header dep
# #805: kept as a module attribute so tests can reach api._workspace_pool (the pool is
# already exposed on the router for introspection; app.py just never kept the reference).
_router_api = build_router_api(_edition, current_user,
                                    current_user_demo_ok=current_user_demo_ok,
                                    subject_token_provider=_subject_provider,
                                    on_rotate=user_auth.VAULT.put,
                                    secrets=_secrets,
                                    manifest_store=_manifest_store,
                                    job_store=_job_store,
                                    # #439: the SAME ADR 0012 chokepoint /search and /graphql
                                    # use, so the router cannot drift from them on what a
                                    # caller's document partition is.
                                    tenant_resolver=_request_tenant)
app.include_router(_router_api)

# /secrets - the write-once credential seam (#319, ADR 0010 s3). Live-only (current_user
# 403s a demo:* identity); a demo visitor must never reach a credential store.
app.include_router(build_secrets_api(_secrets, _edition.tenant_id, current_user,
                                     unavailable_reason=_secrets_unavailable_reason))

# /c/{token} - the ANONYMOUS doorway (#605, ADR 0021). The only route family in this product
# that ANSWERS FROM CUSTOMER DOCUMENTS without `Depends(current_user)`.
#
# Say it that way and not "the only routes without current_user", which is simply false (#605
# review, Finding 2): public-infra routes and the demo-safe router reads are declared without it
# too. They are health probes, build ids, login hops, HTML shells and the demo catalog - nothing
# behind them is a customer's document. The exhaustive, checkable list of every route in that
# position is `PUBLIC_INFRA_PATHS`, `DEMO_SAFE_PATHS` and `ANONYMOUS_LINK_PATHS` in
# tests/selftest_demo_scope_boundary.py, which sweeps the live route table and fails on anything
# not in one of the three.
#
# NO COUNT IS WRITTEN HERE, on purpose (#605 review round 2). The round-1 version of this
# comment said "thirteen public-infra routes", and the number was wrong. A number in prose is a
# fact with nothing checking it - it was wrong on the day it was written and would have gone
# stale anyway at the next route added. The sizes are ASSERTED in that test file instead, so
# widening an unauthenticated allowlist cannot happen without a visible edit to a number that
# fails until somebody changes it deliberately.
#
# THE ABSENCE HERE IS THE DESIGN, not an oversight to be corrected later. `current_user` 401s
# when there is nothing to resolve, so every DATA route refuses an anonymous visitor before any
# authorization logic runs - which is why the six-surface probe (/search, "Your data", segment
# previews, /download, /ask/suggestions, /chat in an unrelated thread) holds for a link visitor
# with no per-route hardening whatsoever. The day something here mints a session cookie
# `current_user` accepts, all six reopen at once and nothing in the link mechanism would notice
# (ADR 0021, "What is NOT granted").
#
# Registered ABOVE the static and marketing-site mounts, like every other router: Starlette
# matches in registration order and the catch-all mount at the bottom of this file would
# otherwise swallow /c/... entirely.
app.include_router(build_link_access_api(_edition,
                                         html=_html,
                                         corpus_for_scope=_corpus_for_scope,
                                         readable_prefix=_readable_prefix,
                                         cookie_secure=_cookie_secure))

# The GraphQL API (same QueryService) at /graphql. INCLUDED as routes, never mounted (#432):
# a Mount's path compiles to `^/graphql(?P<path>/.*)$`, so the bare `/graphql` fell through to
# the catch-all StaticFiles Mount at the bottom of this file and answered 405 to every POST -
# the API was dead in production while `/graphql/` quietly worked.
# `current_user` is attached at the transport, not just inside the resolver: the resolver already
# refuses an absent/demo identity, but default-deny is a property of the route table (the
# demo-scope sweep enforces exactly this), and as a Mount this route was INVISIBLE to that sweep -
# so nothing checked it for as long as the surface was dead. Now /graphql fails closed at the door
# on the same dependency as /search, which is what #184 asked for in the first place.
app.include_router(build_router(_edition.query_service, default_tenant=_edition.tenant_id),
                   prefix="/graphql",
                   dependencies=[Depends(current_user)])

class _RevalidatingStatic(StaticFiles):
    """StaticFiles that forces revalidation instead of heuristic freshness (#313).

    StaticFiles sends ETag + Last-Modified but no Cache-Control. Absent an explicit
    directive a browser is free to apply HEURISTIC freshness — roughly a tenth of the age
    of the resource — and serve a cached copy WITHOUT asking us. So a deploy could leave a
    returning browser running old ES modules against a fresh shell: /chat rendered the
    landing page because it still had a pre-#309 login.js, and only a hard reload fixed it.

    #261/#265 solved exactly this for the HTML shells (no-cache plus an embedded build id
    the page checks against /version). The comment there claimed assets were fine because
    "StaticFiles already sends ETag + Last-Modified and revalidates on its own" — it sends
    the validators, but nothing compels the browser to use them.

    no-cache does not mean "do not store": the copy is still cached, the browser just has
    to revalidate, and an unchanged file costs one 304 with no body."""

    # #415: no-cache was still not enough, because it never reached the browser.
    # Measured through the CDN in front of this box:
    #     GET /ask                -> cache-control: no-cache        (passed through)
    #     GET /version            -> cache-control: no-store        (passed through)
    #     GET /static/js/main.js  -> cache-control: max-age=14400   (REWRITTEN)
    # The edge rewrites only what it classifies as a static asset, and applies its
    # own browser TTL there. So the shell was always fresh while its modules were
    # up to four hours stale - and #413, which moved the navigation out of the HTML
    # and into main.js, turned that from a cosmetic mismatch into a shell with NO
    # NAVIGATION AT ALL for anyone with a warm cache. That is what Malcolm hit.
    #
    # no-store demonstrably survives the same edge, so code assets use it: the HTML
    # and the modules it depends on can no longer disagree. Fonts and images keep
    # revalidation, since they are immutable in practice and worth caching.
    #
    # This is a correctness floor, not the end state. Content-hashed asset URLs
    # would restore caching without reintroducing the skew (#402).
    _NO_STORE_SUFFIXES = (".js", ".css")

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        path = str(args[0] if args else kwargs.get("full_path", ""))
        if path.endswith(self._NO_STORE_SUFFIXES):
            resp.headers["Cache-Control"] = "no-store"
        else:
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


# Serve the no-build ES-module app assets. Mounted last so it never shadows API routes.
app.mount("/static", _RevalidatingStatic(directory=str(_STATIC_DIR)), name="static")

# The exported marketing site (#401), mounted DEAD LAST so every API route, shell
# path and asset mount above it wins. Starlette matches routes in registration
# order, so this can only ever catch what nothing else claimed.
#
# `html=True` makes "/product" resolve to "product/index.html", which is why the
# export sets `trailingSlash: true`. The mount also serves the RSC payloads and
# the favicon that Next's client router asks for during navigation.
#
# Only mounted when a build exists on disk: a self-hoster runs this same code with
# no exported site, and gets the app's own landing from the "/" route above.
if _SITE_DIR.is_dir():
    app.mount("/", _RevalidatingStatic(directory=str(_SITE_DIR), html=True),
              name="marketing_site")
