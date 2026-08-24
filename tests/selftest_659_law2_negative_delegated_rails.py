"""#659 - the LAW 2 NEGATIVE on the delegated rails: a caller who has not linked the cloud
is REFUSED and TOLD, never served an empty result that reads like an answer.

WHY THIS EXISTS. Every POSITIVE path is proven: #653 (BigQuery), #668 (real Redshift, 8641
in and 8641 out), #673 (a real S3 bucket, cited and quoted). The negative was proven on none
of them. That asymmetry matters more than usual here, because the two failures look identical
from the outside: "no rows" and "you may not see these rows" render the same way unless
something deliberately keeps them apart. The repo has the scar already - an empty list from
an under-scoped token reads exactly like a fact.

WHAT WAS ACTUALLY COVERED BEFORE. One HTTP-level test asserted an uncredentialed caller is
refused - selftest_workspace_isolation's health-agrees-with-ask case - and it uses the FAKE
`static` exchange kind, which no deployment has ever run. selftest_656 and selftest_666 do put
two identities against one store, but BOTH are linked: they prove credential ISOLATION, not
refusal. No test had ever exercised an unlinked identity against a shipped rail.

WHAT THIS PROVES, and how it manages it with no cloud account. The refusal happens at the
VAULT LOOKUP, before any network call - so the whole negative path is exercisable offline
against the real exchange classes, built through the real `exchange_from_config`, bound by
the real `_for_idp` guard, refused through the real `IdentityBroker`, and dropped and
disclosed by the real `executor`. Nothing here is a fake but the vault itself, which is
exactly the component whose EMPTY state is the thing under test.

The rails split 2+1 (see #676). This file covers the two broker-backed rails; the connector
rail has no broker binding at all and is covered by selftest_676.

    PYTHONPATH=src python3 tests/selftest_659_law2_negative_delegated_rails.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.adapters.local import InMemoryIdentity  # noqa: E402
from dbsearch.router.decision import RoutedStore, RoutingDecision  # noqa: E402
from dbsearch.router.executor import execute  # noqa: E402
from dbsearch.router.identity_broker import (  # noqa: E402
    AwsKeysExchange, IdentityBroker,
)
from dbsearch.router.provider import ProviderRegistry  # noqa: E402
from dbsearch.router.providers.bigquery import BigQueryProvider  # noqa: E402
from dbsearch.router.providers.redshift import RedshiftProvider  # noqa: E402
from dbsearch.router.provisioning import load_manifest, register_delegations  # noqa: E402

# Alice has linked both clouds. Bob has linked neither. BOTH can SEE the store: the acl
# below names them both, so anything Bob is refused here is the CREDENTIAL gate, never the
# ACL gate (that one is #478's, on a different rail, and passing it is a prerequisite for
# this test meaning anything at all).
IDENTITY = InMemoryIdentity({"alice": ["staff"], "bob": ["staff"]})

_LINKED = {
    ("alice", "aws"): json.dumps({"access_key_id": "AKIAALICE",
                                  "secret_access_key": "alice-secret"}),
    ("alice", "google"): "alice-google-refresh-token",
}


class _NotLinked(Exception):
    """The vault's own refusal, reproduced in the shape the product raises it.

    `.idp` is the load-bearing part: health.py checks for it STRUCTURALLY rather than
    importing NotSignedIn across the layer boundary, and it is what lets the message name
    WHICH cloud to connect instead of failing generically."""

    def __init__(self, idp: str) -> None:
        super().__init__({"aws": "connect Amazon to query this source",
                          "google": "connect Google to query this source"}[idp])
        self.idp = idp


def _vault(user_oid: str, idp: str) -> str:
    """`app._subject_provider`'s contract: (oid, idp) -> the vaulted credential, or raise."""
    try:
        return _LINKED[(user_oid, idp)]
    except KeyError:
        raise _NotLinked(idp) from None


