"""ADR 0012 (#389): the document plane is PARTITIONED, so foreign orgs may ingest.

This file used to pin the opposite rule - `_require_home_tenant`, which 403'd every
foreign tenant off the ingest surfaces because the index was unpartitioned. That gate is
lifted here, and the tests are rewritten to the new contract rather than deleted, because
the interesting assertions are the ones that survive the lift:

  - a foreign tid may now ingest, and its documents land in ITS OWN partition (proved by
    querying as each identity over the SAME server - the isolation property, at the HTTP
    layer, not just in the index unit tests);
  - a session with NO tid but a real oid (Google, local email/password) now gets its OWN
    `acct:<oid>` partition and CAN ingest (ADR 0018, #573). Before ADR 0018, `resolve_tenant`
    failed such a session closed to `""`, and `""` as an ingest target would have been a
    bucket SHARED by every tid-less identity on the box - so the write was refused
    (`_require_partitioned_tenant`). ADR 0018 replaces the shared `""` bucket with a
    partition keyed by the session's own oid, which is unique per account, so the same
    co-mingling risk that justified the old refusal cannot occur under the new rule: see
    `test_missing_tid_gets_own_account_partition` below for the isolation proof and the
    reasoning written out in full. A session with NEITHER tid NOR oid still resolves to
    `""` in `resolve_tenant` (no account to key a partition off of) - but at THIS layer
    such a session never reaches that check at all, because `current_user` 401s it first
    on identity, not tenant; that branch is covered directly against `resolve_tenant`,
    unit-level, in tests/selftest_573_acct_partition.py;
  - an api key still cannot ingest unless its owner is an operator - a key carries no
    Entra tid, so a non-operator key resolves to no partition. The old two-call bypass
    (403 with the cookie, mint a key, 200 with the key) stays closed for a different and
    more structural reason than before.

    PYTHONPATH=src python3 tests/selftest_doc_plane_tenant_gate.py
"""
import os, sys, time
from pathlib import Path
os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from fastapi.testclient import TestClient  # noqa: E402
from dbsearch.server import user_auth  # noqa: E402
from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)
_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "DBSEARCH_OPERATOR_OIDS")
DOC = {"external_id": "d1", "title": "t", "text": "hello", "acl": ["g"], "uri": "u"}
NO_PARTITION = "no tenant"      # the fragment _require_partitioned_tenant's detail carries


def _login_env(on: bool, operators: str = ""):
    for k in _VARS:
        os.environ.pop(k, None)
    if on:
        os.environ.update({"AUTH_TENANT_ID": "home-tid", "AUTH_CLIENT_ID": "cid",
                           "AUTH_CLIENT_SECRET": "sec"})
    if operators:
        os.environ["DBSEARCH_OPERATOR_OIDS"] = operators


def _cookie(tid, oid="u-1"):
    payload = {"oid": oid, "exp": int(time.time()) + 3600}
    if tid is not None:
        payload["tid"] = tid
    return {user_auth.COOKIE: user_auth.sign_session(payload)}


def _mint_key(cookies, label="cli") -> str:
    """Mint an api key through the REAL /developer/keys route, as that session."""
    r = client.post("/developer/keys", json={"label": label}, cookies=cookies)
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()["token"]


FINISH = {"tenant": "x", "drive_id": "d-1"}


def test_foreign_tid_may_now_ingest():
    """THE LIFT. Every surface the old gate closed - a foreign org reaches all of them now.
    SharePoint surfaces answer 503 (no SP_CONNECTOR_* configured on this rig) rather than
    403: the point is that the TENANT is no longer the reason they are refused."""
    _login_env(True)
    client.cookies.clear()
    ck = _cookie("other-tid", "stranger-9")
    r = client.post("/ingest", json=DOC, cookies=ck)
    assert r.status_code == 200, (r.status_code, r.text)
    for call in (lambda: client.get("/connectors/sharepoint/consent", cookies=ck,
                                    follow_redirects=False),
                 lambda: client.get("/connectors/sharepoint/consent-url", cookies=ck),
                 lambda: client.get("/connectors/sharepoint/drives?tenant=x", cookies=ck),
                 lambda: client.post("/connectors/sharepoint/finish", json=FINISH, cookies=ck)):
        resp = call()
        assert resp.status_code != 403, \
            f"a foreign tenant is still gated off a document surface: {resp.status_code} {resp.text}"


