"""#656 / ADR 0022 - a delegated store's schema is read with the CALLER's credential.

The wall this pins: BigQueryProvider introspected with server-side ADC unconditionally, so on
a hosted deployment - which has no ADC, and could not hold a meaningful one for a user's own
GCP project - a store probed `Your default credentials were not found` no matter how correctly
the user had linked Google. Reproduced on prod 260812 with a real linked account.

    PYTHONPATH=src python3 tests/selftest_656_delegated_schema_introspection.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.health import (  # noqa: E402
    ConnectionTest, _count_from, default_strategies,
)
from dbsearch.router.providers.bigquery import BigQueryEngine, BigQueryProvider  # noqa: E402

CONFIG = {"id": "bq-1", "project": "p", "dataset": "d", "acl": ["alice"]}

SCHEMA_ROWS = [("office_expense_limits", "office", "STRING"),
               ("office_expense_limits", "daily_meal_cap", "INT64")]


class FakeClient:
    """Records which identity it was built as, and answers INFORMATION_SCHEMA."""

    def __init__(self, identity):
        self.identity = identity

    def query(self, sql, job_config=None):
        class _Rows:
            schema = ()

            def __iter__(self):
                return iter(SCHEMA_ROWS)

        class _Job:
            def result(self):
                return _Rows()
        return _Job()


def _engine(seen):
    """A BigQueryEngine whose two client factories are distinguishable, so a test can tell
    WHICH identity introspected rather than merely that introspection succeeded."""
    def adc():
        seen.append("adc")
        return FakeClient("adc")

    def as_user(credential):
        seen.append(f"user:{credential}")
        return FakeClient(credential)

    return BigQueryEngine(adc, project="p", dataset="d",
                          user_client_factory=as_user)


def test_probe_without_a_credential_still_uses_adc():
    """The operator-owned-warehouse path is explicitly unchanged by ADR 0022."""
    seen = []
    p = BigQueryProvider(engine_factory=lambda _c: _engine(seen))
    p.probe(CONFIG)
    assert seen == ["adc"], seen


def test_probe_as_with_a_credential_introspects_as_the_caller():
    """The fix. The schema read must go out under the caller's token, not ADC."""
    seen = []
    p = BigQueryProvider(engine_factory=lambda _c: _engine(seen))
    p.probe_as(CONFIG, credential="ya29.alice-token")
    assert seen == ["user:ya29.alice-token"], seen
    assert "adc" not in seen, "ADC was still consulted for a delegated store"


def test_probe_as_without_a_credential_is_exactly_probe():
    """So a caller that cannot mint one loses nothing."""
    seen = []
    p = BigQueryProvider(engine_factory=lambda _c: _engine(seen))
    p.probe_as(CONFIG, credential=None)
    assert seen == ["adc"], seen


def test_a_second_caller_does_not_inherit_the_first_callers_schema():
    """ADR 0022's consequence, and the sharp edge of it. Two callers legitimately see two
    schemas for one store id; a profile cached under the store id alone would hand caller B
    the tables caller A can see. That is a LAW 2 disclosure, so it is pinned here."""
    seen = []
    eng = _engine(seen)
    eng.introspect_as("token-alice")
    eng.schema()
    eng.introspect_as("token-bob")
    eng.schema()
    assert seen == ["user:token-alice", "user:token-bob"], seen


def test_the_engine_caches_within_one_caller():
    """The cache still has to work - it is one introspection per engine lifetime per identity,
    not one per call."""
    seen = []
    eng = _engine(seen)
    eng.introspect_as("token-alice")
    eng.schema()
    eng.schema()
    assert seen == ["user:token-alice"], seen


class _Registry:
    def __init__(self, provider):
        self._p = provider
        self._defaults = {"bigquery": "pushdown"}

    def get(self, kind, mode=None):
        return self._p


