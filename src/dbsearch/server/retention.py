"""#576 - the retention sweep: any access counts, 3 silent days deletes the workspace data.

The decisive design choice (owner, explicit): activity means ANY access, not just the
owner logging in - a colleague querying a document that was shared with them keeps the
SHARER's workspace alive too. The motivating case is an HR policy uploaded once and then
used by colleagues for weeks; deleting it on day 3 because the uploader never came back
would be wrong. That is why the clock lives on `workspace_activity` (accounts.py) keyed
by ACCOUNT, and why every request touches it: `touch()` below fires once for the caller's
own account (app.py's `current_user`) and once more for the OWNER of every document a
query actually returns (app.py's `_touch_retrieved_owners`, wired into /search, /chat and
/chat/stream) - "any access" means any RETRIEVAL, not merely holding an unused grant.

#576 code review (260807), Finding 2 - and the owner's ruling on it - reshaped this module
after the first version shipped: `owner_oid` is ADR 0012 ATTRIBUTION metadata, never an
ACL, and the first sweep design used it as deletion authority over the wrong partition. A
#575 "My organization" upload is ACL'd `tenant:<tid>` with no Grant at all, and a
SharePoint library ingested via `/connect/sharepoint` stamps `owner_oid=<the connecting
user>` over the WHOLE shared home-tenant corpus - so touching only explicit grant holders
left the home tenant's documents one silent employee away from deletion. The owner's fix,
implemented here exactly:

    THE SWEEP TOUCHES ONLY `ACCT_TENANT_PREFIX + account_id` - AN ACCOUNT'S OWN PRIVATE
    PARTITION (ADR 0018). It never reads or writes the home tenant or any foreign tenant
    partition, full stop. No org-audience document and no connector-ingested corpus can
    ever be swept, because they never live in an `acct:` partition to begin with.
    `test_the_home_tenant_partition_is_never_touched_by_the_sweep`
    (tests/selftest_576_retention_sweep.py) is the test that pins this down - it fails if
    the partition set is ever widened back out.

FINAL WHOLE-BRANCH REVIEW (260807), Fix 1 - the narrowing above was applied to DOCUMENT
deletion only, and that half-fix was worse than either whole. `conversation_service.drop_user`
and `manifest_store.delete` are keyed by ACCOUNT, not by partition, so they still ran for
every silent account - including every Microsoft/home-tenant user, whose documents live in the
home partition and whose `acct:` partition is therefore permanently empty. An enterprise user
signing in on Friday and away until Wednesday came back to an empty workspace: composed stores,
connections and chat history irreversibly deleted out of Postgres, while `docs_owned_by`
returned `[]` and the report cheerfully listed her as "swept". That is exactly the #200 lesson
the manifest store's own docstring warns about. The rule now, with no exceptions:

    AN ACCOUNT WHOSE `acct:` PARTITION HOLDS NOTHING COMES OUT OF THE SWEEP COMPLETELY
    UNTOUCHED - no manifest delete, no conversation drop, and no activity-row delete either
    (dropping the row would make the account invisible to `silent_accounts` forever, so a
    later real workspace of theirs could never be swept at all). It is counted in
    `untouched_empty` and does NOT appear in `report["swept"]`.

`test_the_home_tenant_partition_is_never_touched_by_the_sweep` now asserts on the DELETIONS -
manifest, conversation and activity row all still present - rather than on the report's
account list, which is what let this through: the old assertion was `alice in report["swept"]`,
which the bug satisfied.

The one property that matters most beyond that: an account with NO activity row must
NEVER be swept. `AccountStore.silent_accounts()` only ever returns accounts that HAVE a
row older than the cutoff - a pre-existing account with no row (the entire installed base,
on the first run after this feature ships) is invisible to it, not "ancient". Deleting it
would be catastrophic and irreversible, so "unknown" and "silent" must never be conflated,
here or anywhere this module reads that list.

LAW 4: the sweep never runs inside a user request. `/admin/retention/sweep`
(server/app.py) starts `sweep_and_log` on a detached daemon thread and returns 202
immediately (unless `dry_run=True`, a read-only preview cheap enough to answer inline -
see `sweep`'s docstring). The cron path (`main` below) DRIVES that same endpoint rather
than sweeping in its own process - final review Fix 4, see the comment above `run_cron_sweep`
for why a fresh process could not delete conversation history.

LAW 8: `sweep_and_log` emits COUNTS ONLY - never a document id, filename, or account oid.
"""
from __future__ import annotations

