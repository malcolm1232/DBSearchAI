"""#797 - mint a `dbs_session` cookie for a test identity, using the server's OWN signing
key via `user_auth.sign_session`.

WHY IN-CONTAINER: the HMAC signing key (`_key()` in user_auth.py) derives from prod-only
secrets (AUTH_CLIENT_SECRET / DBSEARCH_SESSION_KEY) that never leave the box. So this script
is meant to run INSIDE the api container (`docker exec dbsearch-api-1 python ...`), exactly
the way the QuantifyMe `/pw` skill mints a cookie ON prod and copies only the cookie out. The
key is never printed, never copied; only the resulting signed cookie (an 8h bearer token for a
TEST identity) crosses the wire. `sign_session` is a pure function - running this changes NO
server state (no vault write, no db write, no restart).

#825 - THIS SCRIPT CARRIES NO IDENTITIES. It is a pure signer: the caller supplies the claims
on the command line. The repo is public, so the real tenant id, user OIDs and UPNs live in the
untracked `secrets/entra_test_users.env` and are passed in by `scripts/pw_dbs_auth.py`, which
reads that file locally. Nothing here names a person or a tenant.

Usage (inside the api container):
    python /tmp/mint_session.py --oid <oid> --tid <tid> --email <upn> [--name <display>]

Prints ONLY the cookie value on stdout. See scripts/pw_dbs_auth.py for the caller that ssh's
this in and injects the result into Playwright.

A minted cookie is byte-identical in shape to what `/auth/callback` mints on a real login; it
lacks only the server-side vaulted refresh token, which matters solely for cloud data-plane
redemption (SharePoint / Azure SQL OBO), NOT for the upload/partition path (#791) or any
control-plane / canvas drive.
"""
import argparse
import sys
import time

sys.path.insert(0, "/app/src")
from dbsearch.server import user_auth  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="#797 mint a dbs_session cookie (pure signer)")
    ap.add_argument("--oid", required=True, help="the identity's object id")
    ap.add_argument("--tid", required=True, help="the identity's home tenant id")
    ap.add_argument("--email", required=True, help="the identity's UPN / email")
    ap.add_argument("--name", default="", help="display name (cosmetic; shown in the UI)")
    ap.add_argument("--hours", type=int, default=8, help="cookie lifetime")
    args = ap.parse_args()

    payload = {"oid": args.oid, "name": args.name or args.email, "email": args.email,
               "tid": args.tid, "idp": "entra", "exp": int(time.time()) + args.hours * 3600}
    print(user_auth.sign_session(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
