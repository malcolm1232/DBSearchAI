"""#676 - a delegated CONNECTOR store composed by a caller who has not linked that cloud.

THE DEFECT. `provisioning.load_manifest` swallows a failed credential mint and falls through
to `provider.build(config)` with no credential. That swallow is deliberate and load-bearing
for the SQL rails: a delegated store belonging to an unlinked user must still compose, so the
ask can answer "connect Google to query this source" instead of "no data sources connected".
Those rails survive it because the broker refuses them AGAIN at query time.

The connector rail has no such second chance. `ConnectorBackedStore` extends `IndexedStore`
and does not override `authorize`, so nothing consults the broker, and compose is the ONLY
place an S3 credential is ever minted. So the swallow hands the connector factory
`credential=None`, which `s3_connector_factory` documents as "fall back to the box's ambient
AWS identity" - correct for a self-host operator who declared no delegation, and wrong here,
where the store DID declare one and the caller simply has not linked.

Two consequences, both tested below:
  1. On a self-host box holding ambient AWS credentials, the crawl reads the OPERATOR's
     bucket on behalf of a user who never linked an identity. ADR 0024's LAW 2 section calls
     that shape structurally unreachable; it is unreachable through the aws_keys exchange,
     which is what that section was looking at, and reachable through this fallback.
  2. On a hosted box there are no ambient credentials, so the crawl merely fails - and the
     user is told "matched nothing", a sentence about their QUESTION, when the truth is a
     sentence about their ACCOUNT.

    PYTHONPATH=src python3 tests/selftest_676_unlinked_delegated_connector.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.adapters.local import InMemoryIdentity  # noqa: E402
from dbsearch.router.provider import ProviderRegistry  # noqa: E402
from dbsearch.router.providers.connector import ConnectorStoreProvider  # noqa: E402
from dbsearch.router.provisioning import load_manifest  # noqa: E402

IDENTITY = InMemoryIdentity({"alice": ["alice"]})


class _RecordingConnector:
    """A connector that records the credential it was handed and ingests nothing."""

    built: list = []

    def __init__(self, config, credential=None):
        _RecordingConnector.built.append(credential)
        self._acl = list(config.get("acl") or [])

    def authenticate(self, config):
        return None

    def list_changes(self, cursor):
        return iter(())

    def fetch(self, ref):
        raise AssertionError("nothing to fetch")


def _factory(config: dict, credential: "str | None" = None):
    return _RecordingConnector(config, credential=credential)


def _provider():
    return ConnectorStoreProvider("s3", _factory, identity=IDENTITY)


def _manifest():
    """An S3 store shaped exactly as the canvas emits one: s3 is in _ALWAYS_DELEGATED, so
    the aws_keys block is present on EVERY s3 node regardless of any sign-in toggle."""
    return {"tenant": "acme", "stores": [
        {"id": "s3-1", "kind": "s3", "business_unit": "eng", "acl": ["alice"],
         "title": "Policies bucket",
         "config": {"bucket": "alice-policies", "region": "ap-southeast-1"},
         "delegation": {"kind": "aws_keys", "resource": "s3"}}]}


def _unlinked(entry):
    """What `_credential_for` does for a caller with no AWS link: the vault raises."""
    exc = RuntimeError("connect Amazon to query this source")
    exc.idp = "aws"          # NotSignedIn's shape; health.py checks for it structurally
    raise exc


def _compose(credential_for):
    _RecordingConnector.built = []
    reg = ProviderRegistry()
    reg.register(_provider())
    skipped: list = []
    cat = load_manifest(_manifest(), registry=reg, skipped=skipped,
                        credential_for=credential_for)
    return cat, skipped


def test_an_unlinked_caller_never_crawls_on_the_boxs_ambient_identity():
    """THE SECURITY HALF. The store declared a delegation; the caller cannot supply one.
    Building the connector anyway means crawling as whatever identity the box happens to
    hold - the operator's, on a self-host deployment."""
    _compose(_unlinked)
    assert _RecordingConnector.built != [None], (
        "the connector was built with credential=None after the mint failed, so the crawl "
        "runs on the box's AMBIENT AWS identity for a caller who never linked one. A store "
        "that DECLARED a delegation must not silently fall back to the deployment's "
        "credential - that fallback belongs to the no-delegation-block case (ADR 0024 "
        f"consequence 5), not this one. built={_RecordingConnector.built!r}")
    print("  PASS  a declared delegation the caller cannot satisfy never falls back to the "
          "box's ambient identity")


