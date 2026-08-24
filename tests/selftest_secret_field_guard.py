"""#368 / ADR 0010 s2 enforcement: a manifest that will rest server-side must never carry
a plaintext credential. The guard names the field, never the value (LAW 1).

    PYTHONPATH=src python3 tests/selftest_secret_field_guard.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.secret_fields import (  # noqa: E402
    DELEGATION_SECRET_FIELDS, SECRET_FIELDS, find_secret_literals,
)


def _manifest(kind, config):
    return {"tenant": "acme", "stores": [
        {"id": "s1", "kind": kind, "business_unit": "eng", "acl": ["a"], "config": config}]}


def _delegated(delegation, kind="azure_sql", config=None):
    return {"tenant": "acme", "stores": [
        {"id": "s1", "kind": kind, "business_unit": "eng", "acl": ["a"],
         "config": config or {"server": "${AZURE_SQL_SERVER}"},
         "delegation": delegation}]}


def test_plaintext_password_is_flagged():
    bad = find_secret_literals(_manifest("postgres", {"host": "db.example.com",
                                                      "password": "hunter2"}))
    assert bad == [{"store_id": "s1", "field": "password"}], bad
    assert all("hunter2" not in str(v) for b in bad for v in b.values()), \
        "THE GUARD MUST NEVER CARRY THE VALUE"
    print("  PASS  a plaintext password is flagged by store and field, value not echoed")


def test_legal_forms_pass():
    for value in ("${AZURE_PG_PASSWORD}", "secret://acme/a/s1/password", ""):
        assert find_secret_literals(_manifest("postgres", {"password": value})) == [], value
    print("  PASS  env ref, secret handle, and empty all pass")


def test_non_secret_literals_pass():
    assert find_secret_literals(_manifest("postgres",
                                          {"host": "db.example.com", "user": "app"})) == []
    print("  PASS  literals in non-secret fields (host, user) pass")


def test_every_pushdown_kind_has_secret_fields():
    for kind in ("azure_sql", "postgres", "mysql", "synapse", "cosmos_db",
                 "redshift", "graph_search", "databricks"):
        assert kind in SECRET_FIELDS and SECRET_FIELDS[kind], kind
    print("  PASS  every credentialed kind declares its secret fields")


def test_unknown_kind_passes():
    assert find_secret_literals(_manifest("local", {"description": "notes"})) == []
    print("  PASS  a kind with no secret fields is untouched")


def test_non_string_secret_values_are_flagged():
    for value in (12345, True, {"nested": "dict"}):
        bad = find_secret_literals(_manifest("postgres", {"password": value}))
        assert bad == [{"store_id": "s1", "field": "password"}], (value, bad)
    print("  PASS  int, bool, and nested-dict secret values are flagged (not just str)")


def test_none_and_absent_secret_field_pass():
    assert find_secret_literals(_manifest("postgres", {"password": None})) == []
    assert find_secret_literals(_manifest("postgres", {"host": "db.example.com"})) == []
    print("  PASS  None and an absent secret field both pass")


def test_malformed_config_does_not_crash():
    for config in ([], "not-a-dict", 42):
        manifest = {"tenant": "acme", "stores": [
            {"id": "s1", "kind": "postgres", "business_unit": "eng", "acl": ["a"],
             "config": config}]}
        assert find_secret_literals(manifest) == [], config
    print("  PASS  a malformed (non-dict) config is tolerated, not a crash")


def test_delegation_client_secret_literal_is_flagged():
    """#368 final review (IMPORTANT 2). The guard only inspected `config:`, so a plaintext
    `client_secret` in a `delegation:` block passed the 400 and was written verbatim into
    user_manifests.manifest - defeating the branch's claim that the only durable credential
    form at rest is a scoped secret:// handle."""
    for kind in ("entra_obo", "entra_refresh", "google_refresh"):
        bad = find_secret_literals(_delegated(
            {"kind": kind, "tenant_id": "${AUTH_TENANT_ID}", "client_id": "${AUTH_CLIENT_ID}",
             "client_secret": "PLAINTEXT-app-secret"}))
        assert bad == [{"store_id": "s1", "field": "delegation.client_secret"}], (kind, bad)
        assert all("PLAINTEXT" not in str(v) for b in bad for v in b.values()), \
            "THE GUARD MUST NEVER CARRY THE VALUE"
    print("  PASS  a plaintext delegation client_secret is flagged for every entra/google kind")


