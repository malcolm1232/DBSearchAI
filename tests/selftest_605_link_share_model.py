"""#605: a share row learns its AUDIENCE - "people" (ADR 0020) or "link" (ADR 0021).

Data model only. No route, no UI, no anonymous access path - those are later tasks built on
top of what this file pins. What is pinned here:

  - `create_link` returns the plaintext token exactly ONCE and the row keeps only its SHA-256
    digest (ADR 0021, "Mechanism": a database read, a backup or a leaked snapshot must yield a
    table of hashes that authorize nothing on their own)
  - the plaintext never reaches `to_dict()` - that surface is LAW 1 AND, for a link share, the
    difference between metadata and a live credential
  - the grantee is the synthetic sentinel `link:<share_id>`, which is never a real account -
    so `create`'s self-share and blank-grantee refusals must not fire on it, and must stay
    exactly as strict for the people path (selftest_600_conversation_shares.py pins them)
  - `find_by_token` is LIVE shares only: an expired link and a revoked link are both
    indistinguishable from a token that never existed (the 404-shape). The expired case is
    the one that makes the `is_live()` filter load-bearing - a revoked row is gone from
    `_by_id` altogether, so it would answer None even with the filter deleted.
  - `record_open` counts and persists, and NEVER raises - a counter is not worth failing a
    page load over
  - a people share is untouched: `audience` defaults to "people", `token_hash` stays None,
    and every pre-#605 row (a SELECT with the old seven columns) hydrates the same way

    PYTHONPATH=src python3 tests/selftest_605_link_share_model.py
"""
import hashlib
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dbsearch.server import conversation_shares as cs  # noqa: E402
from dbsearch.server.conversation_shares import (  # noqa: E402
    OPEN_PERSIST_INTERVAL_SECONDS, ConversationShare, ConversationShareRegistry,
    InMemoryConversationShareStore, LINK_GRANTEE_PREFIX, PgConversationShareStore,
    _row_to_share,
)
# The column-name-aware Postgres stand-in, reused rather than re-copied - a second copy would
# be a second thing to keep in step with the real emitted SQL, which is the exact drift this
# idiom exists to catch.
from selftest_600_conversation_shares import _FakeSqlTable  # noqa: E402


def test_create_link_returns_token_and_stores_only_its_hash():
    reg = ConversationShareRegistry()
    share, token = reg.create_link("c1", "bob", expires_in_days=7, turn_cutoff=2)
    assert len(token) == 32                      # 128-bit hex
    assert share.audience == "link"
    assert share.grantee_oid == f"{LINK_GRANTEE_PREFIX}{share.share_id}"
    assert share.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in repr(share.to_dict())    # plaintext never on the record


def test_the_token_digest_never_reaches_a_client_surface():
    """`to_dict()` is what a share-management API hands back. `token_hash` is a credential
    digest, not metadata about who and when - putting it there would turn every listing of
    "my shares" into a distribution channel for the one secret this row exists to protect."""
    reg = ConversationShareRegistry()
    share, _ = reg.create_link("c1", "bob", expires_in_days=7, turn_cutoff=2)
    d = share.to_dict()
    assert "token_hash" not in d, "the token digest must never appear on a client surface"
    assert share.token_hash not in repr(d)
    assert d["audience"] == "link" and d["opens"] == 0 and d["last_open_at"] is None


