"""#600: ConversationShare registry - the record that lets a recipient OPEN a shared thread.

A pure sibling of `GrantRegistry`/`PgGrantStore` (tests/selftest_575_durable_grants.py is the
template this file translates), so what these tests pin mirrors that file's shape:

  - create + live_share_for round-trip in a plain in-memory registry
  - create refuses a blank grantee and a self-share (ValueError both)
  - find raises the SAME KeyError, with no distinguishing message, for "no such share" and
    "not yours" - a distinguishable error would be an oracle for other people's shares
  - revoke reaches the store, so a rebuilt registry does not resurrect a revoked share
  - a store that cannot be reached on REVOKE fails closed: the caller learns the revoke did
    not take effect, and the share stays live in this process too (mirrors #575 review,
    Finding 1 - a resurrected conversation share re-opens the transcript AND its conv-grants
    path, not merely one document)
  - expiry is evaluated per read, no sweeper - an already-expired share is never live
  - a store that cannot be reached at construction never blocks boot - starts empty
  - list_shared_with returns only LIVE shares, and only the caller's own
  - PgConversationShareStore's real emitted SQL (not a hand-rolled substitute) round-trips
    every column through the actual INSERT/SELECT text, so a column transposition between
    either statement would show up here exactly as it would against a live database - the
    single most dangerous defect in this kind of file, because a swapped column feeds
    `_aware()` something that is not a datetime, which raises, which
    `ConversationShareRegistry.__init__` swallows, and the box boots with zero shares hydrated
  - create refuses a blank/whitespace-only conv_id (round 1 review finding: `list_for_conversation`
    is an authorization-scoping filter - it must never hand one grantor another grantor's shares
    on the SAME conv_id, and must never leak a share that lives on a DIFFERENT conv_id, and its
    ordering claim (created_at) is pinned too

    PYTHONPATH=src python3 tests/selftest_600_conversation_shares.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.server.conversation_shares import (  # noqa: E402
    ConversationShare, ConversationShareRegistry, InMemoryConversationShareStore,
    PgConversationShareStore,
)


def test_create_then_live_share_for_round_trips():
    r = ConversationShareRegistry()
    s = r.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="bob")
    assert r.live_share_for("conv-1", "bob") is s


def test_self_share_and_blank_grantee_refused():
    r = ConversationShareRegistry()
    try:
        r.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="")
        raise AssertionError("a blank grantee must be refused")
    except ValueError:
        pass
    try:
        r.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="   ")
        raise AssertionError("a whitespace-only grantee must be refused")
    except ValueError:
        pass
    try:
        r.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="alice")
        raise AssertionError("a self-share must be refused")
    except ValueError:
        pass


def test_blank_conv_id_refused():
    """Round 1 review finding: `Grant.create` (grants.py) was hardened against exactly this
    two rounds into this same feature - a blank conv_id produces a record that LOOKS like a
    real share but can never match any conv_id a real read request could carry, so it
    silently never opens. Refused at mint time rather than silently minted."""
    r = ConversationShareRegistry()
    try:
        r.create(conv_id="", grantor_oid="alice", grantee_oid="bob")
        raise AssertionError("a blank conv_id must be refused")
    except ValueError:
        pass
    try:
        r.create(conv_id="   ", grantor_oid="alice", grantee_oid="bob")
        raise AssertionError("a whitespace-only conv_id must be refused")
    except ValueError:
        pass


def test_find_one_message_for_absent_and_not_yours():
    r = ConversationShareRegistry()
    s = r.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="bob")

    try:
        r.find("no-such-share", "alice")
        raise AssertionError("an absent share must raise KeyError")
    except KeyError as e:
        absent_error = e

    try:
        r.find(s.share_id, "mallory")          # exists, but mallory did not create it
        raise AssertionError("a share belonging to someone else must raise KeyError")
    except KeyError as e:
        not_yours_error = e

    # Same exception type, and each carries ONLY the id it was passed - no extra clause
    # ("not yours" / "not found") that would let a caller distinguish the two cases and use
    # that as an oracle for whether a share_id belongs to somebody else.
    assert type(absent_error) is type(not_yours_error) is KeyError
    assert str(absent_error) == repr("no-such-share")
    assert str(not_yours_error) == repr(s.share_id)
    assert r.find(s.share_id, "alice") is s, "the actual grantor can still find her own share"


def test_revoke_reaches_the_store_and_a_rebuilt_registry_stays_revoked():
    store = InMemoryConversationShareStore()
    r1 = ConversationShareRegistry(store=store)
    s = r1.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="bob")
    r1.revoke(s.share_id, "alice")
    r2 = ConversationShareRegistry(store=store)       # "after the deploy"
    assert r2.live_share_for("conv-1", "bob") is None, \
        "a genuinely revoked share must not come back"


def test_revoke_fails_closed_on_a_down_store():
    """Mirrors #575 review Finding 1: a store outage on revoke must surface to the caller
    (raise) and must NOT clear memory - clearing memory anyway would leave the row alive in
    the store, ready to be resurrected as a live share by the next restart's hydration."""
    class DeadDeleteStore:
        def load_all(self):
            return []

        def save(self, s):
            pass

        def delete(self, share_id):
            raise RuntimeError("down")

    r = ConversationShareRegistry(store=DeadDeleteStore())
    s = r.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="bob")
    try:
        r.revoke(s.share_id, "alice")
        raise AssertionError("revoke must surface the store failure, not swallow it")
    except RuntimeError:
        pass
    assert r.live_share_for("conv-1", "bob") is s, \
        "a revoke that failed against the store must leave the share live in this process too"


