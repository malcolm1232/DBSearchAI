"""`dbsearch` CLI — E9's "one command" surface over the /router API (card #107).

    dbsearch compose up -f stores.yml     # join everything in the manifest
    dbsearch probe -f stores.yml --id X   # test one entry's connection
    dbsearch ask "question" [--store id]  # routed (or manually pinned) federated ask
    dbsearch sync <store_id>              # delta re-sync a connector-backed store
    dbsearch catalog                      # the caller-visible catalog tree

The CLI talks to a RUNNING data-plane server (DBSEARCH_URL, default the self-host
:8080) as a dev-auth user (DBSEARCH_USER / --user). The manifest is parsed
client-side (YAML via pyyaml when installed; .json needs nothing), but `${ENV}`
secret placeholders pass through UNRESOLVED — they resolve server-side, in the
tenant (LAW 1: the machine running the CLI never needs the data plane's secrets).
Cloud kinds without a live provider (bigquery/redshift/azure_sql — the
credential-gated half of E9) compose as SKIPPED with a reason, never faked.

The HTTP layer is an injectable `transport(method, path, payload, user)` so tests
drive the real FastAPI app through TestClient.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Callable, Optional

Transport = Callable[[str, str, Optional[dict], str], "tuple[int, dict]"]

DEFAULT_URL = os.environ.get("DBSEARCH_URL", "http://127.0.0.1:8080")
DEFAULT_USER = os.environ.get("DBSEARCH_USER", "alice")


def _load_manifest_file(path: str) -> dict:
    text = open(path).read()
    if path.endswith(".json"):
        return json.loads(text)
    try:
        import yaml
    except ImportError:                                    # pragma: no cover
        raise SystemExit("stores.yml needs pyyaml (pip install pyyaml) — "
                         "or use a .json manifest")
    return yaml.safe_load(text)


def _urllib_transport(base_url: str) -> Transport:
    def transport(method: str, path: str, payload: Optional[dict], user: str):
        req = urllib.request.Request(
            base_url.rstrip("/") + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json", "X-DBSearch-User": user},
            method=method,
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, {"detail": body}
    return transport


def _fail(status: int, body: dict) -> int:
    print(f"error {status}: {body.get('detail', body)}")
    return 1


def _cmd_compose(args, transport: Transport) -> int:
    manifest = _load_manifest_file(args.file)
    status, body = transport("POST", "/router/compose", {"manifest": manifest}, args.user)
    if status != 200:
        return _fail(status, body)
    for s in body.get("stores", []):
        print(f"  ✓ {s['store_id']:<20} {s['kind']:<14} {s.get('freshness', '')}")
    for s in body.get("skipped", []):
        print(f"  ◌ {s['id']:<20} skipped — {s['reason']}")
    print(f"composed tenant {body.get('tenant', '?')}: "
          f"{len(body.get('stores', []))} live, {len(body.get('skipped', []))} skipped")
    return 0


def _cmd_probe(args, transport: Transport) -> int:
    manifest = _load_manifest_file(args.file)
    entry = next((e for e in manifest.get("stores", []) if e.get("id") == args.id), None)
    if entry is None:
        print(f"no entry {args.id!r} in {args.file}")
        return 1
    status, body = transport("POST", "/router/probe", {"entry": entry}, args.user)
    if status != 200:
        return _fail(status, body)
    if not body.get("available"):
        print(f"  ◌ {args.id}: {body.get('reason', 'unavailable')}")
        return 1
    p = body["profile"]
    print(f"  ✓ {p['store_id']} kind={p['kind']} caps={','.join(p['capabilities'])} "
          f"freshness={p.get('freshness', '')}")
    return 0


def _cmd_ask(args, transport: Transport) -> int:
    payload = {"question": args.question}
    if args.store:
        payload["store"] = args.store
    status, body = transport("POST", "/router/ask", payload, args.user)
    if status != 200:
        return _fail(status, body)
    r = body.get("routing", {})
    print(f"[{r.get('query_type')} · {r.get('method')} · conf {r.get('confidence')}] "
          f"{r.get('reason', '')}")
    for sq in r.get("sub_queries", []):
        tgt = ", ".join(s["store_id"] for s in sq.get("stores", [])) or "no accessible source"
        print(f"  ↳ '{sq['question']}' → {tgt}")
    print(body.get("answer", ""))
    if body.get("disclosure"):
        print(f"⚠ {body['disclosure']}")
    return 0


def _cmd_sync(args, transport: Transport) -> int:
    status, body = transport("POST", f"/router/stores/{args.store_id}/sync", None, args.user)
    if status != 200:
        return _fail(status, body)
    print(f"  ✓ {body['store_id']}: {body['docs_synced']} docs synced, {body['freshness']}")
    return 0


def _cmd_catalog(args, transport: Transport) -> int:
    status, body = transport("GET", "/router/catalog", None, args.user)
    if status != 200:
        return _fail(status, body)
    print(f"tenant {body.get('tenant', '?')}")
    for bu in body.get("business_units", []):
        print(f"  {bu['id']}")
        for src in bu.get("sources", []):
            for st in src.get("stores", []):
                print(f"    {st['store_id']:<20} {st['kind']:<14} "
                      f"{','.join(st['capabilities']):<20} {st.get('freshness', '')}")
    return 0


def main(argv: "list[str] | None" = None, transport: Optional[Transport] = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=DEFAULT_URL, help="data-plane server URL")
    common.add_argument("--user", default=DEFAULT_USER, help="dev-auth user (X-DBSearch-User)")

    ap = argparse.ArgumentParser(prog="dbsearch",
                                 description="compose + query the DBSearch federation")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_compose = sub.add_parser("compose", parents=[common],
                               help="compose the federation from a manifest")
    p_compose.add_argument("action", choices=["up"])
    p_compose.add_argument("-f", "--file", default="stores.yml")

    p_probe = sub.add_parser("probe", parents=[common],
                             help="test one manifest entry's connection")
    p_probe.add_argument("-f", "--file", default="stores.yml")
    p_probe.add_argument("--id", required=True)

    p_ask = sub.add_parser("ask", parents=[common],
                           help="federated ask (routed, or pinned via --store)")
    p_ask.add_argument("question")
    p_ask.add_argument("--store", default=None)

    p_sync = sub.add_parser("sync", parents=[common],
                            help="delta re-sync a connector-backed store")
    p_sync.add_argument("store_id")

    sub.add_parser("catalog", parents=[common],
                   help="show the caller-visible catalog tree")

    args = ap.parse_args(argv)
    transport = transport or _urllib_transport(args.url)
    handlers = {"compose": _cmd_compose, "probe": _cmd_probe, "ask": _cmd_ask,
                "sync": _cmd_sync, "catalog": _cmd_catalog}
    return handlers[args.cmd](args, transport)


if __name__ == "__main__":                                  # pragma: no cover
    sys.exit(main())