import json
import logging
import time

from dbsearch.api.auth import ACCT_TENANT_PREFIX, real_login_enabled
from dbsearch.server.operators import is_operator

DAY_SECONDS = 86400.0

_RETENTION_DAYS_ENV = "DBSEARCH_RETENTION_DAYS"
_RETENTION_DAYS_DEFAULT = "3"

# The four blob-key families the pipeline writes per document (pipeline/runner.py:157,236,
# 261,262) - #576 review Finding 1: the first version of this sweep deleted only two of
# these (`raw/`, `chunk/`) and reported the workspace "swept" while `segments/` (the
# extracted document TEXT, verbatim) and `emb/` (embeddings - LAW 1 names both as customer
# content) sat on disk, permanently, because `drop_activity` means the account is never
# reconsidered. All four must go, every time.
_BLOB_KIND_PREFIXES = ("raw", "segments", "chunk", "emb")

# Per-process throttle state for touch() - a fresh dict per process is correct: the whole
# point is to bound how often ONE process writes to the account store, not to coordinate
# across processes (a second process touching the same account within the window just
# means two upserts instead of one, which is harmless).
_LAST_TOUCH: dict[str, float] = {}
_TOUCH_THROTTLE_SECONDS = 300.0

# #576 review Finding 9: _LAST_TOUCH is one entry per DISTINCT account ever touched by this
# process, unbounded for the life of a long-running self-host box. Opportunistically pruned
# every _PRUNE_EVERY calls rather than on every call (an O(n) scan every single request
# would be its own cost) - an entry more than one throttle window old serves no purpose
# (its ONLY job is answering "was this written to less than 300s ago"), so pruning it costs
# nothing but memory.
_PRUNE_EVERY = 500
_touch_call_count = 0

# #576 review Finding 4: a down account store must not turn into a per-request Postgres
# connect-timeout on the auth chokepoint. After a failed write, further attempts are
# skipped for this cooldown - deliberately GLOBAL (not per-account): the failure mode this
# guards is "the store is down", which is a property of the store, not of any one account,
# and a caller holding several live grants must not pay several timeouts in one request.
_BREAKER_COOLDOWN_SECONDS = 30.0
_breaker_open_until = 0.0


def touch(account_id: str, *, now: "float | None" = None) -> None:
    """The "any access counts" clock. Called from the request path (app.py) on every
    authenticated request for the caller's own account, and again for the owner of every
    document a query actually retrieves - so it must be cheap and must never break a
    request.

    Throttled per process (min `_TOUCH_THROTTLE_SECONDS` between writes for the same
    account), circuit-broken on repeated store failures (Finding 4), and fire-and-forget:
    any failure is logged and swallowed, never raised. `now` is a clock-injection seam for
    tests; real callers never pass it.
    """
    global _touch_call_count, _breaker_open_until

    if not account_id:
        return
    ts = now if now is not None else time.time()

    last = _LAST_TOUCH.get(account_id, 0.0)
    if ts - last < _TOUCH_THROTTLE_SECONDS:
        return
    # Recorded BEFORE the store call and regardless of its outcome (Finding 4): a failing
    # store must not be retried on every single request from the same account - that is
    # what turned "PG down" into an effective outage (roughly one connect-timeout per live
    # grant, per request) even though nothing here ever raised into the caller.
    _LAST_TOUCH[account_id] = ts

    _touch_call_count += 1
    if _touch_call_count % _PRUNE_EVERY == 0:
        stale = [a for a, v in _LAST_TOUCH.items() if ts - v >= _TOUCH_THROTTLE_SECONDS]
        for a in stale:
            _LAST_TOUCH.pop(a, None)

    if ts < _breaker_open_until:
        return   # the store failed recently; give it the rest of its cooldown, don't retry

    try:
        # Lazy import: server.app imports this module at request-handling time, and
        # importing it back at module load would be circular. ACCOUNTS is read as a
        # module ATTRIBUTE (not bound at import time) so a test that swaps
        # `dbsearch.server.app.ACCOUNTS` for a fresh store is honored here too.
        from dbsearch.server.app import ACCOUNTS
        ACCOUNTS.touch_activity(account_id, now=ts)
        _breaker_open_until = 0.0   # a success closes the breaker immediately
    except Exception:
        logging.getLogger("dbsearch").exception(
            "retention.touch failed - activity clock not updated for this request")
        _breaker_open_until = ts + _BREAKER_COOLDOWN_SECONDS