def test_find_by_token_round_trips_and_dead_links_vanish():
    reg = ConversationShareRegistry()
    share, token = reg.create_link("c1", "bob", expires_in_days=7, turn_cutoff=1)
    assert reg.find_by_token(token).share_id == share.share_id
    assert reg.find_by_token("0" * 32) is None
    # Expired, not revoked: the row is still in `_by_id`, so ONLY the is_live() filter can
    # make this None. ADR 0021 invariant 3 (bounded in time) has no other enforcement point -
    # there is no sweeper, expiry is evaluated per read, exactly as everywhere else in this
    # file's older sibling.
    #
    # Reached by AGEING a live link rather than minting one with a negative expiry, because
    # fix round 1 finding 3 made that mint a ValueError. Ageing is also the honest shape: this
    # is how production reaches an expired row - time passes under a share that is still
    # sitting in memory - and a test that could only reach it through a refused constructor
    # argument would be pinning a state the product cannot actually produce.
    aged, aged_token = reg.create_link("c2", "bob", expires_in_days=7, turn_cutoff=1)
    reg._by_id[aged.share_id] = replace(
        reg._by_id[aged.share_id],
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert reg.find_by_token(aged_token) is None, \
        "an expired link must be indistinguishable from a token that never existed"
    reg.revoke(share.share_id, "bob")
    assert reg.find_by_token(token) is None      # revoked == never existed


def test_a_link_that_never_expires_cannot_be_minted():
    """Fix round 1, finding 3. `_admit`'s expiry arithmetic is `... if expires_in_days else
    None`, so ZERO takes the falsy branch and yields a link with NO EXPIRY AT ALL - a
    permanent bearer credential, minted silently, from an argument that plainly meant the
    opposite. ADR 0021 invariant 3 says every link is bounded in time and there is no second
    check standing behind it: no sweeper, no default applied later, nothing. Refused where the
    fact is known - in `create_link` - rather than in a route that has to remember.

    A negative value is refused too, in the other direction: it mints a link that is dead on
    arrival, so the owner copies a URL that 404s and has no way to learn why.

    The people path is deliberately NOT changed: an unbounded people share is a supported
    shape (ADR 0020 never bounded it) and selftest_600 pins it. Pinned here so a later tidy-up
    cannot "unify" the two and reopen this."""
    reg = ConversationShareRegistry()
    for bad in (0, -1, -7):
        try:
            reg.create_link("c1", "bob", expires_in_days=bad, turn_cutoff=1)
            raise AssertionError(
                f"expires_in_days={bad} must be refused - 0 mints a link that never expires")
        except ValueError:
            pass
    # None is still accepted: it means "the caller set no expiry", and the route supplies
    # ADR 0021's 7-day default (a later task). Refusing None here would leave that default
    # nowhere to arrive from.
    share, _ = reg.create_link("c1", "bob", expires_in_days=None, turn_cutoff=1)
    assert share.audience == "link"
    # ...and the people path keeps its unbounded shape, untouched by this refusal.
    assert reg.create("c1", "bob", "alice", expires_in_days=0).expires_at is None


def test_record_open_counts_and_survives_the_store():
    """The count is durable across a restart. `list_granted_by` stands in for the owner opening
    her management surface, which is what flushes a coalesced count - see the coalescing test
    below for why the second open no longer writes on its own."""
    store = InMemoryConversationShareStore()
    reg = ConversationShareRegistry(store)
    share, _ = reg.create_link("c1", "bob", expires_in_days=7, turn_cutoff=1)
    reg.record_open(share.share_id)
    reg.record_open(share.share_id)
    assert reg._by_id[share.share_id].opens == 2, "the in-memory count must be exact at once"
    reg.list_granted_by("bob")
    reloaded = ConversationShareRegistry(store)
    assert reloaded._by_id[share.share_id].opens == 2


class _CountingStore(InMemoryConversationShareStore):
    """An in-memory store that remembers how many times it was WRITTEN.

    The finding is about writes, not about counts, so the assertion has to be able to see a
    write. Against `PgConversationShareStore` each of these is a fresh `psycopg.connect()` -
    a TCP connect plus an auth round trip - and a full-row UPSERT behind it."""

    def __init__(self):
        super().__init__()
        self.saves = 0

    def save(self, s):
        self.saves += 1
        super().save(s)


def test_open_counts_are_coalesced_so_a_forwarded_link_cannot_drive_a_write_per_request():
    """#612 review, Finding 2. `record_open` runs on every `GET /c/{token}` - the product's
    ONLY unauthenticated write path, on a sync route, on a single worker, reachable by anyone
    the link was forwarded to. One Slack unfurl bot, one crawler or one loop was one Postgres
    connect-and-auth per request.

    The in-memory count stays exact and immediate, because it is what the owner is shown and
    the whole point of the counter. The durable write is what gets a ceiling."""
    store = _CountingStore()
    reg = ConversationShareRegistry(store)
    share, _ = reg.create_link("c1", "bob", expires_in_days=7, turn_cutoff=1)
    store.saves = 0                       # the create's own write is not what is under test

    N = 200
    for _ in range(N):
        reg.record_open(share.share_id)

    assert reg._by_id[share.share_id].opens == N, (
        f"the in-memory count is no longer exact: {reg._by_id[share.share_id].opens} != {N}")
    # The FIRST open persists (see OPEN_PERSIST_INTERVAL_SECONDS) and nothing else does inside
    # the window. Asserted as a bound rather than as "== 1" so the test states the property -
    # a burst cannot drive writes - rather than the current implementation's exact bookkeeping.
    assert store.saves <= 2, (
        f"{N} opens produced {store.saves} store writes - the write is still per-request")
    assert store.saves >= 1, "the first open on a share must reach the store immediately"
    burst = store.saves

    # EVENTUALLY PERSISTED, path 1: the owner looks at her management surface.
    reg.list_granted_by("bob")
    assert store.saves == burst + 1, store.saves
    assert ConversationShareRegistry(store)._by_id[share.share_id].opens == N, (
        "the coalesced count was never made durable")
    # ...and a second look writes nothing, because nothing is dirty any more.
    reg.list_granted_by("bob")
    assert store.saves == burst + 1, (
        f"a management read writes even with no new opens: {store.saves}")

    # EVENTUALLY PERSISTED, path 2: the window expires and the next open carries everything
    # since the last write. Cumulative, so one write covers all of it and nothing is replayed.
    real_now = cs._now
    cs._now = lambda: real_now() + timedelta(seconds=OPEN_PERSIST_INTERVAL_SECONDS + 1)
    try:
        reg.record_open(share.share_id)
    finally:
        cs._now = real_now
    assert store.saves == burst + 2, store.saves
    assert ConversationShareRegistry(store)._by_id[share.share_id].opens == N + 1, (
        "the open past the window did not carry the current state to the store")


class _BlockingStore(InMemoryConversationShareStore):
    """An in-memory store that lets a test STOP the world inside one operation.

    The point is to make the race deterministic instead of hoping a sleep lands in the window.
    `block` names the operation to pause; `entered` fires once the test's thread is INSIDE it,
    and it stays there until `release` is set."""

    def __init__(self, block: str = None):
        super().__init__()
        self.block = block          # armed later, so setup writes are not caught by it
        self.entered = threading.Event()
        self.release = threading.Event()

    def _maybe_block(self, which):
        if which == self.block:
            self.entered.set()
            assert self.release.wait(5), "the blocked store operation was never released"

    def save(self, s):
        # Pause BEFORE the write, so the write LANDS LATE - the flush window.
        self._maybe_block("save")
        super().save(s)

    def delete(self, share_id):
        # Remove FIRST, then pause. The row is already gone from the store while the caller is
        # parked here, which is the only window in which a counter write can resurrect it:
        # `revoke` has not yet popped `_by_id`, so an `_by_id` test still says "alive".
        super().delete(share_id)
        self._maybe_block("delete")


def test_a_revoke_during_a_coalesced_open_write_still_kills_the_link():
    """#612 re-review, Finding A - a CRITICAL the coalescing pass itself introduced.

    `flush_opens` snapshotted the dirty shares under the lock and then `save`d outside it. A
    `revoke` landing in that window re-INSERTED the row the revoke had just deleted, so a fresh
    registry hydrated the share, `is_live` was True and `find_by_token` resolved it. A revoked
    link coming back to life is the single worst failure this feature can produce - it is a
    live credential, not a stale number - and it is the exact "looks revoked, isn't" shape
    `revoke`'s fail-closed ordering exists to rule out.

    Reproduced by INJECTION, not by timing: the store blocks inside `save`, so the flush is
    provably mid-write when the revoke is attempted."""
    store = _BlockingStore()
    reg = ConversationShareRegistry(store)
    share, token = reg.create_link("c1", "bob", expires_in_days=7, turn_cutoff=1)
    reg.record_open(share.share_id)      # first open - persists
    reg.record_open(share.share_id)      # coalesced - leaves the share dirty
    assert share.share_id in store._rows, "setup: the share should be in the store to start"
    store.block = "save"                 # ARM only now - setup must not be caught by it

    flusher = threading.Thread(target=reg.flush_opens, daemon=True)
    flusher.start()
    assert store.entered.wait(5), "the flush never reached the store"

    revoked = []
    revoker = threading.Thread(
        target=lambda: revoked.append(reg.revoke(share.share_id, "bob")), daemon=True)
    revoker.start()
    time.sleep(0.05)                     # give the revoke every chance to interleave
    store.release.set()
    flusher.join(5)
    revoker.join(5)
    assert revoked, "the revoke never completed"

    assert share.share_id not in store._rows, (
        "a coalesced open write re-inserted the row the revoke had just deleted - the link is "
        "alive again in the store")
    fresh = ConversationShareRegistry(store)
    assert fresh._by_id == {}, f"a revoked link was hydrated by a fresh process: {fresh._by_id}"
    assert fresh.find_by_token(token) is None, (
        "the revoked token still resolves - the credential came back to life")


def test_an_open_arriving_during_a_revoke_cannot_write_the_row_back():
    """The OTHER half of the same race, and the reason `_revoking` exists rather than a bare
    `share_id in self._by_id` test.

    `revoke` deletes from the store BEFORE it clears memory - deliberate, fail-closed, and not
    changing - so for the whole duration of that delete the share is still in `_by_id`. A
    counter write consulting only `_by_id` would pass its check and UPSERT the row straight
    back on top of the delete. Injected the same way, from the other side: the store blocks
    inside `delete`."""
    store = _BlockingStore()
    reg = ConversationShareRegistry(store)
    share, token = reg.create_link("c1", "bob", expires_in_days=7, turn_cutoff=1)
    store.block = "delete"

    revoked = []
    revoker = threading.Thread(
        target=lambda: revoked.append(reg.revoke(share.share_id, "bob")), daemon=True)
    revoker.start()
    assert store.entered.wait(5), "the revoke never reached the store delete"
    assert share.share_id not in store._rows, "setup: the delete should already have landed"

    # A visitor loads the page mid-revoke: the row is GONE from the store but still in `_by_id`,
    # because revoke clears memory last. The count must not become a resurrection.
    reg.record_open(share.share_id)
    store.release.set()
    revoker.join(5)
    assert revoked, "the revoke never completed"

    assert share.share_id not in store._rows, (
        "an open recorded during the revoke wrote the share row back")
    assert ConversationShareRegistry(store).find_by_token(token) is None, (
        "the revoked token still resolves - the credential came back to life")


def test_a_revoked_share_is_never_resurrected_by_a_coalesced_open_count():
    """The one way coalescing could be dangerous rather than merely stale: a dirty marker left
    behind by a revoked share would make the next flush re-INSERT the row the revoke deleted -
    and a resurrected link share is not a wrong number, it is a live credential (`revoke` is
    the file's one fail-closed path for exactly this reason)."""
    store = _CountingStore()
    reg = ConversationShareRegistry(store)
    share, _ = reg.create_link("c1", "bob", expires_in_days=7, turn_cutoff=1)
    reg.record_open(share.share_id)      # persists - first open
    reg.record_open(share.share_id)      # coalesced - leaves the share dirty
    reg.revoke(share.share_id, "bob")
    reg.flush_opens()
    assert share.share_id not in store._rows, (
        "a coalesced open count wrote back a share row that revoke had deleted")
    assert ConversationShareRegistry(store)._by_id == {}, "the revoked share came back"


def test_record_open_never_raises():
    """Best-effort, in the strongest sense: an unknown share_id and a store that is refusing
    every write must both leave the caller's request alone. A visitor opening a live link is
    not made to fail because a counter could not be written."""
    class DeadSaveStore:
        def load_all(self):
            return []

        def save(self, s):
            raise RuntimeError("down")

        def delete(self, share_id):
            pass

    reg = ConversationShareRegistry(store=DeadSaveStore())
    share, _ = reg.create_link("c1", "bob", expires_in_days=7, turn_cutoff=1)
    reg.record_open(share.share_id)              # store refuses - must not raise
    reg.record_open("no-such-share")             # unknown id - must not raise
    assert reg._by_id[share.share_id].opens == 1, \
        "the in-process count still moves even when the store cannot be written"


def test_a_people_share_is_untouched_by_the_new_fields():
    reg = ConversationShareRegistry()
    s = reg.create("c1", "bob", "alice", expires_in_days=7, turn_cutoff=1)
    assert s.audience == "people" and s.token_hash is None


def test_the_sentinel_grantee_does_not_weaken_creates_refusals():
    """`create_link`'s grantee is `link:<share_id>`, which would trip neither refusal by
    accident - but the two paths now share one `_admit` body, and the risk of factoring is
    that a refusal moves down into the shared half and stops applying, or moves up and starts
    applying to the sentinel. Both directions are pinned here; selftest_600 pins the people
    half independently."""
    reg = ConversationShareRegistry()
    for bad in ("", "   "):
        try:
            reg.create("c1", "bob", bad)
            raise AssertionError("a blank grantee must still be refused on the people path")
        except ValueError:
            pass
    try:
        reg.create("c1", "bob", "bob")
        raise AssertionError("a self-share must still be refused on the people path")
    except ValueError:
        pass
    # The link path shares the grantor's own oid space and must not be caught by either.
    share, _ = reg.create_link("c1", "bob", expires_in_days=7, turn_cutoff=1)
    assert share.grantee_oid.startswith(LINK_GRANTEE_PREFIX)
    # ...and the conv_id refusal is genuinely SHARED - it applies to both paths.
    try:
        reg.create_link("   ", "bob", expires_in_days=7, turn_cutoff=1)
        raise AssertionError("a blank conv_id must be refused on the link path too")
    except ValueError:
        pass


def test_pg_store_round_trips_the_new_columns_via_the_real_sql_text():
    """The same guard selftest_600 puts on the original columns, extended to the four new
    ones. `InMemoryConversationShareStore` never touches a column list, so only this can catch
    a transposition between `save`'s param tuple and `load_all`'s SELECT - and a transposed
    `token_hash` would mean either every link 404s after a restart or, far worse, a row
    authorizing a token nobody holds."""
    rows: dict = {}
    store = PgConversationShareStore("postgresql://unused/fake")
    store._conn = lambda: _FakeSqlTable(rows)

    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    opened = datetime(2026, 1, 3, tzinfo=timezone.utc)
    s = ConversationShare(share_id="s-605", conv_id="c1", grantor_oid="bob",
                          grantee_oid="link:s-605", expires_at=None, created_at=created,
                          turn_cutoff=2, audience="link", token_hash="d" * 64, opens=5,
                          last_open_at=opened)
    store.save(s)
    [reloaded] = store.load_all()
    assert reloaded.audience == "link"
    assert reloaded.token_hash == "d" * 64, (
        "token_hash did not round-trip - a live link would stop resolving after a restart")
    assert reloaded.opens == 5
    assert reloaded.last_open_at == opened, (
        "last_open_at came back wrong - it may have swapped columns with created_at")
    assert reloaded.created_at == created
    # #851: the consent list rides the same INSERT/SELECT pair and is the one column whose
    # transposition would be SILENT AND WIDENING - a share hydrating with somebody else's
    # consented sources hands over evidence its grantor never agreed to, and nothing else in
    # the suite goes near this SQL text.
    assert reloaded.shared_stores == [], (
        f"an empty consent list did not round-trip: {reloaded.shared_stores!r}")
    s2 = replace(s, share_id="s-605b", shared_stores=["azure_sql-1", "bigquery-1"])
    store.save(s2)
    [got] = [x for x in store.load_all() if x.share_id == "s-605b"]
    assert got.shared_stores == ["azure_sql-1", "bigquery-1"], (
        f"the consented sources did not round-trip through the real SQL: {got.shared_stores!r}")


def test_a_pre_605_row_hydrates_as_a_people_share():
    """Backward compatibility, at the row level. `audience`/`token_hash`/`opens`/`last_open_at`
    arrive by ALTER TABLE, so a row written before #605 comes back with them NULL - and a
    short row comes back with them absent entirely, which is what every caller that still
    selects the old seven columns produces. Both must read as an ordinary people share:
    "people" is the fail-closed reading, because a people share hands nothing to a caller who
    cannot sign in, while an accidental "link" would."""
    now = datetime.now(timezone.utc)
    legacy_short = _row_to_share(("s1", "c1", "alice", "bob", None, now, 3))
    assert legacy_short.audience == "people"
    assert legacy_short.token_hash is None and legacy_short.opens == 0
    assert legacy_short.last_open_at is None

    legacy_nulls = _row_to_share(("s1", "c1", "alice", "bob", None, now, 3,
                                  None, None, None, None))
    assert legacy_nulls.audience == "people"
    assert legacy_nulls.token_hash is None and legacy_nulls.opens == 0


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
