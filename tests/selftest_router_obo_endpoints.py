"""#107 (OBO endpoints slice) — per-cloud token-exchange adapters behind the E5
TokenExchangePort (ADR 0006: query-as-the-user; the source's own IAM/RLS enforces).
Proven WITHOUT network: the HTTP/STS calls are injected fakes. What these tests pin
is each cloud's exchange CONTRACT — the exact grant/params each IdP requires:

  - Entra OBO   = jwt-bearer grant + requested_token_use=on_behalf_of (NOT RFC 8693)
  - GCP WIF     = RFC 8693 at sts.googleapis.com (+ optional SA impersonation hop)
  - AWS STS     = AssumeRoleWithWebIdentity -> temporary key TRIPLE (JSON-encoded,
                  TokenExchangePort stays str)

Run: python3 tests/selftest_router_obo_endpoints.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router import (  # noqa: E402
    AwsStsExchange, EntraOboExchange, GcpWifExchange,
)


def _subject(user_oid):
    return f"session-jwt-for-{user_oid}"


# ------------------------------------------------------------------ Entra OBO

def test_entra_obo_uses_jwt_bearer_grant_and_caches():
    calls = []

    def post(url, form):
        calls.append((url, form))
        return {"access_token": f"entra-tok-{len(calls)}", "expires_in": 3600}

    x = EntraOboExchange(tenant_id="tid", client_id="cid", client_secret="sec",
                         subject_token_provider=_subject, post=post)
    tok = x.exchange("alice", "https://database.windows.net")
    assert tok == "entra-tok-1", tok
    url, form = calls[0]
    assert url == "https://login.microsoftonline.com/tid/oauth2/v2.0/token", url
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer", form
    assert form["requested_token_use"] == "on_behalf_of", form
    assert form["assertion"] == "session-jwt-for-alice", form
    assert form["scope"] == "https://database.windows.net/.default", form
    assert form["client_id"] == "cid" and form["client_secret"] == "sec", form
    # cached per (user, resource); a different user exchanges separately
    assert x.exchange("alice", "https://database.windows.net") == "entra-tok-1"
    assert x.exchange("bob", "https://database.windows.net") == "entra-tok-2"
    assert len(calls) == 2, calls


# ------------------------------------------------------------------ GCP WIF

def test_gcp_wif_sts_exchange_shape():
    calls = []

    def post(url, form):
        calls.append((url, form))
        return {"access_token": "wif-federated-tok", "expires_in": 3600}

    x = GcpWifExchange(audience="//iam.googleapis.com/projects/1/locations/global/"
                                "workloadIdentityPools/p/providers/x",
                       subject_token_provider=_subject, post=post)
    tok = x.exchange("alice", "bigquery")
    assert tok == "wif-federated-tok", tok
    url, form = calls[0]
    assert url == "https://sts.googleapis.com/v1/token", url
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange", form
    assert form["audience"].startswith("//iam.googleapis.com/"), form
    assert form["subject_token"] == "session-jwt-for-alice", form
    assert form["subject_token_type"] == "urn:ietf:params:oauth:token-type:jwt", form
    assert form["scope"] == "https://www.googleapis.com/auth/cloud-platform", form


def test_gcp_wif_impersonates_service_account_when_configured():
    posts, jposts = [], []

    def post(url, form):
        posts.append(url)
        return {"access_token": "wif-federated-tok", "expires_in": 3600}

    def post_json(url, payload, headers):
        jposts.append((url, payload, headers))
        return {"accessToken": "sa-scoped-tok", "expireTime": "2099-01-01T00:00:00Z"}

    x = GcpWifExchange(audience="//iam.googleapis.com/aud",
                       subject_token_provider=_subject,
                       service_account="bq-reader@p.iam.gserviceaccount.com",
                       post=post, post_json=post_json)
    tok = x.exchange("alice", "bigquery")
    assert tok == "sa-scoped-tok", tok            # the impersonation hop's token wins
    url, payload, headers = jposts[0]
    assert url == ("https://iamcredentials.googleapis.com/v1/projects/-/"
                   "serviceAccounts/bq-reader@p.iam.gserviceaccount.com"
                   ":generateAccessToken"), url
    assert headers["Authorization"] == "Bearer wif-federated-tok", headers
    assert payload["scope"] == ["https://www.googleapis.com/auth/cloud-platform"], payload


# ------------------------------------------------------------------ AWS STS

class _FakeSts:
    def __init__(self):
        self.calls = []

    def assume_role_with_web_identity(self, **kw):
        self.calls.append(kw)
        return {"Credentials": {"AccessKeyId": "AKIA1", "SecretAccessKey": "S3CR3T",
                                "SessionToken": "SESSTOK",
                                "Expiration": "2099-01-01T00:00:00Z"}}


def test_aws_sts_assume_role_returns_json_triple():
    sts = _FakeSts()
    x = AwsStsExchange(role_arn="arn:aws:iam::710731957683:role/dbsearch-reader",
                       subject_token_provider=_subject, sts_client_factory=lambda: sts)
    cred = json.loads(x.exchange("alice", "redshift"))
    assert cred == {"access_key_id": "AKIA1", "secret_access_key": "S3CR3T",
                    "session_token": "SESSTOK"}, cred
    call = sts.calls[0]
    assert call["RoleArn"].endswith("role/dbsearch-reader"), call
    assert call["WebIdentityToken"] == "session-jwt-for-alice", call
    assert call["RoleSessionName"].startswith("dbsearch-"), call
    # per-user session names keep the source's CloudTrail attribution per user
    x.exchange("bob", "redshift")
    assert sts.calls[1]["RoleSessionName"] != call["RoleSessionName"], sts.calls
    # cached: same user again -> no third STS call
    x.exchange("alice", "redshift")
    assert len(sts.calls) == 2, sts.calls


# ------------------------------------------------------- engines consume the credential

def test_federated_store_threads_delegated_credential_to_engine():
    from dbsearch.router import FederatedSqlStore
    from dbsearch.router.store import AccessContext
    from dbsearch.router.structured import SqlEnginePort

    class Eng(SqlEnginePort):
        def __init__(self):
            self.creds = []
            self.principals = []

        def schema(self):
            return [{"table": "sales", "columns": [{"name": "amount", "type": "int"}]}]

        def execute(self, sql, credential=None, principal=None):
            self.creds.append(credential)
            self.principals.append(principal)       # #193: the session identity is threaded too
            return ["amount"], [(1,)]

    eng = Eng()
    store = FederatedSqlStore(
        "s", "bu", "t", "d", engine=eng,
        authorizer=lambda u: AccessContext(user_oid=u, principals=[],
                                           delegated_credential=f"tok-{u}"))
    store.retrieve(store.authorize("alice"), "total amount")
    assert eng.creds == ["tok-alice"], eng.creds
    # #193: the session principal (the signed-in identity) reaches the engine, so a Cloud SQL /
    # Google delegated connect can authenticate as the user even with an opaque OAuth token.
    assert eng.principals == ["alice"], eng.principals
    # the audit records THAT the query was delegated — never the credential (LAW 8)
    rec = store.audit_trail[-1]
    assert rec["delegated"] is True, rec
    assert "tok-alice" not in str(rec), rec


def test_bigquery_executes_as_user_when_credential_present():
    from dbsearch.router import BigQueryEngine

    class _Result:
        schema = []

        def __iter__(self):
            return iter([])

    class _Client:
        def __init__(self):
            self.sqls = []

        def query(self, sql, job_config=None):
            self.sqls.append(sql)

            class _J:
                @staticmethod
                def result():
                    return _Result()
            return _J()

    svc, user = _Client(), _Client()
    made = []
    eng = BigQueryEngine(lambda: svc, project="p", dataset="d",
                         user_client_factory=lambda cred: made.append(cred) or user)
    eng.execute("SELECT 1", credential="u-tok")
    assert made == ["u-tok"] and user.sqls == ["SELECT 1"], (made, user.sqls)
    assert svc.sqls == [], svc.sqls      # the service identity never ran the user query


def test_redshift_executes_with_sts_triple():
    from dbsearch.router import RedshiftEngine

    svc, user = _FakeSts(), None   # placeholder; build data-api fakes below

    class _Api:
        def __init__(self):
            self.executed = []

        def execute_statement(self, **kw):
            self.executed.append(kw)
            return {"Id": "s1"}

        def describe_statement(self, *, Id):
            return {"Status": "FINISHED", "HasResultSet": False}

    svc_api, user_api = _Api(), _Api()
    made = []
    eng = RedshiftEngine(lambda: svc_api, workgroup="wg", database="dev",
                         poll_interval=0,
                         user_client_factory=lambda cred: made.append(cred) or user_api)
    triple = json.dumps({"access_key_id": "A", "secret_access_key": "S",
                         "session_token": "T"})
    eng.execute("SELECT 1", credential=triple)
    assert made and made[0]["access_key_id"] == "A", made   # parsed triple, not raw json
    assert user_api.executed and not svc_api.executed, (user_api.executed,
                                                        svc_api.executed)


def test_databricks_connects_as_user():
    from dbsearch.router import DatabricksEngine

    class _Conn:
        def __init__(self):
            self.sqls = []

        def cursor(self):
            conn = self

            class _Cur:
                description = [("c",)]

                @staticmethod
                def execute(sql):
                    conn.sqls.append(sql)

                @staticmethod
                def fetchall():
                    return []
            return _Cur()

        def close(self):
            pass

    svc, user = _Conn(), _Conn()
    made = []
    eng = DatabricksEngine(lambda: svc, catalog="c", schema="s",
                           user_connect=lambda cred: made.append(cred) or user)
    eng.execute("SELECT 1", credential="u-pat")
    assert made == ["u-pat"] and user.sqls == ["SELECT 1"], (made, user.sqls)
    assert svc.sqls == [], svc.sqls


def test_azure_sql_fails_closed_on_delegated_credential():
    # pymssql cannot present an AAD bearer — running as the service account instead
    # would VIOLATE the delegation promise (LAW 2), so the engine must fail closed.
    from dbsearch.router import AzureSqlEngine

    eng = AzureSqlEngine(lambda: (_ for _ in ()).throw(AssertionError("no connect")))
    try:
        eng.execute("SELECT 1", credential="aad-tok")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "pyodbc" in str(e) and "closed" in str(e).lower(), e


# ------------------------------------------------------- manifest delegation wiring

class _Identity:
    def expand_groups(self, user_oid):
        return [user_oid, "all-staff"]


def test_env_subject_token_provider_per_user_then_shared_then_fails():
    import os

    from dbsearch.router import env_subject_token_provider

    os.environ["DBSEARCH_SUBJECT_TOKEN_ALICE"] = "alice-jwt"
    os.environ["DBSEARCH_SUBJECT_TOKEN"] = "shared-jwt"
    try:
        assert env_subject_token_provider("alice") == "alice-jwt"
        assert env_subject_token_provider("bob") == "shared-jwt"
        del os.environ["DBSEARCH_SUBJECT_TOKEN"]
        try:
            env_subject_token_provider("bob")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "DBSEARCH_SUBJECT_TOKEN" in str(e), e
    finally:
        os.environ.pop("DBSEARCH_SUBJECT_TOKEN_ALICE", None)
        os.environ.pop("DBSEARCH_SUBJECT_TOKEN", None)


def test_register_delegations_builds_adapters_and_broker_resets():
    from dbsearch.router import (
        AwsStsExchange, EntraOboExchange, GcpWifExchange, IdentityBroker,
        StaticTokenExchange, register_delegations,
    )

    spec = {"tenant": "t", "stores": [
        {"id": "s-az", "kind": "azure_sql", "business_unit": "b", "acl": ["g"],
         "delegation": {"kind": "entra_obo", "tenant_id": "tid", "client_id": "cid",
                        "client_secret": "sec",
                        "resource": "https://database.windows.net"}},
        {"id": "s-bq", "kind": "bigquery", "business_unit": "b", "acl": ["g"],
         "delegation": {"kind": "gcp_wif", "audience": "//iam.googleapis.com/aud"}},
        {"id": "s-rs", "kind": "redshift", "business_unit": "b", "acl": ["g"],
         "delegation": {"kind": "aws_sts", "role_arn": "arn:aws:iam::1:role/r"}},
        {"id": "s-dev", "kind": "csv", "business_unit": "b", "acl": ["g"],
         "delegation": {"kind": "static", "tokens": {"alice": "tok-a"}}},
        {"id": "s-plain", "kind": "csv", "business_unit": "b", "acl": ["g"]},
    ]}
    broker = IdentityBroker(_Identity())
    register_delegations(spec, broker, subject_token_provider=_subject)
    kinds = {sid: type(x).__name__ for sid, (x, _res) in broker._delegations.items()}
    assert kinds == {"s-az": "EntraOboExchange", "s-bq": "GcpWifExchange",
                     "s-rs": "AwsStsExchange", "s-dev": "StaticTokenExchange"}, kinds
    # the static path resolves end-to-end through the broker
    ctx = broker.access_for("alice", "s-dev")
    assert ctx.delegated_credential == "tok-a", ctx
    # reset() empties registrations (recompose must not inherit stale delegations)
    broker.reset()
    assert broker.access_for("alice", "s-dev").delegated_credential is None
    # unknown kind is a manifest error, not a silent no-delegation
    bad = {"tenant": "t", "stores": [{"id": "x", "kind": "csv", "business_unit": "b",
                                      "acl": [],
                                      "delegation": {"kind": "wat"}}]}
    try:
        register_delegations(bad, broker, subject_token_provider=_subject)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "wat" in str(e), e


def test_register_delegations_refuses_google_refresh_with_legacy_provider():
    """Belt-and-braces (#193 Task 4, carried from Task 3's review): `_for_idp` treats a
    1-arg provider as the legacy env dev seam REGARDLESS of which idp was requested - so a
    google_refresh block composed with a 1-arg provider would silently redeem whatever that
    provider hands back (e.g. the vaulted ENTRA refresh token) against Google's token
    endpoint. `register_delegations` must refuse to compose that store at all, rather than
    build an exchange that would leak the wrong cloud's credential on first query."""
    from dbsearch.router import IdentityBroker, register_delegations

    broker = IdentityBroker(_Identity())
    spec = {"tenant": "t", "stores": [
        {"id": "s-bq", "kind": "bigquery", "business_unit": "b", "acl": ["g"],
         "delegation": {"kind": "google_refresh", "client_id": "cid",
                        "client_secret": "csec"}},
    ]}
    try:
        register_delegations(spec, broker, subject_token_provider=_subject)  # 1-arg
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "google_refresh" in str(e) and "idp" in str(e), e
    assert broker._delegations == {}, broker._delegations   # nothing partially registered

    # the legacy 1-arg provider must keep working for entra_* and static kinds (unaffected)
    spec_ok = {"tenant": "t", "stores": [
        {"id": "s-az", "kind": "azure_sql", "business_unit": "b", "acl": ["g"],
         "delegation": {"kind": "entra_refresh", "tenant_id": "tid", "client_id": "cid",
                        "client_secret": "sec"}},
        {"id": "s-dev", "kind": "csv", "business_unit": "b", "acl": ["g"],
         "delegation": {"kind": "static", "tokens": {"alice": "tok-a"}}},
    ]}
    register_delegations(spec_ok, broker, subject_token_provider=_subject)
    assert set(broker._delegations) == {"s-az", "s-dev"}, broker._delegations

    # a 2-arg (oid, idp=...) provider is exactly what google_refresh needs - must compose fine
    broker2 = IdentityBroker(_Identity())
    register_delegations(
        {"tenant": "t", "stores": [
            {"id": "s-bq2", "kind": "bigquery", "business_unit": "b", "acl": ["g"],
             "delegation": {"kind": "google_refresh", "client_id": "cid",
                            "client_secret": "csec"}}]},
        broker2, subject_token_provider=lambda oid, idp="entra": "rt-" + idp)
    assert set(broker2._delegations) == {"s-bq2"}, broker2._delegations


def test_provider_binds_store_authorizer_to_broker():
    from dbsearch.router import (
        AzureSqlEngine, AzureSqlProvider, IdentityBroker, StaticTokenExchange,
    )

    broker = IdentityBroker(_Identity())
    broker.register_delegation("sales-az", StaticTokenExchange("st-tok"), "res")
    prov = AzureSqlProvider(
        broker=broker,
        engine_factory=lambda cfg: AzureSqlEngine(lambda: None))
    store = prov.build({"id": "sales-az", "business_unit": "b"})
    ctx = store.authorize("alice")
    assert ctx.delegated_credential == "st-tok", ctx
    assert "all-staff" in ctx.principals, ctx      # broker expands real groups too


def test_entra_refresh_uses_refresh_grant_caches_and_rotates():
    calls = []
    rotated = []

    def fake_post(url, form):
        calls.append((url, form))
        return {"access_token": f"db-tok-{len(calls)}", "expires_in": 3600,
                "refresh_token": "rt-rotated"}

    from dbsearch.router import EntraRefreshExchange
    x = EntraRefreshExchange("tid", "cid", "sec",
                             lambda oid: f"rt-for-{oid}",
                             on_rotate=lambda oid, rt: rotated.append((oid, rt)),
                             post=fake_post)
    tok = x.exchange("alice", "https://database.windows.net")
    assert tok == "db-tok-1"
    url, form = calls[0]
    assert url == "https://login.microsoftonline.com/tid/oauth2/v2.0/token"
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "rt-for-alice"
    assert form["scope"] == "https://database.windows.net/.default offline_access"
    assert form["client_id"] == "cid" and form["client_secret"] == "sec"
    # rotation written back
    assert rotated == [("alice", "rt-rotated")]
    # cached: second call, no new POST
    assert x.exchange("alice", "https://database.windows.net") == "db-tok-1"
    assert len(calls) == 1
    # different user = different exchange
    assert x.exchange("bob", "https://database.windows.net") == "db-tok-2"


def test_http_post_form_surfaces_the_idp_reason_not_a_bare_400():
    """#205: urllib raises HTTPError on 4xx WITHOUT reading the body — but the body is the
    whole diagnostic. Swallowing it collapsed every delegated-auth failure into an identical,
    useless 'HTTP Error 400: Bad Request'."""
    import io
    from urllib import error as urlerror

    from dbsearch.router.identity_broker import TokenExchangeError, http_post_form

    body = json.dumps({"error": "invalid_grant",
                       "error_description": "AADSTS700082: The refresh token has expired.",
                       "correlation_id": "abc-123"}).encode()

    def boom(req, timeout=None):
        raise urlerror.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))

    import urllib.request as urlrequest
    real = urlrequest.urlopen
    urlrequest.urlopen = boom
    try:
        http_post_form("https://login.microsoftonline.com/t/oauth2/v2.0/token", {"a": "b"})
        raise AssertionError("expected TokenExchangeError")
    except TokenExchangeError as exc:
        assert exc.status == 400 and exc.error == "invalid_grant", exc
        assert "AADSTS700082" in exc.description, exc
        assert "abc-123" in str(exc), exc          # correlation id reaches the operator
        assert exc.expired_grant is True
    finally:
        urlrequest.urlopen = real


