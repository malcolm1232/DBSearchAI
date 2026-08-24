#!/usr/bin/env python3
"""e2edbs L1 — DBSearch.AI end-to-end verification (API drive + tally-vs-source).

Proves the router stack is really connected: compose stores, ask AS an identity,
and TALLY the answer/citations/proof/outcomes against the source of truth — then
prove an unauthorized identity is denied (LAW 2). Not a unit test: it drives the
live server over HTTP exactly as the canvas does.

Run:  python3 ~/.claude/skills/e2edbs/e2e_dbsearch.py [--azure] [--base URL]
Exit: 0 = all tallied & gated; 1 = mismatch/leak.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ALICE, BOB = "alice", "bob"
# #193: the live BigQuery demo project/dataset (spec 2026-07-11-gcp-sweep). Overridable by env.
BQ_PROJECT = os.environ.get("GCP_PROJECT", "your-gcp-project")
BQ_DATASET = os.environ.get("GCP_DATASET", "dbsearch_demo")

# #712 - the live PUBLIC Drive folder the gdrive connector is verified against. It holds a
# single text file, which is deliberate: one 45KB .txt is ground truth a human can hold in
# their head, where 30MB of PDFs is only ground truth a script can assert about.
GD_LINK = os.environ.get(
    "E2EDBS_GDRIVE_LINK",
    "https://drive.google.com/drive/folders/1Fln4Vx1MlBm8hCTVBrHO0Z5YFDsdvYjS")
# The question and its fact must be answerable ONLY from that folder. This pair names a board
# card and an effort estimate that appear nowhere in the demo catalog and that no language
# model can guess, so a pass DISCRIMINATES retrieval from fluent invention - which is exactly
# what a reasonable-sounding question like "what do the notes say about databases" would not.
GD_Q = os.environ.get(
    "E2EDBS_GDRIVE_Q",
    "according to the DBSearch session notes, how many full sessions is card #689 "
    "estimated to take?")
GD_FACT = os.environ.get("E2EDBS_GDRIVE_FACT", "2-3 full sessions")
_fails = []


def call(base, path, data=None, user=ALICE, timeout=60, cookie=None):
    h = {"Content-Type": "application/json"}
    if cookie is not None:                      # a real Entra session (--live-entra) wins
        h["Cookie"] = f"dbs_session={cookie}"
    elif user is not None:                      # user=None → anonymous (no identity header)
        h["X-DBSearch-User"] = user
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base + path, data=body, headers=h,
                                 method="POST" if data is not None else "GET")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.getcode(), json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {}


def check(name, cond, detail=""):
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(f"{name}: {detail}")
    return cond


def proofs(resp, kind):
    return [c["proof"] for c in resp.get("citations", [])
            if c.get("proof", {}).get("kind") == kind]


# --- LIVE ENTRA (--live-entra, #156 Task 6/7): sign real test users into the tenant app
# (ROPC — fine for MFA-free test accounts, never for real users) and prove the row-count
# answer is really gated by Azure SQL RLS under each user's own delegated token, not a
# shared service credential. This is the reproducible done-scoreboard for CONTEXT.md §3
# invariants 3/4 on Azure SQL — see docs/ADR/0006-delegated-auth-obo.md addendum. ---
ENTRA_DOMAIN = "QuantifyMeAI.onmicrosoft.com"
ENTRA_TEST_USERS = {"alice": f"alice-test@{ENTRA_DOMAIN}", "bob": f"bob-test@{ENTRA_DOMAIN}"}
DB_IMPERSONATION_SCOPE = "https://database.windows.net/user_impersonation"


def _ropc_token(tenant, client_id, client_secret, upn, pw, scope):
    """Resource-owner-password sign-in — the SAME token endpoint the server's
    EntraRefreshExchange redeems against later, just with a password grant up front."""
    body = urllib.parse.urlencode({
        "grant_type": "password", "client_id": client_id, "client_secret": client_secret,
        "username": upn, "password": pw, "scope": scope}).encode()
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"error": f"HTTP {e.code}"}


def _jwt_oid(jwt):
    part = jwt.split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    return payload.get("oid") or payload.get("sub")


def _dev_seed(base, oid, refresh_token, name, email, timeout=20):
    """POST /auth/dev/seed (Task 3's test-only seam, requires DBSEARCH_DEV_SEED=1 on the
    server) to vault the ROPC refresh token and mint a real session cookie — no
    X-DBSearch-User dev header involved, this is the actual sign-in cookie path."""
    body = json.dumps({"oid": oid, "refresh_token": refresh_token,
                       "name": name, "email": email}).encode()
    req = urllib.request.Request(base + "/auth/dev/seed", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw_cookie = resp.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"/auth/dev/seed failed ({e.code}) — is DBSEARCH_DEV_SEED=1 set "
                           "on the server?") from e
    m = re.search(r"dbs_session=([^;]+)", raw_cookie)
    if not m:
        raise RuntimeError(f"/auth/dev/seed did not return a dbs_session cookie: {raw_cookie!r}")
    return m.group(1)


def _rerun_row_count(base, resp, cookie):
    """Follow the SAME tally pattern as the sql-path check: re-run the proof's exact SQL
    (as this identity) and count the physical rows returned — the RLS signal is rows
    disappearing, not a different aggregate."""
    sql = proofs(resp, "sql")
    if not sql:
        return None
    p = sql[0]
    _, x = call(base, "/router/rerun",
               {"store_id": p["store_id"], "sql": p["sql"], "token": p["rerun_token"]},
               cookie=cookie)
    rows = x.get("rows")
    return len(rows) if isinstance(rows, list) else None


def _admin_ground_truth_count():
    """Raw admin-token COUNT(*) on dbo.sales — same az-CLI + pyodbc technique as
    scripts/provision_sql_users.py's admin_conn(). Returns None (never raises) if az/pyodbc
    aren't available in this environment; callers must treat None as 'ground truth unknown'."""
    server = os.environ.get("AZURE_SQL_SERVER", "")
    database = os.environ.get("AZURE_SQL_DATABASE", "")
    if not server or not database:
        return None
    try:
        import struct

        import pyodbc
        tok = subprocess.check_output(
            ["az", "account", "get-access-token", "--resource",
             "https://database.windows.net/", "--query", "accessToken", "-o", "tsv"],
            timeout=30).decode().strip()
        tok_bytes = tok.encode("utf-16-le")
        packed = struct.pack("<I", len(tok_bytes)) + tok_bytes
        conn = pyodbc.connect(
            f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{server},1433;"
            f"Database={database};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=75",
            attrs_before={1256: packed}, timeout=75)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM dbo.sales")
        return int(cur.fetchone()[0])
    except Exception as exc:
        print(f"  (admin ground truth unavailable: {exc})")
        return None