def test_health_threads_the_credential_through_to_the_probe():
    """The seam that actually failed on prod: ConnectionTest must hand the token to a
    provider that can take one. Asserting on the ENGINE's recorded identity rather than on
    the verdict, because a verdict can be healthy for the wrong reason."""
    seen = []
    p = BigQueryProvider(engine_factory=lambda _c: _engine(seen))
    ct = ConnectionTest(_Registry(p), default_strategies())
    ct.run({"kind": "bigquery", "id": "bq-1", "config": CONFIG}, "alice",
           introspect_credential="ya29.alice-token")
    assert seen and seen[0] == "user:ya29.alice-token", seen


def test_health_without_a_credential_leaves_every_other_provider_alone():
    """A provider with no probe_as, and no credential, must go down the untouched path."""
    seen = []
    p = BigQueryProvider(engine_factory=lambda _c: _engine(seen))
    ct = ConnectionTest(_Registry(p), default_strategies())
    ct.run({"kind": "bigquery", "id": "bq-1", "config": CONFIG}, "alice")
    assert seen and seen[0] == "adc", seen


def _exercised_credentials(registered: "str | None", passed: "str | None") -> list:
    """Run a health check and report which credential reached the ENGINE's execute().

    `registered` is what the store's own authorizer supplies (what a COMPOSED store's
    registered delegation would give); `passed` is the health check's minted credential.
    """
    used = []

    class _Engine(BigQueryEngine):
        def execute(self, sql, credential=None, principal=None):
            used.append(credential)
            return ["office"], [("Singapore",)]

        def schema(self):
            return [{"table": "office_expense_limits",
                     "columns": [{"name": "office", "type": "STRING"}]}]

    def factory(_c):
        return _Engine(lambda: FakeClient("adc"), project="p", dataset="d")

    p = BigQueryProvider(engine_factory=factory,
                         authorizer=lambda u: _Access(u, registered))
    ct = ConnectionTest(_Registry(p), default_strategies())
    ct.run({"kind": "bigquery", "id": "bq-1", "config": CONFIG}, "alice",
           introspect_credential=passed)
    return used


class _Access:
    def __init__(self, user_oid, credential):
        self.user_oid = user_oid
        self.principals = [user_oid]
        self.delegated_credential = credential
        self.row_policy = None


def test_the_round_trip_uses_the_callers_credential_when_the_store_is_not_composed():
    """The second wall. probe read the schema as the caller and then `exercise` fell through
    to ADC, because the broker only registers delegations at COMPOSE time and Test-connection
    runs before that. The whole check has to speak with one identity."""
    used = _exercised_credentials(registered=None, passed="ya29.alice-token")
    assert used == ["ya29.alice-token"], used


def test_a_registered_delegation_is_never_overridden():
    """Fill-a-hole only. A composed store's own registered delegation is the identity it is
    configured to run as, and must win over anything the health check minted."""
    used = _exercised_credentials(registered="from-the-broker", passed="ya29.alice-token")
    assert used == ["from-the-broker"], used


def test_no_credential_anywhere_still_means_no_credential():
    """Nothing is invented when there is nothing to invent."""
    used = _exercised_credentials(registered=None, passed=None)
    assert used == [None], used


def test_the_built_store_reads_its_schema_as_the_caller_too():
    """The wall moved rather than vanished when only probe_as was credentialed.

    probe and build produce SEPARATE engines and `retrieve` re-reads the schema off the BUILT
    one, so the first deploy of this fix read the schema as the caller (1622ms of real work)
    and then died on ADC 8ms into the round-trip. Asserted on the ENGINE, because the verdict
    alone cannot tell you which identity read what."""
    seen = []

    class _Engine(BigQueryEngine):
        def execute(self, sql, credential=None, principal=None):
            return ["office"], [("Singapore",)]

    def factory(_c):
        e = _Engine(lambda: (seen.append("adc"), FakeClient("adc"))[1],
                    project="p", dataset="d",
                    user_client_factory=lambda c: (seen.append(f"user:{c}"),
                                                   FakeClient(c))[1])
        return e

    p = BigQueryProvider(engine_factory=factory)
    ct = ConnectionTest(_Registry(p), default_strategies())
    ct.run({"kind": "bigquery", "id": "bq-1", "config": CONFIG}, "alice",
           introspect_credential="ya29.alice-token")
    assert "adc" not in seen, f"ADC was consulted somewhere in the check: {seen}"
    assert seen and all(s == "user:ya29.alice-token" for s in seen), seen


