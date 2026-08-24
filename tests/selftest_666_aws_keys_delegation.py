"""#666 / ADR 0024 - AWS as a delegated data source: the caller's own vaulted access keys.

The wall this pins: the only AWS delegation kind (`aws_sts`, AssumeRoleWithWebIdentity)
binds the ENTRA subject provider, and on the hosted deployment that provider returns a
vaulted refresh token - not a JWT - so it can never redeem. The canvas offered a Redshift
tile above it, with a panel whose fields (cluster/key) the engine does not even read.
An offer with nothing behind it, twice over (the #646/#652/#654/#656 shape).

`aws_keys` redeems the caller's OWN vaulted access keys via STS GetSessionToken into the
temporary triple RedshiftEngine already parses. No role hop exists: two callers are two
IAM principals end to end, and the source enforces per user (LAW 2).

    PYTHONPATH=src python3 tests/selftest_666_aws_keys_delegation.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.identity_broker import (  # noqa: E402
    AwsKeysExchange, exchange_from_config,
)
from dbsearch.router.providers.redshift import RedshiftEngine, RedshiftProvider  # noqa: E402

CONFIG = {"id": "rs-1", "workgroup": "wg", "database": "db", "acl": ["alice"]}

_SCHEMA_RECORDS = [
    [{"stringValue": "public"}, {"stringValue": "orders"},
     {"stringValue": "sku"}, {"stringValue": "varchar"}],
]


class FakeSts:
    """Records which keys asked, answers with a triple derived from them - so a test can
    tell WHOSE keys were redeemed rather than merely that a redemption happened."""

    calls: list = []

    def __init__(self, keys):
        self._keys = keys

    def get_session_token(self, DurationSeconds=None):
        FakeSts.calls.append((self._keys["access_key_id"], DurationSeconds))
        tag = self._keys["access_key_id"]
        return {"Credentials": {"AccessKeyId": f"ASIA-{tag}",
                                "SecretAccessKey": f"tmp-{tag}",
                                "SessionToken": f"session-{tag}"}}


def _keys_json(tag: str) -> str:
    return json.dumps({"access_key_id": f"AKIA-{tag}", "secret_access_key": f"sec-{tag}"})


def test_the_exchange_redeems_the_vaulted_keys_into_the_engines_triple():
    """The contract with RedshiftEngine._default_user_client: exactly these three fields."""
    FakeSts.calls = []
    x = AwsKeysExchange(lambda oid: _keys_json(oid), sts_client_factory=FakeSts)
    cred = json.loads(x.exchange("alice", "redshift"))
    assert set(cred) == {"access_key_id", "secret_access_key", "session_token"}, cred
    assert cred["session_token"] == "session-AKIA-alice", cred


def test_two_callers_redeem_two_different_key_sets():
    """LAW 2's shape here: no shared principal exists to collapse into. Each caller's
    exchange must go out under THAT caller's own keys."""
    FakeSts.calls = []
    x = AwsKeysExchange(lambda oid: _keys_json(oid), sts_client_factory=FakeSts)
    a = json.loads(x.exchange("alice", "redshift"))
    b = json.loads(x.exchange("bob", "redshift"))
    assert [c[0] for c in FakeSts.calls] == ["AKIA-alice", "AKIA-bob"], FakeSts.calls
    assert a["session_token"] != b["session_token"], "two callers shared one session"


def test_the_exchange_caches_per_caller():
    """One STS round-trip per (user, resource) within the ttl - ADR 0006's amortization."""
    FakeSts.calls = []
    x = AwsKeysExchange(lambda oid: _keys_json(oid), sts_client_factory=FakeSts)
    x.exchange("alice", "redshift")
    x.exchange("alice", "redshift")
    assert len(FakeSts.calls) == 1, FakeSts.calls