def _bigquery_ground_truth(project, dataset):
    """#193: independent ground truth straight from BigQuery via ADC - the anti-'trust the answer'
    guard (rule 1). Returns {region: int(total_amount)} or None if the SDK/ADC is unavailable
    (the tally then reports the gap rather than passing on a guess)."""
    try:
        from google.cloud import bigquery
    except Exception:
        return None
    try:
        client = bigquery.Client(project=project)
        rows = client.query(
            f"SELECT region, ROUND(SUM(amount)) AS t "
            f"FROM `{project}.{dataset}.sales` GROUP BY region").result()
        return {str(r.region).lower(): int(r.t) for r in rows}
    except Exception:
        return None


def live_entra(base):
    """--live-entra: sign alice-test/bob-test into the real tenant app, compose an
    azure_sql store WITH the entra_refresh delegation block (the canvas toggle's exact
    block), and prove: (a) alice sees more sales rows than bob (Azure SQL RLS enforcing
    against each user's own delegated token, not a shared credential), (b) alice's count
    tallies against an admin-token ground truth, (c) an anonymous/dev-header ask on the
    SAME store is denied with a 'sign in' disclosure. Returns True only if every check
    passed; False if it ran but something failed; None if preconditions were unmet (the
    caller should treat None as fatal — the mode was requested but never actually ran)."""
    print("\nLIVE ENTRA (--live-entra: #156 live proof of CONTEXT.md §3 invariants 3/4)")
    tenant = os.environ.get("AUTH_TENANT_ID", "")
    client_id = os.environ.get("AUTH_CLIENT_ID", "")
    client_secret = os.environ.get("AUTH_CLIENT_SECRET", "")
    alice_pw = os.environ.get("E2E_ALICE_PW", "")
    bob_pw = os.environ.get("E2E_BOB_PW", "")
    missing = [n for n, v in (("AUTH_TENANT_ID", tenant), ("AUTH_CLIENT_ID", client_id),
                              ("AUTH_CLIENT_SECRET", client_secret),
                              ("E2E_ALICE_PW", alice_pw), ("E2E_BOB_PW", bob_pw)) if not v]
    if missing:
        print(f"  FATAL: --live-entra needs {missing} in env (never hard-coded — LAW 1)")
        return None, {}

    cookies, oids = {}, {}
    for who, upn, pw in (("alice", ENTRA_TEST_USERS["alice"], alice_pw),
                         ("bob", ENTRA_TEST_USERS["bob"], bob_pw)):
        tok = _ropc_token(tenant, client_id, client_secret, upn, pw,
                          f"openid profile offline_access {DB_IMPERSONATION_SCOPE}")
        if "refresh_token" not in tok or "id_token" not in tok:
            print(f"  FATAL: ROPC sign-in failed for {upn}: "
                 f"{tok.get('error_description', tok)!r:.200}")
            return None, {}
        oid = _jwt_oid(tok["id_token"])
        try:
            cookie = _dev_seed(base, oid, tok["refresh_token"], who, upn)
        except RuntimeError as exc:
            print(f"  FATAL: {exc}")
            return None, {}
        cookies[who] = cookie
        oids[who] = oid
        print(f"  ✓ signed in {upn} (oid={oid[:8]}...) and vaulted the refresh token")

    # compose the azure_sql store WITH delegation — identical shape to the canvas's
    # entryOf()/manifest() when the require_signin toggle is on (server/src/dbsearch/
    # server/static/canvas.html)
    # ACL = the signed-in users' own OIDs (a user's principals always include their oid;
    # /auth/dev/seed registers no Graph groups, and this mirrors the canvas demo's
    # ACL=[alice_oid,bob_oid] from #186) — row-level trimming is the SOURCE's job (RLS).
    manifest = {"tenant": "acme", "stores": [{
        "id": "sales-entra", "kind": "azure_sql", "mode": "pushdown",
        "business_unit": "sales", "acl": [oids["alice"], oids["bob"]],
        "title": "Azure SQL sales (entra)",
        "description": "sales rows visible to the signed-in user",
        "config": {"server": "${AZURE_SQL_SERVER}", "database": "${AZURE_SQL_DATABASE}",
                   "user": "${AZURE_SQL_USER}", "password": "${AZURE_SQL_PASSWORD}",
                   "use_odbc": True, "tables": ["sales"]},
        "delegation": {"kind": "entra_refresh", "tenant_id": "${AUTH_TENANT_ID}",
                      "client_id": "${AUTH_CLIENT_ID}",
                      "client_secret": "${AUTH_CLIENT_SECRET}"}}]}
    # compose as a signed-in identity — the dev header is refused under an AUTH_* server
    # (#183), which is the only server this mode can run against (#185)
    code, _ = call(base, "/router/compose", {"manifest": manifest}, cookie=cookies["alice"])
    if not check("compose azure_sql store + entra_refresh delegation", code == 200, str(code)):
        return False, {}

    question = {"question": "list all sales rows", "store": "sales-entra"}
    _, ra = call(base, "/router/ask", question, cookie=cookies["alice"])
    _, rb = call(base, "/router/ask", question, cookie=cookies["bob"])
    n_alice = _rerun_row_count(base, ra, cookies["alice"])
    n_bob = _rerun_row_count(base, rb, cookies["bob"])
    ok_rls = check("alice (unfiltered) sees more sales rows than bob (RLS, real per-user token)",
                   n_alice is not None and n_bob is not None and n_alice > n_bob,
                   f"alice={n_alice} bob={n_bob}")

    gt = _admin_ground_truth_count()
    ok_tally = check("alice's row count tallies vs admin-token ground truth",
                     gt is not None and n_alice == gt, f"alice={n_alice} admin_ground_truth={gt}")

    code_anon, ranon = call(base, "/router/ask", question, user=None)
    code_dev, rdev = call(base, "/router/ask", question, user=ALICE)   # dev header, no cookie
    ok_deny = check(
        "anonymous/dev-header ask on the entra store is denied (0 rows + 'sign in')",
        code_anon in (401, 403) and code_dev in (401, 403)
        and "sign in" in json.dumps(ranon).lower() and "sign in" in json.dumps(rdev).lower(),
        f"anon={code_anon}:{json.dumps(ranon)[:80]} dev={code_dev}:{json.dumps(rdev)[:80]}")

    passed = bool(ok_rls and ok_tally and ok_deny)
    print(f"  {'PASS' if passed else 'FAIL'} — live-entra scoreboard "
         f"{'proves' if passed else 'does NOT yet prove'} #156 query-as-user live")
    return passed, cookies