def test_the_store_still_composes_so_the_ask_can_disclose_why():
    """THE HONESTY HALF, and the reason the fix is not 'skip the store'. Dropping it from
    the catalog gives the user 'No data sources are connected yet' - a true sentence about
    the catalog and a useless one about their problem (provisioning.py's own comment)."""
    cat, skipped = _compose(_unlinked)
    ids = [n.id for n in cat.stores()]
    assert "s3-1" in ids, (
        "the store vanished from the catalog instead of composing into a state that can "
        f"explain itself. skipped={skipped!r} ids={ids!r}")
    print("  PASS  the store still composes, so the ask has something to disclose about")


def test_the_ask_discloses_the_reason_instead_of_answering_empty():
    """THE HONESTY HALF, driven through the executor the ask actually uses.

    Not `assert authorize raises` - that is a claim about a method. The claim that matters is
    that the REASON reaches the user, and the repo has already been bitten by fixes that
    closed a permission channel and left the answer text flowing. So this asserts on what
    comes back from `execute`: a disclosed outcome carrying the actionable sentence, never an
    empty evidence list that the synthesizer would render as "matched nothing"."""
    from dbsearch.router.executor import execute
    from dbsearch.router.decision import RoutedStore, RoutingDecision

    cat, _ = _compose(_unlinked)
    decision = RoutingDecision(query_type="semantic", stores=[
        RoutedStore(store_id="s3-1", business_unit="eng", score=1.0)])
    report = execute(cat, decision, "alice", "what is the escalation window?")

    assert not report.evidence_by_store, (
        f"an unlinked caller received evidence from a store they cannot authenticate to: "
        f"{report.evidence_by_store!r}")
    assert report.outcomes, "the store was dropped with no outcome at all - silently invisible"
    out = report.outcomes[0]
    assert out.status != "ok", f"the store reported OK for an unlinked caller: {out}"
    assert "connect Amazon" in (out.error or ""), (
        "the drop reached the user with no actionable reason. 'matched nothing' is a sentence "
        f"about the QUESTION; the truth here is a sentence about the ACCOUNT. got: {out!r}")
    print("  PASS  the ask discloses 'connect Amazon' rather than answering from an empty index")


def test_the_refusal_carries_idp_so_health_can_name_the_cloud():
    """health.py checks `hasattr(exc, 'idp')` STRUCTURALLY rather than importing NotSignedIn
    (its own comment says so). A refusal that loses that attribute still drops the store, but
    health degrades to a generic failure and stops telling the user WHICH cloud to connect."""
    cat, _ = _compose(_unlinked)
    store = cat.get("s3-1").store
    try:
        store.authorize("alice")
    except Exception as exc:                                  # noqa: BLE001
        assert getattr(exc, "idp", None) == "aws", (
            f"the refusal lost its .idp, so health cannot name the cloud: {exc!r}")
        print("  PASS  the refusal carries .idp, so health names the cloud to connect")
        return
    raise AssertionError("authorize() did not refuse an unlinked caller at all")


def test_a_linked_caller_is_unaffected():
    """The control. A caller who HAS linked gets their own credential through, unchanged."""
    _compose(lambda e: '{"access_key_id": "AKIA", "secret_access_key": "s"}')
    assert _RecordingConnector.built == ['{"access_key_id": "AKIA", "secret_access_key": "s"}'], (
        f"a linked caller's own credential must reach the connector: {_RecordingConnector.built!r}")
    print("  PASS  a linked caller's own credential still reaches the connector")


def test_a_store_with_no_delegation_block_still_uses_the_ambient_identity():
    """The OTHER control, and the one that stops this fix breaking self-host. ADR 0024
    consequence 5: no delegation block means the operator's box IS the credential owner."""
    _RecordingConnector.built = []
    reg = ProviderRegistry()
    reg.register(_provider())
    m = _manifest()
    m["stores"][0].pop("delegation")
    load_manifest(m, registry=reg, skipped=[], credential_for=_unlinked)
    assert _RecordingConnector.built == [None], (
        "a store that declared NO delegation must still build on the box's ambient identity "
        f"- that is the self-host topology, not a defect: {_RecordingConnector.built!r}")
    print("  PASS  a store with no delegation block still builds on the ambient identity "
          "(self-host unchanged)")


if __name__ == "__main__":
    test_an_unlinked_caller_never_crawls_on_the_boxs_ambient_identity()
    test_the_store_still_composes_so_the_ask_can_disclose_why()
    test_the_ask_discloses_the_reason_instead_of_answering_empty()
    test_the_refusal_carries_idp_so_health_can_name_the_cloud()
    test_a_linked_caller_is_unaffected()
    test_a_store_with_no_delegation_block_still_uses_the_ambient_identity()
    print("ALL PASS  #676")
