"""#576 - the retention sweep: any access counts, 3 silent days deletes the workspace data.

The decisive design choice (owner, explicit): activity means ANY access, not just the
owner logging in - a colleague querying a document that was shared with them keeps the
SHARER's workspace alive. The motivating case is an HR policy uploaded once and then used
by colleagues for weeks; deleting it on day 3 because the uploader never came back would
be wrong.

Code review (260807) found the first version of this sweep could delete a WHOLE COMPANY'S
corpus (Finding 2): `owner_oid` is ADR 0012 attribution, not an ACL, and a #575 "My
organization" upload or a SharePoint-connected library has no single owner whose silence
should ever cost the org its documents. The owner's ruling, implemented here: the sweep
touches ONLY an account's own private `acct:<id>` partition (ADR 0018) - never the home
tenant, never any other partition - and ANY retrieval (not just an explicit grant) touches
the returned documents' owners. `test_the_home_tenant_partition_is_never_touched_by_the_sweep`
and the two real-HTTP retrieval tests pin that down.

The property that matters most beyond that: an account with NO activity row must NEVER be
swept - unknown is not ancient, and the first sweep after this feature ships must delete
nothing for the entire pre-existing installed base.
`test_an_account_with_no_activity_row_is_never_swept` is the test that pins that down.

    PYTHONPATH=src python3 tests/selftest_576_retention_sweep.py
"""
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

import dbsearch.server.app as appmod  # noqa: E402
from dbsearch.api.auth import ACCT_TENANT_PREFIX  # noqa: E402
from dbsearch.server import retention, user_auth  # noqa: E402
from dbsearch.server.accounts import InMemoryAccountStore  # noqa: E402
from dbsearch.query.conversation import Turn  # noqa: E402

DAY = 86400.0

_LOGIN_VARS = ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET")
_HOME_TID = "tid-home-576"


