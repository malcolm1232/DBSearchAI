#!/usr/bin/env python3
"""Re-seed the tiered demo documents after a restart (#258 / #268).

    Project Falcon Plan              -> [DBSearch — Director access]   alice only
    Holiday and Annual Leave Policy  -> [DBSearch — Admin access]      alice, bob, Malcolm

Why not the built-in DBSEARCH_DEMO_SEED corpus: that one ACLs to the STRING principals
"all-staff" / "deal-team", which only match the dev-auth identities. Under a real Entra
sign-in a user's principals are their OID plus their GROUP oids, so those documents are
invisible — the demo silently shows nothing to exactly the identity it is meant to impress.
These are ACL'd to the real entitlement groups, so access follows genuine Entra membership
and adding someone to a tier is a directory change, not a DBSearch change.

Runs as alice (ROPC, MFA-free test account) purely to obtain an authenticated session for the
upload endpoint; the ACLs written are group oids, not hers.

    python3 scripts/seed_demo_docs.py [--base http://127.0.0.1:8080]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import mimetypes
import os
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Entitlement groups (#258), resolved BY NAME at run time — never hardcoded oids (#270).
#
# The first version of this script pinned the oids from one tenant. That is fine there and
# silently wrong everywhere else: a clone run against another directory seeds documents ACL'd
# to groups that do not exist, so they are visible to NOBODY, and nothing says so — the seeding
# reports success and every later question honestly answers "I couldn't find anything you have
# access to". Exactly the class of failure the rest of this work has been removing.
#
# App-scoped names on purpose: the "DBSearch —" prefix marks these as entitlements for THIS
# app rather than org units, so whoever administers the tenant next can tell them apart from
# real teams. Override either with an env var if your directory names them differently.
DIRECTOR_GROUP = os.environ.get("DBSEARCH_DIRECTOR_GROUP", "DBSearch — Director access")
ADMIN_GROUP = os.environ.get("DBSEARCH_ADMIN_GROUP", "DBSearch — Admin access")

DOCS = [
    ("Project Falcon Plan.txt", "Project Falcon Plan", DIRECTOR_GROUP),
    ("Holiday and Annual Leave Policy.txt", "Holiday and Annual Leave Policy", ADMIN_GROUP),
]


def _e2e():
    """Reuse the e2edbs harness's sign-in seams rather than re-implementing ROPC."""
    path = Path(os.path.expanduser("~/.claude/skills/e2edbs/e2e_dbsearch.py"))
    if not path.exists():
        sys.exit(f"e2edbs harness not found at {path} — cannot obtain a session")
    spec = importlib.util.spec_from_file_location("e2e", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_group(name: str, tenant: str, get_json=None, token_fn=None) -> "str | None":
    """The oid of the group called `name` in `tenant`, or None if it is not there (#270).

    Returns None ONLY for a genuine "no such group". A failed LOOKUP raises instead (#312).

    These were conflated, and the conflation was actively misleading: app_token() returns ""
    rather than raising when SP_CONNECTOR_CLIENT_ID/SECRET are absent, so the Graph call went
    out unauthenticated, came back InvalidAuthenticationToken, and the empty result read as
    "the group is not in this tenant". The script then printed az commands to create two
    groups that already existed. An unresolved group must still never become an ACL — but the
    operator has to be told which of the two problems they actually have."""
    from dbsearch.server import sp_connect

    get_json = get_json or sp_connect.http_get_json
    token_fn = token_fn or sp_connect.app_token
    tok = token_fn(tenant)
    if not tok:
        raise RuntimeError(
            "could not obtain a Graph app token - source secrets/sharepoint.env "
            "(SP_CONNECTOR_CLIENT_ID / SP_CONNECTOR_CLIENT_SECRET), and check admin "
            "consent is effective")
    try:
        # $filter on displayName is exact — no fuzzy matching, so we can never ACL to a group
        # that merely resembles the one asked for.
        # Encode the WHOLE query, not just the name: the spaces around `eq` are part of the
        # expression, and leaving them raw makes urllib reject the URL outright. An apostrophe
        # in a group name is doubled, which is how OData escapes it — otherwise a team called
        # "Directors' access" would terminate the string literal early.
        query = urllib.parse.urlencode({
            "$select": "id,displayName",
            "$filter": "displayName eq '%s'" % name.replace("'", "''"),
        })
        body = get_json(f"{sp_connect.GRAPH}/groups?{query}", tok)
    except Exception as exc:
        raise RuntimeError(f"Graph group lookup failed: {type(exc).__name__}: {exc}") from exc
    # Graph reports auth/permission problems as a 200 body with an `error` key, so an
    # unchecked read of `value` turns them into a silent "not found" (#312).
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        raise RuntimeError("Graph group lookup failed: "
                           f"{err.get('code', '?')}: {err.get('message', '')}")
    for g in (body or {}).get("value", []):
        if g.get("displayName") == name and g.get("id"):
            return g["id"]
    return None


def sign_in(e2e, base: str) -> str:
    for var in ("AUTH_TENANT_ID", "AUTH_CLIENT_ID", "AUTH_CLIENT_SECRET", "E2E_ALICE_PW"):
        if not os.environ.get(var):
            sys.exit(f"MISSING {var} — source .env and secrets/entra_test_users.env first")
    scope = "openid profile offline_access " + e2e.DB_IMPERSONATION_SCOPE
    tok = e2e._ropc_token(os.environ["AUTH_TENANT_ID"], os.environ["AUTH_CLIENT_ID"],
                          os.environ["AUTH_CLIENT_SECRET"], e2e.ENTRA_TEST_USERS["alice"],
                          os.environ["E2E_ALICE_PW"], scope)
    if "id_token" not in tok:
        sys.exit(f"sign-in failed: {str(tok.get('error_description', tok))[:200]}")
    oid = e2e._jwt_oid(tok["id_token"])
    return e2e._dev_seed(base, oid, tok.get("refresh_token", ""), "alice",
                         e2e.ENTRA_TEST_USERS["alice"])


def upload(base: str, path: Path, title: str, acl: list[str], cookie: str):
    boundary = f"----dbs{uuid.uuid4().hex}"
    data = path.read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "text/plain"
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="acl"\r\n\r\n{v}\r\n'
             for v in acl]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="title"\r\n\r\n{title}\r\n')
    head = "".join(parts).encode()
    filehdr = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
               f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n').encode()
    body = head + filehdr + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        base + "/admin/upload", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Cookie": f"dbs_session={cookie}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, {"raw": exc.read().decode()[:200]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--dir", default=str(ROOT / "docs" / "demo_docs"),
                    help="where the demo .txt files live")
    args = ap.parse_args()

    src = Path(args.dir)
    missing = [n for n, *_ in DOCS if not (src / n).exists()]
    if missing:
        sys.exit(f"missing demo files in {src}: {missing}")

    tenant = os.environ.get("AUTH_TENANT_ID", "")
    if not tenant:
        sys.exit("MISSING AUTH_TENANT_ID — cannot resolve the entitlement groups")

    # Resolve FIRST, and refuse the whole run if either group is missing. Seeding a document
    # ACL'd to a group that does not exist would "succeed" and then be readable by nobody,
    # with the product honestly reporting no access — a failure that looks like working
    # software. Better to stop here and say exactly what to create.
    resolved = {}
    try:
        for group in {DIRECTOR_GROUP, ADMIN_GROUP}:
            resolved[group] = resolve_group(group, tenant)
    except RuntimeError as exc:
        # #312: a lookup that could not run says NOTHING about whether the groups exist.
        # Do not print the "create them" recipe here — that is what sent an operator off to
        # create two groups that were already in the directory.
        print(f"COULD NOT CHECK THE ENTITLEMENT GROUPS - {exc}")
        print("\nThis is a credential/permission problem, not a missing group. Nothing was")
        print("seeded. Fix the lookup and re-run:")
        print("    set -a; . ./.env; for f in secrets/*.env; do . \"$f\"; done; set +a")
        return 3

    missing = sorted(g for g, oid in resolved.items() if not oid)
    if missing:
        print("REFUSING TO SEED — these entitlement groups are not in this tenant:")
        for g in missing:
            print(f"    {g!r}")
        print("\nA document ACL'd to a group that does not exist is visible to NOBODY, and the")
        print("product will simply answer \"I couldn't find anything you have access to\".")
        print("\nCreate them (app-scoped security groups, no mail), then add your users:")
        for g in missing:
            nick = g.lower().replace(" ", "-").replace("—", "").replace("--", "-").strip("-")
            print(f"    az ad group create --display-name {g!r} --mail-nickname {nick!r} \\")
            print(f"        --description 'Entitlement group for DBSearch.'")
        print("    az ad group member add --group '<name>' --member-id '<user-oid>'")
        print("\nOr point at groups you already have:")
        print("    export DBSEARCH_DIRECTOR_GROUP='Your Restricted Tier'")
        print("    export DBSEARCH_ADMIN_GROUP='Your All Staff Group'")
        return 2

    e2e = _e2e()
    cookie = sign_in(e2e, args.base)
    failed = 0
    for name, title, group in DOCS:
        code, res = upload(args.base, src / name, title, [resolved[group]], cookie)
        ok = code == 200
        failed += 0 if ok else 1
        print(f"  {'OK ' if ok else 'ERR'} {title:35s} acl=[{group}]"
              + ("" if ok else f"  -> {code} {res}"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
