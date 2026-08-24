"""Shared HTTP transport for eval runners that drive a live DBSearch server (#337, spec
2026-07-31 Task 9). Extracted from `scripts/usecase_runner.py`'s `_call` so the golden
suite runner does not duplicate a private helper from a sibling script, and so any
future eval runner has one canonical seam to import instead of re-copying it.

Pure stdlib (urllib) - no server or router imports, so this module never drags product
code into a script's import graph.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


def call(base: str, path: str, payload: "dict | None" = None, session: "str | None" = None,
         identity: "str | None" = None, timeout: int = 120,
         identity_header: str = "X-DBSearch-Demo-User"):
    """GET when payload is None, POST otherwise.

    `session` sends a `dbs_session` cookie (a real signed-in identity); `identity` sends
    the `identity_header` header (default `X-DBSearch-Demo-User`, a demo-scope principal)
    when there is no session.

    `X-DBSearch-Demo-User`, NOT `X-DBSearch-User`, is the default. The latter is the dev
    switcher, and #183 makes resolve_identity refuse it outright whenever a real login is
    configured - which is precisely the hosted-demo configuration. A caller keyed on the
    dev header therefore passes on a bare local rig and 401s on the box the demo actually
    runs on. The demo seam sits ABOVE the dev/real-login branch on purpose
    (api/auth.py:108-127) so an anonymous visitor can play the demo under a real login.

    `identity_header` lets a caller opt into `X-DBSearch-User` instead (Task 10, spec
    2026-07-31): on a bare local rig with no real login configured, that dev header
    resolves to a REAL (non-demo) identity oid, which is required for `/router/compose`
    and for `/router/ask` to route to the composed workspace rather than the baked demo
    catalog (`router_api.py`: `current_user` 403s any `demo:*` identity; a `demo:*`
    identity's `/router/ask` always targets `compose_demo_catalog`, never the per-owner
    workspace). Every existing call site defaults this parameter, so behavior is
    unchanged unless a caller passes it explicitly.

    The identity value is the bare principal name; under the demo header,
    resolve_identity namespaces it to `demo:<name>` and accepts only the fixed allowlist
    (api/auth.py:41); under the dev header it is used verbatim as a real oid.

    Returns (status, parsed-json-or-{}). An HTTPError's body is parsed too when
    possible (a 4xx/5xx still carries a JSON detail payload worth reading), falling
    back to {} when the body is not JSON.
    """
    headers = {"Content-Type": "application/json"}
    if session:
        headers["Cookie"] = f"dbs_session={session}"
    elif identity:
        headers[identity_header] = identity
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base + path, data=body, headers=headers,
        method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except Exception:
            return exc.code, {}


#: #491: a throwaway ask that exercises the chat model without being a pack question -
#: a pack question would pre-warm the #254 memo cache and change what run 1 measures.
WARM_QUESTION = "How many rows does the smallest table hold?"


def warm_rig(ask, tries: int = 6, needed: int = 2) -> tuple:
    """#491: throwaway asks until the rig answers cleanly `needed` times IN A ROW, or
    `tries` exhaust. Returns (asks_spent, ready).

    The first run after a server start reproducibly lost 5-8 points to Ollama cold-start
    (#483) - generations fail while the model loads and degrade silently to the keyword
    stub - so the rule was "discard the whole first run", ~8-20 wasted minutes per
    session. A clean answer is one that arrives without a degraded/timeout/error outcome;
    two in a row means both the embed and chat models are resident. Not ready after
    `tries` is REPORTED, never silent - the caller decides whether to trust run 1."""
    clean = 0
    for i in range(1, tries + 1):
        status, res = ask()
        outcomes = (res or {}).get("outcomes") or []
        ok = status == 200 and not any(
            (o.get("status") in ("timeout", "error"))
            or "degraded" in ((o.get("note") or "") + str(o.get("error") or ""))
            for o in outcomes)
        clean = clean + 1 if ok else 0
        if clean >= needed:
            return i, True
    return tries, False


def catalog_visible(resp_json: dict) -> bool:
    """Pure: true iff a `/router/catalog` response (router_api.py's `visible_tree`
    shape: business_units -> sources -> stores) names at least one store. Probes
    catalog visibility before trusting a run (spec section 3, #368): a
    per-owner-workspace rig can leave bob's catalog empty, which would make LAW
    2/leak checks pass VACUOUSLY green."""
    for bu in (resp_json or {}).get("business_units", []) or []:
        for src in bu.get("sources", []) or []:
            if src.get("stores"):
                return True
    return False
