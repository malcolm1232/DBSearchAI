"""#814 / ADR 0026 - RDS kinds authenticate with an IAM auth token minted from the
caller's vaulted AWS keys; nobody types a database password on the canvas.

The defect (wave-2 #780 audit; the owner's live 260813 dead end): the RDS kinds shared
the base engines verbatim, so a palette-added rds_postgres store failed Test connection
with `postgres config missing [password]` - a password the panel does not collect,
named under a kind the store is not.

What this file proves, engine-first (fakes injected through the same seams the redshift
rail uses - no boto3, no live database):
 - from_config requires host/database/user, NOT password, and names its OWN kind.
 - the delegated path redeems the caller's STS triple into generate_db_auth_token
   (region from config, else parsed from the RDS hostname) and connects as the
   CONFIGURED db user with the token as the password.
 - two callers mint two tokens from two credentials (LAW 2: per-caller auth at AWS).
 - a typed password keeps the self-host service path unchanged (ADR 0010 form 2).
 - no password + no credential fails closed with the remedy named (connect Amazon).
 - introspect_as (ADR 0022) reads the schema as the caller, cache keyed per credential.
 - the providers default to the RDS engines and thread build_as/probe_as credentials.

    PYTHONPATH=src python3 -m pytest tests/selftest_814_rds_iam_auth.py -q
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.router.providers.postgres import (  # noqa: E402
    PostgresEngine, RdsPostgresEngine, RdsPostgresProvider)
from dbsearch.router.providers.mysql import (  # noqa: E402
    RdsMySqlEngine, RdsMySqlProvider)

ROUTER_API = (ROOT / "src/dbsearch/server/router_api.py").read_text()


def _triple(tag):
    return json.dumps({"access_key_id": "ASIA-" + tag,
                       "secret_access_key": "tmp-" + tag,
                       "session_token": "session-" + tag})


class FakeRdsClient:
    """Records the mint and returns a token that names whose credential minted it."""

    def __init__(self, region, cred, log):
        self.region, self.cred, self.log = region, cred, log

    def generate_db_auth_token(self, DBHostname, Port, DBUsername):
        self.log.append({"region": self.region, "session": self.cred["session_token"],
                         "host": DBHostname, "port": Port, "db_user": DBUsername})
        return f"iam-token({self.cred['session_token']})-for-{DBUsername}"


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.description = [("route",), ("cost",)]
        self.sql = None

    def execute(self, sql):
        self.sql = sql

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return FakeCursor(self._rows)

    def close(self):
        pass


def _rig(config, rows=(("sin-hkg", 8000),)):
    """An engine with the mint and the socket both faked, plus their logs."""
    mints, opens = [], []

    def opener(user, password):
        opens.append({"user": user, "password": password})
        return FakeConn(rows)

    eng = RdsPostgresEngine.from_config(
        config, opener=opener,
        rds_client_factory=lambda region, cred: FakeRdsClient(region, cred, mints))
    return eng, mints, opens


CFG = {"id": "rds-1", "host": "mydb.abc123.ap-southeast-1.rds.amazonaws.com",
       "database": "appdb", "user": "dbsearch_reader"}


def test_password_is_not_required():
    eng, _, _ = _rig(dict(CFG))
    assert isinstance(eng, PostgresEngine)


def test_missing_fields_name_the_true_kind():
    with pytest.raises(ValueError) as e:
        RdsPostgresEngine.from_config({"id": "x", "host": "h"})
    assert "rds_postgres config missing" in str(e.value), (
        "the validator still reports the base kind - the #814 leak (an rds_postgres "
        f"store told 'postgres ...'): {e.value}")
    with pytest.raises(ValueError) as e2:
        RdsMySqlEngine.from_config({"id": "x", "host": "h"})
    assert "rds_mysql config missing" in str(e2.value)


def test_delegated_query_authenticates_with_an_iam_token():
    eng, mints, opens = _rig(dict(CFG))
    cols, rows = eng.execute("SELECT route, cost FROM public.freight_costs",
                             credential=_triple("alice"))
    assert rows == [("sin-hkg", 8000)]
    assert mints == [{"region": "ap-southeast-1", "session": "session-alice",
                      "host": CFG["host"], "port": 5432,
                      "db_user": "dbsearch_reader"}], (
        f"the token was not minted from the caller's own STS triple: {mints}")
    assert opens == [{"user": "dbsearch_reader",
                      "password": "iam-token(session-alice)-for-dbsearch_reader"}], (
        f"the connection did not use the IAM token as the password: {opens}")


def test_two_callers_mint_two_tokens():
    eng, mints, opens = _rig(dict(CFG))
    eng.execute("SELECT 1", credential=_triple("alice"))
    eng.execute("SELECT 1", credential=_triple("bob"))
    assert [m["session"] for m in mints] == ["session-alice", "session-bob"], (
        "two callers did not redeem two distinct credentials (LAW 2 - the identity "
        f"collapse ADR 0024 rules out): {mints}")


def test_typed_password_keeps_the_self_host_path():
    eng, mints, opens = _rig(dict(CFG, password="hunter2"))
    eng.execute("SELECT 1")
    assert opens == [{"user": "dbsearch_reader", "password": "hunter2"}], (
        f"a hand-written manifest's typed password no longer connects: {opens}")
    assert mints == [], "the service path minted an IAM token it does not need"


def test_no_password_no_credential_fails_with_the_remedy():
    eng, _, opens = _rig(dict(CFG))
    with pytest.raises(RuntimeError) as e:
        eng.execute("SELECT 1")
    assert "connect Amazon" in str(e.value), (
        "the password-less service path failed without naming the remedy - the skip "
        f"reason is what the user reads (#802 family): {e.value}")
    assert opens == [], "a connection was attempted with no credential at all"


def test_introspect_as_reads_the_schema_as_the_caller():
    rows = (("public", "freight_costs", "route", "varchar"),
            ("public", "freight_costs", "cost", "integer"))
    eng, mints, opens = _rig(dict(CFG), rows=rows)
    eng.introspect_as(_triple("alice"))
    schema = eng.schema()
    assert schema and schema[0]["table"] == "public.freight_costs"
    assert opens and opens[0]["password"].startswith("iam-token(session-alice)"), (
        f"introspection did not run over the caller's delegated connection: {opens}")
    # the cache is per credential: a different caller re-reads
    eng.introspect_as(_triple("bob"))
    eng.schema()
    assert [m["session"] for m in mints] == ["session-alice", "session-bob"], (
        f"a second caller was served the first caller's cached schema: {mints}")


def test_region_config_overrides_and_unparseable_host_fails_clearly():
    eng, mints, _ = _rig(dict(CFG, region="eu-west-1"))
    eng.execute("SELECT 1", credential=_triple("alice"))
    assert mints[0]["region"] == "eu-west-1"
    eng2, _, _ = _rig(dict(CFG, host="10.0.0.5"))
    with pytest.raises(ValueError) as e:
        eng2.execute("SELECT 1", credential=_triple("alice"))
    assert "region" in str(e.value).lower(), (
        f"an unparseable host without a region gave no actionable error: {e.value}")


def test_mysql_twin_mints_and_connects():
    mints, opens = [], []

    def opener(user, password):
        opens.append({"user": user, "password": password})
        return FakeConn([("sin-hkg", 8000)])

    eng = RdsMySqlEngine.from_config(
        dict(CFG), opener=opener,
        rds_client_factory=lambda region, cred: FakeRdsClient(region, cred, mints))
    eng.execute("SELECT 1", credential=_triple("alice"))
    assert mints[0]["port"] == 3306 and mints[0]["region"] == "ap-southeast-1"
    assert opens[0] == {"user": "dbsearch_reader",
                        "password": "iam-token(session-alice)-for-dbsearch_reader"}


class RecordingEngine:
    """Fixture engine for the provider seam - records introspect_as (the ADR 0022 idiom)."""

    dialect = "PostgreSQL"

    def __init__(self):
        self.introspected = []

    def introspect_as(self, credential):
        self.introspected.append(credential)

    def schema(self):
        return [{"table": "public.t", "columns": [{"name": "c", "type": "int"}]}]

    def execute(self, sql, credential=None, principal=None):
        return ["c"], [(1,)]

    def fk_edges(self):
        return []

    def refresh_schema(self):
        pass


def test_providers_default_to_the_rds_engines():
    assert RdsPostgresProvider()._engine_factory == RdsPostgresEngine.from_config, (
        "RdsPostgresProvider still defaults to the base engine - a palette store would "
        "demand the password the panel does not collect")
    assert RdsMySqlProvider()._engine_factory == RdsMySqlEngine.from_config


def test_provider_build_as_threads_the_credential():
    fixtures = []

    def factory(config):
        e = RecordingEngine()
        fixtures.append(e)
        return e

    prov = RdsPostgresProvider(engine_factory=factory)
    prov.build_as({"id": "rds-1"}, credential=_triple("alice"))
    prov.probe_as({"id": "rds-1"}, credential=_triple("alice"))
    assert [e.introspected for e in fixtures] == [[_triple("alice")], [_triple("alice")]], (
        "build_as/probe_as did not hand the caller's credential to the engine - the "
        f"#665 lesson (credential only one of the two paths): {fixtures}")


def test_registrations_hand_the_rds_kinds_their_own_engines():
    assert "RdsPostgresProvider" in ROUTER_API
    assert "_engine_factory(r.RdsPostgresEngine.from_config)" in ROUTER_API, (
        "router_api still registers RdsPostgresProvider over PostgresEngine.from_config - "
        "the fixture rail would build palette RDS stores on the password engine")
    assert "_engine_factory(r.RdsMySqlEngine.from_config)" in ROUTER_API


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