def test_aws_keys_binds_the_aws_vault_slot_never_another_clouds():
    """The `_for_idp` guard, exercised for the new kind: a multi-IdP subject provider must
    be asked for idp='aws' - a mis-binding would redeem one cloud's credential against
    another cloud's endpoint, which is a leak, not a failed query."""
    asked = []

    def provider(oid, idp="entra"):
        asked.append(idp)
        return _keys_json(oid)

    x, resource = exchange_from_config({"kind": "aws_keys"}, provider)
    assert resource == "redshift", resource
    x._sts_factory = FakeSts          # never let the default factory import boto3 here
    x.exchange("alice", "redshift")
    assert asked == ["aws"], asked


def test_the_legacy_entra_seam_cannot_supply_an_aws_credential():
    """The 1-arg env dev seam holds an ENTRA assertion by construction; binding it to aws
    would hand an Entra credential to STS. Refused at the binding site, like google."""
    try:
        exchange_from_config({"kind": "aws_keys"}, lambda oid: "an-entra-jwt")
        raise AssertionError("expected the binding refusal")
    except ValueError as e:
        assert "aws" in str(e), e


def test_an_unknown_kind_error_names_aws_keys():
    """The known-kinds list in the error is how a typo gets diagnosed; it must be current."""
    try:
        exchange_from_config({"kind": "nope"}, lambda oid, idp="entra": "x")
        raise AssertionError("expected unknown-kind refusal")
    except ValueError as e:
        assert "aws_keys" in str(e), e


def test_not_linked_aws_names_the_remedy_the_user_can_reach():
    """Drop-and-disclose must say WHERE to act - the account menu - because there is no
    sign-in flow that mints this credential (#660's lesson: never send a user to a control
    they cannot reach)."""
    from dbsearch.server.user_auth import KNOWN_IDPS, NotSignedIn, TokenVault, not_linked
    assert "aws" in KNOWN_IDPS
    msg = str(not_linked("aws"))
    assert "Amazon" in msg and "account menu" in msg, msg
    v = TokenVault()
    try:
        v.get("alice", idp="aws")
        raise AssertionError("an empty vault answered")
    except NotSignedIn as e:
        assert e.idp == "aws", e.idp


# --- the Redshift engine joins ADR 0022 ------------------------------------------------

class FakeDataClient:
    """A redshift-data client that answers the schema query, tagged with the identity it
    was built as."""

    def __init__(self, identity):
        self.identity = identity

    def execute_statement(self, WorkgroupName=None, Database=None, Sql=None):
        return {"Id": "s1"}

    def describe_statement(self, Id):
        return {"Status": "FINISHED", "HasResultSet": True}

    def get_statement_result(self, Id):
        return {"ColumnMetadata": [{"name": "table_schema"}, {"name": "table_name"},
                                   {"name": "column_name"}, {"name": "data_type"}],
                "Records": _SCHEMA_RECORDS}


def _engine(seen):
    """Distinguishable ambient/user client factories, same instrument as selftest_656."""
    def ambient():
        seen.append("ambient")
        return FakeDataClient("ambient")

    def as_user(cred):
        seen.append(f"user:{cred['access_key_id']}")
        return FakeDataClient(cred["access_key_id"])

    return RedshiftEngine(ambient, workgroup="wg", database="db",
                          user_client_factory=as_user)


def test_probe_without_a_credential_uses_the_ambient_identity():
    """The self-host topology is explicitly unchanged (ADR 0024 decision 5)."""
    seen = []
    p = RedshiftProvider(engine_factory=lambda _c: _engine(seen))
    p.probe(CONFIG)
    assert seen == ["ambient"], seen


def test_probe_as_introspects_as_the_caller():
    seen = []
    p = RedshiftProvider(engine_factory=lambda _c: _engine(seen))
    p.probe_as(CONFIG, credential=json.dumps({"access_key_id": "ASIA-alice",
                                              "secret_access_key": "s",
                                              "session_token": "t"}))
    assert seen == ["user:ASIA-alice"], seen
    assert "ambient" not in seen, "the ambient identity was still consulted"


