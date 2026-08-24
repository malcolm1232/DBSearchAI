"""#158 (mysql slice) — MySqlEngine + MySqlProvider: Azure Database for PostgreSQL
as a SqlEnginePort sibling of AzureSqlEngine (ADR 0007), proven WITHOUT network or psycopg
(a fake DB-API connection is injected). The guard/audit/NL2SQL stay in FederatedSqlStore —
the engine only introspects information_schema and executes.

Run: python3 tests/selftest_router_mysql.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router import MySqlEngine, MySqlProvider  # noqa: E402
from dbsearch.router.store import FEDERATED_SQL  # noqa: E402


_SCHEMA_ROWS = [
    ("notes", "body", "text"),
    ("tickets", "id", "integer"),
    ("tickets", "team", "text"),
    ("tickets", "hours", "numeric"),
]
_DATA = (["team", "total_hours"], [("platform", 15.5), ("mobile", 22.0)])


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows = []

    def execute(self, sql, params=()):
        if self._conn.broken:
            raise RuntimeError("server closed the connection unexpectedly")
        self._conn.executed.append(sql)
        if "information_schema" in sql:
            self.description = [("table_name",), ("column_name",), ("data_type",)]
            self._rows = list(_SCHEMA_ROWS)
        else:
            self.description = [(c,) for c in _DATA[0]]
            self._rows = list(_DATA[1])

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, broken=False):
        self.broken = broken
        self.closed = False
        self.executed = []

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


def test_schema_groups_information_schema_rows():
    eng = MySqlEngine(lambda: _FakeConn())
    schema = eng.schema()
    assert [t["table"] for t in schema] == ["notes", "tickets"], schema
    tk = next(t for t in schema if t["table"] == "tickets")
    assert tk["columns"] == [{"name": "id", "type": "integer"},
                             {"name": "team", "type": "text"},
                             {"name": "hours", "type": "numeric"}], tk


def test_schema_honors_table_allowlist():
    eng = MySqlEngine(lambda: _FakeConn(), tables=["TICKETS"])   # case-insensitive
    assert [t["table"] for t in eng.schema()] == ["tickets"]


def test_schema_is_cached_per_engine():
    conn = _FakeConn()
    eng = MySqlEngine(lambda: conn)
    eng.schema(); eng.schema()
    assert len([s for s in conn.executed if "information_schema" in s]) == 1, conn.executed


def test_execute_returns_cols_and_rows():
    eng = MySqlEngine(lambda: _FakeConn())
    cols, rows = eng.execute("SELECT team, SUM(hours) AS total_hours FROM tickets GROUP BY team")
    assert cols == ["team", "total_hours"], cols
    assert rows == [("platform", 15.5), ("mobile", 22.0)], rows


def test_execute_reconnects_once_after_dropped_connection():
    dead, live = _FakeConn(broken=True), _FakeConn()
    conns = [dead, live]
    eng = MySqlEngine(lambda: conns.pop(0))
    cols, rows = eng.execute("SELECT team FROM tickets")
    assert dead.closed and live.executed, (dead.closed, live.executed)
    assert cols and rows


def test_delegated_execute_uses_per_credential_connection():
    # ADR 0006: a delegated credential (Entra token) must open its OWN connection as that
    # user (token AS password) — MySQL enforces source-side — never the service conn.
    service = _FakeConn()
    user_conns = {}

    def user_connect(token):
        user_conns.setdefault(token, []).append(_FakeConn())
        return user_conns[token][-1]

    eng = MySqlEngine(lambda: service, user_connect=user_connect)
    cols, rows = eng.execute("SELECT team FROM tickets", credential="tok-alice")
    assert cols and rows
    assert not service.executed, service.executed
    assert user_conns["tok-alice"][0].executed
    eng.execute("SELECT team FROM tickets", credential="tok-alice")
    assert len(user_conns["tok-alice"]) == 1, user_conns          # cached
    eng.execute("SELECT team FROM tickets", credential="tok-bob")
    assert len(user_conns) == 2, user_conns


def test_delegated_fails_closed_without_user_connect():
    eng = MySqlEngine(lambda: _FakeConn())
    try:
        eng.execute("SELECT 1", credential="tok")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "failing closed" in str(e), e


def test_from_config_requires_connection_fields():
    try:
        MySqlEngine.from_config({"host": "h", "database": "d"})
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "user" in str(e) and "password" in str(e), e


def test_from_config_delegated_fails_closed_without_aad_user():
    # SQL-login config (no aad_user) -> the delegated ask fails closed, not as the service
    eng = MySqlEngine.from_config({"host": "h", "database": "d",
                                      "user": "u", "password": "p"})
    # patch the service connect so we never touch the network, then assert OBO refuses
    eng._connect = lambda: _FakeConn()
    try:
        eng.execute("SELECT 1", credential="tok")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "aad_user" in str(e) and "failing closed" in str(e), e


def _fake_driver(module_name, record):
    """A stand-in pymysql whose connect() records the (user, password) it was handed."""
    import types
    fake = types.ModuleType(module_name)

    def connect(*a, **kw):
        record.append((kw.get("user"), kw.get("password")))
        return _FakeConn()

    fake.connect = connect
    return fake


def _with_driver(module_name, fn):
    import sys
    rec = []
    saved = sys.modules.get(module_name)
    sys.modules[module_name] = _fake_driver(module_name, rec)
    try:
        fn()
    finally:
        if saved is not None:
            sys.modules[module_name] = saved
        else:
            sys.modules.pop(module_name, None)
    return rec


def _entra_jwt(upn):
    import base64
    import json
    body = base64.urlsafe_b64encode(json.dumps({"upn": upn}).encode()).decode().rstrip("=")
    return "hdr." + body + ".sig"


def test_delegated_principal_google_cloud_sql_uses_the_session_email():
    """#193: Cloud SQL / Google IAM auth uses an OPAQUE OAuth token with no principal, so the
    delegated connect authenticates as the SESSION email (threaded from the AccessContext)."""
    eng = MySqlEngine.from_config({"host": "h", "database": "d", "user": "svc", "password": "p"})
    rec = _with_driver("pymysql", lambda: eng.execute(
        "SELECT 1", credential="ya29-opaque-google-token", principal="alice@gmail.com"))
    assert rec == [("alice@gmail.com", "ya29-opaque-google-token")], rec


def test_delegated_principal_entra_token_wins_over_the_session():
    """Backward-compat (#189): an Entra JWT carrying the principal wins; the session is only the
    fallback for opaque (Google) tokens."""
    eng = MySqlEngine.from_config({"host": "h", "database": "d", "user": "svc", "password": "p"})
    tok = _entra_jwt("bob@corp.com")
    rec = _with_driver("pymysql", lambda: eng.execute(
        "SELECT 1", credential=tok, principal="someone-else@x.com"))
    assert rec == [("bob@corp.com", tok)], rec


def test_delegated_fails_closed_on_opaque_token_with_no_session_principal():
    eng = MySqlEngine.from_config({"host": "h", "database": "d", "user": "svc", "password": "p"})
    try:
        eng.execute("SELECT 1", credential="ya29-opaque", principal=None)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "failing closed" in str(e), e


def test_provider_builds_federated_store_with_pushdown_mode():
    assert MySqlProvider.kind == "mysql"
    assert tuple(MySqlProvider.modes) == ("pushdown",)
    prov = MySqlProvider(engine_factory=lambda cfg: MySqlEngine(lambda: _FakeConn(),
                                                                      tables=cfg.get("tables")))
    store = prov.build({"id": "tickets-pg", "business_unit": "support", "title": "Tickets",
                        "description": "support tickets", "tables": ["tickets"]})
    p = store.profile()
    assert p.kind == FEDERATED_SQL and p.store_id == "tickets-pg", p
    assert [t["table"] for t in p.schema] == ["tickets"], p.schema
    assert prov.probe({"id": "x", "tables": ["tickets"]}).freshness == "live"


def main():
    print("#158 mysql engine+provider self-test:")
    test_schema_groups_information_schema_rows()
    test_schema_honors_table_allowlist()
    test_schema_is_cached_per_engine()
    test_execute_returns_cols_and_rows()
    print("  PASS  schema introspection (+allowlist) / execute cols+rows")
    test_execute_reconnects_once_after_dropped_connection()
    print("  PASS  dropped-connection resilience: reconnect-once on severed conn")
    test_delegated_execute_uses_per_credential_connection()
    test_delegated_fails_closed_without_user_connect()
    test_from_config_delegated_fails_closed_without_aad_user()
    print("  PASS  delegated query-as-user: per-credential conns / fail-closed (no path / no aad_user)")
    test_delegated_principal_google_cloud_sql_uses_the_session_email()
    test_delegated_principal_entra_token_wins_over_the_session()
    test_delegated_fails_closed_on_opaque_token_with_no_session_principal()
    print("  PASS  #193 Cloud SQL principal: session-email for opaque Google tokens, Entra "
          "token still wins, fail-closed with neither")
    test_from_config_requires_connection_fields()
    test_provider_builds_federated_store_with_pushdown_mode()
    print("  PASS  config validation / provider -> FederatedSqlStore (pushdown)")
    print("\n#158 MYSQL SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
