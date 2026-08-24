"""#368: which fields are credentials, per kind - the server-side twin of the `secret:true`
markers in the canvas's KINDS table (static/js/surfaces/canvas.js) (the canvas renders the panel from its copy; compose
enforces from this one). Covers BOTH halves of a store entry: `config:` (engine connection
detail) and `delegation:` (identity wiring, ADR 0006).

The guard closes the enforcement gap ADR 0010 named and never got: with manifests now
resting server-side (manifest_store.py), a plaintext password in a secret-typed field
would become a durable secret in compute state (LAW 6). Refusing it at compose turns
"user pasted a password into the wrong box" from a silent durable leak into a 400 that
points at the credential panel.
"""
from __future__ import annotations

from dbsearch.router.secret_handles import is_handle

SECRET_FIELDS: dict = {
    "azure_sql": frozenset({"password"}),
    "postgres": frozenset({"password"}),
    "mysql": frozenset({"password"}),
    # #672: the RDS aliases are separate KINDS, and this guard is keyed by kind - so a new
    # kind is UNPROTECTED until it is listed here. Omitting them would let a plaintext
    # password ride into a durable manifest (LAW 6) through a door that looks identical to
    # the guarded one. selftest_672 asserts every registered SQL kind has an entry.
    "rds_postgres": frozenset({"password"}),
    "rds_mysql": frozenset({"password"}),
    "synapse": frozenset({"password"}),
    "cosmos_db": frozenset({"key"}),
    "redshift": frozenset({"key"}),
    "databricks": frozenset({"token"}),
    "graph_search": frozenset({"token"}),
}

# #368 final review (IMPORTANT 2): a `delegation:` block carries FIRST-CLASS credentials, and
# the guard used to look only at `config:` - so `delegation: {kind: entra_refresh,
# client_secret: "<literal>"}` sailed past the 400 and was written verbatim into
# user_manifests.manifest as plaintext, defeating this branch's whole claim that the only
# durable credential form at rest is a scoped secret:// handle.
#
# Keyed by DELEGATION kind (not store kind): the block's own `kind:` selects the exchange, and
# `identity_broker.exchange_from_config` is the authority on which field each one reads.
# provisioning.register_delegations already threads `secrets=` into resolve_env for these
# blocks, which is the codebase's own statement that these fields may legally be secret://
# handles - and therefore may illegally be literals.
#
# gcp_wif (audience, service_account) and aws_sts (role_arn) are deliberately absent: those
# are resource IDENTIFIERS, not credentials - WIF and STS federate from an Entra assertion
# minted at request time, so there is no long-lived secret in the block to protect.
# aws_keys (ADR 0024) is absent for the same conclusion via a different route: the block
# carries NO fields at all - the caller's access keys live per-account in the vault, written
# only through /auth/aws/connect, never through a manifest.
DELEGATION_SECRET_FIELDS: dict = {
    "entra_obo": frozenset({"client_secret"}),
    "entra_refresh": frozenset({"client_secret"}),
    "google_refresh": frozenset({"client_secret"}),
    # `static` is a fixed bearer token or a resource->token MAP; both are raw credentials.
    "static": frozenset({"token", "tokens"}),
}

# An unrecognised delegation kind gets the UNION, so a typo or a kind newer than this map
# cannot be used as a bypass. Such a block also fails compose outright (exchange_from_config
# raises on an unknown kind, which the endpoint turns into a 400), so this only ever changes
# WHICH 400 the caller sees - never whether the manifest can be stored.
_ANY_DELEGATION_SECRET = frozenset().union(*DELEGATION_SECRET_FIELDS.values())


def _is_env_ref(value: str) -> bool:
    return value.startswith("${") and value.endswith("}")


def _is_plaintext(value) -> bool:
    """A secret field is legal ONLY if it is None, an empty string, a `${ENV}` ref, or a
    `secret://` handle. Every other value - including a non-string type such as an int or
    bool - is treated as a plaintext credential. A bare numeric or boolean secret (e.g. an
    unquoted YAML/JSON scalar) is exactly the kind of plaintext this guard exists to catch,
    so type-narrowing to `str` before flagging would reopen the hole."""
    if value is None:
        return False
    if isinstance(value, str):
        return not (not value or _is_env_ref(value) or is_handle(value))
    return True


def _has_plaintext(value) -> bool:
    """`_is_plaintext`, applied to every LEAF of a container. Needed for the delegation
    `tokens` map (resolve_env recurses into dicts/lists, so `tokens: {sql: "secret://..."}`
    is a legal authoring form and must pass, while any literal leaf in it must not)."""
    if isinstance(value, dict):
        return any(_has_plaintext(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_plaintext(v) for v in value)
    return _is_plaintext(value)


def find_secret_literals(manifest: dict) -> list:
    """Every secret-typed field in `manifest` carrying a bare literal, across `config:` and
    `delegation:`. Returns store and field names ONLY - never the value (LAW 1: the caller
    builds an error detail from this, and that detail must not be able to echo a credential).

    Delegation fields are reported as `delegation.<field>` so one flat list can name both
    halves unambiguously ("pg-1.password" vs "pg-1.delegation.client_secret")."""
    bad = []
    for entry in manifest.get("stores", []):
        store_id = entry.get("id", "?")
        fields = SECRET_FIELDS.get(entry.get("kind", ""), frozenset())
        config = entry.get("config")
        if not isinstance(config, dict):
            config = {}
        for field in sorted(fields):        # sorted: a stable error detail across runs
            if field in config and _is_plaintext(config[field]):
                bad.append({"store_id": store_id, "field": field})
        block = entry.get("delegation")
        if not isinstance(block, dict):
            # A non-dict delegation is a manifest error compose will refuse; nothing here to
            # inspect, and this guard must never be the thing that crashes on bad input.
            continue
        dfields = DELEGATION_SECRET_FIELDS.get(block.get("kind", ""),
                                               _ANY_DELEGATION_SECRET)
        for field in sorted(dfields):
            if field in block and _has_plaintext(block[field]):
                bad.append({"store_id": store_id, "field": f"delegation.{field}"})
    return bad