# --- PRODUCT CONFORMANCE (CONTEXT.md §3): does this run adhere to the "John signs into
# his tenant and queries as himself" PRODUCT story, or is it still a DEMO shortcut? ---
_gaps = []


def _gdrive_ground_truth(link):
    """The folder's REAL text, read straight from Google over raw HTTP (#712).

    Deliberately does NOT import GDriveConnector. A probe built out of the code under test
    agrees with that code by construction - if the connector mis-parses a link or drops a
    file, a connector-based "ground truth" would drop it in exactly the same way and the
    tally would still pass. Only Google can disagree with the product, so the bytes have to
    come from Google by an independent path.

    Covers BOTH retrieval branches, because the product does: binaries via `alt=media` and
    native Google Docs via `files.export`. Skipping natives - as this did until 260817, when
    the test folder contained none - silently narrows the tally to whatever happens to be a
    real file, so a snippet quoted from a Google Doc reads as "not in the live bytes" and
    fails a correct product. It passed only by luck: every shown snippet came from the .txt.

    Recurses into subfolders for the same reason - the connector does, so ground truth that
    stopped at the top level would call a correctly-ingested nested document a hallucination.

    Returns {title: text}, or None when GOOGLE_API_KEY is absent (the caller reports that as
    a skip, never as a pass).
    """
    key = os.environ.get("GOOGLE_API_KEY", "")
    if not key:
        return None
    m = re.search(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)", link)
    folder = m.group(1) if m else link
    _FOLDER = "application/vnd.google-apps.folder"
    out, queue = {}, [folder]
    while queue:
        fid, page = queue.pop(0), None
        while True:
            q = {"q": f"'{fid}' in parents and trashed = false",
                 "fields": "nextPageToken,files(id,name,mimeType)",
                 "pageSize": "1000", "key": key}
            if page:
                q["pageToken"] = page
            with urllib.request.urlopen(
                    "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(q),
                    timeout=60) as r:
                body = json.load(r)
            for f in body.get("files", []):
                mime = f["mimeType"]
                if mime == _FOLDER:
                    queue.append(f["id"])
                    continue
                if mime.startswith("application/vnd.google-apps"):
                    # No bytes exist for a native Doc, so alt=media cannot serve it - this is
                    # the files.export branch, confirmed live on 260817. Export to text/plain,
                    # which is what the connector asks for too.
                    u = (f"https://www.googleapis.com/drive/v3/files/{f['id']}/export"
                         f"?mimeType=text/plain&key={key}")
                else:
                    u = f"https://www.googleapis.com/drive/v3/files/{f['id']}?alt=media&key={key}"
                try:
                    with urllib.request.urlopen(u, timeout=120) as fr:
                        out[f["name"]] = fr.read().decode("utf-8", "replace")
                except urllib.error.HTTPError:
                    continue                    # binary/oversized/unexportable: not text truth
            page = body.get("nextPageToken")
            if not page:
                break
    return out


def gdrive_checks(B, call_fn):
    """#712 section: a composed PUBLIC-Drive store answers from LIVE Drive content, every
    cited snippet is really in the file's bytes, and an unauthorized identity learns nothing.

    The tally here is the document analogue of the SQL re-run: for SQL we re-execute the
    proof and compare rows; for a document we go back to Google, fetch the file, and require
    that each snippet the product showed actually occurs in it. That is what separates
    "retrieved" from "generated a plausible sentence about a Drive folder"."""
    print(f"\n7. live Google Drive (#712) — alice asks a question only this folder answers")
    # ﻿ is stripped, not treated as whitespace: Drive's text/plain export of a NATIVE
    # Google Doc begins with a UTF-8 BOM, and Python's `\s` does not match it (it is a format
    # character). Leaving it in makes an invisible byte decide whether a tally passes.
    norm = lambda s: re.sub(r"\s+", " ", (s or "").replace("﻿", "")).strip()   # noqa: E731

    # Deliberately NOT pinned with "store": the azure/bigquery sections pin for determinism
    # against rival sales stores, but nothing here rivals a Drive folder, so leaving it
    # unpinned makes this prove ROUTING as well as retrieval - one more real claim, free.
    _, r = call_fn("/router/ask", {"question": GD_Q})
    ans = r.get("answer", "")
    check("gdrive answer carries the folder's fact", norm(GD_FACT).lower() in norm(ans).lower(),
          f"expected {GD_FACT!r} in {ans[:160]!r}")
    check("outcomes: gdrive-notes ok",
          any(o.get("store_id") == "gdrive-notes" and o.get("status") == "ok"
              for o in r.get("outcomes", [])), json.dumps(r.get("outcomes", []))[:160])
    foots = [f for f in (r.get("footnotes") or []) if f.get("store_id") == "gdrive-notes"]
    check("cited as a document with a real Drive uri",
          bool(foots) and all(f.get("kind") == "document" for f in foots)
          and any("drive.google.com" in (f.get("uri") or "") for f in foots),
          json.dumps(foots[:1])[:200])
    # Every [n] the prose prints must resolve to a footnote that exists (#233 dangling marker).
    marks = sorted({int(m) for m in re.findall(r"\[(\d+)\]", ans)})
    check("gdrive answer citations resolve (no dangling [n])",
          (not marks) or max(marks) <= len(r.get("footnotes") or []),
          f"markers={marks} footnotes={len(r.get('footnotes') or [])}")

    # ---- THE TALLY: every shown snippet must exist in the bytes Google serves ----
    truth = _gdrive_ground_truth(GD_LINK)
    if truth is None:
        check("gdrive TALLY vs live Drive bytes", False,
              "GOOGLE_API_KEY unset — ground truth was never read, so nothing was tallied")
    else:
        hay = norm(" ".join(truth.values()))
        shown = [f.get("snippet") or "" for f in foots] + \
                [e.get("content") or e.get("text") or "" for e in (r.get("evidence") or [])]
        shown = [s for s in shown if s.strip()]
        missing = [s[:60] for s in shown if norm(s)[:120] not in hay]
        check(f"gdrive TALLY: all {len(shown)} shown snippets are in the live file bytes",
              bool(shown) and not missing, f"{len(missing)} not found: {missing[:2]}")

    # ---- LAW 2: bob is not in the store's audience (acl=deal-team) ----
    _, rb = call_fn("/router/ask", {"question": GD_Q}, user=BOB)
    blob = json.dumps(rb)
    check("LAW 2: bob never sees the Drive content",
          GD_FACT.lower() not in (rb.get("answer") or "").lower(), (rb.get("answer") or "")[:120])
    check("LAW 2: bob never learns the store exists",
          "gdrive-notes" not in blob and "drive.google.com" not in blob,
          "store id or a Drive uri leaked into bob's response")