def _retention_days(days: "float | None") -> float:
    if days is not None:
        return days
    import os

    raw = os.environ.get(_RETENTION_DAYS_ENV, _RETENTION_DAYS_DEFAULT)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(_RETENTION_DAYS_DEFAULT)


def blob_prefixes(partition: str, doc_id: str) -> tuple:
    """The four key families for one document, WITHOUT a manual trailing separator - the
    ObjectStorePort.delete_prefix contract (#576 review Finding 5) matches a key that
    EQUALS the prefix or starts with `prefix + "/"`, so a caller never has to remember
    which of these four are single leaf blobs (`raw`, `segments`) and which are numbered
    families (`chunk`, `emb`); the boundary is enforced once, in the port, not re-derived
    at every call site.

    Public since #594: a user deleting ONE of their own documents has to erase exactly the
    same four families the sweep does. Two hand-maintained lists of blob prefixes is how the
    sweep came to be missing three of them in the first place."""
    return tuple(f"{kind}/{partition}/{doc_id}" for kind in _BLOB_KIND_PREFIXES)


def sweep(edition, accounts, manifest_store, *,
          days: "float | None" = None, now: "float | None" = None,
          dry_run: bool = False) -> dict:
    """Delete the data of every ABANDONED workspace - an account whose activity row is
    older than `days` (default `DBSEARCH_RETENTION_DAYS`, "3") - and nothing else.

    Two independent ways this is a no-op, both reported as `{"disabled": True, "reason": ...}`:
      - `days <= 0`: retention is explicitly turned off. No reads, no deletes.
      - `real_login_enabled()` is False (#576 review Finding 6): a deployment with no real
        login IS the operator's own machine (`is_operator`'s own stance, server/operators.py) -
        there is no "other user" whose account can go silent independent of the operator.
        Relying on `is_operator` returning True for everyone to make the sweep INERT there
        was an accident, not a decision; this makes the decision explicit and stops the
        sweep before it does any enumeration at all, rather than silently reporting
        `skipped_operators` for an entire deployment's worth of accounts.

    An account is a candidate only if `accounts.silent_accounts(cutoff)` names it, which by
    construction excludes every account with no activity row at all (see module
    docstring). Operators (`is_operator`) are excluded too and counted separately - an
    operator's own workspace, if they have one, must survive regardless of silence.

    #576 review Finding 3 (TOCTOU): `silent_accounts` is a snapshot taken once, but a user
    can sign in WHILE the sweep is running. Immediately before doing any deletion work for
    an account, `last_activity` is re-read; if it has moved (or the row is simply gone),
    that account is skipped for this pass entirely - counted in `raced`, not `swept`. The
    final activity-row delete (`accounts.drop_activity(..., cutoff_epoch=cutoff)`) carries
    the SAME cutoff as a second, DB-level guard.

    RESIDUAL WINDOW, recorded rather than fixed (review round 2): that second guard
    protects only the ACTIVITY ROW itself. The index/blob/grant/conversation/manifest
    deletes for this account already ran, earlier in this same loop body, BEFORE that
    guarded delete is reached - so a sign-in landing specifically in the (small) gap
    between the re-check above and the `drop_activity` call below still loses its
    DOCUMENTS: the row survives (the DB guard does its job), but the account's data does
    not. Closing that fully would need the whole per-account sequence - enumerate, delete
    docs, delete blobs, drop grants, drop conversation, drop manifest, drop the row - to be
    one atomic unit across THREE different backends (index, blob store, account store),
    which none of the ports here support. The trade accepted: a real, if narrow, sign-in
    mid-delete can still lose documents; an account with NO activity row, or one that
    signed in before the re-check, is fully protected either way.

    For every remaining silent account A, ONLY its own `ACCT_TENANT_PREFIX + A` partition
    is read - see the module docstring's INVARIANT. `index.docs_owned_by` names A's
    documents there (a backend that cannot answer that raises NotImplementedError - counted
    as `partitions_unsupported`, never guessed at); each of those documents is removed from
    the index AND from all four blob-key families (`_BLOB_KIND_PREFIXES` - #576 review
    Finding 1) - a backend with no `delete_prefix` is counted as `blobs_unsupported` rather
    than silently leaving bytes on disk while the report claims the workspace is gone.

    Grants on the deleted documents, the account's in-memory conversation history, and its
    workspace manifest (skipped when `manifest_store` is None - an unconfigured/memory
    rig) are then dropped, and finally the activity row itself. The ACCOUNT ROW SURVIVES -
    a returning user keeps their login; only the workspace data they abandoned is gone.

    ALL of those deletes are gated on that partition holding at least one document (final
    review Fix 1 - see the module docstring). An empty `acct:` partition means this account
    has no workspace HERE to abandon: it is either a Microsoft/home-tenant user whose
    documents live in the home partition the sweep is forbidden to read, or an account that
    never uploaded anything. Either way the sweep has nothing to delete and must not delete
    the account-keyed things it CAN reach just because it can reach them. Counted in
    `untouched_empty`, never in `swept`.

    `dry_run=True` (#576 review Finding 10) runs every READ above - candidate selection,
    the TOCTOU re-check, `docs_owned_by` enumeration - and skips every WRITE, so the report
    shows exactly what a real sweep would do without deleting anything. Cheap enough (no
    blob/index/grant/manifest mutation, ever) to answer synchronously inside a request; see
    the operator endpoint in server/app.py.
    """
    if not real_login_enabled():
        return {"disabled": True, "reason": "no_real_login"}

    d = _retention_days(days)
    if d <= 0:
        return {"disabled": True, "reason": "retention_days_zero"}

    clock = now if now is not None else time.time()
    cutoff = clock - d * DAY_SECONDS

    report = {
        "checked": 0,
        "swept": [],
        "docs_deleted": 0,
        "grants_dropped": 0,
        "skipped_operators": 0,
        "partitions_unsupported": 0,
        "blobs_unsupported": 0,
        "raced": 0,
        "untouched_empty": 0,
        "dry_run": bool(dry_run),
    }

    silent = accounts.silent_accounts(cutoff)
    report["checked"] = len(silent)

    for account_id in silent:
        if is_operator(account_id):
            report["skipped_operators"] += 1
            continue

        # Finding 3: close the snapshot-to-delete window. A sign-in that lands here keeps
        # its own brand-new row (this account is simply skipped this pass, not touched at
        # all) rather than being wiped out from under it. Deliberately NOT gated on
        # `dry_run` (review round 2, Finding C): this is a plain READ, so it costs nothing
        # to also run during a preview, and a preview that skipped it could over-report an
        # account as "would be swept" when a real run at that same instant would have
        # caught the very race this exists to catch. Running it unconditionally makes
        # `report["raced"]` and `report["swept"]` mean the same thing in both modes.
        fresh = accounts.last_activity(account_id)
        if fresh is None or fresh >= cutoff:
            report["raced"] += 1
            continue

        # INVARIANT (Finding 2, owner's ruling): the account's OWN partition only. Never
        # home_tenant, never any partition this account did not privately own. See the
        # module docstring - this line is what `test_the_home_tenant_partition_is_never_
        # touched_by_the_sweep` pins down.
        partition = ACCT_TENANT_PREFIX + account_id

        try:
            doc_ids = list(edition.index.docs_owned_by(partition, account_id))
        except NotImplementedError:
            report["partitions_unsupported"] += 1
            doc_ids = []

        # FINAL REVIEW Fix 1 - the seam this whole module got wrong. Every remaining delete
        # below is ACCOUNT-keyed (conversation history, workspace manifest, the activity
        # row); only the document/blob/grant deletes are partition-keyed. Running the
        # account-keyed ones for an account whose OWN partition is empty deletes a
        # home-tenant user's entire workspace - stores, connections, chat history - on the
        # most ordinary silence there is, irreversibly, while deleting not one document.
        # Nothing here is scoped to anything, so the correct scope is: do nothing at all.
        # This `continue` sits ABOVE the `dry_run` branch on purpose, so a preview reports
        # exactly what a real run would do (the same reason the TOCTOU re-check does).
        if not doc_ids:
            report["untouched_empty"] += 1
            continue

        deleted_ids: set[str] = set()
        if not dry_run:
            for doc_id in doc_ids:
                try:
                    edition.index.delete(partition, doc_id)
                except Exception:
                    logging.getLogger("dbsearch").error(
                        "retention sweep: index delete failed for one document")
                    continue
                deleted_ids.add(doc_id)
                for prefix in blob_prefixes(partition, doc_id):
                    try:
                        edition.store.delete_prefix(prefix)
                    except NotImplementedError:
                        report["blobs_unsupported"] += 1
                    except Exception:
                        logging.getLogger("dbsearch").error(
                            "retention sweep: blob delete failed for one prefix")

            if deleted_ids:
                report["grants_dropped"] += edition.grant_registry.drop_for_documents(deleted_ids)

            try:
                edition.conversation_service.drop_user(account_id)
            except Exception:
                logging.getLogger("dbsearch").error(
                    "retention sweep: conversation drop failed for one account")

            # #623: the audit trail became durable, so the sweep became responsible for it.
            # While it died with the process this line would have been pointless - a swept
            # account's questions were gone by the next deploy either way - and its absence
            # was therefore not a bug. Persisting the rows is what turns that absence into
            # one: a swept account whose question TEXT outlives every deploy, in a table
            # nothing else reaps. Fixing durability without this would have traded a
            # durability bug for a retention bug.
            try:
                edition.audit_log.drop_user(account_id)
            except Exception:
                logging.getLogger("dbsearch").error(
                    "retention sweep: audit drop failed for one account")

            if manifest_store is not None:
                try:
                    manifest_store.delete(account_id)
                except Exception:
                    logging.getLogger("dbsearch").error(
                        "retention sweep: manifest delete failed for one account")

            # Finding 3's second guard: the DB-level cutoff predicate on the delete itself.
            accounts.drop_activity(account_id, cutoff_epoch=cutoff)

        report["docs_deleted"] += len(doc_ids)
        report["swept"].append(account_id)

    return report


