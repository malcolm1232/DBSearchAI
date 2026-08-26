"""#962 - "Book a demo" leads: stored first, emailed second.

WHAT WENT WRONG, because it dictates the shape of this module. The form POSTed straight
to a third-party endpoint read from `NEXT_PUBLIC_FORM_ENDPOINT`, and that variable was
never set on any build - so the placeholder constant shipped, every submission went to
`https://formspree.io/f/PLACEHOLDER_NOT_A_REAL_ENDPOINT`, and every one of them 404'd.
"Book a demo" is a primary nav CTA. Nothing stored the lead, nothing logged it, nothing
alerted anyone: the ONLY record of a submission was a red message telling the visitor to
try again, which they then did, to the same 404.

So the rule here is that **storage is the record and email is the notification**, never
the other way round. A notification-only design is exactly the thing that just lost every
lead this site has ever taken, and mail is the least reliable link in any chain - an
expired API key, a suspended sender, a spam folder. `record()` returning means the lead is
durable; `notify()` is best-effort and may fail loudly in the log without ever failing the
request. If the two disagree, the table wins and /admin/demo-requests is how you read it.

That also means an UNCONFIGURED mailer is not an error. A self-hoster has no Postgres and
no mail provider, and must still be able to run this box; a deployment with a DSN but no
mail key keeps every lead and simply does not send. What is NOT tolerated is silence: a
send that fails, or is skipped for want of configuration, says so in the log, because
"nobody was told" is the whole defect.

Sending goes over HTTPS, not SMTP. Verified on the prod box: outbound port 25 is blocked
(as it is on most hosts), 443 is open. Cloudflare Email Routing serves dbsearch.ai's MX
and only FORWARDS inbound mail, so it cannot be used to send either.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading

_log = logging.getLogger("dbsearch")

#: Where a lead notification is sent. Already advertised on the site (privacy@ appears on
#: seven pages), and its MX is live, so this is a real mailbox rather than a new one to
#: provision.
DEFAULT_NOTIFY_TO = "privacy@dbsearch.ai"

#: Deliberately loose - the same shape the form's own client-side check uses. A stricter
#: rule here would reject real addresses and lose the lead, which costs more than a bad
#: row does. The address is never used as an identity, only replied to by a human.
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

#: Field caps. A demo form is an unauthenticated public write, so the size of what it can
#: put in the table is part of its attack surface; these are generous for a real human and
#: useless for anyone trying to store a payload.
LIMITS = {"name": 200, "email": 320, "company": 200, "message": 4000}

REQUIRED = ("name", "email", "company")


class DemoRequestRejected(Exception):
    """The submission is not a usable lead. Carries a field name, never the value."""


def clean(payload: dict) -> dict:
    """Validate and normalise a submission, or raise DemoRequestRejected.

    Re-validated HERE and not merely in the browser: the client-side check in demo-form.tsx
    is a courtesy to the visitor, and anyone can POST this route directly.
    """
    if not isinstance(payload, dict):
        raise DemoRequestRejected("body")

    out = {}
    for field, cap in LIMITS.items():
        value = payload.get(field, "")
        if not isinstance(value, str):
            raise DemoRequestRejected(field)
        value = value.strip()
        if len(value) > cap:
            raise DemoRequestRejected(field)
        out[field] = value

    for field in REQUIRED:
        if not out[field]:
            raise DemoRequestRejected(field)
    if not EMAIL_PATTERN.match(out["email"]):
        raise DemoRequestRejected("email")
    return out


def is_bot(payload: dict) -> bool:
    """True when the honeypot was filled in.

    A hidden field no human can see; a bot that fills every input trips it. Answered with
    the SAME 202 as a real submission rather than an error, because an error teaches a
    scraper which field to skip next time. The lead is simply not recorded.
    """
    value = payload.get("website", "")
    return isinstance(value, str) and bool(value.strip())


# ── storage ─────────────────────────────────────────────────────────────────────────

def _safe_reason(exc: BaseException) -> str:
    """Class name plus SQLSTATE, never `str(exc)` - the manifest_store rule, and it applies
    here for the same reason: a psycopg error can quote the offending row, and this row is
    a named human being's work email."""
    sqlstate = getattr(exc, "sqlstate", None)
    return (f"{type(exc).__name__} (sqlstate {sqlstate})" if sqlstate
            else type(exc).__name__)


class DemoRequestStoreUnavailable(Exception):
    """Configured but unreachable. Data-free, see _safe_reason."""