def test_expired_refresh_token_tells_the_user_to_sign_in_again():
    """A dead grant is the one failure the USER can fix. It must say so, not hand them an
    AADSTS code. A consent/config failure keeps the IdP's own wording (operator problem)."""
    from dbsearch.router import EntraRefreshExchange
    from dbsearch.router.identity_broker import TokenExchangeError

    def dead_grant(url, form):
        raise TokenExchangeError(400, "invalid_grant",
                                 "AADSTS700082: The refresh token has expired.", "cid-9")

    x = EntraRefreshExchange("tid", "cid", "sec", lambda oid: "rt", post=dead_grant)
    try:
        x.exchange("alice", "https://database.windows.net")
        raise AssertionError("expected TokenExchangeError")
    except TokenExchangeError as exc:
        assert "sign in again" in str(exc), exc
        assert "AADSTS700082" in str(exc), exc     # the raw reason is kept, not hidden

    def no_consent(url, form):
        raise TokenExchangeError(400, "invalid_client", "AADSTS7000215: Invalid client secret.")

    y = EntraRefreshExchange("tid", "cid", "sec", lambda oid: "rt", post=no_consent)
    try:
        y.exchange("alice", "https://database.windows.net")
        raise AssertionError("expected TokenExchangeError")
    except TokenExchangeError as exc:
        assert "sign in again" not in str(exc), exc
        assert "Invalid client secret" in str(exc), exc


