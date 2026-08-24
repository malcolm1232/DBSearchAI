"""#572 - ADR 0013: accounts, identities, and (later) the activity clock.

An account is a ROW written at callback time, not an implicit side effect of the first
manifest write. `account_id` is the workspace key: for Entra it IS the oid and for Google
the verified email (additive migration - existing user_manifests keys keep working
untouched); only accounts with no preexisting key shape (local email/password, Task #574)
get a generated opaque id.

Unconfigured (no DSN) runs memory-only, same contract as manifest_store.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid


class AccountStoreUnavailable(Exception):
    """Configured but unreachable. Message is data-free (class name + SQLSTATE only)."""


def _safe_reason(exc: BaseException) -> str:
    sqlstate = getattr(exc, "sqlstate", None)
    return (f"{type(exc).__name__} (sqlstate {sqlstate})" if sqlstate
            else type(exc).__name__)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def new_account_id() -> str:
    return "acct_" + uuid.uuid4().hex


class InMemoryAccountStore:
    """Hermetic adapter for tests and rigs without Postgres."""

    def __init__(self) -> None:
        self._accounts: dict[str, dict] = {}
        # #582: a ROW per identity now, not a bare account_id - it also carries the
        # verified `tid` and `email` recorded at that identity's last sign-in (ADR 0019 D2).
        self._identities: dict[tuple[str, str], dict] = {}
        self._local: dict[str, dict] = {}      # email -> {email, account_id, salt, pwhash}
        self._activity: dict[str, float] = {}  # account_id -> epoch seconds (#576)
        self._entitlements: dict[str, dict] = {}  # account_id -> billing row (#775)
        self._lock = threading.Lock()

    def resolve(self, idp: str, subject: str,
                preferred_account_id: "str | None" = None,
                tid: "str | None" = None, email: "str | None" = None) -> str:
        """#582 added `tid` and `email`, both optional and additive: every pre-existing
        caller keeps working untouched, and a caller that has the values records them.

        They BACKFILL and never clear. An identity recorded before #582 shipped has no
        tid, and learns it on the owner's next sign-in; but one odd sign-in that cannot
        supply a tid must never erase a known one, or a working share would start being
        refused for no reason the user could act on."""
        with self._lock:
            row = self._identities.get((idp, subject))
            if row is None:
                acc = preferred_account_id or new_account_id()
                row = {"idp": idp, "subject": subject, "account_id": acc,
                       "tid": "", "email": ""}
                self._identities[(idp, subject)] = row
                self._accounts.setdefault(acc, {"account_id": acc,
                                                "created_at": _now_iso(),
                                                "last_seen": _now_iso()})
            if tid:
                row["tid"] = tid
            if email:
                row["email"] = email.strip().casefold()
            self._accounts[row["account_id"]]["last_seen"] = _now_iso()
            return row["account_id"]

    def identity_tenants(self, account_id: str) -> list[dict]:
        """Every identity linked to this account, with whatever tenant and email were
        recorded at its last sign-in (#582 / ADR 0019 D2).

        STORAGE ONLY. The rule that turns these rows into partitions is
        `api.auth.account_partitions`, which is the layer that knows the deployment
        constant - keeping it there is what lets ONE canonicalization function serve both
        session-time and share-time resolution."""
        with self._lock:
            return [dict(r) for r in self._identities.values()
                    if r["account_id"] == account_id]

    def account_for_email(self, email: str) -> "str | None":
        """#582 / ADR 0019 D5: resolve a verified email to its account, so a share can name
        a person the way a human would instead of by an opaque `acct_<hex>`."""
        key = (email or "").strip().casefold()
        if not key:
            return None
        with self._lock:
            for r in self._identities.values():
                if r["email"] == key:
                    return r["account_id"]
        return None

    def link(self, idp: str, subject: str, account_id: str) -> str:
        with self._lock:
            existing = self._identities.get((idp, subject))
            if existing is not None:
                return existing["account_id"]   # never re-point (ADR 0013 decision 3)
            self._identities[(idp, subject)] = {"idp": idp, "subject": subject,
                                                "account_id": account_id,
                                                "tid": "", "email": ""}
            self._accounts.setdefault(account_id, {"account_id": account_id,
                                                   "created_at": _now_iso(),
                                                   "last_seen": _now_iso()})
            return account_id

    def get(self, account_id: str) -> "dict | None":
        with self._lock:
            row = self._accounts.get(account_id)
            return dict(row) if row else None

    def create_local_user(self, email: str, salt_hex: str, hash_hex: str) -> str:
        """#574: mint an opaque account for a local (email/password) signup. The email is
        the credential lookup key ONLY - the returned id, never the email, is what the
        caller must use as the session principal (LAW 2)."""
        with self._lock:
            if email in self._local:
                raise ValueError("account exists")
            acc = new_account_id()
            self._local[email] = {"email": email, "account_id": acc,
                                  "salt": salt_hex, "pwhash": hash_hex}
            # A local account's subject IS its email, so it is knowable by email from the
            # moment it is created - no sign-in needed before somebody can share with it.
            self._identities[("local", email)] = {"idp": "local", "subject": email,
                                                  "account_id": acc, "tid": "",
                                                  "email": email.strip().casefold()}
            self._accounts.setdefault(acc, {"account_id": acc, "created_at": _now_iso(),
                                            "last_seen": _now_iso()})
            return acc

    def get_local_user(self, email: str) -> "dict | None":
        with self._lock:
            row = self._local.get(email)
            return dict(row) if row else None

    # ---- #775 billing entitlement -------------------------------------------------------
    def get_entitlement(self, account_id: str) -> "dict | None":
        with self._lock:
            row = self._entitlements.get(account_id)
            return dict(row) if row else None

    def set_entitlement(self, account_id: str, tier: str, status: str,
                        stripe_customer_id: "str | None" = None,
                        stripe_subscription_id: "str | None" = None,
                        cancel_at_period_end: bool = False,
                        current_period_end: "float | None" = None,
                        last_event_at: "int | None" = None) -> None:
        """Record what Stripe says this account is paying for.

        Stripe is the source of truth, so this OVERWRITES rather than merges: a webhook
        carries the subscription's whole current state, and merging would let a stale field
        outlive the change that was supposed to replace it."""
        with self._lock:
            self._entitlements[account_id] = {
                "account_id": account_id, "tier": tier, "status": status,
                "stripe_customer_id": stripe_customer_id,
                "stripe_subscription_id": stripe_subscription_id,
                "cancel_at_period_end": bool(cancel_at_period_end),
                "current_period_end": current_period_end,
                "last_event_at": last_event_at,     # #838: what the ordering guard compares
            }

    def account_for_stripe_customer(self, stripe_customer_id: str) -> "str | None":
        """Which account a Stripe customer belongs to. The webhook needs this: Stripe knows
        its own customer id and nothing about ours."""
        with self._lock:
            for acct, row in self._entitlements.items():
                if row.get("stripe_customer_id") == stripe_customer_id:
                    return acct
            return None

    def touch_activity(self, account_id: str, now: "float | None" = None) -> None:
        """#576: the "any access counts" clock - upserted on every authenticated request
        for the caller AND for the document owner behind every live grant the caller
        holds (retention.touch). `now` is a clock-injection seam for tests only; the real
        caller (retention.touch) never passes it and gets wall-clock time."""
        with self._lock:
            self._activity[account_id] = now if now is not None else time.time()

    def last_activity(self, account_id: str) -> "float | None":
        with self._lock:
            return self._activity.get(account_id)

    def silent_accounts(self, cutoff_epoch: float) -> list[str]:
        """Accounts WITH an activity row older than `cutoff_epoch`. An account with NO
        row is never returned - unknown is not ancient, and the retention sweep must
        never treat "we have no record" as "safe to delete" (#576)."""
        with self._lock:
            return [a for a, ts in self._activity.items() if ts < cutoff_epoch]

    def drop_activity(self, account_id: str, cutoff_epoch: "float | None" = None) -> bool:
        """#576 review Finding 3 (TOCTOU): when `cutoff_epoch` is given, the row is only
        dropped if it is STILL older than the cutoff at the moment of deletion - a second,
        atomic guard beside the caller's own re-check (retention.sweep), closing the
        (much smaller) window between that re-check and this call. Returns whether a row
        was actually dropped, so a caller can tell "deleted" from "raced and survived"."""
        with self._lock:
            if cutoff_epoch is not None:
                current = self._activity.get(account_id)
                if current is None or current >= cutoff_epoch:
                    return False
            return self._activity.pop(account_id, None) is not None


class PgAccountStore:
    """Same tables in Postgres. Per-op connections, committed DDL before first op,
    _schema_done set only after the DDL transaction commits - manifest_store.py's
    _ensure_schema docstring explains why each of those is load-bearing."""

    def __init__(self, dsn: str, connect_timeout: int = 5) -> None:
        self._dsn = dsn
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
                conn.execute("""CREATE TABLE IF NOT EXISTS accounts (
                    account_id text PRIMARY KEY,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    last_seen  timestamptz NOT NULL DEFAULT now()
                )""")
                conn.execute("""CREATE TABLE IF NOT EXISTS account_identities (
                    idp        text NOT NULL,
                    subject    text NOT NULL,
                    account_id text NOT NULL,
                    PRIMARY KEY (idp, subject)
                )""")
                # #582 / ADR 0019 D2: additive, so an existing deployment upgrades with no
                # migration step. NULL means "not recorded yet", which the share-time rule
                # treats as UNKNOWABLE and fails closed on - never as "this account has no
                # tenant", which would hand a foreign-tenant user a partition they do not
                # have and re-open the silent failure #582 exists to close.
                conn.execute("ALTER TABLE account_identities "
                             "ADD COLUMN IF NOT EXISTS tid text")
                conn.execute("ALTER TABLE account_identities "
                             "ADD COLUMN IF NOT EXISTS email text")
                conn.execute("CREATE INDEX IF NOT EXISTS account_identities_email_idx "
                             "ON account_identities (email)")
                conn.execute("""CREATE TABLE IF NOT EXISTS local_credentials (
                    email      text PRIMARY KEY,
                    account_id text NOT NULL,
                    salt       text NOT NULL,
                    pwhash     text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now()
                )""")
                # #576: the "any access counts" activity clock. A missing row is NEVER
                # "ancient" (see silent_accounts) - it means this account has not been
                # touched since the feature shipped, and the retention sweep must leave it
                # alone.
                conn.execute("""CREATE TABLE IF NOT EXISTS workspace_activity (
                    account_id    text PRIMARY KEY,
                    last_activity timestamptz NOT NULL
                )""")
                # #775 / ADR 0027: what Stripe says this account is paying for. One row per
                # account, overwritten wholesale by the webhook, because a webhook carries
                # the subscription's whole current state and merging would let a stale field
                # outlive the change meant to replace it. The Stripe ids are nullable: an
                # account on the free tier has never met Stripe.
                conn.execute("""CREATE TABLE IF NOT EXISTS account_entitlements (
                    account_id             text PRIMARY KEY,
                    tier                   text NOT NULL,
                    status                 text NOT NULL,
                    stripe_customer_id     text,
                    stripe_subscription_id text,
                    cancel_at_period_end   boolean NOT NULL DEFAULT false,
                    current_period_end     timestamptz,
                    updated_at             timestamptz NOT NULL DEFAULT now()
                )""")
                conn.execute("CREATE INDEX IF NOT EXISTS account_entitlements_customer_idx "
                             "ON account_entitlements (stripe_customer_id)")
                # #838: the Stripe `created` timestamp of the event that last wrote this row.
                # Stripe does NOT guarantee delivery order and retries failed events for
                # days, so "overwrite wholesale" needed something to overwrite AGAINST or a
                # late-arriving older event could restore an entitlement that was cancelled.
                # Nullable and added the same way `tid`/`email` were: a row written before
                # this column existed reads NULL, which the guard treats as "cannot order,
                # so do not refuse" - the conservative direction for money.
                conn.execute("ALTER TABLE account_entitlements "
                             "ADD COLUMN IF NOT EXISTS last_event_at bigint")
            self._schema_done = True

    def _run(self, fn):
        try:
            self._ensure_schema()
            with self._conn() as conn:
                return fn(conn)
        except Exception as exc:
            reason = _safe_reason(exc)
            logging.getLogger("dbsearch").error("account store unavailable: %s", reason)
        raise AccountStoreUnavailable(reason)

    def resolve(self, idp: str, subject: str,
                preferred_account_id: "str | None" = None,
                tid: "str | None" = None, email: "str | None" = None) -> str:
        acc_new = preferred_account_id or new_account_id()
        norm_email = (email or "").strip().casefold()

        def _resolve(conn):
            row = conn.execute(
                "SELECT account_id FROM account_identities WHERE idp=%s AND subject=%s",
                (idp, subject)).fetchone()
            if row:
                conn.execute("UPDATE accounts SET last_seen=now() WHERE account_id=%s",
                             (row[0],))
                _record(conn)
                return row[0]
            conn.execute("""INSERT INTO accounts (account_id) VALUES (%s)
                            ON CONFLICT (account_id) DO UPDATE SET last_seen=now()""",
                         (acc_new,))
            conn.execute("""INSERT INTO account_identities (idp, subject, account_id)
                            VALUES (%s, %s, %s) ON CONFLICT (idp, subject) DO NOTHING""",
                         (idp, subject, acc_new))
            _record(conn)
            # a concurrent racer may have won the identity insert; return the truth
            return conn.execute(
                "SELECT account_id FROM account_identities WHERE idp=%s AND subject=%s",
                (idp, subject)).fetchone()[0]

        def _record(conn):
            """#582: backfill tid/email onto the identity row. COALESCE(NULLIF(...)) is the
            backfill-never-clear rule in SQL - an empty incoming value leaves the stored one
            alone, so a sign-in that cannot supply a tid never makes a known account
            unknowable again."""
            if not (tid or norm_email):
                return
            conn.execute(
                """UPDATE account_identities
                      SET tid   = COALESCE(NULLIF(%s, ''), tid),
                          email = COALESCE(NULLIF(%s, ''), email)
                    WHERE idp=%s AND subject=%s""",
                (tid or "", norm_email, idp, subject))

        return self._run(_resolve)

    def identity_tenants(self, account_id: str) -> list[dict]:
        """#582 / ADR 0019 D2 - see InMemoryAccountStore.identity_tenants. NULL columns
        coalesce to "" so both stores answer with the same shape, and "unrecorded" is one
        value rather than two the caller has to remember to handle."""
        def _rows(conn):
            return [{"idp": r[0], "subject": r[1], "account_id": account_id,
                     "tid": r[2] or "", "email": r[3] or ""}
                    for r in conn.execute(
                        """SELECT idp, subject, tid, email FROM account_identities
                           WHERE account_id=%s""", (account_id,)).fetchall()]
        return self._run(_rows)

    def account_for_email(self, email: str) -> "str | None":
        """#582 / ADR 0019 D5: a verified email -> its account. Emails are stored casefolded
        (see `resolve`), so the lookup is an index hit rather than a scan with lower()."""
        key = (email or "").strip().casefold()
        if not key:
            return None

        def _one(conn):
            row = conn.execute(
                "SELECT account_id FROM account_identities WHERE email=%s LIMIT 1",
                (key,)).fetchone()
            return row[0] if row else None
        return self._run(_one)

    def link(self, idp: str, subject: str, account_id: str) -> str:
        def _link(conn):
            conn.execute("""INSERT INTO accounts (account_id) VALUES (%s)
                            ON CONFLICT (account_id) DO NOTHING""", (account_id,))
            conn.execute("""INSERT INTO account_identities (idp, subject, account_id)
                            VALUES (%s, %s, %s) ON CONFLICT (idp, subject) DO NOTHING""",
                         (idp, subject, account_id))
            return conn.execute(
                "SELECT account_id FROM account_identities WHERE idp=%s AND subject=%s",
                (idp, subject)).fetchone()[0]
        return self._run(_link)

    def get(self, account_id: str) -> "dict | None":
        def _get(conn):
            row = conn.execute(
                """SELECT account_id, created_at, last_seen FROM accounts
                   WHERE account_id=%s""", (account_id,)).fetchone()
            return ({"account_id": row[0], "created_at": row[1].isoformat(),
                     "last_seen": row[2].isoformat()} if row else None)
        return self._run(_get)

    def create_local_user(self, email: str, salt_hex: str, hash_hex: str) -> str:
        """Atomic: the `local_credentials` insert is the arbiter of uniqueness (its PK is
        email). A duplicate email is a CALLER error, not store unavailability, so the
        ValueError is raised OUTSIDE `_run` - inside, it would be caught by `_run`'s
        broad `except Exception` and misreported as AccountStoreUnavailable (a 503),
        which would tell a caller with a perfectly good email/password to retry later."""
        acc = new_account_id()

        def _create(conn):
            conn.execute("""INSERT INTO accounts (account_id) VALUES (%s)
                            ON CONFLICT (account_id) DO NOTHING""", (acc,))
            cur = conn.execute(
                """INSERT INTO local_credentials (email, account_id, salt, pwhash)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (email) DO NOTHING""",
                (email, acc, salt_hex, hash_hex))
            if cur.rowcount == 0:
                return None                     # email already taken - signal, don't raise here
            # #582: record the email on the identity row at CREATION. A local account's
            # subject is its email, so it is shareable-with immediately - the owner does
            # not have to sign in once before a colleague can name them.
            conn.execute(
                """INSERT INTO account_identities (idp, subject, account_id, email)
                   VALUES ('local', %s, %s, %s) ON CONFLICT (idp, subject) DO NOTHING""",
                (email, acc, email.strip().casefold()))
            return acc

        result = self._run(_create)
        if result is None:
            raise ValueError("account exists")
        return result

    def get_local_user(self, email: str) -> "dict | None":
        def _get(conn):
            row = conn.execute(
                """SELECT email, account_id, salt, pwhash FROM local_credentials
                   WHERE email=%s""", (email,)).fetchone()
            return ({"email": row[0], "account_id": row[1], "salt": row[2],
                     "pwhash": row[3]} if row else None)
        return self._run(_get)

    # ---- #775 billing entitlement -------------------------------------------------------
    def get_entitlement(self, account_id: str) -> "dict | None":
        def _get(conn):
            row = conn.execute(
                """SELECT account_id, tier, status, stripe_customer_id,
                          stripe_subscription_id, cancel_at_period_end, current_period_end,
                          last_event_at
                     FROM account_entitlements WHERE account_id=%s""",
                (account_id,)).fetchone()
            if not row:
                return None
            return {"account_id": row[0], "tier": row[1], "status": row[2],
                    "stripe_customer_id": row[3], "stripe_subscription_id": row[4],
                    "cancel_at_period_end": bool(row[5]),
                    "current_period_end": row[6].timestamp() if row[6] else None,
                    "last_event_at": row[7]}      # #838: None on rows predating the column
        return self._run(_get)

    def set_entitlement(self, account_id: str, tier: str, status: str,
                        stripe_customer_id: "str | None" = None,
                        stripe_subscription_id: "str | None" = None,
                        cancel_at_period_end: bool = False,
                        current_period_end: "float | None" = None,
                        last_event_at: "int | None" = None) -> None:
        """Same overwrite-wholesale contract as InMemoryAccountStore.set_entitlement.

        The account row is upserted first, because a Stripe webhook can legitimately arrive
        for an account this store has never written (checkout completes before the customer
        signs in again), and a foreign-key-shaped surprise at that moment would drop a
        payment we have already taken."""
        import datetime as _dt

        end = (_dt.datetime.fromtimestamp(current_period_end, tz=_dt.timezone.utc)
               if current_period_end is not None else None)

        def _set(conn):
            conn.execute("""INSERT INTO accounts (account_id) VALUES (%s)
                            ON CONFLICT (account_id) DO NOTHING""", (account_id,))
            conn.execute(
                """INSERT INTO account_entitlements
                       (account_id, tier, status, stripe_customer_id,
                        stripe_subscription_id, cancel_at_period_end, current_period_end,
                        last_event_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (account_id) DO UPDATE SET
                       tier=EXCLUDED.tier,
                       status=EXCLUDED.status,
                       stripe_customer_id=EXCLUDED.stripe_customer_id,
                       stripe_subscription_id=EXCLUDED.stripe_subscription_id,
                       cancel_at_period_end=EXCLUDED.cancel_at_period_end,
                       current_period_end=EXCLUDED.current_period_end,
                       last_event_at=EXCLUDED.last_event_at,
                       updated_at=now()""",
                (account_id, tier, status, stripe_customer_id, stripe_subscription_id,
                 bool(cancel_at_period_end), end, last_event_at))
        self._run(_set)

    def account_for_stripe_customer(self, stripe_customer_id: str) -> "str | None":
        def _get(conn):
            row = conn.execute(
                "SELECT account_id FROM account_entitlements WHERE stripe_customer_id=%s",
                (stripe_customer_id,)).fetchone()
            return row[0] if row else None
        return self._run(_get)

    def touch_activity(self, account_id: str, now: "float | None" = None) -> None:
        """#576: same upsert-the-clock contract as InMemoryAccountStore.touch_activity."""
        import datetime as _dt

        ts = (_dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc) if now is not None
              else _dt.datetime.now(_dt.timezone.utc))

        def _touch(conn):
            conn.execute(
                """INSERT INTO workspace_activity (account_id, last_activity)
                   VALUES (%s, %s)
                   ON CONFLICT (account_id) DO UPDATE SET last_activity=EXCLUDED.last_activity""",
                (account_id, ts))
        self._run(_touch)

    def last_activity(self, account_id: str) -> "float | None":
        def _get(conn):
            row = conn.execute(
                "SELECT last_activity FROM workspace_activity WHERE account_id=%s",
                (account_id,)).fetchone()
            return row[0].timestamp() if row else None
        return self._run(_get)

    def silent_accounts(self, cutoff_epoch: float) -> list[str]:
        """Same "no row is never ancient" guarantee as the in-memory store: this only ever
        SELECTs rows that exist, so an account nobody has touched since the feature
        shipped simply never appears (#576)."""
        import datetime as _dt

        cutoff = _dt.datetime.fromtimestamp(cutoff_epoch, tz=_dt.timezone.utc)

        def _list(conn):
            rows = conn.execute(
                "SELECT account_id FROM workspace_activity WHERE last_activity < %s",
                (cutoff,)).fetchall()
            return [r[0] for r in rows]
        return self._run(_list)

    def drop_activity(self, account_id: str, cutoff_epoch: "float | None" = None) -> bool:
        """#576 review Finding 3: the SAME `AND last_activity < %s` predicate the in-memory
        store applies under its lock, expressed as a real atomic DELETE...WHERE - a sign-in
        landing between the caller's own re-check and this statement still cannot make the
        row vanish, because Postgres evaluates the predicate against the CURRENT row, not a
        value read earlier in Python."""
        if cutoff_epoch is None:
            def _drop(conn):
                conn.execute("DELETE FROM workspace_activity WHERE account_id=%s", (account_id,))
                return True
            return bool(self._run(_drop))

        import datetime as _dt

        cutoff = _dt.datetime.fromtimestamp(cutoff_epoch, tz=_dt.timezone.utc)

        def _drop_guarded(conn):
            cur = conn.execute(
                "DELETE FROM workspace_activity WHERE account_id=%s AND last_activity < %s",
                (account_id, cutoff))
            return cur.rowcount > 0
        return bool(self._run(_drop_guarded))