def test_delegation_legal_forms_pass():
    for value in ("${AUTH_CLIENT_SECRET}", "secret://acme/a/s1/client_secret", "", None):
        bad = find_secret_literals(_delegated(
            {"kind": "entra_refresh", "tenant_id": "${AUTH_TENANT_ID}",
             "client_id": "${AUTH_CLIENT_ID}", "client_secret": value}))
        assert bad == [], (value, bad)
    # ...and the canvas's own authored block (delegationFor) must pass unchanged
    assert find_secret_literals(_delegated(
        {"kind": "entra_refresh", "tenant_id": "${AUTH_TENANT_ID}",
         "client_id": "${AUTH_CLIENT_ID}", "client_secret": "${AUTH_CLIENT_SECRET}"})) == []
    print("  PASS  env ref, secret handle, empty and absent all pass in a delegation block")


def test_static_delegation_token_and_token_map():
    """`kind: static` carries a raw bearer token, or a resource->token MAP. resolve_env
    recurses into that map, so a map of handles is a legal authoring form - but any literal
    leaf inside it is still a plaintext credential at rest."""
    assert find_secret_literals(_delegated({"kind": "static", "token": "raw-bearer"})) == \
        [{"store_id": "s1", "field": "delegation.token"}]
    assert find_secret_literals(_delegated(
        {"kind": "static", "token": "secret://acme/a/s1/token"})) == []
    assert find_secret_literals(_delegated(
        {"kind": "static", "tokens": {"sql": "secret://acme/a/s1/token"}})) == []
    assert find_secret_literals(_delegated(
        {"kind": "static", "tokens": {"sql": "secret://acme/a/s1/token",
                                      "graph": "raw-bearer"}})) == \
        [{"store_id": "s1", "field": "delegation.tokens"}], "a literal leaf must be caught"
    print("  PASS  static delegation: a raw token and a literal leaf in a token map are both "
          "flagged, handles pass")


def test_identifier_only_delegation_kinds_are_not_flagged():
    """gcp_wif and aws_sts federate from a request-time assertion: their fields are resource
    identifiers, not credentials, so a literal there is correct and must NOT 400."""
    assert find_secret_literals(_delegated(
        {"kind": "gcp_wif", "audience": "//iam.googleapis.com/projects/1/locations/global",
         "service_account": "svc@proj.iam.gserviceaccount.com"}, kind="bigquery")) == []
    assert find_secret_literals(_delegated(
        {"kind": "aws_sts", "role_arn": "arn:aws:iam::1:role/r"}, kind="redshift")) == []
    print("  PASS  gcp_wif / aws_sts identifiers are not treated as credentials")


def test_unknown_delegation_kind_cannot_be_used_as_a_bypass():
    bad = find_secret_literals(_delegated(
        {"kind": "brand_new_idp", "client_secret": "PLAINTEXT-app-secret"}))
    assert bad == [{"store_id": "s1", "field": "delegation.client_secret"}], bad
    print("  PASS  an unknown delegation kind falls back to the union of secret fields")


def test_malformed_delegation_does_not_crash():
    for block in ("not-a-dict", [], 42, None):
        assert find_secret_literals(_delegated(block)) == [], block
    print("  PASS  a malformed (non-dict) delegation block is tolerated, not a crash")


def test_config_and_delegation_offenders_are_both_reported():
    bad = find_secret_literals(_delegated(
        {"kind": "entra_refresh", "tenant_id": "${AUTH_TENANT_ID}",
         "client_id": "${AUTH_CLIENT_ID}", "client_secret": "PLAINTEXT-app-secret"},
        config={"server": "${AZURE_SQL_SERVER}", "password": "hunter2"}))
    assert bad == [{"store_id": "s1", "field": "password"},
                   {"store_id": "s1", "field": "delegation.client_secret"}], bad
    print("  PASS  both halves of an entry are reported in one pass")


def test_every_credentialed_delegation_kind_declares_its_fields():
    """The exchange kinds identity_broker.exchange_from_config knows, minus the two that hold
    no long-lived secret. A new credentialed kind must land in the map, or it is a hole."""
    for kind in ("entra_obo", "entra_refresh", "google_refresh", "static"):
        assert kind in DELEGATION_SECRET_FIELDS and DELEGATION_SECRET_FIELDS[kind], kind
    print("  PASS  every credentialed delegation kind declares its secret fields")


if __name__ == "__main__":
    test_plaintext_password_is_flagged()
    test_legal_forms_pass()
    test_non_secret_literals_pass()
    test_every_pushdown_kind_has_secret_fields()
    test_unknown_kind_passes()
    test_non_string_secret_values_are_flagged()
    test_none_and_absent_secret_field_pass()
    test_malformed_config_does_not_crash()
    test_delegation_client_secret_literal_is_flagged()
    test_delegation_legal_forms_pass()
    test_static_delegation_token_and_token_map()
    test_identifier_only_delegation_kinds_are_not_flagged()
    test_unknown_delegation_kind_cannot_be_used_as_a_bypass()
    test_malformed_delegation_does_not_crash()
    test_config_and_delegation_offenders_are_both_reported()
    test_every_credentialed_delegation_kind_declares_its_fields()
    print("OK selftest_secret_field_guard")