def test_expiry_is_evaluated_per_read_no_sweeper():
    r = ConversationShareRegistry()
    r.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="bob", expires_in_days=-1)
    assert r.live_share_for("conv-1", "bob") is None, \
        "an already-expired share must never be live, evaluated fresh on this read"


def test_hydration_failure_starts_empty_never_blocks_boot():
    class Dead:
        def load_all(self):
            raise RuntimeError("down")

    r = ConversationShareRegistry(store=Dead())
    assert r.list_shared_with("bob") == []
    # boot must not have raised to get here at all


def test_list_shared_with_returns_live_only_and_only_mine():
    r = ConversationShareRegistry()
    live = r.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="bob")
    r.create(conv_id="conv-2", grantor_oid="alice", grantee_oid="bob", expires_in_days=-1)
    r.create(conv_id="conv-3", grantor_oid="alice", grantee_oid="carol")
    assert r.list_shared_with("bob") == [live]


def test_list_for_conversation_is_scoped_to_the_requesting_grantor():
    """Round 1 review Finding 1: `list_for_conversation` is an authorization-scoping filter,
    not a convenience listing. Pins all three ways it could leak: another grantor's share on
    the SAME conv_id, this grantor's own share on a DIFFERENT conv_id, and the created_at
    ordering the method's sorted() call promises."""
    r = ConversationShareRegistry()
    # alice's two shares on conv-1, created in a known order
    mine_1 = r.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="bob")
    mine_2 = r.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="carol")
    # carol's share on the SAME conversation - must never appear in alice's listing, even
    # though it shares conv_id with alice's own shares (the exact leak a dropped
    # grantor_oid filter would produce)
    r.create(conv_id="conv-1", grantor_oid="carol", grantee_oid="dave")
    # alice's share on a DIFFERENT conversation - must never appear in the conv-1 listing
    r.create(conv_id="conv-2", grantor_oid="alice", grantee_oid="bob")

    listing = r.list_for_conversation("conv-1", "alice")

    assert [s.share_id for s in listing] == [mine_1.share_id, mine_2.share_id], (
        "must be exactly alice's two conv-1 shares, in created_at order - not carol's share "
        "on the same conversation, not alice's share on a different one")
    assert all(s.grantor_oid == "alice" for s in listing), (
        "every returned share must belong to the requesting grantor - a share with a "
        "different grantor_oid here is exactly the leak this test exists to catch"
    )


