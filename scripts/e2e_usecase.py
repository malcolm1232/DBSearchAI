#!/usr/bin/env python3
"""#337 - /e2edbs --usecase: the capability matrix.

Runs each capability in BOTH modes. A capability passes only if both modes pass.
That coupling is the point: it turns "we wired login and the demo broke" from a
surprise into a red test.

    python3 scripts/e2e_usecase.py                 both modes: mints alice+bob,
                                                    composes the fixture catalog
                                                    into the live scope, runs all
                                                    nine capabilities x two modes
    python3 scripts/e2e_usecase.py --demo          demo only (no login needed)
    python3 scripts/e2e_usecase.py --user          signed-in only (alice+bob, fixture compose)
    python3 scripts/e2e_usecase.py --only 4,6      a subset
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import csv as _csv                                                    # noqa: E402

from usecase_cases import CAPABILITIES, CapabilityResult, by_num  # noqa: E402
from usecase_runner import run_capability               # noqa: E402
from e2e_dbsearch import (_dev_seed, _jwt_oid, _ropc_token,          # noqa: E402
                          DB_IMPERSONATION_SCOPE, ENTRA_TEST_USERS)
from dbsearch.router.demo_backing import demo_fixture_path            # noqa: E402
from dbsearch.server.router_api import DEMO_MANIFEST, DEMO_USER_GROUPS  # noqa: E402


def mint_session(base: str, who: str = "alice") -> tuple[str, str]:
    """Sign a real Entra test user in WITHOUT a browser (spec 6.3).

    ROPC password grant -> refresh token -> /auth/dev/seed vaults it and mints the
    dbs_session cookie. Same path --live-entra already proves. Needs AUTH_TENANT_ID,
    AUTH_CLIENT_ID, AUTH_CLIENT_SECRET, E2E_<WHO>_PW in the env, and the server
    launched with DBSEARCH_DEV_SEED=1.

    Returns (cookie, oid) - the oid is needed to build the fixture manifest's ACLs.
    """
    tenant = os.environ["AUTH_TENANT_ID"]
    client_id = os.environ["AUTH_CLIENT_ID"]
    client_secret = os.environ["AUTH_CLIENT_SECRET"]
    upn = ENTRA_TEST_USERS[who]
    pw = os.environ[f"E2E_{who.upper()}_PW"]
    # Scope and token choice mirror e2e_dbsearch.py:205-211 deliberately. `profile` is
    # required for the id_token to carry the oid claim, and the oid comes from the
    # id_token rather than the access_token: an access token issued for a non-Graph
    # resource is opaque to us and is not reliably decodable as a JWT.
    tok = _ropc_token(tenant, client_id, client_secret, upn, pw,
                      f"openid profile offline_access {DB_IMPERSONATION_SCOPE}")
    if "refresh_token" not in tok or "id_token" not in tok:
        raise RuntimeError(
            f"ROPC sign-in failed for {upn}: {tok.get('error_description', tok)!r:.200}")
    oid = _jwt_oid(tok["id_token"])
    cookie = _dev_seed(base, oid, tok["refresh_token"], who, upn)
    return cookie, oid


_FIXTURE_TABLES = (
    ("azure-deals", "sales", ("azure_sql", "sales.csv"),
     "Deals", "sales", "closed deals revenue amount by region product"),
    ("support-tickets", "support_tickets", ("postgres", "support_tickets.csv"),
     "Support tickets", "support", "support tickets resolution hours by region priority"),
    ("storefront", "storefront_orders", ("mysql", "storefront_orders.csv"),
     "Storefront", "sales", "storefront orders amount by region category"),
    ("warehouse", "warehouse_sales", ("synapse", "warehouse_sales.csv"),
     "Warehouse", "sales", "warehouse units by region sku"),
)


def _csv_table(kind: str, filename: str) -> dict:
    with open(demo_fixture_path(kind, filename), newline="") as fh:
        rows = list(_csv.reader(fh))
    header, data = rows[0], rows[1:]
    typed = [[float(c) if c.replace(".", "", 1).lstrip("-").isdigit() else c
              for c in row] for row in data]
    return {"columns": header, "rows": typed}


def fixture_manifest(alice_oid: str, bob_oid: str) -> dict:
    """The SAME data the demo serves, composed into the LIVE scope under real oids.
    Group names cannot work there (a real user's groups are just their oid on this
    rig), so ACLs are translated: all-staff -> both oids, deal-team -> alice only.
    SQL fixtures become kind=csv inline tables (the fixture-backed cloud factories
    are demo-compose-only by design, LAW 7 / demo_backing.py).

    group_oids is DERIVED from DEMO_USER_GROUPS (the single source of truth for
    who belongs to which demo group) rather than a second, hand-copied mapping.
    This is on the code path that decides who can see restricted documents (LAW
    2 - permission-faithful retrieval, the product's core invariant): a second
    definition would be correct today but could drift silently the moment
    DEMO_USER_GROUPS changes, and the user-mode LAW 2 assertions would quietly
    stop testing what they claim to test."""
    principal_oids = {"alice": alice_oid, "bob": bob_oid}
    group_oids: dict[str, list[str]] = {}
    for principal, groups in DEMO_USER_GROUPS.items():
        for group in groups:
            group_oids.setdefault(group, []).append(principal_oids[principal])

    def to_oids(acl):
        out = []
        for g in acl:
            out.extend(group_oids.get(g, [g]))
        return sorted(set(out))

    stores = []
    for doc in DEMO_MANIFEST["stores"]:
        if doc["id"] not in ("hr-wiki", "fin-ledger"):
            continue
        seeds = [dict(s, acl=to_oids(s["acl"])) for s in doc["config"]["seed"]]
        stores.append({"id": doc["id"], "kind": "local",
                       "business_unit": doc["business_unit"],
                       "acl": to_oids(doc["acl"]), "title": doc["title"],
                       "description": doc["description"],
                       "config": {"seed": seeds}})
    for store_id, table, (kind, filename), title, bu, desc in _FIXTURE_TABLES:
        stores.append({"id": store_id, "kind": "csv", "business_unit": bu,
                       "acl": [alice_oid, bob_oid], "title": title,
                       "description": desc,
                       "config": {"tables": {table: _csv_table(kind, filename)}}})
    return {"tenant": "acme-fixture", "stores": stores}


def compose_fixture_catalog(base: str, cookie: str, alice_oid: str, bob_oid: str) -> None:
    import json as _json
    import urllib.request as _rq
    body = _json.dumps({"manifest": fixture_manifest(alice_oid, bob_oid)}).encode()
    req = _rq.Request(base + "/router/compose", data=body, method="POST",
                      headers={"Content-Type": "application/json",
                               "Cookie": f"dbs_session={cookie}"})
    with _rq.urlopen(req, timeout=120) as resp:
        assert resp.getcode() == 200, f"fixture compose failed: {resp.getcode()}"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="DBSearch capability use-case suite")
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--demo", action="store_true", help="demo mode only")
    parser.add_argument("--user", action="store_true", help="signed-in mode only")
    parser.add_argument("--only", default="", help="comma-separated capability numbers")
    parser.add_argument("--session", default="", help="dbs_session cookie for user mode")
    return parser.parse_args(argv)


def selected_modes(args) -> tuple[str, ...]:
    if args.demo and not args.user:
        return ("demo",)
    if args.user and not args.demo:
        return ("user",)
    return ("demo", "user")


def selected_capabilities(args):
    if not args.only:
        return CAPABILITIES
    nums = [int(n) for n in args.only.split(",") if n.strip().isdigit()]
    return by_num(nums)


def main(argv=None) -> int:
    args = parse_args(argv)
    modes = selected_modes(args)
    caps = selected_capabilities(args)

    print(f"DBSearch capability suite - {len(caps)} capabilities x {len(modes)} mode(s)\n")

    # User mode needs TWO real signed-in identities: alice (the primary asker) and
    # bob (the LAW 2 re-ask - an anonymous re-ask 401s under a real login, so the
    # suite could never pass without a second signed-in session). alice's session
    # also composes the fixture catalog into the live scope under her and bob's
    # real oids, so the SAME data the demo serves is reachable in user mode too.
    alice_cookie = bob_cookie = None
    user_mode_error = None
    if "user" in modes:
        try:
            if args.session:
                # A pre-supplied cookie has no oid, so the fixture compose (which
                # needs both oids to build the ACLs) is the caller's responsibility.
                alice_cookie = args.session
            else:
                alice_cookie, alice_oid = mint_session(args.base, "alice")
            bob_cookie, bob_oid = mint_session(args.base, "bob")
            if not args.session:
                compose_fixture_catalog(args.base, alice_cookie, alice_oid, bob_oid)
        except KeyError as exc:
            user_mode_error = (f"missing env {exc} (need AUTH_* + E2E_ALICE_PW + "
                               "E2E_BOB_PW, server with DBSEARCH_DEV_SEED=1)")
        except Exception as exc:                       # noqa: BLE001
            user_mode_error = f"user mode setup failed: {exc}"
        if user_mode_error:
            print(f"  ! user mode skipped: {user_mode_error}\n")

    results = []
    for cap in caps:
        for mode in modes:
            if mode not in cap.modes:
                continue
            if mode == "user" and user_mode_error:
                # #336 review finding 2: a mode that never ran must be a recorded
                # FAILURE, not a `continue` that drops it from `results` entirely.
                # Dropping it let a demo-only run print "N/N passed" and the PASSED
                # banner while having tested only half the matrix - indistinguishable
                # from a real green run. The explanatory message is still useful, so
                # it stays in the detail instead of only the console line above.
                result = CapabilityResult(cap.num, mode, False,
                                          f"user mode did not run: {user_mode_error}")
            elif mode == "user":
                result = run_capability(cap, "user", args.base, alice_cookie,
                                        unauth_session=bob_cookie)
            else:
                result = run_capability(cap, "demo", args.base, None)
            results.append(result)
            mark = "✓" if result.passed else "✗"
            line = f"  {mark} [{mode:4}] {cap.num} {cap.name}"
            if not result.passed:
                line += f"\n        {result.detail}"
                if cap.question:
                    line += f"\n        question: {cap.question!r}"
            print(line)

    failed = [r for r in results if not r.passed]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")

    if failed:
        print("\nFAILED:")
        for r in failed:
            print(f"  capability {r.num} [{r.mode}]: {r.detail}")
        return 1
    print("\n#337 CAPABILITY SUITE PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