def sweep_and_log(edition, accounts, manifest_store, *,
                  days: "float | None" = None, now: "float | None" = None,
                  dry_run: bool = False) -> dict:
    """Wraps `sweep` with a structured log line and a counts-only control-plane event
    (LAW 8). This is what runs off the request thread: the `/admin/retention/sweep`
    endpoint (server/app.py) starts it on a detached daemon thread (LAW 4), which is now
    the ONLY way a real sweep runs - the cron entrypoint below drives that endpoint
    (final review Fix 4) rather than calling `sweep` in a process that does not hold the
    serving process's conversation history."""
    report = sweep(edition, accounts, manifest_store, days=days, now=now, dry_run=dry_run)

    if report.get("disabled"):
        logging.getLogger("dbsearch").info("retention sweep disabled: %s", report.get("reason"))
        return report

    # #576 review Finding 8: `report["swept"]` is a list of ACCOUNT OIDS. The control-plane
    # emit below was always counts-only, but this local log line used to `json.dumps` the
    # WHOLE report - identifiers, not content, but still more than "counts only" ought to
    # write to a log file. Build the counts once and log exactly that, nothing wider.
    counts = {
        "checked": report["checked"],
        "swept": len(report["swept"]),
        "docs_deleted": report["docs_deleted"],
        "grants_dropped": report["grants_dropped"],
        "skipped_operators": report["skipped_operators"],
        "partitions_unsupported": report["partitions_unsupported"],
        "blobs_unsupported": report["blobs_unsupported"],
        "raced": report["raced"],
        "untouched_empty": report["untouched_empty"],
        "dry_run": report["dry_run"],
    }
    logging.getLogger("dbsearch").info("retention sweep: %s", json.dumps(counts))

    if dry_run:
        return report   # a preview changed nothing; nothing to tell the control plane

    try:
        edition.agent.emit("retention.swept", counts=counts, ts=edition._now())
    except Exception:
        # Expected today: "retention.swept" is not yet in boundary.schema.json's event
        # enum, so BoundaryValidator refuses it (LAW 1 defense-in-depth, working as
        # designed). Widening that contract is a follow-up card, not something this task
        # should do just to make its own event pass - the counts are already logged
        # locally above, so nothing is lost, and this catches broadly (not only
        # BoundaryViolation) because a control-plane emit is best-effort telemetry, never
        # something that may take the sweep down with it.
        pass
    return report