# --- #660: the default (non-delegated) path must fail HONESTLY ------------------------

def _with_client_raising(message):
    """Patch the REAL google.cloud.bigquery.Client so from_config's own make_client wrapper
    is the thing under test. An earlier version of this test replaced _client_factory, which
    IS the wrapper - so it asserted against code the fix had never touched and failed for the
    wrong reason."""
    from google.cloud import bigquery as _bq
    import dbsearch.router.providers.bigquery as bq
    orig = _bq.Client

    def boom(*a, **k):
        raise Exception(message)
    _bq.Client = boom
    try:
        eng = bq.BigQueryEngine.from_config({"project": "p", "dataset": "d"})
        try:
            eng.schema()
        except Exception as exc:
            return str(exc)
        raise AssertionError("expected a refusal")
    finally:
        _bq.Client = orig


def test_the_adc_failure_names_the_control_the_user_can_reach():
    """#660. A store left at require_signin: no has no server credential to run as on a
    hosted box. The refusal must point at the panel field, not at Google's ADC setup docs."""
    msg = _with_client_raising(
        "Your default credentials were not found. To set up Application Default "
        "Credentials, see https://cloud.google.com/docs/authentication/external/set-up-adc")
    assert "require_signin: yes" in msg, msg
    assert "linked Google account" in msg, msg


def test_a_non_credentials_error_is_not_swallowed():
    """Only the ADC case is reworded. Rewriting an unrelated failure into 'set require_signin'
    would send the user to flip a switch that has nothing to do with their problem."""
    msg = _with_client_raising("connection reset by peer")
    assert "connection reset by peer" in msg, msg
    assert "require_signin" not in msg, msg


def test_compose_introspects_a_delegated_store_as_the_owner():
    """#665. THE GAP THAT LET #656 SHIP INCOMPLETE: selftest_656 covered probe and health,
    so a green suite said the fix was done while compose - the path that PERSISTS a store -
    was still on ADC. A correctly-configured BigQuery store health-checked green and then
    vanished from the catalog the moment the user pressed Compose up."""
    from dbsearch.router.provisioning import load_manifest
    seen = []

    class _Engine(BigQueryEngine):
        def execute(self, sql, credential=None, principal=None):
            return ["n"], [(4,)]

    def factory(_c):
        return _Engine(lambda: (seen.append("adc"), FakeClient("adc"))[1],
                       project="p", dataset="d",
                       user_client_factory=lambda c: (seen.append(f"user:{c}"),
                                                      FakeClient(c))[1])

    class _Reg:
        def __init__(self, p): self._p = p
        def get(self, kind, mode=None): return self._p

    p = BigQueryProvider(engine_factory=factory)
    spec = {"tenant": "acme", "stores": [{
        "id": "bq-1", "kind": "bigquery", "business_unit": "unassigned", "acl": ["alice"],
        "config": {"project": "p", "dataset": "d"},
        "delegation": {"kind": "google_refresh", "client_id": "c",
                       "client_secret": "s", "resource": "bigquery"}}]}
    skipped = []
    load_manifest(spec, registry=_Reg(p), skipped=skipped,
                  credential_for=lambda e: "ya29.owner-token")
    assert not skipped, f"the delegated store was skipped: {skipped}"
    assert "adc" not in seen, f"compose still consulted ADC: {seen}"
    assert seen and all(s == "user:ya29.owner-token" for s in seen), seen