class _FixtureEngine:
    """A stand-in warehouse, and the reason one is legitimate here.

    `FederatedSqlStore.authorize` (structured.py:1315) consults the broker seam and NOTHING
    else - it never touches the engine. The refusal under test therefore happens strictly
    before any engine call, so a fixture engine removes a live warehouse from the test
    without removing one line of the path being proven. What it DOES buy is that `profile()`
    can introspect at compose time; against a real RedshiftEngine that is a network call, and
    the store would land in `skipped` and never reach the catalog at all - which is how this
    test first failed, with a boto3 parameter-validation error rather than a LAW 2 verdict.

    `execute` raises rather than returning rows: nothing in this file is entitled to reach
    it, and a fixture that quietly answered would let a broken refusal look like a pass."""

    @staticmethod
    def schema():
        return [{"table": "pallets", "columns": [{"name": "depot"}, {"name": "quota"}]}]

    @staticmethod
    def fk_edges():
        return []

    @staticmethod
    def execute(*a, **k):
        raise AssertionError(
            "the engine was reached for a caller who should have been refused at authorize()")


class _FakeSts:
    """Stands in for STS only on the LINKED path, so alice can get past the vault and prove
    the refusal is caller-specific rather than a rail that is simply broken for everyone."""

    def __init__(self, keys):
        self.keys = keys

    def get_session_token(self, DurationSeconds=None):    # noqa: N803 - boto3's own casing
        assert self.keys["access_key_id"] == "AKIAALICE", (
            f"STS was handed keys that are not alice's: {self.keys!r}")
        return {"Credentials": {"AccessKeyId": "ASIA-TEMP",
                                "SecretAccessKey": "temp-secret",
                                "SessionToken": "temp-session"}}


_REDSHIFT = {"id": "rs-1", "kind": "redshift", "business_unit": "eng",
             "acl": ["alice", "bob"], "title": "Warehouse",
             "config": {"workgroup": "wg", "database": "db", "region": "ap-southeast-1"},
             "delegation": {"kind": "aws_keys", "resource": "redshift"}}

_BIGQUERY = {"id": "bq-1", "kind": "bigquery", "business_unit": "eng",
             "acl": ["alice", "bob"], "title": "Analytics",
             "config": {"project": "p", "dataset": "d"},
             "delegation": {"kind": "google_refresh", "client_id": "cid",
                            "client_secret": "sec", "resource": "bigquery"}}


class _sts_stubbed:
    """Stub STS for the duration of a block, INCLUDING the compose inside it.

    Ordering is the whole point and it is easy to get wrong: `AwsKeysExchange.__init__` binds
    `self._sts_factory = sts_client_factory or self._default_factory`, so the class attribute
    is read once, at construction, during `register_delegations`. Patching after compose
    leaves the already-built exchange pointing at the real boto3 factory - which is exactly
    what happened on the first run of this file, and it reached the live STS endpoint and
    came back InvalidClientTokenId. A test that silently makes a network call is a test that
    will fail in a tunnel and pass in a cafe."""

    def __enter__(self):
        self._original = AwsKeysExchange._default_factory
        AwsKeysExchange._default_factory = staticmethod(_FakeSts)
        return self

    def __exit__(self, *exc):
        AwsKeysExchange._default_factory = self._original
        return False


def _compose(entry):
    """The real compose path: register_delegations THEN load_manifest, same order as
    /router/compose (router_api.py), so the broker holds the delegation before any store is
    built - which is what makes the query-time refusal reachable at all."""
    broker = IdentityBroker(IDENTITY)
    spec = {"tenant": "acme", "stores": [entry]}
    register_delegations(spec, broker, _vault)
    reg = ProviderRegistry()
    reg.register(RedshiftProvider(broker=broker, engine_factory=lambda cfg: _FixtureEngine()))
    reg.register(BigQueryProvider(broker=broker, engine_factory=lambda cfg: _FixtureEngine()))
    cat = load_manifest(spec, registry=reg, skipped=[])
    return cat, broker


def _ask(cat, entry, who):
    decision = RoutingDecision(query_type="analytical", stores=[
        RoutedStore(store_id=entry["id"], business_unit="eng", score=1.0)])
    return execute(cat, decision, who, "what is the daily pallet quota?")


def _assert_refused_and_told(report, expect, rail):
    assert not report.evidence_by_store, (
        f"{rail}: an unlinked caller received evidence: {report.evidence_by_store!r}")
    assert report.outcomes, (
        f"{rail}: the store was dropped with NO outcome - silently invisible, which is the "
        "one shape indistinguishable from 'you have no such data'")
    out = report.outcomes[0]
    assert out.status != "ok", f"{rail}: reported OK for an unlinked caller: {out!r}"
    assert expect in (out.error or ""), (
        f"{rail}: the drop carries no actionable reason. An empty result is a claim about "
        f"the QUESTION; this is a claim about the ACCOUNT. got: {out!r}")