# --- CHAIN ASSERTION (--chain, #914 — launch-gate item 1, handover 260821c §4) ---
# "After any deploy, restart, or resync, the node's count equals the number of documents a
# signed-in owner can actually retrieve, and Ask answers from them." Every leg of this walk
# was proven BY HAND on prod 260821 (#883/#910/#911); this encodes exactly that walk so
# cause number six of the corpus-count scoreboard is caught by a machine, not a session.
#
# Standalone mode: composes NOTHING, reads the same surfaces the product renders
# (/admin/sources is what the Admin panel AND the canvas node read), and performs exactly
# one state-changing gesture — the Admin panel's own Resync — because surviving that
# gesture IS the assertion (#910's defect was durable zero after a quiet resync).


def _chain_ask(callf, q, facts, urifilter, label):
    """Ask through POST /chat — the REAL Ask-page surface (document plane), not a probe
    endpoint — and require the answer to (a) carry the corpus facts, (b) cite documents
    that CONTRIBUTED (`referenced`, #724: shown != used), (c) point into THIS corpus."""
    norm = lambda s: re.sub(r"\s+", " ", (s or "")).lower()   # noqa: E731
    code, r = callf("/chat", {"conv_id": f"chain-{uuid.uuid4()}", "question": q},
                    timeout=240)
    ans = r.get("answer", "")
    check(f"{label}: answer carries the corpus fact(s) {facts}",
          code == 200 and all(norm(f) in norm(ans) for f in facts),
          f"HTTP {code}, answer={ans[:140]!r}")
    cites = r.get("citations", [])
    refd = r.get("referenced", [])
    markers = sorted({int(m) for m in re.findall(r"\[(\d+)\]", ans)})
    if refd:
        # The strong form (#724: shown != used): the answer POINTS AT a corpus document.
        # Keyed on `referenced` itself, NOT on [n] markers in the prose — prod's gpt-oss
        # emits native citation tokens (#893) that this regex would never see, and the
        # server-computed referenced set is the contract either way.
        check(f"{label}: a REFERENCED citation resolves into the source corpus (#724)",
              any(urifilter in norm(cites[i - 1].get("uri"))
                  for i in refd if 1 <= i <= len(cites)),
              f"referenced={refd} uris={[c.get('uri') for c in cites][:3]}")
    elif markers:
        # Prose points at citations but the server says nothing contributed: a defect.
        check(f"{label}: markers present but referenced set empty", False,
              f"markers={markers} referenced=[]")
    else:
        # A marker-less model (bare local rig) carries no referenced set; fall back to
        # the SHOWN set and say so — the fact assertion above is what still ties the
        # answer to the corpus. Prod's models carry `referenced`, so prod runs are strong.
        print(f"  NOTE {label}: no referenced set and no [n] markers — asserting the "
              "SHOWN citation set")
        check(f"{label}: a shown citation resolves into the source corpus",
              any(urifilter in norm(c.get("uri")) for c in cites),
              f"uris={[c.get('uri') for c in cites][:3]}")
    check(f"{label}: no dangling [n] marker",
          (not markers) or max(markers) <= len(cites),
          f"markers={markers} citations={len(cites)}")


def _chain_visible(callf, urifilter):
    """The other side of the equality: the ACL-trimmed document list — the same identity
    port retrieval uses — filtered to this source by uri substring. By URI because the
    index has no per-source key: chunks carry no source_id at all, and SharePoint stamps
    documents `sharepoint:<drive_id>` while the registry keys `sharepoint:<tenant_id>`
    (#908). Until #908 lands, the uri is the only honest per-source discriminator."""
    code, docs = callf("/admin/documents")
    if code != 200 or not isinstance(docs, list):
        return code, None
    return code, [d for d in docs if urifilter in (d.get("uri") or "").lower()]