class InMemoryDemoRequestStore:
    """Hermetic adapter for tests, and the honest fallback for a box with no DSN."""

    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._lock = threading.Lock()

    def record(self, fields: dict, *, source_ip: str = "") -> None:
        with self._lock:
            self._rows.append({**fields, "source_ip": source_ip})

    def recent(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return [dict(r) for r in reversed(self._rows[-limit:])]


class PgDemoRequestStore:
    """Postgres-backed leads. Same idiom as PgManifestStore - see its `_ensure_schema`
    note for why the DDL commits in its own transaction before the flag is set."""

    def __init__(self, dsn: str, table: str = "demo_requests",
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
                        id          bigserial PRIMARY KEY,
                        fields      jsonb NOT NULL,
                        source_ip   text NOT NULL DEFAULT '',
                        created_at  timestamptz NOT NULL DEFAULT now()
                    )""")
            self._schema_done = True

    def _run(self, fn):
        try:
            self._ensure_schema()
            with self._conn() as conn:
                return fn(conn)
        except Exception as exc:
            reason = _safe_reason(exc)
            _log.error("demo request store %s unavailable: %s", self._table, reason)
        raise DemoRequestStoreUnavailable(reason)

    def record(self, fields: dict, *, source_ip: str = "") -> None:
        def _put(conn):
            conn.execute(
                f"INSERT INTO {self._table} (fields, source_ip) VALUES (%s::jsonb, %s)",
                (json.dumps(fields), source_ip))
        self._run(_put)

    def recent(self, limit: int = 100) -> list[dict]:
        def _get(conn):
            rows = conn.execute(
                f"""SELECT fields, source_ip, created_at FROM {self._table}
                    ORDER BY id DESC LIMIT %s""", (int(limit),)).fetchall()
            return [{**r[0], "source_ip": r[1], "created_at": r[2].isoformat()}
                    for r in rows]
        return self._run(_get)


# ── notification ────────────────────────────────────────────────────────────────────

#: Sent on every provider call. See the note beside the header for why it is load-bearing.
USER_AGENT = "DBSearch.AI/1.0 (+https://dbsearch.ai)"

#: Anything email-shaped is stripped from a provider's error before it is logged.
_EMAILISH = re.compile(r"[^\s\"'<>]+@[^\s\"'<>]+")


def _safe_error(body: bytes) -> str:
    """A provider error, trimmed and with addresses removed, safe to put in a log."""
    try:
        text = body.decode("utf-8", "replace")
    except Exception:
        return "<unreadable>"
    return _EMAILISH.sub("<address>", text).strip().replace("\n", " ")[:200]


def mail_config() -> "tuple[str, str, str] | None":
    """(api_key, sender, recipient), or None when this box cannot send mail.

    Resend's REST API: one key, one HTTPS call, and the sending domain is verified with
    two DNS records on a zone already managed in Cloudflare. Any provider with a
    JSON-over-HTTPS send would drop in here; nothing above this function knows which.
    """
    key = (os.environ.get("DEMO_MAIL_API_KEY") or "").strip()
    if not key:
        return None
    sender = (os.environ.get("DEMO_MAIL_FROM") or "").strip()
    if not sender:
        return None
    to = (os.environ.get("DEMO_MAIL_TO") or DEFAULT_NOTIFY_TO).strip()
    return key, sender, to


def notify(fields: dict) -> bool:
    """Best-effort email about one lead. Returns whether it was sent.

    NEVER raises. The caller has already stored the lead by this point, and a mail outage
    must not turn a captured lead into a red error message that makes the visitor submit
    again - which is precisely the loop this card exists to end. Every outcome is logged,
    including "not configured", because an unnoticed silence is the defect.
    """
    config = mail_config()
    if config is None:
        _log.warning(
            "demo request stored but NOT emailed: no DEMO_MAIL_API_KEY/DEMO_MAIL_FROM set. "
            "Read them at /admin/demo-requests.")
        return False

    key, sender, to = config
    body = (
        "New demo request from dbsearch.ai\n\n"
        f"Name:    {fields.get('name', '')}\n"
        f"Email:   {fields.get('email', '')}\n"
        f"Company: {fields.get('company', '')}\n\n"
        f"What they want to search:\n{fields.get('message', '') or '(not given)'}\n"
    )
    payload = json.dumps({
        "from": sender,
        "to": [to],
        # The lead's own address, so hitting reply in the mail client answers the human
        # rather than the robot.
        "reply_to": fields.get("email", ""),
        "subject": f"Demo request: {fields.get('company', '')} ({fields.get('name', '')})",
        "text": body,
    }).encode("utf-8")

    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json",
                 # NOT cosmetic, and not optional. api.resend.com sits behind Cloudflare,
                 # which blocks urllib's default `Python-urllib/3.11` on client signature.
                 # The reply is a Cloudflare error page - HTTP 403, body "error code: 1010"
                 # - which reads exactly like a Resend auth failure and is not one: the
                 # identical request with this header set returns 200 and an email id.
                 # Verified against the live API from the prod box.
                 "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                return True
            _log.error("demo request email refused: HTTP %s %s",
                       resp.status, _safe_error(resp.read()))
    except urllib.error.HTTPError as exc:
        # The body is included, with anything email-shaped removed. Status alone sent the
        # #962 follow-up chasing a permissions problem that did not exist - the body said
        # "error code: 1010", which names the real cause in four words. What must not
        # travel is the lead's address, which a provider error can echo back.
        _log.error("demo request email refused: HTTP %s %s",
                   exc.code, _safe_error(exc.read()))
    except Exception as exc:
        _log.error("demo request email failed: %s", type(exc).__name__)
    return False
