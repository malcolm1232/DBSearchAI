"""Sharing a CONVERSATION with a named person (#600).

The recipient of a share reads the thread and keeps asking her own questions, answered from
exactly the documents that thread cited (Task 5). Tasks 1-3 made that possible: conversations
are durable, and a document grant can now carry a `conv_id` so its ADR 0019 doorway pair opens
only inside that conversation (`Grant.conv_id`, `GrantRegistry.drop_for_conversation`,
`_request_scope` in server/app.py). What was still missing is the record that lets the
recipient OPEN the thread in the first place - a conversation share is that record.

This is a pure sibling of `GrantRegistry`/`PgGrantStore` (dbsearch.api.grants,
dbsearch.server.grant_store): same shape, same durability story, and the same refusal to give
its write paths one uniform failure stance (four of them here, enumerated below).
It is collapsed into ONE file here (registry + both store adapters) because, unlike the grant
registry, there is exactly one caller-facing record type and no ACL-expansion side to keep
separate.

  revoke   - drop the record; a caller whose share was revoked can no longer open the
             conversation through it (Task 5's read path looks a share up before opening the
             doorway). No rewrite of anything else is needed - there is no second copy of
             this decision anywhere.
  expiry   - evaluated during `is_live`, on every read. No sweeper, so there is no "the
             sweeper did not run and an expired share still opens" failure mode to reason
             about - the same property `Grant.is_live` gives document sharing.
  boundary - `turn_cutoff` freezes WHICH TURNS this share hands over: the grantor's first N,
             as they stood when she pressed share. The dataclass field carries the full
             account of why a live transcript beside snapshot grants is a disclosure route
             rather than a convenience.
  audience - #605 / ADR 0021. The SAME share row now serves two audiences: "people" (a named
             grantee who signs in - ADR 0020, everything above, unchanged) and "link" (anyone
             holding an unguessable token, who signs in to nothing). Everything downstream of
             that choice - which documents are exposed, expiry, revoke, the management
             surface - is common to both; only the identity story differs. That is why this
             is one row with a field and not a second record type beside it.
  LAW 1    - `to_dict()` returns ids, counts and timestamps only. A share record is metadata about
             WHO may open WHICH conversation and UNTIL WHEN; it is never asked to carry, and
             must never be made to carry, a single word of what was said inside that
             conversation.

Durability: optional write-through store (`InMemoryConversationShareStore` for tests/rigs,
`PgConversationShareStore` against the deployment's own PGVECTOR_DSN - no new
infrastructure). Unset, `ConversationShareRegistry` behaves exactly as an in-memory-only
registry always has; every existing caller keeps working untouched. `_by_id` stays the ONLY
thing a live request reads - the store adds a write path and a one-time hydration at
construction, never a read-time round trip.

The FOUR failure stances are NOT uniform, and must not be collapsed into one "best-effort" or
one "fail-closed" rule. In order: `create`, `revoke`, `discard_unconfirmed`, `record_open`.
Three of the four are best-effort and exactly one is fail-closed, but they are best-effort for
three DIFFERENT reasons, and that is the point of writing them out rather than counting them:

  `create` is best-effort on the store. A down store costs durability (the share does not
  survive a restart), never the ability to serve the request that is asking for a share right
  now - the same trade `GrantRegistry.create` and `TokenVault`'s refresh-token store make.

  `revoke` is FAIL-CLOSED: the store delete runs first, unguarded, and memory is only cleared
  once it succeeds. A resurrected conversation share is not a cosmetic staleness bug - it
  reopens the transcript AND, transitively, whatever conv-scoped document grants still name
  that `conv_id` (Task 3's `Grant.conv_id` / `drop_for_conversation`), to someone the grantor
  deliberately cut off. `GrantRegistry.revoke`'s docstring (grants.py) is the full account of
  why that direction of failure - "looks revoked, isn't" - is the one this design cannot
  tolerate; the same reasoning applies here verbatim, just one layer up (the thread, not one
  document in it).

  `discard_unconfirmed` is best-effort and NEVER raises. It exists for the same reason
  `GrantRegistry.discard_unconfirmed` does (#575 review, Finding A): Task 5's share operation
  creates the share row BEFORE it knows whether any document the conversation cited still
  exists, and must roll that row back from inside an ALREADY-FAILING request if it doesn't.
  Calling fail-closed `revoke` from that rollback would let a store outage mask an honest 400
  behind a 503, and would leave the dead share sitting in `_by_id` for the rest of the
  process's life besides, since a masked rollback never runs the memory pop it exists to
  guarantee. A share row whose create never actually took effect is not a live authorization
  decision - there is nothing here for a store outage to put at risk - so the in-process
  removal is unconditional and the store delete is logged and swallowed, exactly `create`'s
  stance, because this is closer to "undo a create" than "perform a revoke".

  `record_open` is best-effort and NEVER raises, for a reason of its own: it is a COUNTER, on
  the read path of a page a visitor is loading right now. There is no authorization decision
  in it and nothing it can leave in an unsafe state, so a store outage costing an accurate
  open count is the whole of the damage; letting it cost the page load instead would trade a
  wrong number for a broken feature. It is also the only one of the four whose durable write
  is COALESCED rather than per-call (`OPEN_PERSIST_INTERVAL_SECONDS`, #612 review Finding 2),
  because it is the only one of the four an UNAUTHENTICATED caller can drive: it runs on every
  `GET /c/{token}`, and the store connects per operation.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from dbsearch.api.grants import LINK_PRINCIPAL_PREFIX

# The two audiences a share row can serve (#605, ADR 0021). "people" is the pre-#605
# behaviour and the default everywhere, including for every row written before this field
# existed - see `_row_to_share`.
AUDIENCE_PEOPLE = "people"
AUDIENCE_LINK = "link"

#: How often ONE share's open count is allowed to reach the STORE, in seconds (#612 review,
#: Finding 2). The in-memory count is still bumped on every single open and is always exact;
#: this bounds only the durable write behind it.
#:
#: WHY A CEILING IS NEEDED AT ALL. `record_open` runs on `GET /c/{token}` - the only
#: unauthenticated write in the product that a plain page READ can drive, on a sync route, on
#: a single worker, and reachable by anyone the link was forwarded to.
#:
#: NOT "the only unauthenticated write", which an earlier version of this comment said and
#: which is false (#612 re-review, Finding B): `POST /c/{token}/chat` also writes anonymously -
#: a `conversation_turns` row (`query/conversation.py`) and an audit row - and
#: `docs/DEPLOY_CONVERSATION_SHARING.md` (not in the public tree, #685) documents those turn rows. The distinction that
#: matters is not authentication, it is what bounds the write. Those two are behind
#: `_check_link_rate` (`DBSEARCH_LINK_QUESTIONS_PER_HOUR`, default 30) and cost an LLM call
#: each anyway, so they are already ceilinged twice over. A page load was ceilinged by nothing.
#:
#: `PgConversationShareStore` opens
#: a NEW `psycopg.connect()` per operation and `save` writes the full row, so before this
#: constant existed one Slack unfurl bot, one crawler or one `while true; do curl` turned into
#: one TCP-plus-auth Postgres connect per request. The read itself is NOT rate limited and must
#: not be - reading is free (no retrieval, no LLM call) and capping it would refuse a visitor
#: re-reading a page they already paid for. So the fix is on the WRITE, which is the only part
#: that costs anything.
#:
#: SIXTY SECONDS, and the number is a judgement about what the count is FOR rather than about
#: database load. It is a signal to the owner that her link travelled further than she meant -
#: a number she reads on a management surface, minutes or days after the fact. Nothing anywhere
#: makes a decision on it, no authorization consults it, and no view refreshes fast enough for
#: a minute of staleness to be visible. Against that, 60s turns a sustained crawl from one
#: connect per request into one connect per minute per share, which is a bound rather than a
#: reduction: it does not matter how fast the requests arrive.
#:
#: WHAT IT COSTS. Up to a minute of opens on any one share are in memory only, so a process
#: killed inside that window loses them. That is the correct trade for a counter and the same
#: one `record_open`'s never-raises stance already made: the worst outcome here has always been
#: a wrong number, and a number that is occasionally low is better than an unauthenticated
#: connect storm. The FIRST open on a share always persists immediately, so "this link has been
#: opened at all" is never the fact that gets lost.
OPEN_PERSIST_INTERVAL_SECONDS = 60.0

LINK_GRANTEE_PREFIX = LINK_PRINCIPAL_PREFIX
# ONE definition, imported from `dbsearch.api.grants` rather than spelled out again here (#605
# review, Finding 3). The ACL layer has to RECOGNISE this shape - `GrantAwareIdentity.
# direct_principals` short-circuits it so a synthetic principal is never handed to a directory
# adapter that would try to look it up as a person - and this registry only MINTS it, so the
# constant belongs at the layer that must not be allowed to drift away from the other.
# A link share has no grantee - that is the entire feature - but every mechanism downstream of
# this row is keyed on one: `live_share_for`, and (ADR 0021, "Mechanism") the conv-scoped
# document grants whose principal `GrantRegistry.live_principals_for` expands. Rather than
# make all of them understand a null grantee, a link share names a SYNTHETIC principal,
# `link:<share_id>`, minted with the row and requiring no identity to exist behind it. It is
# a NAME, not a credential: it is safe to log and to show in a management surface. The
# credential is the token, and the token is never this.
#
# The prefix is a constant, not a literal spelled out at each site, because two of the places
# that will care about it are authorization code in other modules (ADR 0021's route family and
# the ACL expansion), and a drifted prefix there means a principal that matches nothing - a
# link that silently opens no documents - or, in the other direction, a real oid that happens
# to start with "link:" being treated as a link visitor.


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_digest(token: str) -> str:
    """SHA-256 hex of a link token. The row stores THIS and never the token itself (ADR 0021,
    "Mechanism", and its rejected alternative "storing the token in plaintext for simpler
    lookup"): anyone who can read `conversation_shares` - an operator, a support engineer, a
    stolen backup, a SQL-injection foothold - must not thereby acquire the ability to open
    every live share. A plain digest with no salt and no stretching is correct here and is not
    a password shortcut: the input is 128 bits of `secrets.token_hex` entropy, so there is no
    dictionary to run and the only attack left is the same brute force the token's own
    unguessability already assumes."""
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt):
    """Defend against a naive datetime coming back off a row - the same floor
    grant_store.py's `_aware` sits under: `ConversationShare.is_live()` compares `expires_at`
    against an AWARE `datetime.now(timezone.utc)`, and comparing aware to naive raises
    TypeError, which would turn a live share's next read into a 500 rather than an open
    conversation. A real psycopg connection against a `timestamptz` column already returns
    aware datetimes; this is a defensive floor under that assumption, not a correction of a
    known bug."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def _safe_reason(exc: BaseException) -> str:
    """Data-free store error: class name + SQLSTATE only, never `str(exc)` - a driver message
    can quote a stored value, and `grantor_oid`/`grantee_oid` live here (manifest_store.py's
    `ManifestStoreUnavailable` docstring is the full account of why)."""
    sqlstate = getattr(exc, "sqlstate", None)
    return (f"{type(exc).__name__} (sqlstate {sqlstate})" if sqlstate
            else type(exc).__name__)


class ConversationShareStoreUnavailable(Exception):
    """Configured but unreachable. Message is data-free (class name + SQLSTATE only) - see
    `_safe_reason`."""


@dataclass(frozen=True)
class ConversationShare:
    share_id: str
    conv_id: str
    grantor_oid: str
    grantee_oid: str
    expires_at: "datetime | None"
    created_at: datetime = field(default_factory=_now)
    turn_cutoff: int = 0
    # Fix round 1, CRITICAL-2: how many of the GRANTOR's turns existed when this share was
    # made. The share hands over a TRANSCRIPT AS IT WAS READ, not a subscription to a thread
    # that keeps growing. Without this the document grants were a snapshot while the
    # transcript was live, so every turn the grantor added afterwards reached the recipient
    # as answer TEXT - including answers synthesized from documents she was never granted and
    # provably cannot retrieve. The grant channel was closed and the content channel was
    # open, which is the same disclosure by a different route.
    #
    # A COUNT, not a timestamp, because that is what the data can honestly support: `Turn`
    # carries no time, and conversation history is returned oldest-first and append-only per
    # (conv_id, user_oid) - PgConversationStore orders by its dense `seq` and
    # InMemoryConversationStore by list position - so "the first N turns" is exactly "the
    # turns that existed at share time" and nothing can renumber them under it.

    audience: str = AUDIENCE_PEOPLE
    # #605 / ADR 0021. DEFAULTED, and defaulted to the pre-#605 behaviour, on purpose: every
    # existing constructor call in this repo and every row already in Postgres keeps meaning
    # exactly what it meant before this field existed. "people" is also the fail-closed
    # reading of a missing value - a people share hands nothing to a caller who cannot sign
    # in, whereas an accidental "link" would.

    token_hash: "str | None" = None
    # SHA-256 of the link's bearer token, for "link" rows only; None for a people share, which
    # has no bearer credential at all. The PLAINTEXT token is never here, never persisted and
    # never logged - it is returned exactly once, from `create_link`, and after that it exists
    # only in the URL the owner sends and in the visitor's browser (ADR 0021, "Mechanism").
    # Everything about how this field is read is in `_token_digest` and `find_by_token`.

    shared_stores: list = field(default_factory=list)
    # #851 (owner ruling on #850). WHICH COMPOSED STORES the grantor consented to pass on,
    # for turns the router answered from one. It is the store-plane twin of the conv-scoped
    # document grants, and it exists for the same reason they do: a share must record WHAT
    # WAS AGREED, because the transcript read re-checks it at every open.
    #
    # A LIST ON THE ROW rather than a grant per store, and the difference is not laziness. A
    # document grant is an AUTHORIZATION - it widens what the recipient may RETRIEVE, and is
    # re-checked live so a revoke closes retrieval and reading together. There is nothing to
    # widen here: DBSearch does not own the customer's warehouse permissions and cannot grant
    # them. What travels is a FROZEN RECORD of what the grantor already saw, exactly as she
    # saw it, because she said it could. So this is consent, not capability, and modelling it
    # as a grant would claim an authority the product does not have.
    #
    # DEFAULTED EMPTY, and empty means "this share predates the field", which is read as
    # CONSENT TO NOTHING - the pre-#851 behaviour, where a routed turn stopped the transcript
    # unconditionally. Fail-closed on an old row is the only safe reading: the grantor of a
    # row written before this existed was never asked the question, and answering it for her
    # in the generous direction would hand over evidence she never agreed to.

    opens: int = 0
    last_open_at: "datetime | None" = None
    # How often, and when last, this share was actually opened. Both are DISCLOSURE facts, not
    # analytics: ADR 0021's accepted consequences say the owner can see what strangers did
    # with a link she forwarded, and a link that has been opened forty times when she sent it
    # to one person is the only signal she gets that it travelled further than she meant.
    # These are the "who and when" class of fact, never "what was said" - see `to_dict`.

    def is_live(self, at: "datetime | None" = None) -> bool:
        return self.expires_at is None or (at or _now()) < self.expires_at

    def to_dict(self) -> dict:
        """LAW 1: who and when, never conversation content. `turn_cutoff` is a COUNT of
        turns - the same class of fact as an expiry timestamp, and the boundary the surface
        needs in order to say "you are reading the first N turns of this thread".

        `token_hash` is DELIBERATELY ABSENT and must stay absent. Everything else on this row
        is metadata a management surface is entitled to render; the digest is a credential
        derivative, and this method is the one place on the record that reaches a client, so
        including it would turn every "shares I have made" listing into a distribution channel
        for the one secret the row exists to protect. `audience`, `opens` and `last_open_at`
        ARE included: they say who this share is for and when it was used, never a word of
        what was said inside the conversation."""
        return {"share_id": self.share_id, "conv_id": self.conv_id,
                "grantor_oid": self.grantor_oid, "grantee_oid": self.grantee_oid,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "created_at": self.created_at.isoformat(),
                "turn_cutoff": self.turn_cutoff,
                "audience": self.audience,
                # #851: WHICH SOURCES were agreed - store ids, the same class of fact as
                # `turn_cutoff` (the shape of the agreement, never its content). The sharer's
                # own management surface needs it to render what she consented to; a store id
                # is a name she chose and typed herself.
                "shared_stores": list(self.shared_stores),
                "opens": self.opens,
                "last_open_at": (self.last_open_at.isoformat()
                                 if self.last_open_at else None),
                "live": self.is_live()}


class ConversationShareRegistry:
    def __init__(self, store=None) -> None:
        """`store` is optional, exactly as `GrantRegistry`'s is (grants.py): unset, this is a
        plain in-memory registry, and every caller that constructs `ConversationShareRegistry()`
        with nothing keeps working untouched. When set, hydration is best-effort - an
        unreachable store logs and starts empty rather than failing boot, the same stance
        `GrantRegistry` and `TokenVault` take on their own stores, because a store outage must
        cost durability, never the ability to serve a request."""
        self._by_id: dict[str, ConversationShare] = {}
        self._lock = threading.Lock()
        self._store = store
        #: Open-count coalescing state, both guarded by `self._lock` (see
        #: `OPEN_PERSIST_INTERVAL_SECONDS` and `record_open`). `_open_persisted_at` is when this
        #: share's count last reached the store; `_opens_dirty` names the shares whose memory
        #: count has moved since. Neither is ever read by an authorization path - they exist
        #: only to decide when a counter is written.
        self._open_persisted_at: dict[str, datetime] = {}
        self._opens_dirty: set[str] = set()
        #: Shares whose `revoke` has begun but not finished - see `_counter_save` and `revoke`.
        #: A counter write for an id in here is DROPPED. This is what makes "a coalesced open
        #: count can never resurrect a revoked link" a property of the code rather than of the
        #: comment that used to claim it (#612 re-review, Finding A).
        self._revoking: set[str] = set()
        if store is not None:
            try:
                self._by_id = {s.share_id: s for s in store.load_all()}
            except Exception:
                logging.getLogger("dbsearch").error(
                    "conversation share store unreadable - starting with no shares loaded")

    def create(self, conv_id: str, grantor_oid: str, grantee_oid: str,
               expires_in_days: "int | None" = None,
               turn_cutoff: int = 0,
               shared_stores: "list | None" = None) -> ConversationShare:
        """The PEOPLE path (ADR 0020), unchanged in behaviour by #605: a named grantee who
        signs in.

        The two refusals below are this path's ALONE and stay here rather than moving down
        into `_admit`, which `create_link` also calls. Neither can hold for a link share: its
        grantee is the synthetic `link:<share_id>` sentinel, which is not a person, is not
        blank, and can never equal the grantor's oid. Pushing them down would make
        `create_link` unreachable; deleting them to make the factoring tidier would let a
        route that forwards an empty body field mint a people share nobody can ever hold.
        tests/selftest_600_conversation_shares.py pins both, and selftest_605 pins that
        factoring the shared half did not move them."""
        if not grantee_oid or not grantee_oid.strip():
            raise ValueError("a conversation share needs somebody to share with")
        if grantee_oid == grantor_oid:
            raise ValueError("you already have access to this conversation")
        return self._admit(conv_id, grantor_oid, grantee_oid,
                           expires_in_days=expires_in_days, turn_cutoff=turn_cutoff,
                           shared_stores=shared_stores)

    def create_link(self, conv_id: str, grantor_oid: str,
                    expires_in_days: "int | None" = None,
                    turn_cutoff: int = 0,
                    shared_stores: "list | None" = None) -> "tuple[ConversationShare, str]":
        """The LINK path (#605, ADR 0021): a share for anyone holding an unguessable token,
        who signs in to nothing. Returns the row AND the plaintext token.

        THE TOKEN IS RETURNED EXACTLY ONCE AND IS NEVER RECOVERABLE. It is not stored, not
        logged, not placed on the record and not put in any exception message - the row keeps
        only `_token_digest(token)`, so if the caller loses this return value the only remedy
        is to mint a new link. That is the point, not an inconvenience: ADR 0021's whole
        security argument is that a database read yields hashes that authorize nothing, and
        the first log line or error string that echoes a token back out defeats it in a place
        (a log aggregator, a traceback in an error tracker) that is far easier to read than
        the database ever was.

        `secrets.token_hex(16)` - 128 bits, from the CSPRNG, never `random`. Possession of
        this value IS the authorization (ADR 0021, "Decision"), so its unguessability is the
        entire access-control margin for everything the share exposes; there is no password,
        no second factor and no rate-limited login behind it.

        The share_id is minted HERE, before the row exists, because the sentinel grantee is
        derived from it - `link:<share_id>` cannot be computed by `_admit` after the fact
        without either a second write or a placeholder grantee briefly living in `_by_id`.
        `_admit` therefore accepts a caller-minted id; it is the only difference between the
        two paths' calls, and everything after it - validation of conv_id, expiry arithmetic,
        the lock, the write-through and its best-effort stance - is one shared body.

        Deliberately NOT deduplicated the way `create` is. Two links on the same conversation
        are two independently revocable credentials, which is exactly what an owner who sent
        one link to a customer and another to a colleague is asking for; collapsing them onto
        one row would make revoking either kill both, silently.

        `expires_in_days` must be None or a POSITIVE INTEGER - see the refusal below."""
        # ADR 0021 invariant 3: every link is bounded in time. `_admit`'s expiry arithmetic is
        # `(at + timedelta(days=expires_in_days)) if expires_in_days else None`, so ZERO takes
        # the falsy branch and mints a link that NEVER EXPIRES - the opposite of what a caller
        # passing 0 could possibly mean, arrived at silently, on a credential whose whole
        # bounding story is its expiry. A negative value is worse in a different direction: it
        # mints a link that is dead the instant it is created, so the owner copies a URL that
        # 404s and has no way to learn why.
        #
        # Refused HERE, where the fact is known, rather than sanitised in the route that will
        # call this. A check placed before the deciding fact is UX; this is the only place
        # that knows a link is being minted at all, and any future second caller (a CLI, a
        # test rig, a later route) would otherwise have to remember the same rule and would
        # eventually not. `create`'s people path is deliberately NOT changed - a people share
        # with no expiry is a supported shape there, ADR 0020 never bounded it, and
        # tests/selftest_600_conversation_shares.py pins the current behaviour.
        #
        # None still means "the caller set no expiry": the ROUTE supplies ADR 0021's 7-day
        # default (a later task), and this method must keep accepting None so that default
        # has somewhere to arrive from. `bool` is excluded explicitly because `isinstance(True,
        # int)` is True in Python, and `expires_in_days=True` silently meaning "one day" is
        # exactly the kind of accident this refusal exists to stop.
        if expires_in_days is not None and (
                isinstance(expires_in_days, bool)
                or not isinstance(expires_in_days, int)
                or expires_in_days <= 0):
            raise ValueError(
                "a link share must be bounded in time: expires_in_days must be a positive "
                "whole number of days, or None to take the default")
        share_id = uuid.uuid4().hex
        token = secrets.token_hex(16)
        share = self._admit(conv_id, grantor_oid, f"{LINK_GRANTEE_PREFIX}{share_id}",
                            expires_in_days=expires_in_days, turn_cutoff=turn_cutoff,
                            share_id=share_id, audience=AUDIENCE_LINK,
                            token_hash=_token_digest(token),
                            # #851: a LINK is the outsider case the consent model exists for,
                            # so it carries the same list. The link reader never receives
                            # citations (docmeta is None on that path, ADR 0021), so what
                            # consent governs here is whether the ANSWER TEXT travels - which
                            # is the only channel that audience has, and the one that carries
                            # the figure.
                            shared_stores=shared_stores)
        return share, token

    def _admit(self, conv_id: str, grantor_oid: str, grantee_oid: str,
               expires_in_days: "int | None" = None, turn_cutoff: int = 0,
               share_id: "str | None" = None, audience: str = AUDIENCE_PEOPLE,
               token_hash: "str | None" = None,
               shared_stores: "list | None" = None) -> ConversationShare:
        """Everything both audiences share: conv_id validation, expiry arithmetic, the
        one-live-row rule, and the best-effort write-through. Only the caller-specific
        refusals live above this (see `create`).

        `share_id` is None on the people path, where the id is minted here (or reused from the
        live row this share replaces), and supplied on the link path, where the grantee is
        derived from the id and so the id must exist first. Supplying it also SKIPS the
        one-live-row lookup below, which is correct rather than incidental - see
        `create_link`'s note on why two links are two credentials."""
        # Review finding (round 1): grants.py's `Grant.create` was hardened against a
        # blank-or-whitespace conv_id two rounds into this same feature - a route that
        # forwards an empty body field would otherwise mint a record that LOOKS like a real
        # share but can never match any `conv_id` a real read request could carry, so it
        # silently never opens and nothing anywhere raises. Refused here for the same reason,
        # not merely normalized to None: unlike `Grant.conv_id` (optional - None means
        # "applies to every conversation"), conv_id is REQUIRED for a conversation share -
        # there is no meaningful "unscoped" conversation share, so a blank value is never
        # anything but caller error, and a share that can never open is worse than an error at
        # mint time.
        if not conv_id or not conv_id.strip():
            raise ValueError("a conversation share needs a conversation to share")
        conv_id = conv_id.strip()
        at = _now()
        expires_at = (at + timedelta(days=expires_in_days)) if expires_in_days else None
        # Fix round 2, NEW-2: ONE LIVE SHARE per (conv_id, grantee_oid, grantor_oid). Sharing
        # the same thread with the same person again UPDATES that row - same share_id, fresh
        # boundary, fresh expiry - instead of laying a second row beside it.
        #
        # Duplicates were not a cosmetic redundancy, they were three defects at once, all
        # reproduced: a deliberate re-share was IGNORED for the transcript because
        # `live_share_for` answered with the oldest match (so the boundary stayed frozen at
        # the first share, contradicting the promise that re-sharing is how new material gets
        # included); revoking the OLDER of two shares WIDENED what the recipient could read,
        # because the read then resolved to the newer, later-cutoff row - an authorization
        # operation that grants access; and revoking either one dropped ALL of that grantee's
        # conversation grants while removing one row, leaving a survivor share opening a
        # transcript backed by no documents. One row per (conversation, person, sharer) makes
        # the first work, the second impossible and the third vacuous.
        #
        # `grantor_oid` is part of the key, not omitted: two different people can share
        # threads that happen to carry the same client-chosen conv_id, and keying on
        # (conv_id, grantee) alone would let the second sharer silently overwrite - which is
        # to say silently revoke - the first one's share.
        #
        # `created_at` moves to now. Every other field on this row describes the CURRENT
        # share decision, and a created_at left behind at an earlier one would make the row
        # internally inconsistent - the sharer would read a boundary and an expiry from today
        # beside a date from last month and have no way to tell which act she is looking at.
        with self._lock:
            existing = None if share_id is not None else next(
                (x for x in self._by_id.values()
                 if x.conv_id == conv_id and x.grantee_oid == grantee_oid
                 and x.grantor_oid == grantor_oid and x.is_live(at)), None)
            s = ConversationShare(
                share_id=(share_id if share_id is not None else
                          (existing.share_id if existing is not None else uuid.uuid4().hex)),
                conv_id=conv_id, grantor_oid=grantor_oid, grantee_oid=grantee_oid,
                expires_at=expires_at, created_at=at,
                turn_cutoff=max(0, int(turn_cutoff)),
                audience=audience, token_hash=token_hash,
                # #851: DEFAULTS TO NOTHING, never to `existing.shared_stores`. A re-share is
                # a fresh decision by the grantor over a fresh checklist, exactly as the
                # boundary and the expiry are - carrying the previous consent forward would
                # let a re-share silently re-affirm a source she has since unticked.
                shared_stores=list(shared_stores or []))
            self._by_id[s.share_id] = s
        # Write-through, outside the lock (I/O should never hold up another reader), and
        # best-effort: a share that outlives the process is the goal, but a down store must
        # not turn a working share into a 500 - same stance `GrantRegistry.create` takes. A
        # share born while the store is unreachable simply does not survive a restart; the
        # share itself works right now, which is what the caller asked for.
        if self._store is not None:
            try:
                self._store.save(s)
            except Exception:
                logging.getLogger("dbsearch").error(
                    "conversation share store unreachable - share %s created in-process only",
                    s.share_id)
        return s

    def set_turn_cutoff(self, share_id: str, turn_cutoff: int) -> ConversationShare:
        """NARROW an existing share's boundary. #606.

        The share route computes the boundary twice: once against the documents that passed
        the shareability test, and again against the ones that ACTUALLY landed as grants (#601
        MINOR-F - a document deleted in between would otherwise be withheld from the recipient
        and reported as shared to the sharer). Before #606 that correction was a second
        `create` call, which worked only because the people path's one-live-row rule resolved
        back to the same row. `create_link` deliberately does not deduplicate - two links are
        two independently revocable credentials - so the same trick there would mint a SECOND
        share with a SECOND token. This is the one operation both audiences need, so it is one
        method rather than a branch at the call site.

        NARROWING ONLY, enforced here rather than trusted to the caller: `min` against what
        the row already carries, so this can never WIDEN what a share hands over. A method
        that could raise a live share's cutoff would be an authorization operation wearing the
        name of a correction - the one direction every docstring in this file rules out - and
        the caller that wants a wider boundary already has one, `create`, which is a fresh
        deliberate act by the grantor.

        An unknown share_id raises KeyError. Unlike `record_open`'s silent miss this is not a
        race a revoke can legitimately win: the caller minted the row moments earlier and is
        still inside that request, so a miss means the boundary correction did not happen and
        the share is standing at a cutoff wider than the grants behind it.

        Best-effort on the store, `create`'s stance and for `create`'s reason: a down store
        costs durability, never the ability to serve the request in hand. The narrower
        boundary is in memory either way, and this process is the one serving reads."""
        with self._lock:
            s = self._by_id.get(share_id)
            if s is None:
                raise KeyError(share_id)
            s = replace(s, turn_cutoff=min(s.turn_cutoff, max(0, int(turn_cutoff))))
            self._by_id[share_id] = s
        if self._store is not None:
            try:
                self._store.save(s)
            except Exception:
                logging.getLogger("dbsearch").error(
                    "conversation share store unreachable - share %s boundary narrowed "
                    "in-process only", share_id)
        return s

    def find(self, share_id: str, requester_oid: str) -> ConversationShare:
        """The share record, but only to the person who created it. A recipient never needs
        this lookup to READ the conversation - Task 5's open path resolves through
        `live_share_for`, keyed on the grantee, not through `find` - so the only legitimate
        caller of `find` is the grantor managing her own shares (e.g. listing them, or
        `revoke` below, which reuses this exact ownership check).

        One KeyError, one message, for BOTH "no such share" and "not yours" - mirrors
        `GrantRegistry.revoke`'s ownership check (grants.py). A response that distinguished
        the two would be an oracle: a caller could pass share ids they do not own and learn,
        from which flavour of 404 comes back, whether that id belongs to somebody else at
        all - other people's shares are not this caller's business to enumerate."""
        with self._lock:
            s = self._by_id.get(share_id)
        if s is None or s.grantor_oid != requester_oid:
            raise KeyError(share_id)
        return s

    def find_by_token(self, token: str) -> "ConversationShare | None":
        """Resolve a link token to its share - the lookup ADR 0021's `/c/{token}` doorway will
        sit on. LIVE shares only, and None for everything else.

        ONE return value for "no such token", "expired" and "revoked", exactly as `find` gives
        one KeyError for "absent" and "not yours", and for a sharper version of the same
        reason: the caller here is anonymous by construction. A response that distinguished
        expired from unknown would confirm to anyone sweeping tokens that a particular value
        was once real, which is the only feedback a brute-force search over the token space
        could ever get. None is the 404-shape, and 404 is what every one of these must look
        like from outside.

        `is_live` is the only enforcement of ADR 0021 invariant 3 (bounded in time). There is
        no sweeper - expiry is evaluated per read, the same property `live_share_for` and
        `Grant.is_live` give the rest of this system - so DELETING the `is_live(at)` clause
        below does not merely relax a check, it makes every expired link work forever. Note
        that a REVOKED share is already gone from `_by_id` and so answers None regardless;
        selftest_605's dead-links test therefore pins the EXPIRED case specifically, because
        that is the only one this clause is load-bearing for.

        Compared with `hmac.compare_digest` rather than `==`. Both operands are already
        digests of a value the caller supplied, so a timing signal here is a long way from an
        attack, but this is the one comparison in the file that decides an anonymous caller's
        access and constant-time is the standing treatment for that; there is no reason to
        make an exception and then have to defend it.

        A linear scan under the lock, matching how every other lookup in this file reads
        `_by_id` - the registry is a per-process cache of one deployment's shares, not a
        table to index. If that ever stops being true it stops being true for
        `live_share_for` first, which every conversation read already calls."""
        if not token:
            return None
        digest = _token_digest(token)
        at = _now()
        with self._lock:
            for s in self._by_id.values():
                if (s.token_hash
                        and hmac.compare_digest(s.token_hash, digest)
                        and s.is_live(at)):
                    return s
        return None

    def record_open(self, share_id: str) -> None:
        """Count one open of this share, and when. NEVER RAISES - see the module docstring's
        FOURTH stance (the third is `discard_unconfirmed`, which is best-effort for an
        unrelated reason - it undoes a create from inside an already-failing request). This
        one is a counter on the read path of a page a visitor is loading right now: it makes
        no authorization decision, and nothing it touches can be left in an unsafe state, so
        the worst a total failure costs is a wrong number.

        An unknown share_id is silently ignored for the same reason it is not an error
        anywhere else here: the share may have been revoked between the read that resolved it
        and this call, and that race is the revoke working, not a fault to report.

        The store write is best-effort and goes through `_counter_save`, logged at debug rather
        than error - unlike a lost share, a lost open count is not something an operator needs
        woken up about, and this line runs on every visit, so an error-level log would turn one
        store outage into a flooded log. It is NOT written outside the lock the way `create`'s
        is, and `_counter_save` is where that difference is argued (#612 re-review, Finding A).

        THE MEMORY COUNT IS EXACT AND IMMEDIATE; THE DURABLE WRITE IS COALESCED (#612 review,
        Finding 2). Every call bumps `_by_id`, so the number the owner's management surface
        reads is right on the first request that produces it. The `save` behind it happens at
        most once per share per `OPEN_PERSIST_INTERVAL_SECONDS`, because THIS is the only
        unauthenticated write in the product that a plain page READ can drive: it runs on
        `GET /c/{token}`, on a sync route, on a single worker, reachable by anyone the link was
        forwarded to, and `PgConversationShareStore` opens a new connection and writes the full
        row per call. A Slack unfurl bot or a crawler on one forwarded link was therefore one
        TCP-plus-auth Postgres connect per request, with nothing bounding it.

        Not "the only unauthenticated write" - an earlier version of this said that and it is
        false (#612 re-review, Finding B). `POST /c/{token}/chat` writes a `conversation_turns`
        row (`query/conversation.py`) and an audit row, both anonymous. Those sit behind
        `link_access._check_link_rate` and cost an LLM call each; a page load sat behind
        nothing, which is the actual difference this constant is about.

        The read is deliberately NOT rate limited to fix this and must not be - see
        `link_access._check_link_rate`, which covers the two question routes and skips the page
        for the same reason: reading costs the owner nothing, and capping it would refuse a
        visitor re-reading a page they already paid for. The write is the only part with a cost,
        so the ceiling goes on the write.

        THE FIRST OPEN ALWAYS PERSISTS. "This link has been opened at all" is the fact an owner
        most needs and the one a coalescing window would otherwise be most likely to eat, since
        a link that is opened once and never again is exactly the case where no later call
        arrives to carry it. After that, an open inside the window marks the share dirty and the
        next open past the window writes the CURRENT state - the count is cumulative, so one
        write carries every open since the last one and nothing has to be replayed.

        Eventual persistence of the last state has two paths, deliberately, because the
        window-expiry path alone would strand the final opens of a link nobody touches again:
        the next open past the window, and `flush_opens`, which the owner's own management read
        calls. See that method."""
        try:
            to_save = None
            with self._lock:
                s = self._by_id.get(share_id)
                if s is None:
                    return
                s = replace(s, opens=s.opens + 1, last_open_at=_now())
                self._by_id[share_id] = s
                if self._store is not None:
                    last = self._open_persisted_at.get(share_id)
                    due = (last is None
                           or (s.last_open_at - last).total_seconds()
                           >= OPEN_PERSIST_INTERVAL_SECONDS)
                    if due:
                        # Recorded BEFORE the write, and left recorded even if the write below
                        # fails. A store that is down would otherwise leave every share
                        # permanently "due" and reinstate exactly the per-request connect storm
                        # this exists to bound, at the worst possible moment - when each of
                        # those connects is a doomed connect attempt with a timeout attached.
                        self._open_persisted_at[share_id] = s.last_open_at
                        self._opens_dirty.discard(share_id)
                        to_save = s
                    else:
                        self._opens_dirty.add(share_id)
            if to_save is not None:
                self._counter_save(to_save)
        except Exception:
            logging.getLogger("dbsearch").debug(
                "conversation share open not recorded for %s", share_id, exc_info=True)

    def _counter_save(self, s: ConversationShare) -> None:
        """Write a COUNTER update for a share that is still alive, or drop it.

        THIS IS THE FIX FOR A CRITICAL THE COALESCING PASS INTRODUCED (#612 re-review, Finding
        A). `record_open` and `flush_opens` both used to `save` outside the lock, on a row
        snapshotted under it. A `revoke` landing in that window re-INSERTED the row the revoke
        had just deleted: a fresh registry then hydrated the share, `is_live` was True, and
        `find_by_token` resolved it. A revoked link coming back to life is the single worst
        failure this feature can produce, and it is exactly the "looks revoked, isn't" shape
        `revoke`'s own fail-closed ordering exists to rule out.

        TWO CHECKS, because the race has two halves and neither check closes both:

          `share_id in self._revoking` catches a revoke that is IN FLIGHT. `revoke` deletes
          from the store BEFORE it clears memory - that ordering is deliberate and is not
          changing, see its docstring - so during the delete the share is still in `_by_id` and
          an `_by_id` test alone would happily write it back on top of the delete.

          `self._by_id.get(...) is None` catches a removal that has already COMPLETED, and
          covers `discard_unconfirmed`, which pops memory first and deletes after.

        THE SAVE HAPPENS UNDER THE LOCK, which is what makes the check binding rather than
        advisory: checking and releasing would leave the same window one instruction narrower.
        Holding a lock across I/O is a real cost and is taken deliberately here - coalescing
        bounds this to at most one write per share per `OPEN_PERSIST_INTERVAL_SECONDS`, so what
        is being serialized is a once-a-minute UPSERT, and the only thing that can queue behind
        it is a revoke or another counter. The alternative was leaving a credential
        resurrection live to avoid blocking a counter, which is not a trade.

        Best-effort like everything else on this path: a store failure is logged at debug and
        swallowed, because a counter must never fail the operation that triggered it."""
        with self._lock:
            if s.share_id in self._revoking or self._by_id.get(s.share_id) is None:
                # The share died between the snapshot and this write. Dropping the count is
                # the ONLY correct answer: the row it belongs to is gone on purpose.
                self._forget_open_state(s.share_id)
                return
            try:
                self._store.save(s)
            except Exception:
                logging.getLogger("dbsearch").debug(
                    "conversation share open count not written for %s", s.share_id,
                    exc_info=True)

    def _forget_open_state(self, share_id: str) -> None:
        """Drop this share's coalescing bookkeeping. CALLER MUST HOLD `self._lock`.

        Called from both removal paths so a dead share cannot leave a dirty marker behind for
        `flush_opens` to write a row the revoke just deleted - which would resurrect a REVOKED
        SHARE ROW in the store, and a resurrected link share is not a stale counter, it is a
        live credential (see `revoke`'s docstring for why that direction of failure is the one
        this design cannot tolerate).

        THIS IS HOUSEKEEPING, NOT THE GUARD, and the first version of this comment claimed
        otherwise (#612 re-review, Finding A). It only helps when the removal happens before
        the snapshot; a removal that lands after it was still written back. What actually holds
        the invariant is `_counter_save`, which re-checks under the lock it writes under."""
        self._open_persisted_at.pop(share_id, None)
        self._opens_dirty.discard(share_id)

    def flush_opens(self) -> None:
        """Persist any open counts that `record_open` coalesced and has not written yet.

        NEVER RAISES, for `record_open`'s reason and one more: every caller is some OTHER
        operation that has its own job to do, and a counter must not be able to fail it.

        Called from `list_granted_by` - the owner's management read - which is the natural
        place and not an arbitrary one: it is the moment somebody actually looks at these
        numbers, it is authenticated and owner-scoped so it cannot be driven by a stranger, and
        it is exactly as rare as an owner opening a page. Without a call site like this the
        window-expiry path in `record_open` would strand the last opens of a link that is
        opened a few times and then never again, which is the common shape of a forwarded link
        rather than an edge case.

        Deliberately NOT called from `find_by_token` or anything else on the visitor path: that
        would hand the flush back to the anonymous caller and undo the whole coalescing.

        Snapshots under the lock, then writes one share at a time through `_counter_save`,
        which RE-CHECKS each row under the lock it writes under. The re-check is the whole
        point: a revoke landing between the snapshot and the write used to re-insert the row
        the revoke had just deleted (#612 re-review, Finding A)."""
        if self._store is None:
            return
        with self._lock:
            pending = [self._by_id[sid] for sid in self._opens_dirty if sid in self._by_id]
            self._opens_dirty.clear()
            for s in pending:
                self._open_persisted_at[s.share_id] = s.last_open_at or _now()
        for s in pending:
            self._counter_save(s)

    def revoke(self, share_id: str, requester_oid: str) -> ConversationShare:
        """Only the person who made the share may take it back - `find`'s ownership check,
        reused.

        FAIL-CLOSED on the store, deliberately, the same ordering and the same reason
        `GrantRegistry.revoke` uses (grants.py, #575 review Finding 1): the store delete runs
        FIRST, unguarded, and memory is only cleared once it succeeds. A conversation share
        that LOOKS revoked (API returns success, memory clear) while its row still sits in
        Postgres would be resurrected by the next restart's hydration - not for one window,
        but indefinitely - and a resurrected conversation share re-opens the transcript itself
        AND, transitively, whatever conv-scoped document grants still name that `conv_id`
        (Task 3's `Grant.conv_id`), handing back an entire deliberately-cut-off reading
        session rather than one document. On a store failure this raises, and the share stays
        live in THIS process too, so behaviour is honest and consistent rather than silently
        wrong - the cost is that revoke becomes unavailable during a store outage instead of
        silently unsafe, which is the correct trade for an authorization operation."""
        s = self.find(share_id, requester_oid)
        # ANNOUNCE THE REVOKE BEFORE TOUCHING THE STORE (#612 re-review, Finding A). The delete
        # below runs before any memory mutation - that ordering is what makes revoke
        # fail-closed and it is not changing - which means the share is still in `_by_id` for
        # the whole duration of the delete, and a concurrent counter write consulting only
        # `_by_id` would happily UPSERT the row back on top of it. `_counter_save` reads this
        # set under the same lock it writes under, so from here on no open count can be written
        # for this share, whatever the delete does. Cleared again if the delete raises, since
        # the share is then still live and its counter must keep working.
        with self._lock:
            self._revoking.add(share_id)
            self._forget_open_state(share_id)
        try:
            # Ownership already verified above. The store call runs outside the lock (I/O
            # should not block another reader) but BEFORE any memory mutation, and is left to
            # raise on failure - see the docstring above for why that is the point, not a bug.
            if self._store is not None:
                self._store.delete(share_id)
        except BaseException:
            with self._lock:
                self._revoking.discard(share_id)
            raise
        with self._lock:
            self._by_id.pop(share_id, None)
            self._forget_open_state(share_id)
            # Safe to drop the marker now: `_by_id` no longer holds the share, so
            # `_counter_save`'s second check covers every write from here on.
            self._revoking.discard(share_id)
        return s

    def discard_unconfirmed(self, share_id: str) -> None:
        """Compensates for a `create` whose share never actually took effect. Task 5's share
        operation creates this row and then still has to confirm every document the
        conversation cited is still readable; if that confirmation fails partway (a cited
        document was deleted between the citation and the share), the share never really
        happened and this record must not outlive that request.

        Deliberately NOT `revoke` - see the module docstring's account of the two camps and
        why this is `create`'s (best-effort, never raises), not `revoke`'s (fail-closed): a
        share that never took effect is not a live authorization decision, so there is nothing
        here for a store outage to put at risk, and raising from a cleanup that runs inside an
        already-failing request would mask the caller's honest error behind a store 500."""
        with self._lock:
            self._by_id.pop(share_id, None)
            self._forget_open_state(share_id)
        if self._store is not None:
            try:
                self._store.delete(share_id)
            except Exception:
                logging.getLogger("dbsearch").error(
                    "conversation share store unreachable - unconfirmed share %s discarded "
                    "in-process only", share_id)

    def live_share_for(self, conv_id: str, grantee_oid: str) -> "ConversationShare | None":
        """What Task 5's open path resolves against: does THIS person currently hold a live
        share of THIS conversation. Expiry is applied HERE, per read, which is why no sweeper
        exists - the same property `GrantRegistry.live_principals_for` gives document
        sharing.

        Fix round 2, NEW-2: the NEWEST live match, not the first one iteration happens to
        reach. `create` now keeps at most one live row per (conv_id, grantee, grantor), so
        the only way to see two here is two DIFFERENT people sharing threads that carry the
        same client-chosen conv_id - a client id collision, not an authorization question,
        since each of those shares is independently legitimate and names its own grantor
        (whose history is what actually gets read). Answering with the newest makes that
        deterministic instead of dict-order dependent, and it is the share the recipient most
        recently received."""
        at = _now()
        with self._lock:
            live = [s for s in self._by_id.values()
                    if s.conv_id == conv_id and s.grantee_oid == grantee_oid and s.is_live(at)]
        return max(live, key=lambda s: s.created_at) if live else None

    def list_for_conversation(self, conv_id: str, grantor_oid: str) -> list[ConversationShare]:
        """This conversation's shares, but only the ones THIS grantor made - the management
        view a "who have I shared this thread with" surface would read, scoped to the caller
        the same way `find`/`revoke` are, so it can never list a share somebody else made."""
        with self._lock:
            return sorted((s for s in self._by_id.values()
                           if s.conv_id == conv_id and s.grantor_oid == grantor_oid),
                          key=lambda s: s.created_at)

    def list_granted_by(self, grantor_oid: str) -> list[ConversationShare]:
        """Every share THIS person has granted, across every conversation - LIVE only. #607.

        `list_for_conversation` answers "who did I give this thread to" and is what the share
        modal reads; this answers "what have I given away, anywhere", which is the question the
        management surface exists for and which no per-conversation call can reach, because the
        owner does not remember her conv_ids and has no list of them to iterate.

        LIVE ONLY, matching `list_shared_with` rather than `list_for_conversation`. An expired
        share opens nothing, and the row this feeds carries a Revoke button and an Edit button
        beside it: listing a dead share would offer the owner two operations that change
        nothing on a thing she would reasonably read as still exposing her documents. The
        cost - she cannot see that a link she sent has lapsed - is the right way round, because
        "no longer listed" and "no longer open" then say the same thing.

        Scoped to `grantor_oid` inside the registry, the same way `find`, `revoke` and
        `list_for_conversation` are, so no route above it can widen this into somebody else's
        shares by passing a different id.

        Flushes any coalesced open counts first (#612 review, Finding 2). This is the read that
        DISPLAYS those numbers, so it is the one place where a durable write is worth doing on
        a read path - and it is the eventual-persistence path for a link that was opened a few
        times and then left alone, which `record_open`'s window expiry cannot reach on its own.
        `flush_opens` never raises, so a store outage costs a stale count here, never the
        listing."""
        self.flush_opens()
        at = _now()
        with self._lock:
            return sorted((s for s in self._by_id.values()
                           if s.grantor_oid == grantor_oid and s.is_live(at)),
                          key=lambda s: s.created_at)

    def list_shared_with(self, grantee_oid: str) -> list[ConversationShare]:
        """Every conversation currently shared with this person - LIVE only. An expired share
        is not a share this person currently holds, and listing it would show a thread she can
        no longer open, the exact "looks live, isn't" confusion `is_live`'s per-read
        evaluation exists to prevent everywhere else in this file."""
        at = _now()
        with self._lock:
            return sorted((s for s in self._by_id.values()
                           if s.grantee_oid == grantee_oid and s.is_live(at)),
                          key=lambda s: s.created_at)


class InMemoryConversationShareStore:
    """Hermetic adapter for tests and rigs without Postgres - also the shape
    `ConversationShareRegistry` defaults to needing none of, since `store=None` is its own
    no-store path. A dict keyed by share_id, holding the `ConversationShare` dataclasses
    themselves (frozen, so no copy-on-write is needed)."""

    def __init__(self) -> None:
        self._rows: dict[str, ConversationShare] = {}
        self._lock = threading.Lock()

    def load_all(self) -> list[ConversationShare]:
        with self._lock:
            return list(self._rows.values())

    def save(self, s: ConversationShare) -> None:
        with self._lock:
            self._rows[s.share_id] = s

    def delete(self, share_id: str) -> None:
        with self._lock:
            self._rows.pop(share_id, None)


def _row_to_share(row) -> ConversationShare:
    (share_id, conv_id, grantor_oid, grantee_oid, expires_at, created_at,
     turn_cutoff) = row[:7]
    # #605's four columns are read POSITIONALLY-BUT-OPTIONALLY, and a SHORT row is not an
    # error. `load_all` below always selects all eleven, so a short row here means a caller
    # that still selects the pre-#605 seven - which the golden-suite tests do, and which any
    # older SELECT still in flight during a deploy does. Unpacking `row` in one statement (as
    # this did before #605) would turn every such caller into a ValueError, and
    # `ConversationShareRegistry.__init__` swallows hydration errors, so the visible symptom
    # would be a box booting with ZERO shares - every share in the deployment silently dead.
    # #851 extends the same rule to a FIFTH optional column rather than replacing it: the pad
    # simply grew. A short row is still not an error, and a caller selecting the pre-#605
    # seven or the pre-#851 eleven still hydrates instead of booting the box with zero shares.
    audience, token_hash, opens, last_open_at, shared_stores = (
        (tuple(row[7:]) + (None,) * 5)[:5])
    return ConversationShare(share_id=share_id, conv_id=conv_id, grantor_oid=grantor_oid,
                             grantee_oid=grantee_oid, expires_at=_aware(expires_at),
                             created_at=_aware(created_at),
                             # NULL means a row written before `turn_cutoff` existed, so the
                             # boundary is UNKNOWN - and an unknown boundary reads as ZERO
                             # grantor turns, showing nothing, rather than as "no limit",
                             # showing the whole live thread. Fail-closed is the only safe
                             # reading of a missing authorization boundary.
                             turn_cutoff=int(turn_cutoff or 0),
                             # Same fail-closed reading of a NULL from an ALTER TABLE-era row:
                             # a missing audience is "people", the audience that hands nothing
                             # to a caller who cannot sign in. Reading it as "link" would let a
                             # migration artefact promote an ordinary named share into an
                             # anonymously-openable one - and the token_hash of such a row is
                             # NULL anyway, so `find_by_token` could never resolve it.
                             audience=audience or AUDIENCE_PEOPLE,
                             token_hash=token_hash,
                             opens=int(opens or 0),
                             last_open_at=_aware(last_open_at),
                             # #851, fail-closed for the third time and for the third distinct
                             # reason: NULL (or a row from before the column) means this
                             # grantor was never ASKED which sources to pass on. Reading that
                             # as "all of them" would hand a recipient evidence she never
                             # agreed to, and the question cannot be answered on her behalf.
                             shared_stores=list(shared_stores or []))


class PgConversationShareStore:
    """conversation_shares in the deployment's own compose-managed Postgres (PGVECTOR_DSN) -
    no new infrastructure, the same DSN doc_grants (grant_store.py) and conversations
    (conversation_store.py) already use.

    Connects per operation - manifest_store.py's `PgManifestStore` docstring explains why (a
    held connection is one more thing that dies with the DB and has to be liveness checked).
    DDL is CREATE TABLE IF NOT EXISTS on first touch, `_schema_done` set only after that
    transaction commits - manifest_store.py's `_ensure_schema` docstring is the full account
    of why each half of that matters; this is the same pattern verbatim.
    """

    def __init__(self, dsn: str, table: str = "conversation_shares",
                connect_timeout: int = 5) -> None:
        self._dsn = dsn
        self._table = table
        self._timeout = connect_timeout
        self._schema_done = False
        self._schema_lock = threading.Lock()

    def _conn(self):
        import psycopg
        return psycopg.connect(self._dsn, connect_timeout=self._timeout)

    def _ensure_schema(self) -> None:
        with self._schema_lock:
            if self._schema_done:
                return
            with self._conn() as conn:
                conn.execute(
                    f"""CREATE TABLE IF NOT EXISTS {self._table} (
                        share_id    text PRIMARY KEY,
                        conv_id     text NOT NULL,
                        grantor_oid text NOT NULL,
                        grantee_oid text NOT NULL,
                        expires_at  timestamptz,
                        created_at  timestamptz NOT NULL,
                        turn_cutoff integer
                    )""")
                # Fix round 1, CRITICAL-2. Idempotent ADD COLUMN for a table created before
                # `turn_cutoff` existed - the same shape grant_store.py uses for `conv_id`,
                # and the reason CREATE TABLE IF NOT EXISTS alone is not enough (it is a
                # no-op on an existing table, so the new column would never appear and every
                # SELECT below would fail).
                conn.execute(
                    f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS turn_cutoff integer")
                # #605 / ADR 0021, the same idiom for the same reason - this table already
                # exists on every deployed box, so CREATE TABLE IF NOT EXISTS above is a
                # no-op there and these four columns can only arrive by ALTER. Idempotent, in
                # the SAME committed transaction as the create, and `_schema_done` is still
                # set only after that transaction commits: a half-applied schema that was
                # remembered as done would make every SELECT below fail for the life of the
                # process.
                #
                # The DDL lives HERE, in the store that owns the table, on first touch - never
                # in init_db. That is a standing rule in this repo and not a stylistic one:
                # schema that lands somewhere other than its owner is schema that a deployment
                # can boot without.
                #
                # `audience` gets NOT NULL DEFAULT 'people' so every existing row is
                # backfilled to the behaviour it already had, in one statement, rather than
                # left NULL for `_row_to_share` to interpret. `opens` likewise defaults to 0.
                # `token_hash` and `last_open_at` are nullable because a people share
                # genuinely has neither.
                conn.execute(
                    f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS "
                    f"audience text NOT NULL DEFAULT 'people'")
                conn.execute(
                    f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS token_hash text")
                conn.execute(
                    f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS "
                    f"opens integer NOT NULL DEFAULT 0")
                conn.execute(
                    f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS "
                    f"last_open_at timestamptz")
                # #851, same idiom for the same reason. `'[]'` is the fail-closed backfill and
                # is the whole point: a row written before this column existed belongs to a
                # grantor who was never ASKED which sources to pass on, and defaulting her to
                # "all of them" would hand over evidence she never agreed to. Empty means
                # consent to nothing, which is exactly the pre-#851 behaviour those rows were
                # created under.
                conn.execute(
                    f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS "
                    f"shared_stores jsonb NOT NULL DEFAULT '[]'::jsonb")
            self._schema_done = True

    def _run(self, fn):
        try:
            self._ensure_schema()
            with self._conn() as conn:
                return fn(conn)
        except Exception as exc:
            reason = _safe_reason(exc)
            logging.getLogger("dbsearch").error(
                "conversation share store %s unavailable: %s", self._table, reason)
        # Raised outside the except block - manifest_store.py's `_run` docstring explains why:
        # it keeps this exception's __cause__/__context__ clear of the driver error, so a
        # future traceback render of THIS exception cannot echo a grantor/grantee oid back out.
        raise ConversationShareStoreUnavailable(reason)

    def load_all(self) -> list[ConversationShare]:
        def _load(conn):
            rows = conn.execute(
                f"""SELECT share_id, conv_id, grantor_oid, grantee_oid, expires_at,
                           created_at, turn_cutoff, audience, token_hash, opens,
                           last_open_at, shared_stores
                    FROM {self._table}""").fetchall()
            return [_row_to_share(r) for r in rows]
        return self._run(_load)

    def save(self, s: ConversationShare) -> None:
        # Lazily, like `_conn`'s psycopg import: an unconfigured deployment runs memory-only
        # and must not need the driver installed to import this module.
        from psycopg.types.json import Jsonb

        def _save(conn):
            conn.execute(
                f"""INSERT INTO {self._table}
                    (share_id, conv_id, grantor_oid, grantee_oid, expires_at, created_at,
                     turn_cutoff, audience, token_hash, opens, last_open_at,
                     shared_stores)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (share_id) DO UPDATE SET
                      conv_id = EXCLUDED.conv_id,
                      grantor_oid = EXCLUDED.grantor_oid,
                      grantee_oid = EXCLUDED.grantee_oid,
                      expires_at = EXCLUDED.expires_at,
                      created_at = EXCLUDED.created_at,
                      turn_cutoff = EXCLUDED.turn_cutoff,
                      audience = EXCLUDED.audience,
                      token_hash = EXCLUDED.token_hash,
                      opens = EXCLUDED.opens,
                      last_open_at = EXCLUDED.last_open_at,
                      shared_stores = EXCLUDED.shared_stores""",
                (s.share_id, s.conv_id, s.grantor_oid, s.grantee_oid, s.expires_at,
                 s.created_at, s.turn_cutoff, s.audience, s.token_hash, s.opens,
                 s.last_open_at, Jsonb(list(s.shared_stores or []))))
        self._run(_save)

    def delete(self, share_id: str) -> None:
        self._run(lambda conn: conn.execute(
            f"DELETE FROM {self._table} WHERE share_id = %s", (share_id,)))