# ---- the cron entrypoint (final review Fix 4) -------------------------------------------
#
# The documented cron line is `docker exec dbsearch-api-1 python3 -m dbsearch.server.retention`,
# and the first version of this entrypoint ran `sweep()` in that FRESH process. Index, blob,
# grant and manifest deletes worked, because those are shared Postgres and shared volumes.
# Conversation history did not, in the deployment shape that mattered at the time:
# `ConversationService` held an in-memory dict per process (no #596 store yet), so the cron
# process cleared its own empty dict while the SERVING process kept every transcript,
# including answer text derived verbatim from the documents that were just deleted. The
# operator endpoint, running inside the serving process, did delete it - so the two
# sanctioned sweep paths disagreed about what a sweep even means, and the one the deploy
# note tells operators to use was the weaker.
#
# #596 made conversation history durable when PGVECTOR_DSN is set - a fresh cron process's
# PgConversationStore now reaches the SAME Postgres table the serving process does, so the
# original failure mode (an empty dict, cleared for nothing) no longer reproduces in that
# configuration. It still reproduces exactly as before in the memory-only fallback
# (unconfigured, demo/self-host), which #596 leaves unchanged. The fix chosen here does not
# depend on knowing which case a given deployment is in: the cron entrypoint DRIVES the
# operator endpoint rather than sweeping in its own process, so there is exactly one real
# sweep implementation, correct under either store backend, instead of two that can drift
# apart. LAW 4 is unaffected - the endpoint still hands the real sweep to a detached daemon
# thread and returns 202 immediately; cron is not a user request either way.
_SWEEP_URL_ENV = "DBSEARCH_SWEEP_URL"
_SWEEP_URL_DEFAULT = "http://127.0.0.1:8000/admin/retention/sweep"
_SWEEP_KEY_ENV = "DBSEARCH_SWEEP_KEY"


