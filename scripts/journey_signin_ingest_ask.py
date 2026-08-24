#!/usr/bin/env python3
"""The end-user journey, driven exactly as a customer hits it (#548).

    PYTHONPATH=src python3 scripts/journey_signin_ingest_ask.py

Needs: a server on :8080 launched with DBSEARCH_DEV_SEED=1 and a real login configured
(AUTH_TENANT_ID/AUTH_CLIENT_ID/AUTH_CLIENT_SECRET), plus E2E_ALICE_PW/E2E_BOB_PW from
secrets/entra_test_users.env. Use a backend with a REAL chat model - memory-ollama-embed -
because the `memory` backend's ExtractiveLlm cannot demonstrate talking to anything.

WHY THIS EXISTS: e2edbs and the golden pack both exercise BAKED demo stores on a pre-seeded
corpus. Neither has ever signed a user in and uploaded a file, which is why the LAW 2
metadata leak (#549) survived a 179/179 suite. This is the only check that walks the path a
customer walks.

    sign in for real  ->  upload YOUR OWN document  ->  ask it a question  ->  and prove
    the person next to you cannot see it.

No dev header anywhere: every call below rides a real `dbs_session` cookie minted from a
real Entra ROPC sign-in, which is the same token path the browser sign-in ends in.
"""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = "http://127.0.0.1:8080"
DOMAIN = "QuantifyMeAI.onmicrosoft.com"
# The tenant app's IDENTITY scopes ("openid profile email offline_access" - what the product's
# own login_url asks for) are NOT consented for these test users: ROPC gets AADSTS65001, because
# a password grant has no way to show the consent screen a browser would. The Azure SQL
# delegation scope IS consented, and returns the SAME user's id_token + refresh_token, so it is
# what the e2edbs --live-entra tier already uses. The identity minted below is the real
# alice-test/bob-test principal either way; only the consent round differs, and the browser leg
# is re-checked for real on the canvas in step 5.
SCOPE = "openid profile offline_access https://database.windows.net/user_impersonation"

# A fact that exists in NO corpus anywhere - so a correct answer cannot come from the baked
# demo documents, the model's parameters, or luck. This is the whole point of the check.
MARKER = uuid.uuid4().hex[:8]
DOC_TEXT = f"""Northwind Robotics - Field Service Bulletin FSB-{MARKER}

Subject: Meridian-7 actuator retrofit programme, Q3 2026

Following the retrofit programme completed in August 2026, the measured failure rate of the
Meridian-7 actuator fell to 0.42 percent per thousand operating hours, down from 3.10 percent
before the retrofit.

The warranty reserve held against the Meridian-7 line is 1.83 million euros.

The retrofit was authorised by Priya Raghunathan, VP of Field Operations, under change order
CO-{MARKER}. Units still awaiting retrofit as of 30 September 2026: 214.
"""
EXPECT = {"failure rate": "0.42", "warranty reserve": "1.83", "units awaiting": "214",
          "authoriser": "Raghunathan"}


def _post(path, data, cookie=None, headers=None, timeout=180):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    if cookie:
        h["Cookie"] = f"dbs_session={cookie}"
    body = json.dumps(data).encode() if not isinstance(data, bytes) else data
    req = urllib.request.Request(BASE + path, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def _get(path, cookie=None, timeout=60):
    h = {"Cookie": f"dbs_session={cookie}"} if cookie else {}
    req = urllib.request.Request(BASE + path, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode("utf-8", "replace")}


def ropc(tenant, cid, secret, upn, pw):
    body = urllib.parse.urlencode({
        "grant_type": "password", "client_id": cid, "client_secret": secret,
        "username": upn, "password": pw, "scope": SCOPE}).encode()
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"error": f"HTTP {e.code}"}


def jwt_oid(id_token):
    import base64
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def sign_in(who, tenant, cid, secret, pw):
    """The real thing: password grant against the tenant, then vault the refresh token and
    take the session cookie the server mints - no X-DBSearch-User anywhere."""
    upn = f"{who}-test@{DOMAIN}"
    tok = ropc(tenant, cid, secret, upn, pw)
    if "id_token" not in tok:
        raise SystemExit(f"FATAL sign-in failed for {upn}: {tok.get('error_description', tok)[:300]}")
    claims = jwt_oid(tok["id_token"])
    oid = claims.get("oid") or claims.get("sub")
    body = json.dumps({"oid": oid, "refresh_token": tok.get("refresh_token", ""),
                       "name": claims.get("name", who), "email": upn}).encode()
    req = urllib.request.Request(BASE + "/auth/dev/seed", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.headers.get("Set-Cookie", "")
    m = re.search(r"dbs_session=([^;]+)", raw)
    if not m:
        raise SystemExit(f"FATAL no session cookie minted for {upn}")
    return oid, m.group(1), claims.get("name", who)


def upload(cookie, filename, text, acl, title):
    """multipart/form-data by hand - the same shape the browser's uploadDocument() sends."""
    b = f"----journey{uuid.uuid4().hex}"
    parts = []
    for key, val in [("acl", a) for a in acl] + [("title", title)]:
        parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{val}\r\n")
    head = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: text/plain\r\n\r\n")
    body = ("".join(parts) + head).encode() + text.encode() + f"\r\n--{b}--\r\n".encode()
    return _post("/admin/upload", body, cookie=cookie,
                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})


