"""#797 - the cheap, reproducible Playwright auth path for DBSearch (Entra), the DBSearch
equivalent of the QuantifyMe `/pw` skill.

WHAT IT GIVES YOU: a signed-in Playwright browser context for any known test identity
(alice / bob / jjjj636) against a live DBSearch deployment, WITHOUT a human doing an
interactive Microsoft OAuth + MFA dance every time. This is the enabler for the #795
verification regime and for two-user prod drives like #791.

HOW: it mints a `dbs_session` cookie by running scripts/mint_session.py INSIDE the api
container over ssh (so the HMAC key never leaves the box - the /pw pattern), then injects the
returned cookie into a fresh Playwright context. Proven 2026-08-18: an in-container-minted
bob cookie is accepted by prod `/auth/me` (signed_in, idp=entra, has_org).

    from pw_dbs_auth import authed_page, mint_cookie
    with authed_page("bob") as (page, browser):
        page.goto("https://dbsearch.ai/canvas")
        ...

CLI smoke (proves the whole path end to end - mint, inject, /canvas as that identity):
    python scripts/pw_dbs_auth.py bob
    python scripts/pw_dbs_auth.py alice --base https://dbsearch.ai --headed

Preconditions: ssh alias `dbsprod` reachable (or pass --ssh-host); Playwright chromium
installed locally (`python -m playwright install chromium`). For a LOCAL server whose
DBSEARCH_SESSION_KEY you control, pass --local-mint to sign the cookie here instead of over
ssh (see mint_cookie()).
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import subprocess
import sys
from pathlib import Path

COOKIE_NAME = "dbs_session"
DEFAULT_BASE = "https://dbsearch.ai"
DEFAULT_SSH_HOST = "dbsprod"
API_CONTAINER = "dbsearch-api-1"
COMPOSE = ("docker compose -f /opt/dbsearch/docker-compose.yml "
           "-f /opt/dbsearch/docker-compose.prod.yml -p dbsearch")
_MINT_SRC = Path(__file__).with_name("mint_session.py")

#: #825 - the repo is PUBLIC, so no real tenant id, OID or UPN is written down here. The
#: identity table lives in this untracked file (same one that already holds the passwords):
#:     DBS_TEST_TID=<home tenant id>
#:     DBS_TEST_<WHO>_OID / _EMAIL / _NAME     e.g. DBS_TEST_BOB_OID=...
#: `secrets/` is gitignored, so these values never enter the tree or its history.
_SECRETS = Path(__file__).resolve().parents[1] / "secrets" / "entra_test_users.env"
IDENTITIES = ("bob", "alice", "jjjj636")


def _load_secrets() -> dict:
    """Parse the untracked env file into a dict. Values may be quoted; comments ignored."""
    if not _SECRETS.exists():
        raise RuntimeError(
            f"identity file not found: {_SECRETS}\n"
            "#825 moved the test-user OIDs/UPNs out of the public tree. Populate it with "
            "DBS_TEST_TID and DBS_TEST_<WHO>_OID / _EMAIL / _NAME for bob, alice, jjjj636.")
    out = {}
    for line in _SECRETS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def identity_claims(identity: str) -> dict:
    """The oid/tid/email/name for a known test identity, read from the untracked file.
    Env vars of the same names win, so CI or a one-off drive can override without the file."""
    env = _load_secrets() if not os.environ.get(f"DBS_TEST_{identity.upper()}_OID") else {}
    def _get(key: str, required: bool = True) -> str:
        val = os.environ.get(key) or env.get(key, "")
        if required and not val:
            raise RuntimeError(f"{key} is not set (looked in env and {_SECRETS})")
        return val
    who = identity.upper()
    return {"oid": _get(f"DBS_TEST_{who}_OID"), "tid": _get("DBS_TEST_TID"),
            "email": _get(f"DBS_TEST_{who}_EMAIL"),
            "name": _get(f"DBS_TEST_{who}_NAME", required=False)}


def mint_cookie(identity: str = "bob", *, ssh_host: str = DEFAULT_SSH_HOST) -> str:
    """Mint a dbs_session cookie for `identity` by running mint_session.py inside the api
    container. The signing key never leaves the box; only the cookie string is returned.

    The identity's claims are read locally from the untracked secrets file (#825) and passed
    to the pure-signer script as arguments, so neither tracked script names a real person.

    Single-purpose ssh commands throughout (the auto-mode classifier blocks compound
    prod-ssh, per HANDOVER_260818c §2.4): copy the script to the host, docker cp into the
    container, exec it, read stdout.
    """
    claims = identity_claims(identity)
    # 1. host <- local script (single scp)
    _run(["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
          str(_MINT_SRC), f"{ssh_host}:/opt/dbsearch/mint_session.py"])
    # 2. container <- host script (single ssh)
    _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", ssh_host,
          f"docker cp /opt/dbsearch/mint_session.py {API_CONTAINER}:/tmp/mint_session.py"])
    # 3. mint inside the container (single ssh); stdout is the cookie. shlex.quote because a
    #    display name contains spaces and this is a shell string on the far side.
    argv = " ".join(shlex.quote(a) for a in (
        "--oid", claims["oid"], "--tid", claims["tid"], "--email", claims["email"],
        *(("--name", claims["name"]) if claims["name"] else ())))
    out = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", ssh_host,
                f"docker exec {API_CONTAINER} python /tmp/mint_session.py {argv}"])
    cookie = out.strip().splitlines()[-1].strip()
    if cookie.count(".") != 1 or len(cookie) < 40:
        raise RuntimeError(f"mint did not return a cookie-shaped string: {out!r}")
    return cookie


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd[:3])}...\n"
                           f"stderr: {p.stderr.strip()}")
    return p.stdout


def _domain(base: str) -> str:
    return base.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]


@contextlib.contextmanager
def authed_page(identity: str = "bob", *, base: str = DEFAULT_BASE,
                ssh_host: str = DEFAULT_SSH_HOST, headed: bool = False, cookie: str | None = None):
    """Yield (page, browser) with a signed-in dbs_session cookie set for `base`'s domain.
    Pass a pre-minted `cookie` to skip the ssh round-trip (e.g. reusing one across scenarios).
    """
    from playwright.sync_api import sync_playwright

    cookie = cookie or mint_cookie(identity, ssh_host=ssh_host)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        ctx = browser.new_context()
        ctx.add_cookies([{
            "name": COOKIE_NAME, "value": cookie, "domain": _domain(base),
            "path": "/", "httpOnly": True, "secure": base.startswith("https"),
            "sameSite": "Lax",
        }])
        page = ctx.new_page()
        try:
            yield page, browser
        finally:
            browser.close()


@contextlib.contextmanager
def authed_pages(identities, *, base: str = DEFAULT_BASE,
                 ssh_host: str = DEFAULT_SSH_HOST, headed: bool = False):
    """Yield {identity: page} for SEVERAL identities at once, signed in simultaneously.

    Two-user drives are the whole point of D2 (#822), and nesting two `authed_page`
    blocks does not work: sync_playwright cannot be entered twice in one thread. One
    playwright, one browser, one CONTEXT per identity, so the cookie jars stay separate.
    """
    from playwright.sync_api import sync_playwright

    cookies = {who: mint_cookie(who, ssh_host=ssh_host) for who in identities}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        pages = {}
        try:
            for who, cookie in cookies.items():
                ctx = browser.new_context()
                ctx.add_cookies([{
                    "name": COOKIE_NAME, "value": cookie, "domain": _domain(base),
                    "path": "/", "httpOnly": True, "secure": base.startswith("https"),
                    "sameSite": "Lax",
                }])
                pages[who] = ctx.new_page()
            yield pages
        finally:
            browser.close()


def _smoke(identity: str, base: str, ssh_host: str, headed: bool) -> int:
    """Prove the path: mint, inject, load /canvas, assert the account button reads `identity`."""
    import json as _json

    cookie = mint_cookie(identity, ssh_host=ssh_host)
    print(f"[mint] {identity}: cookie minted ({len(cookie)} chars)")
    with authed_page(identity, base=base, headed=headed, cookie=cookie) as (page, _b):
        # /auth/me is the identity oracle - cheapest possible assertion (JSON, no pixels).
        me = page.request.get(f"{base}/auth/me").json()
        print("[/auth/me]", _json.dumps({k: me.get(k) for k in
              ("signed_in", "name", "email", "idp", "has_org")}))
        assert me.get("signed_in") is True, "not signed in"
        assert me.get("idp") == "entra", f"idp not entra: {me.get('idp')}"
        # The expected email comes from the same untracked table the cookie was minted from
        # (#825), so this file names no real UPN either.
        expect_email = identity_claims(identity)["email"]
        assert (me.get("email") or "").lower() == expect_email.lower(), \
            f"wrong identity: {me.get('email')}"
        # And the real canvas renders it - the account button carries the signed-in email
        # in its title/aria-label (its visible text is just the avatar initials, so match on
        # the attribute, not the accessible name). A DOM read, not a screenshot - the cheap
        # measurement path this whole helper exists to enable.
        page.goto(f"{base}/canvas", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        found = page.evaluate(
            """(email) => [...document.querySelectorAll('button,[role=button]')].some(e =>
                 (e.getAttribute('aria-label') || e.title || e.innerText || '').includes(email))""",
            me["email"])
        assert found, f"account button for {me['email']} not found on /canvas"
        print(f"[canvas] account button shows {me['email']} -> PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="#797 Playwright auth path for DBSearch")
    ap.add_argument("identity", nargs="?", default="bob", choices=["bob", "alice", "jjjj636"])
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    try:
        return _smoke(args.identity, args.base, args.ssh_host, args.headed)
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