def test_redshift_refuses_an_unlinked_caller_and_names_the_cloud():
    cat, _ = _compose(_REDSHIFT)
    _assert_refused_and_told(_ask(cat, _REDSHIFT, "bob"), "connect Amazon", "redshift")
    print("  PASS  redshift (aws_keys): an unlinked caller is dropped and told to connect "
          "Amazon, not answered empty")


def test_bigquery_refuses_an_unlinked_caller_and_names_the_cloud():
    cat, _ = _compose(_BIGQUERY)
    _assert_refused_and_told(_ask(cat, _BIGQUERY, "bob"), "connect Google", "bigquery")
    print("  PASS  bigquery (google_refresh): an unlinked caller is dropped and told to "
          "connect Google, not answered empty")


def test_two_identities_one_store_and_only_the_unlinked_one_is_refused():
    """THE CONTROL THAT MAKES THE REFUSALS ABOVE MEAN ANYTHING.

    A rail that is simply broken refuses everybody, and would pass both tests above. So the
    same store, in the same composed catalog, must let the LINKED caller past the credential
    gate. Alice is asserted to get a real credential out of the real exchange; bob, asking
    the same store a moment later, is refused."""
    with _sts_stubbed():
        cat, broker = _compose(_REDSHIFT)
        access = broker.access_for("alice", "rs-1")
        assert access.delegated_credential, (
            "the LINKED caller got no delegated credential, so the refusals in this file "
            "prove only that the rail is broken for everyone")
        assert json.loads(access.delegated_credential)["session_token"] == "temp-session", (
            f"alice's credential is not the STS session triple: {access.delegated_credential!r}")

        _assert_refused_and_told(_ask(cat, _REDSHIFT, "bob"), "connect Amazon",
                                 "redshift/two-identity")
    print("  PASS  same store, two identities: the linked caller is served and the unlinked "
          "one is refused - the gate is per-caller, not a broken rail")


def test_the_credential_cache_never_serves_one_callers_credential_to_another():
    """ADR 0024 names this as the rail's known LAW 2 residue: anything caching a credential
    must key on the caller, never on the store id alone. _CachedExchange keys (user,
    resource) - this pins it, because a cache that dropped the user from its key would make
    bob's refusal disappear the instant alice asked first, and only ever in production."""
    with _sts_stubbed():
        cat, broker = _compose(_REDSHIFT)
        broker.access_for("alice", "rs-1")            # warm the cache AS ALICE first
        _assert_refused_and_told(_ask(cat, _REDSHIFT, "bob"), "connect Amazon",
                                 "redshift/after-alice-warmed-the-cache")
    print("  PASS  a warm cache belonging to alice does not answer for bob")


def test_the_refused_caller_can_SEE_the_store_so_this_is_the_credential_gate():
    """Keeps this file honest about WHICH gate it tests. Bob is in the store's acl, so the
    catalog shows it to him; #478 covers the visibility gate on the SQL rail and it is a
    prerequisite here, not a substitute. If bob could not see the store, every assertion
    above would pass vacuously."""
    cat, _ = _compose(_REDSHIFT)
    visible = [n.id for n in cat.visible_stores(["bob"])]
    assert "rs-1" in visible, (
        "bob cannot even see the store, so the refusals above prove nothing about "
        f"credentials - they would hold for a store that simply is not there. visible={visible!r}")
    print("  PASS  the refused caller CAN see the store - the refusal is the credential gate, "
          "not the ACL gate")


if __name__ == "__main__":
    test_redshift_refuses_an_unlinked_caller_and_names_the_cloud()
    test_bigquery_refuses_an_unlinked_caller_and_names_the_cloud()
    test_two_identities_one_store_and_only_the_unlinked_one_is_refused()
    test_the_credential_cache_never_serves_one_callers_credential_to_another()
    test_the_refused_caller_can_SEE_the_store_so_this_is_the_credential_gate()
    print("ALL PASS  #659")