def main():
    import os
    tenant = os.environ["AUTH_TENANT_ID"]
    cid, secret = os.environ["AUTH_CLIENT_ID"], os.environ["AUTH_CLIENT_SECRET"]
    fails = []

    def check(ok, label, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail else ""))
        if not ok:
            fails.append(label)

    print(f"\n=== STEP 1 — log in for real (Entra ROPC -> vaulted session cookie) ===")
    a_oid, a_cookie, a_name = sign_in("alice", tenant, cid, secret, os.environ["E2E_ALICE_PW"])
    b_oid, b_cookie, b_name = sign_in("bob", tenant, cid, secret, os.environ["E2E_BOB_PW"])
    print(f"  alice oid={a_oid}  name={a_name!r}")
    print(f"  bob   oid={b_oid}  name={b_name!r}")
    s, me = _get("/auth/me", cookie=a_cookie)
    check(me.get("signed_in") and me.get("oid") == a_oid,
          "alice is signed in as herself", f"signed_in={me.get('signed_in')} oid={me.get('oid')}")
    check(a_oid != b_oid, "the two identities are genuinely different people")

    print(f"\n=== STEP 2 — alice uploads HER OWN document ===")
    s, up = upload(a_cookie, f"fsb-{MARKER}.txt", DOC_TEXT, [a_oid],
                   f"Field Service Bulletin FSB-{MARKER}")
    print(f"  POST /admin/upload -> {s}  {json.dumps(up)[:220]}")
    check(s == 200 and up.get("chunk_count", 0) > 0,
          "the document indexed", f"chunks={up.get('chunk_count')}")
    ext_id = up.get("external_id", "")

    s, docs = _get("/admin/documents", cookie=a_cookie)
    mine = [d for d in (docs if isinstance(docs, list) else docs.get("documents", []))
            if ext_id and ext_id in json.dumps(d)]
    check(bool(mine), "alice can see her document in her own corpus",
          f"{len(mine)} match(es)")

    print(f"\n=== STEP 3 — alice ASKS her document (this is 'talking to your data') ===")
    q = "What is the Meridian-7 actuator failure rate after the retrofit, and how large is the warranty reserve?"
    s, ans = _post("/chat", {"conv_id": f"journey-{MARKER}", "question": q}, cookie=a_cookie)
    text = json.dumps(ans)
    answer = ans.get("answer", "")
    print(f"  Q: {q}")
    print(f"  A: {answer[:600]}")
    check(s == 200, "the ask succeeded", f"HTTP {s}")
    for label, val in EXPECT.items():
        if label in ("failure rate", "warranty reserve"):
            check(val in answer, f"answer carries the {label} ({val})")
    cites = ans.get("citations", [])
    print(f"  citations: {json.dumps(cites)[:400]}")
    check(any(f"fsb-{MARKER}" in json.dumps(c).lower() or ext_id in json.dumps(c)
              for c in cites),
          "a citation resolves to the uploaded document")

    print(f"\n=== STEP 4 — bob is a different signed-in person and must NOT see it ===")
    # conv_id deliberately carries NO marker: the first cut of this script put the marker in
    # bob's conv_id, which /chat echoes back, and the check failed on my own input rather than
    # on a leak. A negative assertion is only worth having if the needle cannot arrive by post.
    s, bans = _post("/chat", {"conv_id": "bob-own-conversation", "question": q}, cookie=b_cookie)
    btext = json.dumps(bans)
    print(f"  A(bob): {bans.get('answer', '')[:400]}")
    check("0.42" not in btext, "bob never learns the failure rate")
    check("1.83" not in btext, "bob never learns the warranty reserve")
    check(MARKER not in btext, "bob never sees the document id or title")
    check("Raghunathan" not in btext, "bob never learns who authorised it")
    check(bans.get("corpus", {}).get("authorized_docs") == 0,
          "bob's own corpus count says zero, honestly",
          f"corpus={bans.get('corpus')}")

    # #549: the metadata plane, which is where this journey found a real leak.
    s, bdocs = _get("/admin/documents", cookie=b_cookie)
    check(ext_id not in json.dumps(bdocs) and MARKER not in json.dumps(bdocs),
          "bob's document listing does not name alice's doc", f"HTTP {s}")
    s, baudit = _get("/admin/audit", cookie=b_cookie)
    check(s == 403 and "Meridian" not in json.dumps(baudit),
          "bob cannot read alice's question text", f"HTTP {s}")
    for path in ("/admin/telemetry", "/admin/principals", "/admin/identities", "/admin/index"):
        s, _ = _get(path, cookie=b_cookie)
        check(s == 403, f"bob is refused {path}", f"HTTP {s}")

    print(f"\n=== and alice still sees her own corpus (the fix must not cost the owner) ===")
    s, adocs = _get("/admin/documents", cookie=a_cookie)
    check(ext_id in json.dumps(adocs), "alice still sees her own document", f"HTTP {s}")

    print("\n" + "=" * 72)
    if fails:
        print(f"RESULT: {len(fails)} FAILED — " + "; ".join(fails))
        return 1
    print("RESULT: the whole journey passed — signed in, ingested, answered, and isolated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