def test_resharing_the_same_conversation_to_the_same_person_updates_one_row():
    """Fix round 2, NEW-2. A second share of the same thread to the same person is the SAME
    row with a fresh boundary, not a second row beside it. Duplicates were three defects at
    once: the later share was ignored by `live_share_for`, revoking the older one widened what
    the recipient could read, and revoking either dropped every conversation grant while
    leaving a survivor share open."""
    r = ConversationShareRegistry()
    first = r.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="bob", turn_cutoff=1)
    second = r.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="bob", turn_cutoff=4)
    assert second.share_id == first.share_id, (
        f"a second row was minted: {first.share_id} then {second.share_id}")
    assert r.live_share_for("conv-1", "bob").turn_cutoff == 4, (
        "the deliberate re-share did not move the boundary")
    assert len(r.list_for_conversation("conv-1", "alice")) == 1
    # A different person, and a different conversation, are still separate shares.
    assert r.create(conv_id="conv-1", grantor_oid="alice",
                    grantee_oid="carol").share_id != first.share_id
    assert r.create(conv_id="conv-2", grantor_oid="alice",
                    grantee_oid="bob").share_id != first.share_id


def test_a_second_grantor_never_overwrites_the_first_ones_share():
    """`grantor_oid` is part of the dedupe key, not omitted. Two people can share threads
    that happen to carry the same client-chosen conv_id, and keying on (conv_id, grantee)
    alone would let the second sharer silently overwrite - which is to say silently revoke -
    the first one's share. `live_share_for` then answers with the NEWEST, so which one a
    reader resolves to is deterministic rather than dict-order dependent; under the bug it
    was the oldest, which is what made a revoke able to WIDEN the transcript."""
    r = ConversationShareRegistry()
    mine = r.create(conv_id="conv-1", grantor_oid="alice", grantee_oid="bob", turn_cutoff=1)
    theirs = r.create(conv_id="conv-1", grantor_oid="carol", grantee_oid="bob", turn_cutoff=9)
    assert theirs.share_id != mine.share_id, "one sharer's share overwrote another's"
    assert r.find(mine.share_id, "alice") is not None      # alice's share survived intact
    assert r.live_share_for("conv-1", "bob").share_id == theirs.share_id, (
        "live_share_for must answer with the NEWEST live share, not the first one iteration "
        "happens to reach")


class _FakeSqlTable:
    """Column-name-aware stand-in for a Postgres connection - the same idea
    tests/selftest_575_durable_grants.py's `_FakeSqlTable` uses, built to expose exactly the
    bug that idiom exists to catch: `InMemoryConversationShareStore` stores `ConversationShare`
    objects directly and never goes anywhere near `save`'s param tuple or `load_all`'s SELECT
    text, so a column swap in either (conversation_shares.py) would leave the whole suite
    green while a real deploy booted with zero shares hydrated (`_aware()` called on a
    non-datetime raises, `ConversationShareRegistry.__init__` swallows it and starts empty -
    the exact silent failure this guards).

    This does NOT reimplement conversation_shares.py's logic - it reads the REAL SQL text
    `PgConversationShareStore` emits (the actual `INSERT INTO ... (col, col, ...)` and
    `SELECT col, col, ... FROM` strings) and binds `params` to those column names
    positionally, the same job Postgres itself would do. A transposition of two columns in
    either the emitted column list or the params tuple shows up here exactly as it would
    against a live database - no live database required."""

    def __init__(self, rows: dict) -> None:
        self._rows = rows           # shared across every _FakeConn built from this table

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("CREATE TABLE") or s.startswith("ALTER TABLE"):
            # ALTER TABLE ADD COLUMN IF NOT EXISTS is the migration half of the schema (a
            # CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so a column added
            # later needs it). Nothing to model here - `_rows` is keyed by column NAME, so a
            # column simply exists once save() binds it.
            return self
        if s.startswith("INSERT INTO"):
            cols = [c.strip() for c in s[s.index("(") + 1:s.index(")")].split(",")]
            assert len(cols) == len(params), (cols, params)
            # #851: a jsonb param arrives wrapped (`psycopg.types.json.Jsonb`) and comes back
            # from a real Postgres as the plain Python value. Model that, rather than storing
            # the wrapper and handing it to `_row_to_share` - a fake that returns something no
            # database ever returns tests the code against a world that does not exist, and
            # the "fix" it demands is defensive handling of a driver type the read path will
            # never see.
            bound = [getattr(p, "obj", p) for p in params]
            self._rows[dict(zip(cols, bound))["share_id"]] = dict(zip(cols, bound))
            return self
        if s.startswith("SELECT"):
            names = [c.strip() for c in s[len("SELECT"):s.index("FROM")].split(",")]
            self._fetched = [tuple(row[n] for n in names) for row in self._rows.values()]
            return self
        if s.startswith("DELETE"):
            self._rows.pop(params[0], None)
            return self
        raise AssertionError(f"_FakeSqlTable does not understand: {s}")

    def fetchall(self):
        return self._fetched