def test_a_foreign_ingest_lands_in_its_own_partition():
    """The isolation property that made the lift safe, driven over HTTP: two tenants ingest
    into one server, and neither can retrieve the other's document - even though both ACLs
    name the SAME group, so the LAW-2 trim alone would NOT have separated them."""
    _login_env(True)
    client.cookies.clear()
    shared_acl = ["all-staff"]
    home = _cookie("home-tid", "malcolm-1")
    foreign = _cookie("other-tid", "stranger-9")

    assert client.post("/ingest", cookies=home, json={
        "external_id": "home-doc", "title": "Home Doc", "acl": shared_acl,
        "text": "adr0012 partition probe home widget revenue", "uri": "u-home"}).status_code == 200
    assert client.post("/ingest", cookies=foreign, json={
        "external_id": "foreign-doc", "title": "Foreign Doc", "acl": shared_acl,
        "text": "adr0012 partition probe foreign widget revenue", "uri": "u-foreign"}).status_code == 200

    q = {"question": "adr0012 partition probe widget revenue"}
    h = client.post("/search", json=q, cookies=home).json()
    f = client.post("/search", json=q, cookies=foreign).json()
    assert "foreign-doc" not in h["authorized_docs"], \
        f"home retrieved the foreign tenant's document: {h['authorized_docs']}"
    assert "home-doc" not in f["authorized_docs"], \
        f"the foreign tenant retrieved home's document: {f['authorized_docs']}"


def test_missing_tid_gets_own_account_partition():
    """ADR 0018 (#573) flips this from the old pinned contract. A no-tid session used to
    be refused here (403, `""` is not a real partition). It is no longer refused: it now
    ingests into `acct:<its own oid>`, a partition keyed by an identity that is unique per
    account by construction (a signed session's oid is the account key - ADR 0013).

    Why the flip cannot reopen the co-mingling risk the old gate existed to prevent: the
    old refusal existed because `""` is a SINGLE bucket that every tid-less identity would
    have shared - two different Google users would have landed in the same partition and
    been able to read each other's documents. `acct:<oid>` is not a single bucket; it is a
    distinct partition PER oid, so two different no-tid identities land in two different
    partitions, same as two different foreign tids already do (see
    `test_a_foreign_ingest_lands_in_its_own_partition` above). Proved at the HTTP layer
    below: two different no-tid identities ingest documents with the SAME (wide) ACL, and
    neither can retrieve the other's - the ACL trim alone would not separate them, so the
    partition trim is what holds, exactly as ADR 0012 proved for foreign tids."""
    _login_env(True)
    client.cookies.clear()
    shared_acl = ["all-staff"]
    a = _cookie(None, oid="no-tid-a")
    b = _cookie(None, oid="no-tid-b")

    r = client.post("/ingest", json=DOC, cookies=a)
    assert r.status_code == 200, (r.status_code, r.text)

    assert client.post("/ingest", cookies=a, json={
        "external_id": "acct-a-doc", "title": "A Doc", "acl": shared_acl,
        "text": "adr0018 acct partition probe alpha widget revenue", "uri": "u-a"}).status_code == 200
    assert client.post("/ingest", cookies=b, json={
        "external_id": "acct-b-doc", "title": "B Doc", "acl": shared_acl,
        "text": "adr0018 acct partition probe bravo widget revenue", "uri": "u-b"}).status_code == 200

    q = {"question": "adr0018 acct partition probe widget revenue"}
    ra = client.post("/search", json=q, cookies=a).json()
    rb = client.post("/search", json=q, cookies=b).json()
    assert "acct-b-doc" not in ra["authorized_docs"], \
        f"no-tid account A retrieved no-tid account B's document: {ra['authorized_docs']}"
    assert "acct-a-doc" not in rb["authorized_docs"], \
        f"no-tid account B retrieved no-tid account A's document: {rb['authorized_docs']}"


# A session with neither tid nor oid never reaches `_require_partitioned_tenant` at all -
# `current_user` (identity resolution) 401s it first, since a session with no oid carries
# no identity to authenticate as (`resolve_identity` / `_session_oid`). That branch of
# `resolve_tenant` (no tid, no oid -> "") is therefore a unit-level case, not an HTTP-level
# one; it is covered directly against `resolve_tenant` in
# tests/selftest_573_acct_partition.py::test_a_session_with_neither_tid_nor_oid_fails_closed.