def chain_checks(B, args):
    """Returns True/False (ran; checks recorded via check()) or None (preconditions unmet
    — the walk NEVER ran, callers must fail loudly with exit 2, not report a pass)."""
    cookie = args.cookie or os.environ.get("DBS_SESSION_COOKIE") or None
    user = None if cookie else args.chain_user
    ident = "dbs_session cookie" if cookie else f"dev header {user!r}"

    def callf(path, data=None, timeout=60):
        return call(B, path, data, user=user, timeout=timeout, cookie=cookie)

    urifilter = args.chain_uri_filter.lower()
    facts = [f.strip() for f in args.chain_facts.split(",") if f.strip()]
    print(f"\nCHAIN ASSERTION (#914) — identity: {ident}")

    code, me = callf("/auth/me")
    if cookie:
        check("signed in (cookie resolves to a real session)",
              code == 200 and me.get("signed_in") is True, f"HTTP {code} {json.dumps(me)[:120]}")
    else:
        print(f"  (auth/me: HTTP {code}, real login "
              f"{'enabled' if me.get('enabled') else 'not configured'} — dev-header rig)")

    # ---- the registry row: the number the canvas node and Admin panel render ----
    code, rows = callf("/admin/sources")
    if code != 200 or not isinstance(rows, list):
        print(f"  UNTESTED: GET /admin/sources returned HTTP {code} — the walk never ran")
        return None
    if args.chain_source:
        cands = [r for r in rows if args.chain_source in r.get("source_id", "")]
    else:
        cands = [r for r in rows if r.get("kind") == "sharepoint"]
    cands = [r for r in cands if r.get("last_sync_at")]
    if not cands:
        print(f"  UNTESTED: no synced source matching "
              f"{args.chain_source or 'kind=sharepoint'!r} on this rig "
              f"(sources: {[r.get('source_id') for r in rows]}) — the walk never ran")
        return None
    src = max(cands, key=lambda r: r.get("doc_count") or 0)
    sid, n0 = src["source_id"], src.get("doc_count") or 0
    print(f"  source: {sid} (kind={src.get('kind')}, doc_count={n0}, "
          f"status={src.get('status')}, last_sync_at={src.get('last_sync_at')})")
    # PRECONDITION assertion, from ground truth (feedback 260819): a walk over an empty
    # corpus looks identical whether or not the chain is broken — it proves nothing.
    if n0 <= 0:
        print(f"  UNTESTED: {sid} has doc_count={n0}; an empty corpus cannot show a broken "
              "chain — connect/sync the source first")
        return None

    # ---- equality: registry count == documents this identity can actually retrieve ----
    code, vis = _chain_visible(callf, urifilter)
    if vis is None:
        print(f"  UNTESTED: GET /admin/documents returned HTTP {code} — the walk never ran")
        return None
    check(f"count == retrievable: registry doc_count ({n0}) equals ACL-visible documents "
          f"with uri~{urifilter!r} ({len(vis)})", n0 == len(vis),
          f"registry says {n0}, this identity can list {len(vis)} "
          f"({[d.get('title') for d in vis][:6]})")

    # ---- depth: a listed document is really chunked and servable, not just a row ----
    if vis:
        doc_id = vis[0].get("doc_external_id", "")
        if "/" in doc_id:
            # #915: a slashed doc id (folder connector) can never reach this route — the
            # raw path falls to the frontend catch-all and %2F 404s. SharePoint ids are
            # slash-free, so prod runs still get this probe. Skip LOUDLY, never silently.
            print(f"  NOTE: segments probe SKIPPED for {doc_id!r} — slashed doc ids are "
                  "unreachable on this route (#915)")
        else:
            code, segs = callf(
                f"/admin/documents/{urllib.parse.quote(doc_id, safe='')}/segments")
            check("a listed document serves real segments (chunked, retrievable)",
                  code == 200 and isinstance(segs, list) and len(segs) >= 1,
                  f"HTTP {code} for {doc_id!r}: {json.dumps(segs)[:100]}")

    # ---- Ask answers from them ----
    _chain_ask(callf, args.chain_q, facts, urifilter, "ask (pre-resync)")

    # ---- the Resync gesture: the exact Admin-panel action that durably zeroed the count
    # on 260820 (#910). A quiet incremental must leave count, retrievability and the
    # answer standing. ----
    if args.chain_no_resync:
        print("  (resync gesture skipped — --chain-no-resync)")
        return True
    code, handle = callf("/admin/resync", {"source_id": sid})
    if not check("resync accepted (202 + job handle)",
                 code == 202 and bool(handle.get("job_id")),
                 f"HTTP {code} {json.dumps(handle)[:120]}"):
        return False
    poll, waited, job = handle.get("poll") or f"/ingest/jobs/{handle['job_id']}", 0, {}
    while waited < 300:
        _, job = callf(poll)
        if job.get("status") in ("succeeded", "failed"):
            break
        time.sleep(5)
        waited += 5
    check(f"resync job finished ok ({waited}s)", job.get("status") == "succeeded",
          f"status={job.get('status')!r} phase={job.get('phase')!r} "
          f"error={job.get('error')!r}")

    _, rows2 = callf("/admin/sources")
    after = next((r for r in rows2 if r.get("source_id") == sid), {}) \
        if isinstance(rows2, list) else {}
    n1 = after.get("doc_count")
    check(f"quiet resync leaves the corpus count standing ({n0})", n1 == n0,
          f"doc_count {n0} -> {n1} (#910's exact defect; if the corpus REALLY changed "
          "mid-run, re-run to confirm)")
    code, vis2 = _chain_visible(callf, urifilter)
    check("count == retrievable still holds after the resync",
          vis2 is not None and n1 == len(vis2),
          f"registry says {n1}, this identity can list "
          f"{len(vis2) if vis2 is not None else f'HTTP {code}'}")
    _chain_ask(callf, args.chain_q, facts, urifilter, "ask (post-resync)")
    return True


def gap(name, card, strict):
    """A product invariant not yet met (a demo shortcut still stands). Always printed
    LOUDLY so a green mechanics run can never hide it. In --product (strict) mode it is a
    hard failure; otherwise it's a visible warning."""
    print(f"  ⚠ DEMO-GAP: {name}  → {card}")
    if strict:
        _gaps.append(f"{name} ({card})")