@contextmanager
def _real_login(operator_oids: str = ""):
    """Turns on `real_login_enabled()` (required for `resolve_tenant`'s acct: partitioning,
    `is_operator`'s real gating, and now `retention.sweep`'s own explicit no-real-login
    disablement - Finding 6) and optionally seeds DBSEARCH_OPERATOR_OIDS. Restores every
    mutated var, whatever it was before, in a finally - never a blind `pop`."""
    saved = {k: os.environ.get(k) for k in _LOGIN_VARS + ("DBSEARCH_OPERATOR_OIDS",)}
    os.environ.update({"AUTH_TENANT_ID": _HOME_TID, "AUTH_CLIENT_ID": "cid-576",
                       "AUTH_CLIENT_SECRET": "sec-576"})
    if operator_oids:
        os.environ["DBSEARCH_OPERATOR_OIDS"] = operator_oids
    else:
        os.environ.pop("DBSEARCH_OPERATOR_OIDS", None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _fresh_accounts(store=None):
    """Swaps a hermetic account store onto appmod.ACCOUNTS for the duration, so each
    test's activity rows are isolated from every other test AND from whatever the real
    deployment singleton holds. Restored in a finally. Pass `store` to swap in a custom
    double (Finding 3/4's tests need one); default is a plain InMemoryAccountStore."""
    saved = appmod.ACCOUNTS
    fresh = store if store is not None else InMemoryAccountStore()
    appmod.ACCOUNTS = fresh
    try:
        yield fresh
    finally:
        appmod.ACCOUNTS = saved


class _FakeManifestStore:
    """A manifest store double: `sweep()` only ever calls `.delete(owner)` on it, and this
    test suite cares about WHICH accounts that was called for, not manifest persistence
    itself (Task #368 territory, not this task's)."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, owner: str) -> None:
        self.deleted.append(owner)


class _RacyAccounts(InMemoryAccountStore):
    """#576 review Finding 3 (TOCTOU): simulates "a user signs in WHILE the sweep is
    running". The instant `silent_accounts()` hands back its snapshot - the exact moment
    the real sweep would start deleting - the target account's clock gets touched again,
    exactly as if a real request had landed in that window."""

    def __init__(self, race_account: str, race_at: float) -> None:
        super().__init__()
        self._race_account = race_account
        self._race_at = race_at
        self._raced_once = False

    def silent_accounts(self, cutoff_epoch: float) -> list:
        out = super().silent_accounts(cutoff_epoch)
        if not self._raced_once and self._race_account in out:
            self._raced_once = True
            self.touch_activity(self._race_account, now=self._race_at)
        return out


class _FailingAccounts:
    """#576 review Finding 4: a store double whose `touch_activity` always raises -
    simulates "the account store is down" for the circuit-breaker test."""

    def __init__(self) -> None:
        self.attempts = 0

    def touch_activity(self, account_id: str, now=None) -> None:
        self.attempts += 1
        raise RuntimeError("store down")


def _cookies(oid: str, tid: str = ""):
    return {user_auth.COOKIE: user_auth.sign_session(
        {"oid": oid, "tid": tid, "exp": int(time.time()) + 3600})}


def test_a_silent_workspace_is_swept_and_an_active_one_is_spared():
    """Alice uploads at t0 and never returns; Bob uploads at t0 and touches again at
    t0+2.5d. A sweep at t0+3.5d (3-day retention) must delete Alice's workspace data and
    leave Bob's fully intact - checked both directly (docs_owned_by) and through the real
    /search surface each of them would actually use."""
    t0 = 1_700_000_000.0
    alice = "acct-t1-alice-576"
    bob = "acct-t1-bob-576"

    with _fresh_accounts() as accounts, _real_login():
        accounts.touch_activity(alice, now=t0)
        accounts.touch_activity(bob, now=t0)
        accounts.touch_activity(bob, now=t0 + 2.5 * DAY)

        edition = appmod._edition
        part_alice = ACCT_TENANT_PREFIX + alice
        part_bob = ACCT_TENANT_PREFIX + bob
        edition.ingest_document(external_id="t1-alice-doc", title="Alice HR policy",
                                text="vacation policy alpha1niner text", acl=[alice],
                                tenant_id=part_alice, owner_oid=alice)
        edition.ingest_document(external_id="t1-bob-doc", title="Bob HR policy",
                                text="vacation policy betaseven2 text", acl=[bob],
                                tenant_id=part_bob, owner_oid=bob)

        manifest = _FakeManifestStore()
        report = retention.sweep(edition, accounts, manifest, days=3, now=t0 + 3.5 * DAY)

        assert alice in report["swept"], report
        assert bob not in report["swept"], report
        assert edition.index.docs_owned_by(part_alice, alice) == [], "alice's doc survived"
        assert edition.index.docs_owned_by(part_bob, bob) == ["t1-bob-doc"], "bob's doc vanished"
        assert alice in manifest.deleted, "alice's manifest was not deleted"
        assert bob not in manifest.deleted, "bob's manifest was deleted"

        from fastapi.testclient import TestClient
        client = TestClient(appmod.app)

        alice_search = json.dumps(client.post(
            "/search", cookies=_cookies(alice), json={"question": "alpha1niner vacation"}).json())
        assert "alpha1niner" not in alice_search, (
            f"swept workspace still answers from deleted content: {alice_search[:300]}")

        bob_search = json.dumps(client.post(
            "/search", cookies=_cookies(bob), json={"question": "betaseven2 vacation"}).json())
        assert "betaseven2" in bob_search, (
            f"spared workspace lost its own content: {bob_search[:300]}")


def test_a_touched_owner_keeps_the_workspace_alive_until_touches_stop():
    """Sweep-timing behavior once a document owner's clock has been touched by SOME
    access (simulated here with a direct `retention.touch` call - the mechanism that
    actually PRODUCES such a touch from a real HTTP request is covered separately by
    `test_an_org_audience_readers_real_http_request_touches_the_owner` and
    `test_a_grantees_real_http_request_touches_the_owner`).

    Alice uploads + grants Bob at t0. A touch landing on Alice's clock at t0+2.9d must
    spare her from a sweep at t0+3.5d, even though she herself never came back. With no
    further touches, a sweep at t0+6d must sweep Alice AND drop the now-orphaned grant."""
    t0 = 1_700_100_000.0
    alice = "acct-t2-alice-576"
    bob = "acct-t2-bob-576"
    doc_id = "t2-alice-doc"

    with _fresh_accounts() as accounts, _real_login():
        accounts.touch_activity(alice, now=t0)

        edition = appmod._edition
        part_alice = ACCT_TENANT_PREFIX + alice
        edition.ingest_document(external_id=doc_id, title="Shared HR policy",
                                text="shared onboarding policy gammathree text", acl=[alice],
                                tenant_id=part_alice, owner_oid=alice)
        grant = edition.grant_registry.create(doc_external_id=doc_id, tenant_id=part_alice,
                                              grantee_oid=bob, granted_by=alice)
        touched = edition.index.add_doc_principals(part_alice, doc_id, [grant.principal])
        assert touched, "grant never landed on the document's ACL"

        retention.touch(grant.granted_by, now=t0 + 2.9 * DAY)
        assert accounts.last_activity(alice) == t0 + 2.9 * DAY, (
            "the touch did not land on alice's activity clock")

        manifest = _FakeManifestStore()
        report1 = retention.sweep(edition, accounts, manifest, days=3, now=t0 + 3.5 * DAY)
        assert alice not in report1["swept"], report1
        assert edition.index.docs_owned_by(part_alice, alice) == [doc_id]

        report2 = retention.sweep(edition, accounts, manifest, days=3, now=t0 + 6 * DAY)
        assert alice in report2["swept"], report2
        assert report2["grants_dropped"] == 1, report2
        assert edition.grant_registry.list_for_document(doc_id) == [], "grant outlived its document"
        assert edition.index.docs_owned_by(part_alice, alice) == []


def test_an_org_audience_readers_real_http_request_touches_the_owner():
    """#576 review Finding 2, the exact bug named in the review: alice uploads with the
    #575 "My organization" audience - ACL'd [alice, tenant:<tid>], NO Grant row at all,
    owner_oid=alice. Bob, a same-tenant colleague who was never individually granted
    anything, reads it via a REAL HTTP /search request. That request must touch ALICE's
    activity clock (the document's owner) - proving retrieval itself, not merely holding a
    grant, is what "any access counts" means."""
    alice = "acct-t9-alice-576"
    bob = "acct-t9-bob-576"
    doc_id = "t9-org-doc"

    with _fresh_accounts() as accounts, _real_login():
        edition = appmod._edition
        home = edition.tenant_id
        edition.ingest_document(external_id=doc_id, title="Org expenses policy",
                                text="expenses policy zetaeleven reimbursement text",
                                acl=[alice, f"tenant:{_HOME_TID}"],
                                tenant_id=home, owner_oid=alice)
        edition.identity.set_user_groups(bob, [f"tenant:{_HOME_TID}"])
        try:
            from fastapi.testclient import TestClient
            client = TestClient(appmod.app)
            before = time.time()
            r = client.post("/search", cookies=_cookies(bob, _HOME_TID),
                            json={"question": "zetaeleven reimbursement"})
            assert r.status_code == 200, r.text
            blob = json.dumps(r.json())
            assert "zetaeleven" in blob, f"bob could not read the org-audience doc: {blob[:300]}"

            moved = accounts.last_activity(alice)
            assert moved is not None and moved >= before, (
                "a colleague's real HTTP read of an org-audience document did not touch "
                f"its OWNER's activity clock: {moved}")
        finally:
            edition.identity.set_user_groups(bob, [])


def test_a_grantees_real_http_request_touches_the_owner():
    """The untested safety property the review flagged: a document shared via an explicit
    Grant (#538/ADR 0017), read by the grantee through a REAL HTTP /search request, must
    touch the SHARER's clock. Alice uploads privately in the home tenant and grants Bob by
    name; retrieval-based touch covers this exactly the same way it covers an org-audience
    read (`owner_oid` rides on the chunk regardless of WHY the caller's principals overlap
    its ACL) - this is deliberately the SAME mechanism as the org-audience test above, not
    a second one, which is itself the fix: the old grant-only touch in `current_user` and
    the new retrieval-based touch have been unified into one correct mechanism."""
    alice = "acct-t10-alice-576"
    bob = "acct-t10-bob-576"
    doc_id = "t10-shared-doc"

    with _fresh_accounts() as accounts, _real_login():
        edition = appmod._edition
        home = edition.tenant_id
        edition.ingest_document(external_id=doc_id, title="Shared onboarding doc",
                                text="onboarding checklist thetatwelve orientation text",
                                acl=[alice], tenant_id=home, owner_oid=alice)
        grant = edition.grant_registry.create(doc_external_id=doc_id, tenant_id=home,
                                              grantee_oid=bob, granted_by=alice)
        touched = edition.index.add_doc_principals(home, doc_id, [grant.principal])
        assert touched, "grant never landed on the document's ACL"

        from fastapi.testclient import TestClient
        client = TestClient(appmod.app)
        before = time.time()
        r = client.post("/search", cookies=_cookies(bob, _HOME_TID),
                        json={"question": "thetatwelve orientation checklist"})
        assert r.status_code == 200, r.text
        blob = json.dumps(r.json())
        assert "thetatwelve" in blob, f"bob could not read the shared doc: {blob[:300]}"

        moved = accounts.last_activity(alice)
        assert moved is not None and moved >= before, (
            f"a grantee's real HTTP read did not touch the sharer's activity clock: {moved}")


def test_chat_stream_never_leaks_an_owner_account_id():
    """#576 review round 2, Finding B (Important): `retrieved_owners` is an ACCOUNT ID -
    the document owner's oid, server-internal input to the touch call - and it rode
    verbatim onto the `/chat/stream` SSE wire (built from the same internal dict `/search`
    and `/chat` never expose). Alice org-uploads; bob (a same-tenant colleague, no grant)
    drives a REAL SSE request and reads it. Alice's account id must not appear ANYWHERE in
    the raw stream text, in any frame, while the touch mechanism itself keeps working."""
    from fastapi.testclient import TestClient

    alice = "acct-t17-alice-576"
    bob = "acct-t17-bob-576"
    doc_id = "t17-org-doc"

    with _fresh_accounts() as accounts, _real_login():
        edition = appmod._edition
        home = edition.tenant_id
        edition.ingest_document(external_id=doc_id, title="Org travel policy",
                                text="travel policy iotasixteen reimbursement text",
                                acl=[alice, f"tenant:{_HOME_TID}"],
                                tenant_id=home, owner_oid=alice)
        edition.identity.set_user_groups(bob, [f"tenant:{_HOME_TID}"])
        try:
            client = TestClient(appmod.app)
            raw_text = ""
            frames = []
            with client.stream("POST", "/chat/stream", cookies=_cookies(bob, _HOME_TID),
                               json={"conv_id": "cv-t17", "question": "iotasixteen reimbursement"}) as r:
                assert r.status_code == 200, r.status_code
                for line in r.iter_lines():
                    line = line.strip()
                    raw_text += line + "\n"
                    if line.startswith("data:"):
                        frames.append(json.loads(line[5:].strip()))

            done = [f for f in frames if f.get("type") == "done"]
            assert done, "expected a done event"
            assert "iotasixteen" in done[0].get("answer", ""), (
                f"bob could not read the org-audience doc: {done[0]}")

            assert alice not in raw_text, (
                f"the document owner's account id leaked onto the SSE wire: {raw_text[:500]}")
            for f in frames:
                assert "retrieved_owners" not in f, f"a frame still carries retrieved_owners: {f}"

            # The mechanism itself still worked server-side - alice's clock moved, even
            # though the client was never told whose it was.
            moved = accounts.last_activity(alice)
            assert moved is not None, "the stream path must still touch the owner's clock"
        finally:
            edition.identity.set_user_groups(bob, [])


def test_the_home_tenant_partition_is_never_touched_by_the_sweep():
    """INVARIANT (owner's ruling, #576 review Finding 2): the sweep must NEVER read or
    write the home tenant partition - only `ACCT_TENANT_PREFIX + account_id`.

    REWRITTEN at the final whole-branch review (Fix 1). The version this replaces asserted
    `alice in report["swept"]` and then checked only that her org document survived - so it
    passed while the sweep was irreversibly deleting her user_manifests row (her composed
    stores and connections) and her whole chat history, because those deletes are
    ACCOUNT-keyed and ran for every silent account regardless of partition. An invariant test
    that reads the report instead of the DELETIONS cannot catch a half-applied narrowing;
    this one asserts on every delete the sweep can perform, one by one.

    The scenario is the ordinary one, which is what makes it serious: an enterprise user
    signs in on Friday, is away Saturday to Tuesday, and Wednesday's sweep finds an activity
    row older than 3 days and an empty `acct:` partition (her documents live in the home
    tenant, where the sweep is forbidden to look)."""
    t0 = 1_700_600_000.0
    alice = "acct-t8-alice-576"
    doc_id = "t8-org-doc"

    with _fresh_accounts() as accounts, _real_login():
        accounts.touch_activity(alice, now=t0)

        edition = appmod._edition
        home = edition.tenant_id
        edition.ingest_document(external_id=doc_id, title="Org policy",
                                text="org wide policy etaten text",
                                acl=[alice, f"tenant:{_HOME_TID}"],
                                tenant_id=home, owner_oid=alice)

        # A real chat transcript for Alice. Its answer text is derived from documents the
        # sweep is not allowed to touch, so deleting it is a data loss with no counterpart.
        convs = edition.conversation_service
        saved_store = dict(convs._store._turns)
        # Seeded through `append`, not by writing the private row shape by hand: #611 gave the
        # in-memory store an (turn, asked_at) row, and a test that builds the row itself is a
        # test that goes stale silently - it would keep passing here (this one only checks key
        # membership) while leaving a row `history()` cannot read.
        convs._store.append("conv-t8", alice,
                            Turn(question="what is the policy",
                                 standalone="what is the policy",
                                 answer="org wide policy etaten text"))

        calls: list = []
        original = edition.index.docs_owned_by

        def _spy(tenant_id, owner_oid):
            calls.append(tenant_id)
            return original(tenant_id, owner_oid)

        edition.index.docs_owned_by = _spy
        try:
            manifest = _FakeManifestStore()
            report = retention.sweep(edition, accounts, manifest, days=3, now=t0 + 10 * DAY)
            # Read the surviving history BEFORE the finally puts the store back, or the
            # restore itself removes the seeded turn and the assertion tests nothing.
            history_survived = ("conv-t8", alice) in convs._store._turns
        finally:
            del edition.index.docs_owned_by   # restore the class method
            convs._store._turns.clear()
            convs._store._turns.update(saved_store)

        assert home not in calls, f"the sweep read the home tenant partition: {calls}"
        assert calls == [ACCT_TENANT_PREFIX + alice], (
            f"the sweep queried more than the account's own acct: partition: {calls}")

        # The four deletions, asserted individually. Each one on its own is an irreversible
        # loss for a user whose only offence was four quiet days.
        assert manifest.deleted == [], (
            "the sweep deleted the user_manifests row of an account whose acct: partition "
            f"was empty - her composed stores and connections are gone: {manifest.deleted}")
        assert history_survived, (
            "the sweep dropped the chat history of an account it deleted no documents for")
        assert accounts.last_activity(alice) == t0, (
            "the sweep deleted the activity row, which makes the account invisible to "
            "silent_accounts forever - a real workspace of hers could never be swept later")
        assert edition.index.docs_owned_by(home, alice) == [doc_id], "the org doc was deleted"

        # And the report must say so honestly, rather than claiming a workspace was swept.
        assert alice not in report["swept"], report
        assert report["untouched_empty"] == 1, report


def test_an_account_with_no_activity_row_is_never_swept():
    """A pre-existing account with NO workspace_activity row must never be treated as
    silent - unknown is not ancient. On the first sweep after this feature ships, every
    account is in exactly this state, and sweeping them all would be catastrophic and
    irreversible."""
    legacy = "acct-t3-legacy-576"
    doc_id = "t3-legacy-doc"

    with _fresh_accounts() as accounts, _real_login():
        # legacy IS a real, pre-existing account row (it signed in once, long before this
        # feature shipped) - the realistic shape of "the entire installed base" the module
        # docstring talks about. Deliberately NO accounts.touch_activity(legacy, ...) call:
        # that absence is the entire point of this test.
        accounts.resolve("entra", "legacy-subject-t3", preferred_account_id=legacy)

        edition = appmod._edition
        part_legacy = ACCT_TENANT_PREFIX + legacy
        edition.ingest_document(external_id=doc_id, title="Old doc",
                                text="undisturbed legacy deltafour text", acl=[legacy],
                                tenant_id=part_legacy, owner_oid=legacy)

        manifest = _FakeManifestStore()
        report = retention.sweep(edition, accounts, manifest, days=3, now=1_700_200_000.0)

        assert report["checked"] == 0, (
            f"a no-row account was counted as a silent candidate: {report}")
        assert legacy not in report["swept"], report
        assert edition.index.docs_owned_by(part_legacy, legacy) == [doc_id], (
            "a no-row account's document was deleted")
        assert manifest.deleted == []


def test_operators_are_never_swept():
    """An operator's own workspace must never be swept, however long it has been silent."""
    t0 = 1_700_300_000.0
    op = "op-1-t4-576"
    doc_id = "t4-op-doc"

    with _fresh_accounts() as accounts, _real_login(operator_oids=op):
        accounts.touch_activity(op, now=t0)

        edition = appmod._edition
        part_op = ACCT_TENANT_PREFIX + op
        edition.ingest_document(external_id=doc_id, title="Operator doc",
                                text="operator epsilonfive text", acl=[op],
                                tenant_id=part_op, owner_oid=op)

        manifest = _FakeManifestStore()
        report = retention.sweep(edition, accounts, manifest, days=3, now=t0 + 10 * DAY)  # 10d silent

        assert report["skipped_operators"] == 1, report
        assert op not in report["swept"], report
        assert edition.index.docs_owned_by(part_op, op) == [doc_id]
        assert manifest.deleted == []


def test_swept_document_bytes_are_gone_from_the_object_store():
    """#576 review Finding 1 (CRITICAL): a workspace is not actually deleted if its bytes
    are still sitting in the object store, readable by key. The pipeline writes FOUR blob
    key families per document (raw, segments, chunk, emb - pipeline/runner.py); the first
    version of this sweep deleted only two. All four must be gone, and `segments/` (the
    extracted document TEXT, verbatim) matters most - that is what a reviewer read back
    after the first version's sweep."""
    t0 = 1_700_400_000.0
    alice = "acct-t5-alice-576"
    doc_id = "t5-alice-doc"

    with _fresh_accounts() as accounts, _real_login():
        accounts.touch_activity(alice, now=t0)

        edition = appmod._edition
        part_alice = ACCT_TENANT_PREFIX + alice
        edition.ingest_document(external_id=doc_id, title="Alice doc",
                                text="zetasix confidential text", acl=[alice],
                                tenant_id=part_alice, owner_oid=alice)

        keys = {kind: f"{kind}/{part_alice}/{doc_id}" for kind in ("raw", "segments")}
        keys["chunk"] = f"chunk/{part_alice}/{doc_id}/0"
        for kind, key in keys.items():
            assert edition.store.get(key), f"{kind} bytes should exist before the sweep"
        # #834: ingest no longer writes emb/ blobs (the vector rides the queue message) -
        # but pre-#834 documents left them on disk, and the sweep's four-family contract
        # must keep deleting the LEGACY ones. Seed one by hand to prove exactly that.
        keys["emb"] = f"emb/{part_alice}/{doc_id}/0"
        edition.store.put(keys["emb"], b"[0.1, 0.2]")

        manifest = _FakeManifestStore()
        report = retention.sweep(edition, accounts, manifest, days=3, now=t0 + 3.5 * DAY)
        assert alice in report["swept"], report
        assert report["blobs_unsupported"] == 0, (
            f"the local ObjectStorePort adapters must implement delete_prefix: {report}")

        for kind, key in keys.items():
            try:
                edition.store.get(key)
                raise AssertionError(f"{kind} bytes are still readable after the sweep: {key}")
            except KeyError:
                pass


def test_days_zero_disables_the_sweep():
    """DBSEARCH_RETENTION_DAYS=0 must disable the sweep entirely - no reads, no deletes."""
    saved = os.environ.get("DBSEARCH_RETENTION_DAYS")
    os.environ["DBSEARCH_RETENTION_DAYS"] = "0"
    try:
        with _fresh_accounts() as accounts, _real_login():
            edition = appmod._edition
            manifest = _FakeManifestStore()
            report = retention.sweep(edition, accounts, manifest, now=1_700_500_000.0)
            assert report == {"disabled": True, "reason": "retention_days_zero"}, report
            assert manifest.deleted == []
    finally:
        if saved is None:
            os.environ.pop("DBSEARCH_RETENTION_DAYS", None)
        else:
            os.environ["DBSEARCH_RETENTION_DAYS"] = saved


def test_the_sweep_does_nothing_without_real_login():
    """#576 review Finding 6: a deployment with no real login IS the operator's own
    machine - there is no "other user" to go silent independently. This must be an
    EXPLICIT decision the sweep states (`reason: "no_real_login"`), not an accident that
    falls out of `is_operator` returning True for everyone; and it must stop before any
    enumeration at all, not merely end up deleting nothing via skipped_operators."""
    alice = "acct-t15-alice-576"
    doc_id = "t15-alice-doc"

    with _fresh_accounts() as accounts:   # NOTE: no _real_login() here - that is the point
        accounts.touch_activity(alice, now=1_700_950_000.0)
        edition = appmod._edition
        part_alice = ACCT_TENANT_PREFIX + alice
        edition.ingest_document(external_id=doc_id, title="Alice doc",
                                text="norealLogin etafourteen text", acl=[alice],
                                tenant_id=part_alice, owner_oid=alice)

        manifest = _FakeManifestStore()
        report = retention.sweep(edition, accounts, manifest, days=3,
                                 now=1_700_950_000.0 + 10 * DAY)

        assert report == {"disabled": True, "reason": "no_real_login"}, report
        assert edition.index.docs_owned_by(part_alice, alice) == [doc_id]
        assert manifest.deleted == []


def test_a_sign_in_during_the_sweep_is_not_wiped_out():
    """#576 review Finding 3 (TOCTOU): `silent_accounts()` is a snapshot; a real user can
    sign in in the window between that snapshot and this account's actual deletion. The
    re-check immediately before deleting must catch that and skip the account entirely -
    not delete its documents out from under a clock that has already moved, and not
    clobber the very activity row that sign-in just wrote."""
    t0 = 1_700_700_000.0
    alice = "acct-t11-alice-576"
    doc_id = "t11-alice-doc"
    now = t0 + 3.5 * DAY
    race_at = now - 1.0   # "signs in" one second before the sweep would have deleted her

    accounts = _RacyAccounts(race_account=alice, race_at=race_at)
    accounts.touch_activity(alice, now=t0)

    with _fresh_accounts(store=accounts), _real_login():
        edition = appmod._edition
        part_alice = ACCT_TENANT_PREFIX + alice
        edition.ingest_document(external_id=doc_id, title="Alice doc",
                                text="raced etatwelve text", acl=[alice],
                                tenant_id=part_alice, owner_oid=alice)

        manifest = _FakeManifestStore()
        report = retention.sweep(edition, accounts, manifest, days=3, now=now)

        assert alice not in report["swept"], report
        assert report["raced"] == 1, report
        assert edition.index.docs_owned_by(part_alice, alice) == [doc_id], (
            "a mid-sweep sign-in's documents were deleted anyway")
        assert accounts.last_activity(alice) == race_at, (
            "the raced sign-in's OWN fresh activity row was clobbered")


def test_delete_prefix_does_not_cross_a_doc_id_boundary():
    """#576 review Finding 5 (CRITICAL): doc_id="policy" is a Python string-PREFIX of
    doc_id="policy-2024". A naive `str.startswith(prefix)` match would let deleting the
    first document's raw blob also delete the second's. Tested with exactly that pair, on
    BOTH local ObjectStorePort adapters."""
    import tempfile

    from dbsearch.adapters.local import FilesystemObjectStore, InMemoryObjectStore

    for store in (InMemoryObjectStore(), FilesystemObjectStore(tempfile.mkdtemp())):
        label = type(store).__name__
        store.put("raw/acct:alice-576/policy", b"alice's policy bytes")
        store.put("raw/acct:alice-576/policy-2024", b"unrelated policy-2024 bytes")

        n = store.delete_prefix("raw/acct:alice-576/policy")
        assert n == 1, f"{label}: expected exactly 1 key deleted, deleted {n}"

        try:
            store.get("raw/acct:alice-576/policy")
            raise AssertionError(f"{label}: the targeted blob survived")
        except (KeyError, FileNotFoundError):
            pass

        survivor = store.get("raw/acct:alice-576/policy-2024")
        assert survivor == b"unrelated policy-2024 bytes", (
            f"{label}: an unrelated document's blob was deleted by a prefix collision")


def test_delete_prefix_cannot_escape_the_filesystem_store_root():
    """#576 review round 2, Finding A (CRITICAL), layer 1 (adapter): the reviewer's exact
    repro - `delete_prefix("raw/acct:<id>/..")` resolves to `<root>/raw` and `rmtree`s the
    WHOLE raw/ tree, every account's blobs at once, while still technically staying under
    root. A pure "stays under root" check misses that; `_safe_path` must refuse any `..`
    path SEGMENT outright, and separately refuse anything that resolves outside root at
    all (an absolute key, or `../..` walking past root entirely)."""
    import tempfile
    from pathlib import Path

    from dbsearch.adapters.local import FilesystemObjectStore

    base = Path(tempfile.mkdtemp())
    store = FilesystemObjectStore(str(base / "store"))

    # Two unrelated accounts' blobs, both legitimately inside the store.
    store.put("raw/acct:alice-576/alice-doc", b"alice bytes")
    store.put("raw/acct:bob-576/bob-doc", b"bob bytes")

    # A sentinel OUTSIDE the store root entirely - the sharpest version of the exploit.
    sibling = base / "sibling-account-data"
    sibling.mkdir()
    (sibling / "secret.txt").write_bytes(b"another account's bytes, outside the store")

    traversals = [
        "raw/acct:alice-576/..",           # the reviewer's exact repro: -> <root>/raw
        "../sibling-account-data",         # walks past root entirely
        "raw/acct:alice-576/../../../../sibling-account-data",
    ]
    for bad_prefix in traversals:
        try:
            store.delete_prefix(bad_prefix)
            raise AssertionError(f"delete_prefix accepted a traversal prefix: {bad_prefix!r}")
        except ValueError:
            pass

    # Nothing outside alice's own document was touched.
    assert store.get("raw/acct:alice-576/alice-doc") == b"alice bytes", (
        "alice's own doc should be untouched by these refused calls")
    assert store.get("raw/acct:bob-576/bob-doc") == b"bob bytes", (
        "a traversal prefix reached a sibling account's blob")
    assert sibling.exists() and (sibling / "secret.txt").exists(), (
        "a traversal prefix reached outside the store root entirely")


def test_put_and_get_also_refuse_a_traversal_key():
    """Finding A named delete_prefix specifically, but put/get build a filesystem path
    from the SAME kind of caller-influenced key and must refuse identically - the
    reviewer's ask to check every method that touches a path, not just the one named.

    #576 review round 3, Finding F (fixed first, per the coordinator's instruction): the
    original version of this test targeted `/etc/passwd` directly - a REAL system path. If
    the guard ever regressed (or this ran as root), the test itself would have clobbered
    the machine's password file. A test for a destructive bug must not itself be
    destructive: every traversal key here targets a SENTINEL file inside a disposable temp
    directory, and the absolute-path case points at that same sentinel rather than
    anywhere on the real filesystem."""
    import tempfile
    from pathlib import Path

    from dbsearch.adapters.local import FilesystemObjectStore

    base = Path(tempfile.mkdtemp())
    store = FilesystemObjectStore(str(base / "store"))

    sentinel_dir = base / "sibling-account-data"
    sentinel_dir.mkdir()
    sentinel = sentinel_dir / "secret.txt"
    sentinel.write_bytes(b"another account's bytes")

    bad_keys = (
        "../escape",
        "raw/acct:x/../../sibling-account-data/secret.txt",
        str(sentinel),   # absolute path, but still just the sentinel - never a real system path
    )
    for bad_key in bad_keys:
        try:
            store.put(bad_key, b"clobbered")
            raise AssertionError(f"put() accepted a traversal key: {bad_key!r}")
        except ValueError:
            pass
        try:
            store.get(bad_key)
            raise AssertionError(f"get() accepted a traversal key: {bad_key!r}")
        except ValueError:
            pass

    assert sentinel.read_bytes() == b"another account's bytes", (
        "a traversal key reached outside the store root")


def test_ingest_refuses_a_traversal_external_id():
    """#576 review round 2, Finding A (CRITICAL), layer 2 (API boundary): the reviewer's
    exact repro - `POST /ingest {"external_id": ".."}` - must be REFUSED (400), not
    accepted and left for the adapter layer to catch later. Covers the several shapes
    `is_safe_external_id` refuses, not only `..`."""
    from fastapi.testclient import TestClient

    client = TestClient(appmod.app)
    alice = "acct-t16-alice-576"

    with _real_login():
        for bad_id in ("..", ".", "../escape", "a/b", "a\\b", "", ".hidden"):
            r = client.post("/ingest", cookies=_cookies(alice, ""),
                            json={"external_id": bad_id, "title": "x", "text": "y", "acl": [alice]})
            assert r.status_code == 400, (
                f"external_id={bad_id!r} should be refused, got {r.status_code}: {r.text[:200]}")

        # A normal id is unaffected.
        r = client.post("/ingest", cookies=_cookies(alice, ""),
                        json={"external_id": "t16-good-doc", "title": "x",
                              "text": "fine document text", "acl": [alice]})
        assert r.status_code == 200, f"a normal external_id was refused: {r.status_code} {r.text[:200]}"


def test_ingest_refuses_control_characters_in_external_id():
    """#576 review round 3, Finding H (Minor): a NUL or newline embedded in external_id
    used to sail through `is_safe_external_id` and crash deeper down as an uncaught
    ValueError (a 500) - the filesystem adapter's OS calls raise their own "embedded null
    byte" error, and pgvector's `doc_external_id` column never gets the NUL-stripping
    `content`/`title`/`uri` get (pgvector.py's `_pg_text`). Must be a clean 400 at the
    boundary, not a 500 downstream."""
    from fastapi.testclient import TestClient

    client = TestClient(appmod.app)
    alice = "acct-t20-alice-576"

    with _real_login():
        for bad_id in ("a\x00b", "a\nb", "a\rb", "a\tb"):
            r = client.post("/ingest", cookies=_cookies(alice, ""),
                            json={"external_id": bad_id, "title": "x", "text": "y", "acl": [alice]})
            assert r.status_code == 400, (
                f"external_id={bad_id!r} should be refused with 400, got {r.status_code}: "
                f"{r.text[:200]}")


def test_run_ingestion_refuses_a_traversal_external_id_from_any_connector():
    """#576 review round 3, Finding D (Important): the connector-side backstop
    (`has_traversal_segment` inside `pipeline/runner.py`'s `run_ingestion`) is the ONLY
    accept-side guard covering every ingestion path except `/ingest` itself - folder, CSV,
    SharePoint, SharePointGraph, upload, resync all funnel through it. This drives that
    exact function with a minimal fake connector (the backstop lives in `run_ingestion`
    itself, not in any one connector, so it protects a connector this test has never seen
    too) yielding one document with a traversal external_id and one normal document, and
    asserts the crawl skips ONLY the bad one - never reaching the object store or the
    index."""
    from dbsearch.adapters.local import (HashingEmbedding, InMemoryIndex, InMemoryObjectStore,
                                          InMemoryQueue, LocalRichExtractor)
    from dbsearch.core.models import Document, Principal
    from dbsearch.pipeline.runner import run_ingestion

    class _MaliciousConnector:
        """Stands in for SharePoint/folder/CSV/upload/resync - the SOURCE decides
        external_id, not the transport, so any of them could hand run_ingestion a hostile
        one. Deliberately minimal: only what ConnectorPort callers here actually use."""

        @staticmethod
        def _doc_id(item):
            return "../escape-t18" if item["id"] == "bad" else "t18-good-doc"

        def list_changes(self, cursor):
            return ([{"id": "bad"}, {"id": "good"}], None)

        def external_ids(self, item):
            return [self._doc_id(item)]

        def fetch_content(self, item):
            return (b"payload text for the traversal probe", "text/plain")

        def to_documents(self, item):
            return [Document(
                tenant_id="acct:t18-victim-576", source_id="probe",
                external_id=self._doc_id(item), content_ref="",
                acl=[Principal(oid="t18-victim", kind="user")],
                title="probe", uri="", content_hash="x", owner_oid="t18-victim",
            )]

    store = InMemoryObjectStore()
    index = InMemoryIndex(store)
    result = run_ingestion(_MaliciousConnector(), InMemoryQueue(), store,
                           LocalRichExtractor(), HashingEmbedding(), index)

    assert result.doc_count == 1, (
        f"expected exactly 1 document ingested (the traversal id skipped), got {result.doc_count}")
    docs = index.docs_owned_by("acct:t18-victim-576", "t18-victim")
    assert docs == ["t18-good-doc"], (
        f"the traversal-id document should have been skipped, not indexed: {docs}")
    for key in list(store._blobs):   # noqa: SLF001 - proving nothing traversal-shaped landed
        assert ".." not in key, f"a traversal external_id reached the object store: {key}"


def test_empty_or_root_key_is_refused():
    """#576 review round 3, Finding E (Important): `_safe_path("")` (and `_safe_path(".")`
    - a lone dot is silently collapsed away by `PurePosixPath` before it ever reaches the
    component check, so it is invisible to that guard too) resolves to the STORE ROOT
    ITSELF - unreachable in production today (both accept-side guards already refuse an
    empty id, and no real id is ever a bare "."), but `delete_prefix("")` would `rmtree`
    the ENTIRE store, every account at once, if it were ever reached. The adapter's own
    docstring promises a future method gets both guards "by construction" - this is the
    gap that promise didn't cover until now, so it gets its own explicit test rather than
    being left for a future caller to discover the hard way."""
    import tempfile
    from pathlib import Path

    from dbsearch.adapters.local import FilesystemObjectStore

    base = Path(tempfile.mkdtemp())
    store = FilesystemObjectStore(str(base / "store"))
    store.put("raw/acct:alice-576/alice-doc", b"alice bytes")

    for bad_key in ("", "   ", "."):
        for op, args in (("delete_prefix", (bad_key,)), ("put", (bad_key, b"x")),
                         ("get", (bad_key,))):
            try:
                getattr(store, op)(*args)
                raise AssertionError(f"{op}() accepted an empty/root key: {bad_key!r}")
            except ValueError:
                pass

    # If delete_prefix("") had ever rmtree'd the store root, this doc would be gone too.
    assert store.get("raw/acct:alice-576/alice-doc") == b"alice bytes", (
        "an empty/root key deleted (part of) the whole store")


def test_delete_prefix_refuses_a_symlink_escape():
    """#576 review round 3, Finding G (Minor): the component check (no `.`/`..` segment)
    and the resolved-root comparison catch DIFFERENT attacks. Every `delete_prefix`
    traversal tested so far contains a literal `..`, so only the component check was ever
    exercised on the delete side - the reviewer found that removing the root-comparison
    check alone left every existing test green. A symlink planted INSIDE the store tree
    that resolves OUTSIDE root has NO `.`/`..` anywhere in the key at all; only the
    resolved-path-vs-root comparison can catch it, which is what this proves."""
    import tempfile
    from pathlib import Path

    from dbsearch.adapters.local import FilesystemObjectStore

    base = Path(tempfile.mkdtemp())
    store = FilesystemObjectStore(str(base / "store"))

    outside = base / "outside-account-data"
    outside.mkdir()
    (outside / "victim.txt").write_bytes(b"a sibling account's bytes")

    link_parent = base / "store" / "raw" / "acct:victim-576"
    link_parent.mkdir(parents=True)
    (link_parent / "escape-link").symlink_to(outside)

    try:
        store.delete_prefix("raw/acct:victim-576/escape-link")
        raise AssertionError("delete_prefix followed a symlink out of the store root")
    except ValueError:
        pass

    assert (outside / "victim.txt").read_bytes() == b"a sibling account's bytes", (
        "a symlink escape reached outside the store root")


def test_touch_survives_a_failing_account_store():
    """#576 review Finding 4 (Important): a down account store must not become a
    per-request retry storm. One failed `touch_activity` call opens a circuit breaker;
    further `touch()` calls - even for a DIFFERENT account - are skipped, not attempted,
    until the cooldown passes, and the per-account throttle timestamp is recorded
    regardless of outcome so the SAME account does not retry on its very next request."""
    failing = _FailingAccounts()
    saved_last_touch = dict(retention._LAST_TOUCH)
    saved_breaker = retention._breaker_open_until
    retention._LAST_TOUCH.clear()
    retention._breaker_open_until = 0.0

    with _fresh_accounts(store=failing):
        try:
            t0 = 1_700_800_000.0
            retention.touch("acct-t12-a-576", now=t0)          # fails, opens the breaker
            assert failing.attempts == 1, failing.attempts

            # A DIFFERENT account, well clear of ITS OWN per-account throttle, must still
            # be skipped while the breaker is open.
            retention.touch("acct-t12-b-576", now=t0 + 1.0)
            assert failing.attempts == 1, "the breaker did not stop a second account's attempt"

            # After the cooldown, a fresh account is attempted again.
            retention.touch("acct-t12-c-576",
                            now=t0 + retention._BREAKER_COOLDOWN_SECONDS + 1.0)
            assert failing.attempts == 2, "the breaker never closed after its cooldown"
        finally:
            retention._LAST_TOUCH.clear()
            retention._LAST_TOUCH.update(saved_last_touch)
            retention._breaker_open_until = saved_breaker


def test_last_touch_is_pruned():
    """#576 review Finding 9 (Minor): `_LAST_TOUCH` must not grow unbounded over a
    long-running process's life. Every `_PRUNE_EVERY` calls, entries older than one
    throttle window are dropped - they serve no further purpose once outside the window."""
    saved_last_touch = dict(retention._LAST_TOUCH)
    saved_count = retention._touch_call_count
    saved_breaker = retention._breaker_open_until
    retention._LAST_TOUCH.clear()
    retention._touch_call_count = 0
    retention._breaker_open_until = 0.0

    with _fresh_accounts():
        try:
            t0 = 1_700_900_000.0
            for i in range(retention._PRUNE_EVERY - 1):
                retention.touch(f"acct-t13-{i}-576", now=t0)
            assert len(retention._LAST_TOUCH) == retention._PRUNE_EVERY - 1

            # The call that lands on the prune boundary, far enough past the throttle
            # window that every PRIOR entry is now stale.
            retention.touch("acct-t13-last-576",
                            now=t0 + retention._TOUCH_THROTTLE_SECONDS + 1.0)
            assert len(retention._LAST_TOUCH) == 1, (
                f"stale entries were not pruned: {len(retention._LAST_TOUCH)} remain")
        finally:
            retention._LAST_TOUCH.clear()
            retention._LAST_TOUCH.update(saved_last_touch)
            retention._touch_call_count = saved_count
            retention._breaker_open_until = saved_breaker


def test_dry_run_previews_without_deleting():
    """#576 review Finding 10 (Minor): `dry_run=True` must report what WOULD be swept and
    delete nothing at all - no index delete, no blob delete, no grant/conversation/
    manifest delete, no activity-row delete. A real sweep run afterwards must still see
    everything exactly as it was."""
    t0 = 1_701_000_000.0
    alice = "acct-t14-alice-576"
    doc_id = "t14-alice-doc"

    with _fresh_accounts() as accounts, _real_login():
        accounts.touch_activity(alice, now=t0)

        edition = appmod._edition
        part_alice = ACCT_TENANT_PREFIX + alice
        edition.ingest_document(external_id=doc_id, title="Alice doc",
                                text="dryrun etathirteen text", acl=[alice],
                                tenant_id=part_alice, owner_oid=alice)

        manifest = _FakeManifestStore()
        preview = retention.sweep(edition, accounts, manifest, days=3,
                                  now=t0 + 3.5 * DAY, dry_run=True)

        assert preview["dry_run"] is True, preview
        assert alice in preview["swept"], preview
        assert preview["docs_deleted"] == 1, preview

        assert edition.index.docs_owned_by(part_alice, alice) == [doc_id], "dry run deleted a document"
        assert accounts.last_activity(alice) == t0, "dry run touched the activity row"
        assert manifest.deleted == [], "dry run deleted the manifest"
        assert edition.store.get(f"raw/{part_alice}/{doc_id}")   # still there

        report = retention.sweep(edition, accounts, manifest, days=3, now=t0 + 3.5 * DAY)
        assert alice in report["swept"], report
        assert edition.index.docs_owned_by(part_alice, alice) == []


def test_the_sweep_endpoint_is_operator_only():
    """POST /admin/retention/sweep: 404 (not 403 - naming the route to a non-operator is
    its own small leak on a data-deleting endpoint) for a non-operator, 202 + started for
    an operator (real sweep, off-thread, LAW 4). `?dry_run=true` (Finding 10) answers
    SYNCHRONOUSLY, 200, with a preview report, for an operator only."""
    from fastapi.testclient import TestClient
    client = TestClient(appmod.app)

    with _real_login():
        r = client.post("/admin/retention/sweep", cookies=_cookies("acct-t7-nonop-576"))
        assert r.status_code == 404, f"non-operator got {r.status_code}: {r.text[:200]}"

        r = client.post("/admin/retention/sweep?dry_run=true",
                        cookies=_cookies("acct-t7-nonop-576"))
        assert r.status_code == 404, f"non-operator dry-run got {r.status_code}: {r.text[:200]}"

    with _real_login(operator_oids="acct-t7-op-576"):
        r = client.post("/admin/retention/sweep", cookies=_cookies("acct-t7-op-576"))
        assert r.status_code == 202, f"operator got {r.status_code}: {r.text[:200]}"
        assert r.json().get("started") is True, r.text

        r = client.post("/admin/retention/sweep?dry_run=true",
                        cookies=_cookies("acct-t7-op-576"))
        assert r.status_code == 200, f"operator dry-run got {r.status_code}: {r.text[:200]}"
        body = r.json()
        assert body.get("dry_run") is True, body
        assert "started" not in body, "dry-run must not look like a started background sweep"


class _FakeResponse:
    """Minimal urlopen context-manager result."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextmanager
def _sweep_env(**values):
    """Set/clear the cron entrypoint's env vars and restore every one in a finally."""
    keys = ("DBSEARCH_SWEEP_KEY", "DBSEARCH_SWEEP_URL")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update({k: v for k, v in values.items() if v is not None})
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_the_cron_entrypoint_drives_the_operator_endpoint():
    """FINAL REVIEW Fix 4: `docker exec ... python3 -m dbsearch.server.retention` used to run
    `sweep()` in a FRESH process, which holds a FRESH in-memory ConversationService - so it
    deleted the documents and left the chat history (answer text quoted from those very
    documents) alive in the SERVING process indefinitely. The operator endpoint, running
    inside that process, did delete it, so the two sanctioned paths disagreed about what a
    sweep means and the documented one was the weaker.

    The entrypoint must now POST the endpoint rather than sweep locally. Asserted on the
    REQUEST it makes - method, URL and operator credential - because "it called something"
    is exactly the assertion strength that let the original defect through."""
    seen: list = []

    def _fake_urlopen(req, timeout=None):
        seen.append((req.get_method(), req.full_url, req.get_header("Authorization"),
                     timeout))
        return _FakeResponse(202, b'{"started": true}')

    with _sweep_env(DBSEARCH_SWEEP_KEY="dbk_operator_576"):
        out = retention.run_cron_sweep(urlopen=_fake_urlopen)

    assert len(seen) == 1, f"the cron entrypoint made {len(seen)} requests, expected 1"
    method, url, auth, timeout = seen[0]
    assert method == "POST", f"the sweep endpoint is a POST, got {method}"
    assert url == "http://127.0.0.1:8000/admin/retention/sweep", (
        f"cron drove the wrong url: {url}")
    assert auth == "Bearer dbk_operator_576", (
        f"cron did not present the operator key, so the endpoint would 404: {auth}")
    assert timeout is not None, "a cron http call with no timeout can hang the crontab forever"
    assert out.get("status") == 202, out

    # And the URL is overridable for a box whose api does not listen on the default address.
    seen.clear()
    with _sweep_env(DBSEARCH_SWEEP_KEY="dbk_operator_576",
                    DBSEARCH_SWEEP_URL="http://api:9000/admin/retention/sweep"):
        retention.run_cron_sweep(urlopen=_fake_urlopen)
    assert seen[0][1] == "http://api:9000/admin/retention/sweep", seen


def test_the_cron_entrypoint_says_which_credential_is_missing():
    """The endpoint answers 404 to a non-operator (#549: a 403 would confirm the route
    exists). So a cron run with no operator key would log a bare 404 that reads like a typo'd
    URL, on a job whose whole purpose is deleting data - and an operator would conclude the
    sweep is running when nothing is. Refuse before the request, and name the missing thing."""
    called: list = []

    def _must_not_be_called(req, timeout=None):
        called.append(req)
        raise AssertionError("unreachable")

    with _sweep_env():
        out = retention.run_cron_sweep(urlopen=_must_not_be_called)

    assert called == [], "cron sent a request with no operator key - it would just 404"
    assert out.get("error") == "no_operator_key", out
    assert "DBSEARCH_SWEEP_KEY" in out.get("detail", ""), (
        f"the error does not name the variable to set: {out}")
    assert "dbk_operator" not in json.dumps(out), "the key must never be echoed back"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