def test_probe_as_without_a_credential_is_exactly_probe():
    seen = []
    p = RedshiftProvider(engine_factory=lambda _c: _engine(seen))
    p.probe_as(CONFIG, credential=None)
    assert seen == ["ambient"], seen


def test_a_second_caller_does_not_inherit_the_first_callers_schema():
    """ADR 0022's sharp edge, now on the Redshift rail: two callers see two schemas for
    one store id, so the per-engine schema cache must drop when the credential changes."""
    seen = []
    eng = _engine(seen)
    eng.introspect_as(json.dumps({"access_key_id": "ASIA-alice",
                                  "secret_access_key": "s", "session_token": "t"}))
    eng.schema()
    eng.introspect_as(json.dumps({"access_key_id": "ASIA-bob",
                                  "secret_access_key": "s", "session_token": "t"}))
    eng.schema()
    assert seen == ["user:ASIA-alice", "user:ASIA-bob"], seen


def test_the_engine_caches_within_one_caller():
    seen = []
    eng = _engine(seen)
    cred = json.dumps({"access_key_id": "ASIA-alice", "secret_access_key": "s",
                       "session_token": "t"})
    eng.introspect_as(cred)
    eng.schema()
    eng.schema()
    assert seen == ["user:ASIA-alice"], seen


def test_the_built_store_reads_its_schema_as_the_caller_too():
    """#665's lesson applied on day one: probe and build produce SEPARATE engines, and
    retrieve re-reads the schema off the BUILT one. build_as ships with probe_as, not
    after it."""
    seen = []
    p = RedshiftProvider(engine_factory=lambda _c: _engine(seen))
    store = p.build_as(CONFIG, credential=json.dumps(
        {"access_key_id": "ASIA-alice", "secret_access_key": "s", "session_token": "t"}))
    store.profile()
    assert "ambient" not in seen, f"the ambient identity was consulted: {seen}"
    assert seen and all(s == "user:ASIA-alice" for s in seen), seen


def test_compose_builds_a_delegated_redshift_store_as_the_owner():
    """The compose path (the one #665 found untouched on the BigQuery rail) - through the
    real load_manifest seam, with an aws_keys-shaped credential."""
    from dbsearch.router.provisioning import load_manifest
    seen = []

    class _Reg:
        def __init__(self, p): self._p = p
        def get(self, kind, mode=None): return self._p

    p = RedshiftProvider(engine_factory=lambda _c: _engine(seen))
    spec = {"tenant": "acme", "stores": [{
        "id": "rs-1", "kind": "redshift", "business_unit": "unassigned", "acl": ["alice"],
        "config": {"workgroup": "wg", "database": "db"},
        "delegation": {"kind": "aws_keys", "resource": "redshift"}}]}
    skipped = []
    cat = load_manifest(spec, registry=_Reg(p), skipped=skipped,
                        credential_for=lambda e: json.dumps(
                            {"access_key_id": "ASIA-owner", "secret_access_key": "s",
                             "session_token": "t"}))
    assert not skipped, f"the delegated store was skipped: {skipped}"
    assert cat.get("rs-1") is not None, "the store is missing from the catalog"
    assert "ambient" not in seen, f"compose consulted the ambient identity: {seen}"
    assert seen and all(s == "user:ASIA-owner" for s in seen), seen


def test_compose_without_a_credential_keeps_the_server_identity():
    from dbsearch.router.provisioning import load_manifest
    seen = []

    class _Reg:
        def __init__(self, p): self._p = p
        def get(self, kind, mode=None): return self._p

    spec = {"tenant": "acme", "stores": [{
        "id": "rs-1", "kind": "redshift", "business_unit": "unassigned", "acl": ["alice"],
        "config": {"workgroup": "wg", "database": "db"}}]}   # no delegation block
    load_manifest(spec, registry=_Reg(RedshiftProvider(engine_factory=lambda _c: _engine(seen))),
                  skipped=[], credential_for=lambda e: None)
    assert seen == ["ambient"], seen


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    print("FAILED" if fails else "all green")
    sys.exit(1 if fails else 0)
