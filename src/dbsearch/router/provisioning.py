"""Provisioning — assemble a StoreCatalog from a declarative manifest (Phase E, card #98).

The 'compose for databases' loader: one spec -> the whole federated catalog. E1 consumes an
already-parsed dict (YAML parsing + `dbsearch connect` CLI arrive in E9). Secrets are given
as "${NAME}" and resolved from the environment — never inlined (12-factor).

Hierarchy built per store: tenant -> business_unit -> source(implicit) -> store. Ancestor
ACLs are the UNION of descendant store ACLs, so anyone who can see a store can see its
ancestors, while a business unit with no viewable store stays hidden — gate #1 intact.
"""
from __future__ import annotations

import logging
import os

from dbsearch.router.catalog import (
    BUSINESS_UNIT, CatalogNode, SOURCE, STORE, StoreCatalog, TENANT,
)
from dbsearch.router.identity_broker import IdentityBroker, exchange_from_config
from dbsearch.router.origins import origin_for
from dbsearch.router.provider import ProviderRegistry, get_provider
from dbsearch.router.secret_handles import is_handle


def resolve_env(obj, env: dict | None = None, secrets=None):
    """Resolve a manifest value's three legal forms (ADR 0010 s2): a literal, `${ENV_NAME}`
    from the server environment, or `secret://<handle>` through a caller-scoped resolver.

    `secrets` is a ScopedSecretResolver bound to the REQUESTING tenant+owner, or None on the
    operator-configured paths that have no self-serve context. A handle with no resolver
    raises rather than passing the literal string through: "secret://acme/..." handed to a
    database driver as a password is a confusing failure far from its cause, and in the worst
    case an attempted connection with an attacker-visible string.
    """
    env = os.environ if env is None else env
    if isinstance(obj, str):
        if is_handle(obj):
            if secrets is None:
                raise KeyError(
                    f"manifest references the secret handle {obj!r} but no secret resolver is "
                    "wired for this request (self-serve credentials need a signed-in owner)")
            return secrets.resolve(obj)
        if obj.startswith("${") and obj.endswith("}"):
            name = obj[2:-1]
            if name not in env:
                raise KeyError(f"manifest references unset env var {name!r}")
            return env[name]
        return obj
    if isinstance(obj, dict):
        return {k: resolve_env(v, env, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_env(v, env, secrets) for v in obj]
    return obj


def _server_resolved(obj) -> bool:
    """True when any value here is one the SERVER resolves - a `${ENV}` ref or a
    `secret://` handle - as opposed to a literal the caller typed themselves."""
    if isinstance(obj, str):
        return is_handle(obj) or (obj.startswith("${") and obj.endswith("}"))
    if isinstance(obj, dict):
        return any(_server_resolved(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_server_resolved(v) for v in obj)
    return False


def _skip_reason(entry: dict, exc: Exception) -> str:
    """The `skipped` reason for a failed store - sanitized when the entry references
    anything the server resolved for it.

    A driver's exception text routinely quotes the connection values it was handed. For a
    `${ENV}` ref or a `secret://` handle those values are RESOLVED server-side secrets, so
    echoing the raw string handed the caller the credential itself: a store whose `host` was
    `${AUTH_CLIENT_SECRET}` came back with the client secret verbatim in its reason. The
    same channel doubled as a presence oracle for the server's environment.

    So an entry that references a resolved value gets the exception CLASS and a generic
    sentence; the full text goes to the server log, where it is still exactly as debuggable
    as before. An entry made only of the caller's own literals keeps the detailed message -
    it is their data, and it is what makes a typo'd hostname fixable without a log grep."""
    # The WHOLE entry, not `config:`/`delegation:` alone: `id`, `business_unit`, `title` and
    # `description` go through `resolve_env` too (see `load_manifest` below), so a resolved
    # value can reach an exception message from any of them.
    if not _server_resolved(entry):
        return f"build/probe failed: {exc}"
    logging.getLogger("dbsearch").warning(
        "store %s (kind %s) failed to build/probe: %s",
        entry.get("id", "?"), entry.get("kind", "?"), exc, exc_info=True)
    return (f"build/probe failed: {type(exc).__name__} - details withheld because this "
            "store's config references a server-resolved value; see the server log")


def load_manifest(spec: dict, registry: ProviderRegistry | None = None,
                  skipped: "list | None" = None, secrets=None,
                  credential_for=None) -> StoreCatalog:
    """`skipped`: pass a list to make per-store build/probe failures non-fatal (#107) —
    each failing store is appended as {id, kind, reason} and the REST of the manifest
    composes (one miscredentialed cloud store never takes down the catalog). Without
    it the first failure raises (strict default — unchanged E1 behavior).

    `secrets`: a ScopedSecretResolver bound to the requesting tenant+owner (ADR 0010 s2),
    forwarded to every `resolve_env` call so `secret://` handles in `config:` resolve to
    that caller's own credentials. None on operator-configured paths (unchanged default).

    `credential_for` (#665, ADR 0022): entry -> the OWNER's delegated access token, or None.
    Supplied by the server layer, which is where identity and the vault live; this layer
    stays credential-agnostic and never learns which cloud the token belongs to.

    Why compose needs it at all: ADR 0022 moved schema introspection onto the caller's
    credential, and #656 threaded it into /router/probe and /router/health - but NOT here.
    Compose is the path that PERSISTS a store, so the one flow that matters most was still
    introspecting with the server's ADC: a correctly-configured BigQuery store health-checked
    green and then vanished from the catalog the moment the user pressed Compose up."""
    get = registry.get if registry is not None else get_provider
    cat = StoreCatalog()
    tenant_id = spec["tenant"]

    # 1) collect entries + derive ancestor ACLs (union of descendant store ACLs)
    entries = spec.get("stores", [])
    bu_acl: dict[str, set[str]] = {}
    tenant_acl: set[str] = set(spec.get("tenant_acl", []))
    for e in entries:
        acl = set(e.get("acl", []))
        bu_acl.setdefault(e["business_unit"], set()).update(acl)
        tenant_acl.update(acl)

    # ids share ONE namespace (#114): reject collisions BEFORE any provider builds —
    # a colliding store id used to silently replace its BU node and cycle the catalog.
    ids = [tenant_id] + list(bu_acl)
    for e in entries:
        ids += [e["id"], f"{e['id']}--src"]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"manifest id collision across tenant/BU/source/store ids: {dupes}")

    # 2) tenant + business-unit scaffold
    cat.register(CatalogNode(tenant_id, TENANT, None, acl=sorted(tenant_acl)))
    for bu, acl in bu_acl.items():
        cat.register(CatalogNode(bu, BUSINESS_UNIT, tenant_id, acl=sorted(acl)))

    # 3) each store: build via its provider, register under an implicit source node.
    # `mode` (ADR 0008, #111) selects among providers for the same kind (e.g. sharepoint
    # native vs index); omitted -> the kind's default. Probe AFTER build: probes are
    # side-effect-free, and a stateful provider (the connector rail) reports honest
    # post-ingest freshness only once the store exists.
    for e in entries:
        provider = get(e["kind"], e.get("mode"))
        try:
            # #551: `acl` rides along so a connector can inherit the store's audience. The
            # folder connector needs it: its per-document ACL comes from the immediate
            # sub-directory name, and files sitting directly in the root are SKIPPED by
            # default-deny — so pointing it at an ordinary flat folder indexed NOTHING and
            # reported success. A silently empty store is the worst failure shape available.
            config = resolve_env({"id": e["id"], "business_unit": e["business_unit"],
                                  "title": e.get("title", e["id"]),
                                  "description": e.get("description", ""),
                                  "acl": e.get("acl", []),
                                  **e.get("config", {})}, secrets=secrets)
            # #665: the owner's delegated credential, when one can be minted. A FAILURE TO
            # MINT MUST NOT SKIP THE STORE - it falls back to the server identity, which is
            # the behaviour that existed before this parameter did.
            #
            # That is not defensiveness, it is the product's contract: a delegated store
            # belonging to a user who has not linked that cloud STILL COMPOSES, so the ask
            # can answer "connect Google to query this source" through the executor's
            # drop+disclose channel. Skipping it instead removes it from the catalog, and the
            # user gets "No data sources are connected yet" - a true sentence about the
            # catalog and a useless one about their problem.
            # selftest_workspace_isolation's health-agrees-with-ask case pins exactly this,
            # and caught the first version of this change doing the wrong thing.
            cred = None
            mint_error: "BaseException | None" = None
            if credential_for is not None:
                try:
                    cred = credential_for(e)
                except Exception as exc:      # NotSignedIn, a dead grant, a down IdP
                    mint_error = exc
                    logging.getLogger("dbsearch").info(
                        "no delegated credential for %s; introspecting with the server "
                        "identity instead (%s)", e["id"], type(exc).__name__)
            # #676: falling back to the server identity is right for a store that declared NO
            # delegation - that is the self-host topology, where the operator IS the
            # credential owner (ADR 0024 consequence 5). It is WRONG for a store that
            # declared one and whose caller simply has not linked that cloud: the fallback
            # then reads the deployment's data on behalf of somebody who never presented an
            # identity. A provider that can tell the two apart says so here; one that cannot
            # is unchanged, because the SQL rails are refused again by the broker at query
            # time and this rail is the one with no second chance.
            build_unavailable = getattr(provider, "build_unavailable", None)
            build_as = getattr(provider, "build_as", None)
            if mint_error is not None and e.get("delegation") and build_unavailable is not None:
                store = build_unavailable(config, mint_error)
            else:
                store = (build_as(config, credential=cred)
                         if build_as is not None and cred else provider.build(config))
            # #306: route on the BUILT store's profile, not the cheap pre-build probe() profile.
            # The built store reflects real content — a document store's ingested doc-title topics
            # and a SQL store's live schema — which is exactly the signal the router ranks on
            # (#296). probe() omits them, so an undescribed doc store fell below unrelated stores.
            profile = store.profile()
        except PermissionError:
            # Task 5 policy (ADR 0010 s3): a foreign secret handle in `config:` is an
            # AUTHORIZATION failure, not a "this store's credentials are bad" failure. Folding
            # it into `skipped` alongside every other build/probe error would make a
            # cross-tenant handle attempt indistinguishable from a miscredentialed store, and
            # the REST layer would answer 200 with the offending store quietly missing instead
            # of refusing the request. So this propagates uncaught even when `skipped` is set,
            # for the endpoint above to turn into a 403 - never absorbed, never a 500.
            raise
        except Exception as exc:
            if skipped is None:
                raise
            skipped.append({"id": e["id"], "kind": e["kind"],
                            "reason": _skip_reason(e, exc)})
            continue
        profile.origin = origin_for(e["kind"], config, config.get("title", e["id"]))
        src_id = f"{e['id']}--src"
        cat.register(CatalogNode(src_id, SOURCE, e["business_unit"], acl=list(e.get("acl", []))))
        node = CatalogNode(e["id"], STORE, src_id, acl=list(e.get("acl", [])),
                           profile=profile, store=store)
        cat.register(node)
        _refresh_profile_when_content_lands(node)
    return cat


def _refresh_profile_when_content_lands(node) -> None:
    """Re-derive a store's routing profile when its background crawl finishes (#454).

    The snapshot taken above is what the ROUTER ranks on — `profiles.py` even memoizes its
    embedding on the object. That was harmless while `build()` ingested inline, because the
    snapshot was taken over a full index. Now that the initial crawl is submitted rather than
    run (ADR 0016 §1), the snapshot is taken over an EMPTY one, and a document store's topics
    come from its ingested doc titles (#306/#453) — so a freshly connected library would be
    correctly indexed, correctly permission-trimmed, and still unroutable, because the router
    had memoized a vector for a store that looked empty. It would answer questions only if
    the user named the store explicitly, and recompose would be the only cure.

    Replacing the whole profile (rather than mutating fields) is deliberate: `profile_vector`
    is a cache keyed by nothing, so a fresh object is the only way to guarantee it is
    re-embedded from the new topics. `origin` is carried over because it is compose-time
    knowledge the store itself does not have."""
    subscribe = getattr(node.store, "on_ingest_complete", None)
    if subscribe is None:
        return                              # not a connector-backed store: nothing to await

    def _refresh() -> None:
        fresh = node.store.profile()
        fresh.origin = node.profile.origin if node.profile is not None else None
        node.profile = fresh

    subscribe(_refresh)


def register_delegations(spec: dict, broker: IdentityBroker,
                         subject_token_provider, *, on_rotate=None, secrets=None) -> None:
    """Register every store entry's optional `delegation:` block on the broker
    (ADR 0006). The block is a SIBLING of `config:` — identity wiring, not engine
    connection detail — and its secrets take ${ENV} refs, or `secret://` handles
    (ADR 0010 s2) resolved through `secrets` when one is wired, like everything else.
    Call before serving asks; pair with broker.reset() on recompose.

    #193: which cloud a kind needs, and the refusal to hand it another cloud's credential,
    live in `identity_broker._for_idp` — the binding site. This function only adds the store
    id to the error, so a manifest with a bad delegation names the store that fails and no
    delegation is partially registered (the raise happens before register_delegation)."""
    for e in spec.get("stores", []):
        block = e.get("delegation")
        if not block:
            continue
        try:
            exchange, resource = exchange_from_config(
                resolve_env(dict(block), secrets=secrets), subject_token_provider,
                on_rotate=on_rotate)
        except ValueError as exc:
            raise ValueError(f"store {e['id']!r}: {exc}") from exc
        # Task 5 policy (same as `load_manifest` above): a foreign secret handle in a
        # `delegation:` block raises PermissionError, deliberately NOT caught here. It is
        # not a ValueError-shaped config mistake, so wrapping it with the store id would put
        # attacker-controlled context (the store name) next to an authorization failure for
        # no benefit; the caller turns an uncaught PermissionError into a 403 instead.
        broker.register_delegation(e["id"], exchange, resource)