def test_home_tid_passes():
    _login_env(True)
    client.cookies.clear()
    r = client.post("/ingest", json=DOC, cookies=_cookie("home-tid"))
    assert r.status_code == 200, (r.status_code, r.text)


def test_dev_rig_untouched():
    _login_env(False)
    client.cookies.clear()
    r = client.post("/ingest", json=DOC, headers={"X-DBSearch-User": "alice"})
    assert r.status_code == 200, (r.status_code, r.text)


def test_the_real_callback_mints_a_session_that_can_ingest():
    """C1 regression, still worth its keep: the other tests hand-mint cookies, so they say
    nothing about SIGN-IN - and the callback was once dropping `tid` entirely. Under ADR 0012
    a dropped tid no longer 403s the home user by tenant MISMATCH; it 403s them for having no
    partition at all. Either way the first real sign-in must not be the thing that finds out."""
    _login_env(True)
    client.cookies.clear()
    from dbsearch.server import app as app_mod
    saved = (app_mod._state_ok, user_auth.exchange_code,
             user_auth.fetch_member_principals, user_auth.fetch_principal_facts)
    app_mod._state_ok = lambda request, params: True
    user_auth.exchange_code = lambda code: {"oid": "op-1", "name": "Op", "email": "op@x.test",
                                            "tid": "home-tid"}
    user_auth.fetch_member_principals = lambda tid, oid: []
    user_auth.fetch_principal_facts = lambda tid, oids: {}
    try:
        r = client.get("/auth/callback?code=c&state=s", follow_redirects=False)
        assert r.status_code == 302, (r.status_code, r.text)
        token = r.cookies.get(user_auth.COOKIE)
        assert token, "the callback set no session cookie"
        sess = user_auth.read_session(token)
        assert sess and sess.get("tid") == "home-tid", \
            f"the callback dropped the verified tid from the session: {sess}"
        g = client.post("/ingest", json=DOC, cookies={user_auth.COOKIE: token})
        assert g.status_code == 200, \
            f"a REAL sign-in cannot ingest: {g.status_code} {g.text}"
    finally:
        (app_mod._state_ok, user_auth.exchange_code,
         user_auth.fetch_member_principals, user_auth.fetch_principal_facts) = saved
        client.cookies.clear()


def test_an_operator_owned_api_key_can_ingest():
    """I2: an operator-issued `dbk_` key carries no Entra tid and never will. `resolve_tenant`
    maps it to the deployment constant, so the operator's scripted ingest (e2edbs, CLI) keeps
    working. Minted through the REAL route - a stubbed resolver would not test ownership."""
    _login_env(True, operators="op-1")
    client.cookies.clear()
    token = _mint_key(_cookie("home-tid", "op-1"))
    client.cookies.clear()             # prove the KEY authenticates, not a stray session
    r = client.post("/ingest", json=DOC, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, (r.status_code, r.text)
    bad = client.post("/ingest", json=DOC, headers={"Authorization": "Bearer dbk_nope"})
    assert bad.status_code in (401, 403), (bad.status_code, bad.text)


def test_a_non_operator_key_still_cannot_ingest():
    """C5, structurally re-founded. A foreign user may now ingest WITH THEIR SESSION (that is
    the lift), but a key they mint themselves carries no tid, so it names no partition and the
    write is refused. The old two-call bypass stays closed - not by a tenant comparison this
    time, but because there is nowhere for the chunks to go."""
    _login_env(True, operators="op-1")
    client.cookies.clear()
    foreign = _cookie("other-tid", "stranger-9")
    token = _mint_key(foreign, label="mine")
    client.cookies.clear()
    r = client.post("/ingest", json=DOC, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, \
        f"a self-issued key ingested into an unnamed partition: {r.status_code} {r.text}"
    assert NO_PARTITION in r.json()["detail"], r.text


if __name__ == "__main__":
    try:
        test_foreign_tid_may_now_ingest()
        test_a_foreign_ingest_lands_in_its_own_partition()
        test_missing_tid_gets_own_account_partition()
        test_home_tid_passes()
        test_dev_rig_untouched()
        test_the_real_callback_mints_a_session_that_can_ingest()
        test_an_operator_owned_api_key_can_ingest()
        test_a_non_operator_key_still_cannot_ingest()
    finally:
        for k in _VARS:
            os.environ.pop(k, None)
    print("OK selftest_doc_plane_tenant_gate (ADR 0012: partitioned, foreign ingest allowed)")