def run_cron_sweep(urlopen=None) -> dict:
    """POST the operator sweep endpoint and return a counts-only result dict.

    Config (both env, read at call time so a test can set them):
      - `DBSEARCH_SWEEP_KEY`: an OPERATOR api key (`dbk_...`, from POST /developer/keys as a
        user in DBSEARCH_OPERATOR_OIDS). Required. Without it this returns
        `{"error": "no_operator_key"}` and deletes nothing - the endpoint answers 404 to a
        non-operator (#549's "a 403 confirms the route exists"), so a silent 404 in a cron log
        would look like a typo'd URL rather than a missing credential. Say which it is.
      - `DBSEARCH_SWEEP_URL`: defaults to the in-container loopback address of the api service.

    `urlopen` is an injection seam for tests; real callers never pass it. No new dependency -
    `urllib.request` is stdlib, and the call is loopback-only by default.

    Never raises: cron output is a log line, and a stack trace in a crontab mail is worse than
    a machine-readable error line. Returns `{"error": ...}` (and `main` exits non-zero) instead.
    """
    import os
    import urllib.request

    key = os.environ.get(_SWEEP_KEY_ENV, "").strip()
    if not key:
        return {"error": "no_operator_key",
                "detail": f"set {_SWEEP_KEY_ENV} to an operator api key (dbk_...); "
                          f"the sweep endpoint answers 404 to everyone else"}

    url = os.environ.get(_SWEEP_URL_ENV, "").strip() or _SWEEP_URL_DEFAULT
    req = urllib.request.Request(url, data=b"", method="POST",
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    opener = urlopen or urllib.request.urlopen
    try:
        with opener(req, timeout=30) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read().decode("utf-8", "replace")[:2000]
            return {"status": int(status), "response": body}
    except Exception as exc:
        # LAW 8 / LAW 1: the class name and, when the server sent one, the status - never a
        # response body from an unknown endpoint and never the key.
        return {"error": type(exc).__name__, "status": getattr(exc, "code", None)}


def main() -> int:
    result = run_cron_sweep()
    print(json.dumps(result))
    if result.get("error") or not (200 <= int(result.get("status") or 0) < 300):
        return 1
    return 0


if __name__ == "__main__":    # cron: docker exec dbsearch-api-1 python3 -m dbsearch.server.retention
    import sys

    sys.exit(main())