def product_conformance(call_fn, strict, live_entra_ok=False):
    """`live_entra_ok` is the ONLY thing allowed to flip invariants 3/4 from DEMO-GAP to a
    real pass — it is True only when --live-entra actually ran AND every one of its
    per-user checks (alice>bob, admin-tally, anonymous/dev-header denial) passed. Plain
    `--product` (live_entra_ok defaults False) must keep reporting both as DEMO-GAPs: a
    green mechanics run must never claim a live proof that never happened."""
    print("\nPRODUCT CONFORMANCE (CONTEXT.md §3 — product, not demo)")
    # 1. No anonymous data access — a request with NO identity must be denied.
    code, _ = call_fn("/router/ask", {"question": "anything"}, user=None)
    check("no anonymous data access (401 without identity)", code == 401, str(code))
    # 2. Identity scopes data — bob must not see the finance store's content or existence.
    _, ra = call_fn("/router/ask", {"question": "confidential revenue ledger invoices"}, user=ALICE)
    _, rb = call_fn("/router/ask", {"question": "confidential revenue ledger invoices"}, user=BOB)
    blob_b = json.dumps(rb)
    check("identity scopes data (bob denied fin-ledger content + existence)",
          "fin-ledger" not in blob_b and "four point two million" not in blob_b)
    # 3. Query-as-user, not a shared service credential (Azure SQL — #156/#131).
    if live_entra_ok:
        check("data-plane queries run as the signed-in user (OBO), not a shared service "
              "credential — proved live by --live-entra", True)
    else:
        gap("data-plane queries run as the signed-in user (OBO), not a shared service credential",
            "#156 / #131", strict)
    # 4. Sign-in required for a real tenant source (Azure SQL still uses static env creds
    #    UNLESS --live-entra just proved the entra_refresh sign-in path live).
    if live_entra_ok:
        check("connecting/querying a tenant source requires the user's own sign-in, not "
              "baked-in env creds — proved live by --live-entra", True)
    else:
        gap("connecting/querying a tenant source requires the user's own sign-in, not baked-in env creds",
            "#156 / #171", strict)
    # VERIFY-ON-CANVAS (user directive 260710): this L1 driver exercises the /router API
    # directly, which is a convenience check, NOT the authoritative one. The product's real
    # verification surface is the CANVAS (/canvas, real browser): drop the source node, set
    # require_signin, Compose up, sign in with Microsoft as the user, and ask — so the whole
    # sign-in -> vault -> delegated query -> source-enforced RLS chain is exercised exactly as
    # a customer hits it. The #156 invariants (3/4) are considered VERIFIED only when they pass
    # through the canvas, not merely here. Prefer L4 (--browser / Claude-in-Chrome canvas drive).
    print("  NOTE: authoritative #156 verification is the CANVAS (real browser sign-in +"
          " ask), not this L1 API drive — see SKILL.md 'verify on canvas'.")
    if not strict and (_gaps or True):
        print("  (run with --product to treat DEMO-GAPs as hard failures)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--azure", action="store_true", help="also tally live Azure SQL")
    ap.add_argument("--bigquery", action="store_true",
                    help="also tally live BigQuery via ADC (needs google-cloud-bigquery + ADC "
                         "for GCP_PROJECT/GCP_DATASET, default your-gcp-project/dbsearch_demo). "
                         "This is the SERVER-SIDE (ADC) tally; query-as-user (alice/bob RLS via "
                         "google_refresh) is the --live-entra/canvas story (Google sign-in gated)")
    ap.add_argument("--gdrive", action="store_true",
                    help="also drive a live PUBLIC Google Drive store (#712): compose the "
                         "folder, wait out the async crawl, ask a question only that folder "
                         "answers, and TALLY every shown snippet against the bytes Google "
                         "itself serves. Needs GOOGLE_API_KEY (secrets/gdrive.env)")
    ap.add_argument("--chain", action="store_true",
                    help="run the CHAIN ASSERTION (#914, launch-gate item 1) against the "
                         "target rig: a synced source's doc_count equals the documents the "
                         "identity can actually retrieve, Ask answers from them, and all of "
                         "it survives the Admin Resync gesture. Standalone: composes "
                         "nothing; the resync is the only state change. Identity: --cookie "
                         "/ DBS_SESSION_COOKIE (prod, signed-in owner) or --chain-user dev "
                         "header (local rig)")
    ap.add_argument("--chain-source", default="",
                    help="substring of the /admin/sources source_id to assert on "
                         "(default: the synced sharepoint-kind source with the most docs)")
    ap.add_argument("--chain-uri-filter",
                    default=os.environ.get("E2EDBS_CHAIN_URI", "sharepoint"),
                    help="uri substring that marks a document as belonging to the source "
                         "(#908: the index has no per-source key, uri is the only honest "
                         "discriminator)")
    ap.add_argument("--chain-q",
                    default=os.environ.get(
                        "E2EDBS_CHAIN_Q", "what is the notice period for resignation"),
                    help="the corpus question (default: #911's canonical prod ask)")
    ap.add_argument("--chain-facts",
                    default=os.environ.get("E2EDBS_CHAIN_FACTS", "month,notice"),
                    help="comma-separated substrings the answer must carry")
    ap.add_argument("--chain-user", default=ALICE,
                    help="dev-header identity for --chain on a local rig (default alice)")
    ap.add_argument("--cookie", default="",
                    help="a real dbs_session cookie value for --chain against a "
                         "signed-in rig (or env DBS_SESSION_COOKIE)")
    ap.add_argument("--chain-no-resync", action="store_true",
                    help="skip the Resync gesture (read-only chain walk)")
    ap.add_argument("--product", action="store_true",
                    help="treat product-conformance DEMO-GAPs (CONTEXT.md §3) as hard failures")
    ap.add_argument("--live-entra", action="store_true",
                    help="sign alice-test/bob-test into the real tenant app (ROPC) and prove "
                         "Azure SQL query-as-user live (#156) — needs AUTH_*, "
                         "E2E_ALICE_PW/E2E_BOB_PW, and DBSEARCH_DEV_SEED=1 on the server; "
                         "only a PASS here flips product_conformance()'s two Azure-SQL "
                         "DEMO-GAPs to a real pass")
    args = ap.parse_args()
    B = args.base

    print("e2edbs L1 — DBSearch end-to-end verification\n")

    # sanity: server up
    try:
        code = urllib.request.urlopen(B + "/canvas", timeout=10).getcode()
    except Exception as e:
        print(f"FATAL: server not reachable at {B} ({e}). Launch it first (see SKILL.md).")
        return 2
    check("server /canvas reachable", code == 200, str(code))

    # ---- --chain mode (#914): standalone — the walk asserts an EXISTING connected
    # corpus, so composing demo stores here would be wrong on prod and pointless
    # locally. UNTESTED preconditions exit 2, never a silent pass. ----
    if args.chain:
        result = chain_checks(B, args)
        if result is None:
            print("\nFATAL: chain assertion UNTESTED — preconditions unmet (see above).")
            return 2
        return _finish(args)

    # ---- --live-entra mode (#185): under an AUTH_* server the X-DBSearch-User dev
    # switcher is refused by design (#183), so every header-identity section below would
    # 401 before live_entra() ever ran. In this mode we run ONLY the live sign-ins +
    # product conformance (threading the seeded session cookies where an identity is
    # needed); the header-identity mechanics run as a SEPARATE pass against a dev-auth
    # server (no AUTH_* env) — see SKILL.md. ----
    if args.live_entra:
        print("\n(--live-entra mode: header-identity L1 sections skipped — the dev header"
              "\n is refused under a real-login server (#183/#185); run the mechanics pass"
              "\n against a dev-auth server separately)")
        result, entra_cookies = live_entra(B)
        if result is None:
            print("\nFATAL: --live-entra preconditions unmet or sign-in failed — see above.")
            return 2
        live_entra_ok = result
        if not live_entra_ok:
            check("--live-entra scoreboard passed", False, "see LIVE ENTRA section above")
        product_conformance(
            lambda p, d=None, user=ALICE: call(
                B, p, d, user, cookie=entra_cookies.get(user)),
            args.product, live_entra_ok)
        return _finish(args)

    # compose the demo catalog (hr-wiki indexed, fin-ledger indexed deal-team, sales-figures csv)
    _, demo = call(B, "/router/demo")
    manifest = demo["manifest"]
    code, _ = call(B, "/router/compose", {"manifest": manifest})
    check("compose demo catalog", code == 200, str(code))

    # optionally add a live Azure SQL store
    if args.azure:
        manifest["stores"].append({
            "id": "azure-deals", "kind": "azure_sql", "mode": "pushdown",
            "business_unit": "sales", "acl": ["all-staff"],
            "title": "Azure SQL deals", "description": "closed deals revenue amount by region product",
            "config": {"server": "${AZURE_SQL_SERVER}", "database": "${AZURE_SQL_DATABASE}",
                       "user": "${AZURE_SQL_USER}", "password": "${AZURE_SQL_PASSWORD}", "use_odbc": True}})
        code, _ = call(B, "/router/compose", {"manifest": manifest})
        check("compose + azure_sql store", code == 200, str(code))

    # optionally add a live BigQuery store (#193, server-side ADC path)
    if args.bigquery:
        manifest["stores"].append({
            "id": "gcp-sales", "kind": "bigquery", "mode": "pushdown",
            "business_unit": "gcp", "acl": ["all-staff"],
            "title": "BigQuery sales",
            "description": "google bigquery sales revenue deals amount by region product closed",
            "config": {"project": BQ_PROJECT, "dataset": BQ_DATASET}})
        code, _ = call(B, "/router/compose", {"manifest": manifest})
        check("compose + bigquery store (ADC)", code == 200, str(code))

    # optionally add a live PUBLIC Google Drive store (#712)
    if args.gdrive:
        manifest["stores"].append({
            "id": "gdrive-notes", "kind": "gdrive", "mode": "index",
            "business_unit": "research",
            # deal-team, NOT all-staff: bob is all-staff only, so the store's own audience is
            # what makes him the built-in negative identity rather than a second fixture.
            "acl": ["deal-team"],
            "title": "Drive notes",
            "description": "google drive folder of DBSearch session notes, waves, "
                           "board cards and effort estimates",
            "config": {"description": "DBSearch session notes and wave planning",
                       "link": GD_LINK}})
        code, _ = call(B, "/router/compose", {"manifest": manifest})
        check("compose + gdrive store (public folder)", code == 200, str(code))
        # #454 made ingest ASYNCHRONOUS, so compose returning 200 only means the crawl was
        # accepted. Asking now races it, and a store that is merely still-crawling would
        # report as an empty answer - i.e. a real failure and a slow one look identical.
        # Wait for the catalog to say `ingested@` and FAIL LOUDLY on the timeout instead.
        fresh, waited = "", 0
        while waited < 180:
            _, cat = call(B, "/router/catalog")
            for bu in cat.get("business_units", []):
                for src in bu.get("sources", []):
                    for st in src.get("stores", []):
                        if st.get("store_id") == "gdrive-notes":
                            fresh = st.get("freshness", "")
            if fresh.startswith("ingested@") or fresh.startswith("sync-failed"):
                break
            time.sleep(5)
            waited += 5
        check("gdrive crawl finished (async ingest, #454)", fresh.startswith("ingested@"),
              f"freshness={fresh!r} after {waited}s")

    # ---- 1. DOC path (alice) ----
    print("\n1. doc path (alice: 'what is our parental leave policy')")
    _, r = call(B, "/router/ask", {"question": "what is our parental leave policy"})
    check("answer has the handbook fact", "sixteen weeks" in r.get("answer", ""), r.get("answer", "")[:80])
    docs = proofs(r, "document")
    check("citation is a document proof with uri", bool(docs) and bool(docs[0].get("uri")),
          json.dumps(docs[:1]))
    check("outcomes: hr-wiki ok", any(o["store_id"] == "hr-wiki" and o["status"] == "ok"
                                      for o in r.get("outcomes", [])))

    # ---- 2. SQL path + TALLY (alice) ----
    print("\n2. sql path + TALLY (alice: 'total amount by region' → sales-figures)")
    # pin the demo store so routing is deterministic even when --azure adds a rival sales store
    _, r = call(B, "/router/ask", {"question": "total amount by region", "store": "sales-figures"})
    sql = proofs(r, "sql")
    if check("got a sql proof", bool(sql), json.dumps(r.get("citations", []))[:120]):
        p = sql[0]
        check("proof carries a rerun token", bool(p.get("rerun_token")))
        # re-run the EXACT proof SQL and tally the rows
        _, x = call(B, "/router/rerun", {"store_id": p["store_id"], "sql": p["sql"],
                                         "token": p["rerun_token"]})
        rows = sorted(map(tuple, x.get("rows", [])))
        # demo ground truth: emea=140, apac=60 over region/SUM(amount)
        gt = sorted([("emea", 140), ("apac", 60)])
        got = sorted((str(a), int(float(b))) for a, b in rows) if rows else []
        check("re-run rows TALLY vs source", got == gt, f"got={got} expected={gt}")
        # proof integrity (#165)
        code_t, _ = call(B, "/router/rerun", {"store_id": p["store_id"],
                                              "sql": p["sql"] + " --x", "token": p["rerun_token"]})
        check("tampered SQL rejected (403)", code_t == 403, str(code_t))
        code_f, _ = call(B, "/router/rerun", {"store_id": p["store_id"], "sql": p["sql"],
                                              "token": p["rerun_token"]}, user=BOB)
        check("foreign-user token rejected (403)", code_f == 403, str(code_f))

    # ---- 3. multi-source traversal (alice) ----
    print("\n3. multi-source traversal (alice: compound question)")
    _, r = call(B, "/router/ask",
                {"question": "parental leave policy versus total amount by region"})
    oc = r.get("outcomes", [])
    check("≥2 sources in traversal", len(oc) >= 2, f"{len(oc)} outcomes")
    check("each outcome tagged with a sub_question",
          all(o.get("sub_question") for o in oc), json.dumps(oc)[:120])
    check("both a doc pill and a sql pill", bool(proofs(r, "document")) and bool(proofs(r, "sql")))

    # ---- 3b. READABLE & RESOLVABLE — would a human understand this? ----
    print("\n3b. human-understandability of that answer")
    ans = r.get("answer", "")
    cites = r.get("citations", [])
    markers = sorted(int(m) for m in set(re.findall(r"\[(\d+)\]", ans)))
    # every [n] footnote in the prose must resolve to a real citation (no dangling [4])
    check("citation markers resolve (no dangling [n])",
          (not markers) or max(markers) <= len(cites),
          f"markers={markers} but only {len(cites)} citations")
    # the answer must be prose, not a raw evidence dump (region=emea, amount=100 ...)
    check("answer is not a raw k=v evidence dump",
          not re.search(r"\b\w+=\w+,\s*\w+=", ans), ans[:80])
    # every citation must carry a HUMAN label + a source-type a person can read
    def human_label(c):
        p = c.get("proof", {})
        return c.get("title") or p.get("title") or c.get("doc") or p.get("table")
    check("every citation has a human-readable label",
          all(human_label(c) for c in cites), json.dumps(cites)[:120])
    check("every citation declares its source kind",
          all(c.get("proof", {}).get("kind") for c in cites),
          "citations missing typed proof.kind")
    # #176: each citation names a legible ORIGIN (system · location · object)
    check("every citation carries a human origin",
          all(c.get("origin") for c in cites), json.dumps(cites)[:120])
    # #175: footnotes resolve every [n] the answer used, 1-indexed in order
    fns = r.get("footnotes", [])
    check("footnotes are 1-indexed and cover the answer's [n]",
          bool(fns) and [f["n"] for f in fns] == list(range(1, len(fns) + 1))
          and ((not markers) or max(markers) <= len(fns)),
          f"markers={markers} footnotes={len(fns)}")
    check("each footnote shows origin + the data snippet backing it",
          all(f.get("origin") and f.get("snippet") is not None for f in fns),
          json.dumps(fns)[:120])

    # ---- 4. NEGATIVE identity (bob) — LAW 2 / gate #1 ----
    print("\n4. negative identity (bob must be denied)")
    _, rb = call(B, "/router/ask", {"question": "confidential revenue ledger invoices"}, user=BOB)
    blob = json.dumps(rb)
    check("bob never sees fin-ledger existence", "fin-ledger" not in blob)
    check("bob gets no finance content", "four point two million" not in blob)
    code_u, ru = call(B, "/router/rerun",
                      {"store_id": "no-such-store", "sql": "SELECT 1", "token": "t"})
    code_i, ri = call(B, "/router/rerun",
                      {"store_id": "fin-ledger", "sql": "SELECT 1", "token": "t"}, user=BOB)
    check("invisible store == nonexistent (identical 404)",
          code_u == 404 and code_i == 404 and ru.get("detail") == ri.get("detail"),
          f"{code_u}/{code_i}")

    # ---- 5. live Azure SQL tally (optional) ----
    if args.azure:
        print("\n5. live Azure SQL tally (alice: 'total deal amount by region')")
        _, r = call(B, "/router/ask",
                    {"question": "total deal amount by region", "store": "azure-deals"})
        sql = proofs(r, "sql")
        if check("azure sql proof present", bool(sql), json.dumps(r.get("citations", []))[:120]):
            p = sql[0]
            _, x = call(B, "/router/rerun", {"store_id": p["store_id"], "sql": p["sql"],
                                             "token": p["rerun_token"]})
            got = {str(a).lower(): int(float(b)) for a, b in x.get("rows", [])}
            gt = {"apac": 205000, "amer": 195000, "emea": 125000}
            check("azure re-run TALLIES vs live DB", got == gt, f"got={got} expected={gt}")

    # ---- 6. live BigQuery tally, server-side/ADC (optional, #193) ----
    if args.bigquery:
        print("\n6. live BigQuery tally, ADC (alice: 'total sales amount by region' -> gcp-sales)")
        _, r = call(B, "/router/ask",
                    {"question": "total sales amount by region", "store": "gcp-sales"})
        check("bigquery routed to the BigQuery store",
              any(o.get("store_id") == "gcp-sales" and o.get("status") == "ok"
                  for o in r.get("outcomes", [])), json.dumps(r.get("outcomes", []))[:160])
        sql = proofs(r, "sql")
        if check("bigquery sql proof present", bool(sql),
                 json.dumps(r.get("citations", []))[:120]):
            p = sql[0]
            check("bigquery proof is cited to BigQuery",
                  (p.get("kind") == "sql") and ("bigquery" in json.dumps(p).lower()
                                                or "gcp-sales" in json.dumps(p).lower()),
                  json.dumps(p)[:160])
            _, x = call(B, "/router/rerun", {"store_id": p["store_id"], "sql": p["sql"],
                                             "token": p["rerun_token"]})
            got = {str(a).lower(): int(round(float(b))) for a, b in x.get("rows", [])}
            gt = _bigquery_ground_truth(BQ_PROJECT, BQ_DATASET)
            check("bigquery re-run TALLIES vs live BigQuery (ground truth from ADC)",
                  gt is not None and got == gt, f"got={got} expected={gt}")
        # every [n] in the BigQuery answer must resolve (no dangling marker, #233)
        bq_markers = sorted(int(m) for m in set(re.findall(r"\[(\d+)\]", r.get("answer", ""))))
        check("bigquery answer citations resolve (no dangling [n])",
              (not bq_markers) or max(bq_markers) <= len(r.get("citations", [])),
              f"markers={bq_markers} cites={len(r.get('citations', []))}")

    # ---- 7. live Google Drive tally (optional, #712) ----
    if args.gdrive:
        gdrive_checks(B, lambda p, d=None, user=ALICE: call(B, p, d, user, timeout=180))

    # ---- PRODUCT CONFORMANCE (always runs; --product makes gaps fatal; the two
    # Azure-SQL DEMO-GAPs can only flip to a pass in --live-entra mode above) ----
    product_conformance(lambda p, d=None, user=ALICE: call(B, p, d, user),
                        args.product, live_entra_ok=False)
    return _finish(args)


def _finish(args):
    print("\n" + ("=" * 48))
    if _fails or _gaps:
        if _fails:
            print(f"FAILED ({len(_fails)}):")
            for f in _fails:
                print("  -", f)
        if _gaps:
            print(f"PRODUCT-GAP ({len(_gaps)}) — not yet product-conformant (see CONTEXT.md §3):")
            for g in _gaps:
                print("  -", g)
        return 1
    tail = " (mechanics ✓; DEMO-GAPs noted — run --product to enforce the tenant story)"
    print("e2edbs L1: ALL TALLIED & PERMISSION-GATED ✓" + ("" if args.product else tail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
