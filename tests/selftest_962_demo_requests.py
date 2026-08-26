"""#962 - a demo request is KEPT, whatever happens to the email.

THE DEFECT. site/components/demo-form.tsx POSTed every lead to an endpoint read from
`NEXT_PUBLIC_FORM_ENDPOINT`, falling back to a hardcoded placeholder when unset. It was
unset on every build, so the placeholder shipped and every submission went to
`https://formspree.io/f/PLACEHOLDER_NOT_A_REAL_ENDPOINT` and 404'd. "Book a demo" is a
primary nav CTA. Nothing was stored, nothing was logged, nobody was told - the only trace
was a red "something went wrong, please try again" shown to the person, who then tried
again into the same 404.

It survived because the frontend test mocked `fetch` and never asserted WHERE it was
called - a fact the component's own docstring stated approvingly. So the tests here are
about the two things that mocked test could not see:

  * the lead reaches OUR store, and is still there afterwards
  * a broken mailer does not cost the lead, and does not surface as an error to the
    visitor either - storage is the record, mail is only the ping

and about the bounds an unauthenticated public write has to carry: server-side validation
(the browser check is a courtesy, not a control), length caps, a honeypot that does not
tell a bot which field caught it, and a rate limit.

The one failure a visitor MUST hear about is a store that refused the row, because then
"we'll be in touch" would be a lie. That case is asserted too.

    PYTHONPATH=src python3 tests/selftest_962_demo_requests.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")
# No DSN -> the in-memory store, which is what makes this hermetic.
for _k in ("DEMO_MAIL_API_KEY", "DEMO_MAIL_FROM", "DEMO_MAIL_TO"):
    os.environ.pop(_k, None)

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server import demo_requests  # noqa: E402
from dbsearch.server.app import DEMO_REQUESTS, app  # noqa: E402

client = TestClient(app)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" - {detail}" if detail else ""))
        failures.append(label)


def lead(**over):
    body = {"name": "Ada Lovelace", "email": "ada@example.com",
            "company": "Analytical Engines", "message": "our contract PDFs"}
    body.update(over)
    return body


def _post(body):
    # A distinct IP per call: the route is rate-limited per client IP, and a test that
    # shares one would start 429ing partway through and read as a broken route.
    _post.n += 1
    return client.post("/demo-request", json=body,
                       headers={"X-Forwarded-For": f"203.0.113.{_post.n % 250 + 1}"})


_post.n = 0


# ── 1. the lead is actually kept ────────────────────────────────────────────────────
print("\n[1] a submitted lead reaches our own store")

before = len(DEMO_REQUESTS.recent(500))
r = _post(lead())
check("accepted with 202", r.status_code == 202, f"got {r.status_code}: {r.text[:120]}")
check("answers with a bare ack and nothing else",
      r.json() == {"received": True}, r.text[:120])

rows = DEMO_REQUESTS.recent(500)
check("the lead was STORED, not just acknowledged", len(rows) == before + 1,
      f"{before} -> {len(rows)}")
if len(rows) > before:
    row = rows[0]
    check("stored with the submitted values",
          row.get("email") == "ada@example.com" and row.get("company") == "Analytical Engines",
          str({k: row.get(k) for k in ("email", "company")}))
    check("the optional message is kept too", row.get("message") == "our contract PDFs")

# The regression itself: nothing may point at the placeholder any more.
form = (ROOT / "site" / "components" / "demo-form.tsx").read_text(encoding="utf-8")
check("the form no longer posts to a third party",
      'const FORM_ENDPOINT = "/demo-request";' in form)
check("the placeholder endpoint constant is GONE",
      "PLACEHOLDER_NOT_A_REAL_ENDPOINT" not in form.split("*/", 1)[-1],
      "a fallback URL that cannot work is the defect, not a safety net")
check("NEXT_PUBLIC_FORM_ENDPOINT is no longer read anywhere",
      "process.env.NEXT_PUBLIC_FORM_ENDPOINT" not in form)


# ── 2. mail is best-effort; the lead is not ─────────────────────────────────────────
print("\n[2] a broken mailer costs a notification, never a lead")

check("unconfigured mailer reports itself unconfigured",
      demo_requests.mail_config() is None)
# notify() must NEVER raise - the caller has already stored the lead by then.
try:
    sent = demo_requests.notify(lead())
    check("notify() with no config returns False instead of raising", sent is False)
except Exception as exc:
    check("notify() with no config returns False instead of raising", False, repr(exc))

# And with a configured-but-broken provider, still no raise and still no lost lead.
os.environ["DEMO_MAIL_API_KEY"] = "re_not_a_real_key"
os.environ["DEMO_MAIL_FROM"] = "noreply@dbsearch.ai"
os.environ["DEMO_MAIL_TO"] = "privacy@dbsearch.ai"
try:
    check("mailer now reports itself configured", demo_requests.mail_config() is not None)
    before = len(DEMO_REQUESTS.recent(500))
    r = _post(lead(email="grace@example.com"))
    check("submit still succeeds while the mail provider rejects the key",
          r.status_code == 202, f"got {r.status_code}")
    check("and the lead is still stored", len(DEMO_REQUESTS.recent(500)) == before + 1)
finally:
    for _k in ("DEMO_MAIL_API_KEY", "DEMO_MAIL_FROM", "DEMO_MAIL_TO"):
        os.environ.pop(_k, None)

# The inverse, and the one the visitor must hear about.
class _RefusingStore:
    def record(self, *a, **k):
        raise demo_requests.DemoRequestStoreUnavailable("OperationalError")

    def recent(self, limit=100):
        return []


import dbsearch.server.app as app_mod  # noqa: E402

_saved = app_mod.DEMO_REQUESTS
app_mod.DEMO_REQUESTS = _RefusingStore()
try:
    r = _post(lead(email="held@example.com"))
    check("a store that refuses the row gives the visitor a 503, not a false 'thanks'",
          r.status_code == 503, f"got {r.status_code}")
finally:
    app_mod.DEMO_REQUESTS = _saved


# ── 3. the bounds an unauthenticated write has to carry ─────────────────────────────
print("\n[3] validation, honeypot and rate limit")

for field, body in (("name", lead(name="   ")),
                    ("email", lead(email="not-an-email")),
                    ("company", lead(company="")),
                    ("email", lead(email="a@" + "x" * 400 + ".com"))):
    r = _post(body)
    check(f"rejects a bad {field} server-side", r.status_code == 400,
          f"got {r.status_code}")
    if r.status_code == 400:
        # The field NAME may travel; the value must not - it is a real person's address.
        check(f"the {field} rejection does not echo the value back",
              "not-an-email" not in r.text and "xxxx" not in r.text, r.text[:80])

r = _post(lead(name=123))
check("a non-string field is rejected rather than coerced", r.status_code == 400,
      f"got {r.status_code}")

before = len(DEMO_REQUESTS.recent(500))
r = _post(lead(website="http://spam.example"))
check("a tripped honeypot is answered EXACTLY like a real submit (no oracle)",
      r.status_code == 202 and r.json() == {"received": True},
      f"{r.status_code} {r.text[:80]}")
check("but the honeypot lead is NOT stored",
      len(DEMO_REQUESTS.recent(500)) == before, "a bot's row was kept")

# Rate limit: same IP, repeatedly.
codes = [client.post("/demo-request", json=lead(),
                     headers={"X-Forwarded-For": "198.51.100.7"}).status_code
         for _ in range(9)]
check("a single IP is rate-limited", 429 in codes, str(codes))
check("but the first few get through", codes.count(202) >= 1, str(codes))


# ── 4. reading the leads is operator-only ───────────────────────────────────────────
print("\n[4] /admin/demo-requests")

r = client.get("/admin/demo-requests")
check("anonymous cannot read the leads", r.status_code in (401, 403),
      f"got {r.status_code}")
check("the refusal does not leak a lead", "ada@example.com" not in r.text)

r = client.get("/admin/demo-requests", headers={"X-DBSearch-Demo-User": "alice"})
check("a demo identity cannot read the leads either", r.status_code in (401, 403),
      f"got {r.status_code}")


print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