def test_compose_without_a_credential_keeps_the_server_identity():
    """The operator-owned-warehouse path is untouched, exactly as at probe time."""
    from dbsearch.router.provisioning import load_manifest
    seen = []

    class _Engine(BigQueryEngine):
        def execute(self, sql, credential=None, principal=None):
            return ["n"], [(4,)]

    def factory(_c):
        return _Engine(lambda: (seen.append("adc"), FakeClient("adc"))[1],
                       project="p", dataset="d")

    class _Reg:
        def __init__(self, p): self._p = p
        def get(self, kind, mode=None): return self._p

    spec = {"tenant": "acme", "stores": [{
        "id": "bq-1", "kind": "bigquery", "business_unit": "unassigned", "acl": ["alice"],
        "config": {"project": "p", "dataset": "d"}}]}   # no delegation block
    load_manifest(spec, registry=_Reg(BigQueryProvider(engine_factory=factory)),
                  skipped=[], credential_for=lambda e: None)
    assert seen == ["adc"], seen


def test_an_unmintable_credential_falls_back_instead_of_skipping_the_store():
    """CORRECTED after selftest_workspace_isolation caught the first version of #665.

    My first cut let a mint failure skip the store. That looked tidy and was wrong: the
    product's contract is that a delegated store belonging to a user who has NOT linked that
    cloud still COMPOSES, so the ask answers "connect Google to query this source" through
    drop+disclose. Skipping removes it from the catalog and the user is told "No data sources
    are connected yet" - true about the catalog, useless about their problem."""
    from dbsearch.router.provisioning import load_manifest
    from dbsearch.server.user_auth import not_linked
    seen = []

    class _Engine(BigQueryEngine):
        def execute(self, sql, credential=None, principal=None):
            return ["n"], [(4,)]

    def factory(_c):
        return _Engine(lambda: (seen.append("adc"), FakeClient("adc"))[1],
                       project="p", dataset="d")

    class _Reg:
        def __init__(self, p): self._p = p
        def get(self, kind, mode=None): return self._p

    def boom(_entry):
        raise not_linked("google")

    spec = {"tenant": "acme", "stores": [{
        "id": "bq-1", "kind": "bigquery", "business_unit": "unassigned", "acl": ["alice"],
        "config": {"project": "p", "dataset": "d"},
        "delegation": {"kind": "google_refresh", "client_id": "c", "client_secret": "s"}}]}
    skipped = []
    cat = load_manifest(spec, registry=_Reg(BigQueryProvider(engine_factory=factory)),
                        skipped=skipped, credential_for=boom)
    assert not skipped, f"an unlinked cloud skipped the store instead of falling back: {skipped}"
    assert cat.get("bq-1") is not None, "the store is missing from the catalog"
    assert seen == ["adc"], f"expected the server-identity fallback, got {seen}"


# --- the canary's count parser (found while proving #656 end to end) -------------------

class _Ev:
    def __init__(self, content):
        self.content = content


def test_an_unaliased_count_is_read_as_its_value_not_its_column_name():
    """THE BUG: BigQuery names an unaliased COUNT(*) `f0_`, evidence content is `f0_=4`, and
    a first-integer regex returns the 0 out of the NAME. A full table reported empty."""
    assert _count_from([_Ev("f0_=4")]) == 4


def test_the_aliased_forms_still_parse():
    for content, want in (("count=5", 5), ("record_count=4", 4), ("n=0", 0),
                          ("total=-3", -3)):
        assert _count_from([_Ev(content)]) == want, content


def test_a_genuinely_empty_count_is_still_zero():
    """The honest-degraded path must survive the fix - 0 still means 0."""
    assert _count_from([_Ev("f0_=0")]) == 0


def test_no_evidence_and_no_number_are_both_unknown():
    """None means 'unexpected shape', which the caller treats differently from 0."""
    assert _count_from([]) is None
    assert _count_from([_Ev("no digits here")]) is None


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
