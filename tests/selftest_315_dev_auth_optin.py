"""#315 - the `X-DBSearch-User` dev header must be OPT-IN, and off by default.

The hole this pins: `dev_auth_enabled()` used to default to "1". On a deployment with no real
login configured - which is exactly what the published `docker compose up -d --build` produces,
since docker-compose.yml sets no auth variables at all - `resolve_identity` then trusted
whatever oid the caller wrote into `X-DBSearch-User`. A request with no session, no cookie and
no API key could read any user's private documents by naming them.

The reproduction that motivated this file, run against the real ASGI app over real HTTP before
the fix: a victim uploaded a document ACL'd to themselves, then a caller who had never
authenticated sent one header and got the confidential text back verbatim.

Two properties are pinned here, and the second is the one that actually protects a stranger:

  1. The switch works when thrown (DBSEARCH_DEV_AUTH=1 still authenticates the dev header),
     because the seeded demo overlay depends on it.
  2. The switch is OFF unless thrown, and while off the victim's CONTENT does not reach an
     unauthenticated caller. Asserting only the status code would let a 401-with-a-body
     regression through, so the secret string itself is asserted absent from the response.

Run: python3 tests/selftest_315_dev_auth_optin.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"   # must be set before importing the app
os.environ.pop("DBSEARCH_DEV_AUTH", None)   # the fresh-clone state, asserted below

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _upload import settle                      # noqa: E402
from fastapi.testclient import TestClient      # noqa: E402

from dbsearch.api import auth as auth_mod       # noqa: E402
from dbsearch.server.app import app             # noqa: E402

VICTIM = "victim-oid-0001"
SECRET = "Q3 severance terms for J Doe 14 months confidential board-only"

_failures: list = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        _failures.append(label)


def set_dev_auth(value):
    if value is None:
        os.environ.pop("DBSEARCH_DEV_AUTH", None)
    else:
        os.environ["DBSEARCH_DEV_AUTH"] = value


def test_default_is_off():
    """The whole point of the card: an operator who never heard of this variable is safe."""
    print("\ndefault, as a fresh clone gets it")
    set_dev_auth(None)
    check(auth_mod.dev_auth_enabled() is False,
          "DBSEARCH_DEV_AUTH unset -> dev_auth_enabled() is False")


def test_optin_and_parsing():
    """The switch works when thrown, and a typo does not throw it."""
    print("\nparsing")
    for val, want in (("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
                      ("0", False), ("false", False), ("no", False), ("", False),
                      ("flase", False), ("banana", False), ("2", False)):
        set_dev_auth(val)
        got = auth_mod.dev_auth_enabled()
        check(got is want, f"DBSEARCH_DEV_AUTH={val!r} -> {got} (want {want})")
    set_dev_auth(None)


def test_spoof_is_refused_and_no_content_leaks():
    """End to end over the app: the victim's own document must not come back to a stranger."""
    print("\nend-to-end spoof attempt (no login configured, dev auth off)")
    client = TestClient(app)

    # Precondition for the branch under test: this is a self-host box with no real login.
    check(auth_mod.real_login_enabled() is False,
          "no real login configured (the branch #315 is about)")

    # The victim uploads, which needs an identity - so the switch is thrown for this step only.
    set_dev_auth("1")
    up = client.post("/admin/upload",
                     headers={"X-DBSearch-User": VICTIM},
                     files={"file": ("severance.txt", SECRET, "text/plain")},
                     data={"title": "Severance - CONFIDENTIAL"})
    check(up.status_code == 202, f"victim uploaded a private document (got {up.status_code})")
    acl = (up.json() or {}).get("acl") if up.status_code == 202 else None
    check(acl == [VICTIM], f"document is ACL'd to the victim alone (acl={acl})")
    # Settle the ingest job while the dev header still authenticates the poll (#917).
    job = settle(client, up, headers={"X-DBSearch-User": VICTIM})
    check(job.get("status") == "succeeded", f"the victim's upload indexed (job={job})")

    # Now the deployment's real posture: the operator never set the variable.
    set_dev_auth(None)
    spoof = client.post("/search", headers={"X-DBSearch-User": VICTIM},
                        json={"question": "what are the severance terms?"})
    check(spoof.status_code == 401,
          f"spoofed identity is refused (got {spoof.status_code})")

    # The property that matters, not merely the status line: no content crossed.
    body = spoof.text
    check(SECRET.split()[0] not in body and "severance terms for" not in body.lower(),
          "the victim's document text does not appear in the response body")

    # And the same call WITH the operator's explicit opt-in still works, so the demo overlay
    # (docker-compose.demo.yml sets DBSEARCH_DEV_AUTH=1) is not collateral damage.
    set_dev_auth("1")
    allowed = client.post("/search", headers={"X-DBSearch-User": VICTIM},
                          json={"question": "what are the severance terms?"})
    check(allowed.status_code == 200,
          f"with the explicit opt-in the same call succeeds (got {allowed.status_code})")
    set_dev_auth(None)


def test_refusal_is_actionable():
    """A stranger following the README hits this message; it must be a next step."""
    print("\nrefusal message")
    set_dev_auth(None)
    client = TestClient(app)
    r = client.post("/search", json={"question": "anything"})
    detail = str((r.json() or {}).get("detail", "")) if r.headers.get(
        "content-type", "").startswith("application/json") else r.text
    check(r.status_code == 401, f"unauthenticated request refused (got {r.status_code})")
    check("DBSEARCH_DEV_AUTH" in detail or "DBSEARCH_LOCAL_AUTH" in detail,
          f"refusal names how to fix it (got: {detail[:120]!r})")


if __name__ == "__main__":
    test_default_is_off()
    test_optin_and_parsing()
    test_spoof_is_refused_and_no_content_leaks()
    test_refusal_is_actionable()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for f in _failures:
            print("  -", f)
        sys.exit(1)
    print("selftest_315_dev_auth_optin: OK")