def test_exchange_from_config_entra_refresh_kind():
    from dbsearch.router import EntraRefreshExchange
    from dbsearch.router.identity_broker import exchange_from_config
    x, res = exchange_from_config(
        {"kind": "entra_refresh", "tenant_id": "t", "client_id": "c",
         "client_secret": "s"},
        lambda oid: "rt", on_rotate=lambda oid, rt: None)
    assert isinstance(x, EntraRefreshExchange)
    assert res == "https://database.windows.net"


def main():
    print("#107 OBO cloud-endpoint adapters self-test:")
    test_entra_obo_uses_jwt_bearer_grant_and_caches()
    print("  PASS  Entra OBO: jwt-bearer grant / on_behalf_of / .default scope / cache")
    test_gcp_wif_sts_exchange_shape()
    test_gcp_wif_impersonates_service_account_when_configured()
    print("  PASS  GCP WIF: RFC 8693 at sts.googleapis.com + SA impersonation hop")
    test_aws_sts_assume_role_returns_json_triple()
    print("  PASS  AWS STS: AssumeRoleWithWebIdentity -> JSON key triple, per-user "
          "session names, cached")
    test_federated_store_threads_delegated_credential_to_engine()
    test_bigquery_executes_as_user_when_credential_present()
    test_redshift_executes_with_sts_triple()
    test_databricks_connects_as_user()
    test_azure_sql_fails_closed_on_delegated_credential()
    print("  PASS  engines consume the credential: store threads it / bq+rs+dbx query "
          "as the user / azure fails CLOSED / audit is metadata-only")
    test_env_subject_token_provider_per_user_then_shared_then_fails()
    test_register_delegations_builds_adapters_and_broker_resets()
    test_register_delegations_refuses_google_refresh_with_legacy_provider()
    test_provider_binds_store_authorizer_to_broker()
    print("  PASS  wiring: env subject-token seam / delegation block -> adapters + "
          "broker reset / provider binds authorize() to the broker / google_refresh "
          "refuses a legacy 1-arg provider (belt-and-braces, #193)")
    test_entra_refresh_uses_refresh_grant_caches_and_rotates()
    test_exchange_from_config_entra_refresh_kind()
    test_http_post_form_surfaces_the_idp_reason_not_a_bare_400()
    test_expired_refresh_token_tells_the_user_to_sign_in_again()
    print("  PASS  #205: token-exchange failures carry the IdP's OWN reason (AADSTS + "
          "correlation id), and a dead grant says 'sign in again' instead of a bare 400")
    print("  PASS  Entra refresh: refresh_token grant per user, cached, rotation "
          "write-back, entra_refresh manifest kind")
    print("\n#107 OBO ENDPOINTS SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