def test_pg_store_round_trips_every_column_via_the_real_sql_text():
    """#600's version of #575 review Finding A. Every column, through PgConversationShareStore's
    real save()/load_all() SQL - not InMemoryConversationShareStore, which every other test in
    this file uses and which cannot catch a column-order bug because it never touches a column
    list at all."""
    rows: dict = {}
    store = PgConversationShareStore("postgresql://unused/fake")
    store._conn = lambda: _FakeSqlTable(rows)          # stand in for a live Postgres

    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expires = datetime(2026, 2, 1, tzinfo=timezone.utc)
    s = ConversationShare(share_id="s-600", conv_id="conv-distinct", grantor_oid="alice",
                          grantee_oid="bob", expires_at=expires, created_at=created,
                          turn_cutoff=7)
    store.save(s)
    [reloaded] = store.load_all()
    assert reloaded.share_id == "s-600"
    assert reloaded.conv_id == "conv-distinct", (
        "conv_id did not round-trip through the real INSERT/SELECT column lists")
    assert reloaded.grantor_oid == "alice"
    assert reloaded.grantee_oid == "bob"
    assert reloaded.expires_at == expires, (
        "expires_at came back wrong - it may have swapped columns with created_at")
    assert reloaded.created_at == created, (
        "created_at came back wrong - it may have swapped columns with expires_at")
    assert reloaded.turn_cutoff == 7, (
        "turn_cutoff did not round-trip through the real INSERT/SELECT column lists - the "
        "transcript boundary would come back wrong, or as NULL, after a restart")


def test_a_naive_expires_at_from_a_row_does_not_raise_on_is_live():
    """`ConversationShare.is_live()` compares `expires_at` against an AWARE
    `datetime.now(timezone.utc)`. A naive datetime coming back off a row would raise TypeError
    on that comparison, turning a live share's next read into a 500 instead of an open
    conversation. `_row_to_share` must normalize a naive value to UTC first."""
    from dbsearch.server.conversation_shares import _row_to_share

    future_naive = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    created_naive = datetime.now(timezone.utc).replace(tzinfo=None)

    live = _row_to_share(("s1", "conv-1", "alice", "bob", future_naive, created_naive, 3))
    assert live.expires_at.tzinfo is not None, "a naive expires_at must come back aware"
    assert live.created_at.tzinfo is not None, "a naive created_at must come back aware"
    assert live.is_live() is True                  # must not raise, and must be correct
    assert live.turn_cutoff == 3


def test_a_null_turn_cutoff_from_a_legacy_row_fails_closed_to_zero():
    """Fix round 1, CRITICAL-2. `turn_cutoff` was added by ALTER TABLE, so a row written
    before it existed comes back NULL - the share's transcript boundary is UNKNOWN. Reading
    that as "no limit" would hand the recipient the whole live thread, which is the exact bug
    the column was added to close, reintroduced by a migration artefact. Unknown reads as
    ZERO: the share opens no grantor turns at all until somebody re-shares deliberately."""
    from dbsearch.server.conversation_shares import _row_to_share

    now = datetime.now(timezone.utc)
    legacy = _row_to_share(("s1", "conv-1", "alice", "bob", None, now, None))
    assert legacy.turn_cutoff == 0, (
        f"an unknown transcript boundary must fail closed, not open: {legacy.turn_cutoff}")


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
