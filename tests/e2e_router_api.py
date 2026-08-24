"""#109 — /router API + live canvas wiring, over HTTP (TestClient).
Proves the compose→route→ask vertical through the server, including gate #1 for a
header-authenticated user (bob must never learn fin-ledger exists) and honest handling
of not-yet-real cloud kinds. #111 adds the connector rail on the same surface: a
`kind: folder, mode: index` manifest entry ingests on compose, answers with LAW-2
trim, and delta-syncs via POST /router/stores/{id}/sync. Run: python3 tests/e2e_router_api.py
"""
import os
import re as _re
import sys
import tempfile
import time
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"   # #315: the dev header is opt-in now
os.environ.pop("USERS_FILE", None)
# The demo rate limiter is for the PUBLIC rig, not this suite: with the falcon-fixture
# collision fixed (osprey), the file's later tests actually run - and their extra
# requests pushed /router/setup/turn over the per-IP budget (429 mid-suite). Same
# switch every gate rig uses.
os.environ["DBSEARCH_RATE_LIMIT"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)
ALICE = {"X-DBSearch-User": "alice"}      # all-staff + deal-team
BOB = {"X-DBSearch-User": "bob"}          # all-staff only


def test_kinds_lists_real_and_planned():
    r = client.get("/router/kinds", headers=ALICE)
    assert r.status_code == 200, r.text
    kinds = {k["kind"]: k for k in r.json()["kinds"]}
    assert kinds["local"]["available"] is True, kinds
    assert kinds["graph_search"]["available"] is True, kinds       # E3b native (ADR 0008)
    assert kinds["csv"]["available"] is True, kinds                # E4 federated SQL
    # #107 pushdown + #155 postgres + #158 mysql + #159 synapse — all real federated-SQL providers now
    for cloud in ("azure_sql", "bigquery", "redshift", "databricks", "postgres", "mysql", "synapse"):
        assert kinds[cloud]["available"] is True, kinds
        assert kinds[cloud]["modes"] == ["pushdown"], kinds


def test_route_before_compose_is_409():
    r = client.post("/router/route", headers=ALICE, json={"question": "anything"})
    assert r.status_code == 409, r.text


def test_demo_compose_reports_stores_and_skips_cloud_kinds():
    demo = client.get("/router/demo", headers=ALICE).json()["manifest"]
    # a cloud store that can't build (config missing, #107 per-store isolation) and a
    # kind with no provider at all — BOTH must be SKIPPED with reasons, never fatal
    demo["stores"].append({"id": "sales-bq", "kind": "bigquery", "business_unit": "sales",
                           "acl": ["sales-staff"], "config": {}})
    # neo4j (graph) has no provider yet — the honest no-provider skip case
    demo["stores"].append({"id": "crm-graph", "kind": "neo4j", "business_unit": "sales",
                           "acl": ["sales-staff"], "config": {}})
    r = client.post("/router/compose", headers=ALICE, json={"manifest": demo})
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {s["store_id"] for s in body["stores"]}
    assert {"hr-wiki", "fin-ledger"} <= ids, body
    skipped = {s["id"]: s["reason"] for s in body["skipped"]}
    assert "missing" in skipped["sales-bq"], body          # build failed -> honest skip
    assert "provider" in skipped["crm-graph"], body        # unknown kind -> honest skip


def test_probe_single_entry():
    entry = {"id": "probe-x", "kind": "local", "business_unit": "hr",
             "title": "Probe X", "config": {}}
    r = client.post("/router/probe", headers=ALICE, json={"entry": entry})
    assert r.status_code == 200, r.text
    assert r.json()["profile"]["store_id"] == "probe-x", r.json()
    bad = dict(entry, kind="redshift")
    r2 = client.post("/router/probe", headers=ALICE, json={"entry": bad})
    assert r2.status_code == 200 and r2.json()["available"] is False, r2.text


def test_health_local_roundtrip_and_unreachable_failed():
    # #130: a real read-only round-trip. Title+description overlap the seed text so the
    # lexical canary reliably retrieves a record -> healthy.
    entry = {"id": "health-x", "kind": "local", "business_unit": "hr",
             "title": "Handbook", "description": "parental leave",
             "config": {"seed": [{"external_id": "d", "title": "Handbook", "uri": "u",
                                  "acl": ["all-staff"],
                                  "text": "handbook parental leave holidays"}],
                        "user_groups": {"alice": ["all-staff"]}}}
    r = client.post("/router/health", headers=ALICE, json={"entry": entry})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["status"] == "healthy", v
    assert [s["name"] for s in v["stages"]][:2] == ["probe", "exercise"], v
    # an empty source is reachable but has nothing to retrieve -> degraded, not failed
    empty = {"id": "empty-x", "kind": "local", "business_unit": "hr", "title": "Empty",
             "config": {"user_groups": {"alice": ["all-staff"]}}}
    rd = client.post("/router/health", headers=ALICE, json={"entry": empty}).json()
    assert rd["status"] == "degraded", rd
    # an unreachable/miscredentialed cloud kind -> failed verdict, never a 500
    bad = {"id": "rs", "kind": "redshift", "business_unit": "sales", "title": "RS",
           "config": {}}
    r2 = client.post("/router/health", headers=ALICE, json={"entry": bad})
    assert r2.status_code == 200 and r2.json()["status"] == "failed", r2.text
    # bug (browser-caught): an UNRESOLVED ${ENV} placeholder must also -> failed, not 500
    unresolved = {"id": "rs2", "kind": "redshift", "business_unit": "sales", "title": "RS2",
                  "config": {"cluster": "${DEFINITELY_UNSET_ENV_VAR}"}}
    r3 = client.post("/router/health", headers=ALICE, json={"entry": unresolved})
    assert r3.status_code == 200, f"env-resolution failure must not 500: {r3.status_code}"
    assert r3.json()["status"] == "failed", r3.json()


def test_alice_routes_and_asks_across_both_stores():
    r = client.post("/router/route", headers=ALICE,
                    json={"question": "what is our parental leave policy"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["stores"] and d["stores"][0]["store_id"] == "hr-wiki", d
    a = client.post("/router/ask", headers=ALICE,
                    json={"question": "what is our parental leave policy"}).json()
    assert "sixteen weeks" in a["answer"], a["answer"]
    assert a["citations"] and a["citations"][0]["doc"] == "handbook", a["citations"]
    assert a["routing"]["stores"][0]["store_id"] == "hr-wiki", a["routing"]


def test_bob_never_learns_finance_exists_over_http():
    r = client.post("/router/route", headers=BOB,
                    json={"question": "confidential revenue ledger invoices"})
    assert r.status_code == 200, r.text
    blob = r.text
    a = client.post("/router/ask", headers=BOB,
                    json={"question": "confidential revenue ledger invoices"})
    blob += a.text
    assert "fin-ledger" not in blob, "store EXISTENCE leaked to bob (gate #1)"
    assert "four point two million" not in blob, "finance CONTENT leaked to bob"


def test_identity_is_header_only():
    r = client.post("/router/ask", json={"question": "q"})     # no auth header
    assert r.status_code == 401, r.text


def test_advisor_why_catalog_and_manual_override():
    # E7 (#104): candidates carry per-store why; the catalog tree is caller-trimmed;
    # a manual pin works for a visible store and never confirms an invisible one.
    r = client.post("/router/route", headers=ALICE,
                    json={"question": "what is our parental leave policy"}).json()
    assert r["candidates"] and "match" in r["candidates"][0]["why"], r["candidates"]

    cat_a = client.get("/router/catalog", headers=ALICE).json()
    cat_b = client.get("/router/catalog", headers=BOB).json()
    assert "fin-ledger" in str(cat_a), cat_a          # alice (deal-team) sees it
    assert "fin-ledger" not in str(cat_b), "catalog tree leaked to bob"
    a_store = cat_a["business_units"][0]["sources"][0]["stores"][0]
    assert "freshness" in a_store, a_store

    pinned = client.post("/router/ask", headers=ALICE,
                         json={"question": "anything interesting?",
                               "store": "hr-wiki"}).json()
    assert pinned["routing"]["method"] == "manual", pinned["routing"]
    assert {o["store_id"] for o in pinned["outcomes"]} == {"hr-wiki"}, pinned["outcomes"]

    sneaky = client.post("/router/ask", headers=BOB,
                         json={"question": "revenue", "store": "fin-ledger"})
    assert "fin-ledger" not in sneaky.text, "override confirmed invisible store to bob"
    assert sneaky.json()["routing"]["method"] == "fallback", sneaky.json()["routing"]


def test_compound_question_spans_semantic_and_sql():
    # E6 (#103): one compound question decomposed across TWO store families —
    # hr-wiki (indexed, semantic) + sales-figures (federated SQL, analytical).
    q = {"question": "parental leave policy versus total amount by region"}
    a = client.post("/router/ask", headers=ALICE, json=q).json()
    assert a["routing"]["query_type"] == "compound", a["routing"]
    assert a["routing"]["method"] == "decompose", a["routing"]
    assert len(a["routing"]["sub_queries"]) == 2, a["routing"]
    cited_stores = {c["store_id"] for c in a["citations"]}
    assert {"hr-wiki", "sales-figures"} <= cited_stores, a["citations"]
    assert "sixteen weeks" in a["answer"], a["answer"]        # semantic half
    assert a["disclosure"] == "", a["disclosure"]             # both halves covered
    # bob (all-staff, NOT deal-team): fin-ledger must not appear even in sub-decisions
    b = client.post("/router/ask", headers=BOB,
                    json={"question": "parental leave policy versus confidential revenue"})
    assert "fin-ledger" not in b.text, "compound sub-routing leaked store existence to bob"


FOLDER_ROOT: "Path | None" = None    # set by test_compose_with_folder_store...


#: #748 fixture: the live-site defect's own shape, >160 chars with the cap mid-word.
LONG_POLICY = ("Sabbatical policy: employees with seven years of continuous service may "
               "take a twelve week paid sabbatical, and the leave may be split into two "
               "blocks taken inside the same calendar year with the written agreement of "
               "their manager and the people team.")


def test_compose_with_folder_store_drives_connector_rail():
    global FOLDER_ROOT
    FOLDER_ROOT = Path(tempfile.mkdtemp(prefix="dbse2e-folder-"))
    (FOLDER_ROOT / "all-staff").mkdir()
    (FOLDER_ROOT / "deal-team").mkdir()
    (FOLDER_ROOT / "all-staff" / "policy.txt").write_text(
        "travel policy: economy flights under six hours")
    # "osprey", not "falcon": 03ef448 added a DEMO Project Falcon valuation doc to
    # fin-ledger (the alice/bob picker's proof doc), so a falcon question here is
    # genuinely answerable from TWO stores and routing may honestly answer from the demo
    # doc - which is what silently pointed this test's ask at the wrong rail for a week.
    # A unique project name keeps this fixture exercising what the test is FOR: the
    # folder-store rail and its deal-team ACL trim.
    (FOLDER_ROOT / "deal-team" / "osprey.txt").write_text(
        "project osprey valuation is nine hundred million")
    demo = client.get("/router/demo", headers=ALICE).json()["manifest"]
    demo["stores"].append({
        "id": "legal-archive", "kind": "folder", "mode": "index",
        "business_unit": "legal", "acl": ["all-staff"], "title": "Legal archive",
        "description": "legal archive osprey merger valuation contracts travel policy",
        "config": {"path": str(FOLDER_ROOT)}})
    r = client.post("/router/compose", headers=ALICE, json={"manifest": demo})
    assert r.status_code == 200, r.text
    body = r.json()
    stores = {s["store_id"]: s for s in body["stores"]}
    assert "legal-archive" in stores, stores
    # Compose no longer waits for the crawl (#454), and says so rather than reporting a store
    # that merely looks empty: freshness is `syncing`, and `ingesting` carries what to poll.
    assert stores["legal-archive"]["freshness"].startswith("syncing"), stores["legal-archive"]
    assert any(j["store_id"] == "legal-archive" for j in body["ingesting"]), body["ingesting"]
    _await_ingest(body)
    after = _catalog_store("legal-archive")
    assert after["freshness"].startswith("ingested@"), after


def _await_ingest(resp_json, timeout: float = 60.0) -> list:
    """Poll every ingest job a compose/sync response started, until it leaves the pool.

    #454: composing or re-syncing a connector-backed store SUBMITS the crawl and hands back a
    job id, because #536 measured a real library exceeding the request timeout. This is the
    client half of that contract, driven the way the UI drives it - poll `/router/jobs/{id}`,
    do not assume the content is there."""
    jobs = [j["job_id"] for j in resp_json.get("ingesting", [])]
    if resp_json.get("job_id"):
        jobs.append(resp_json["job_id"])
    finals = []
    for job_id in jobs:
        deadline = time.time() + timeout
        while time.time() < deadline:
            j = client.get(f"/router/jobs/{job_id}", headers=ALICE)
            assert j.status_code == 200, j.text
            body = j.json()
            if body["status"] in ("succeeded", "failed"):
                assert body["status"] == "succeeded", body
                finals.append(body)
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"ingest job {job_id} never finished within {timeout}s")
    return finals

def _catalog_store(store_id: str, headers=None) -> dict:
    """One store's live entry from the caller-visible catalog tree (freshness included)."""
    tree = client.get("/router/catalog", headers=headers or ALICE).json()
    for bu in tree["business_units"]:
        for src in bu["sources"]:
            for st in src["stores"]:
                if st["store_id"] == store_id:
                    return st
    raise AssertionError(f"{store_id} not visible in catalog: {tree}")


def test_folder_store_answers_with_law2_trim():
    q = {"question": "what is the valuation of project osprey"}
    a = client.post("/router/ask", headers=ALICE, json=q).json()
    assert "nine hundred million" in a["answer"], a["answer"]
    b = client.post("/router/ask", headers=BOB, json=q)
    assert "nine hundred million" not in b.text, "deal-team folder content leaked to bob"


def test_documents_endpoint_names_the_files_and_trims_them_per_caller():
    """#939 / #895 - "connect a node, verify it shows synced + doc count".

    Nothing in the product could answer either question before this: /admin/sources knows only
    about sharepoint rows, and the canvas node's freshness is a compose-time snapshot, so a
    crawl that finished afterwards left the badge reading `syncing` forever.

    The LAW 2 half is the risky half and it is why this runs over HTTP with two identities:
    the inventory is built from `list_doc_acls`, which is the ADMIN permission-tester surface
    and returns EVERY document in the partition. Bob is all-staff only, so he must be told
    about policy.txt and must never learn that osprey.txt exists - a filename is a disclosure
    even with no content attached."""
    a = client.get("/router/stores/legal-archive/documents", headers=ALICE)
    assert a.status_code == 200, a.text
    ab = a.json()
    assert ab["known"] is True, ab
    a_titles = sorted(d["title"] for d in ab["documents"])
    assert a_titles == ["policy.txt", "osprey.txt"] or set(a_titles) == {"policy.txt", "osprey.txt"}, ab
    assert ab["doc_count"] == 2, ab

    b = client.get("/router/stores/legal-archive/documents", headers=BOB)
    assert b.status_code == 200, b.text
    bb = b.json()
    b_titles = [d["title"] for d in bb["documents"]]
    assert b_titles == ["policy.txt"], f"deal-team FILENAME leaked to bob: {bb}"
    assert bb["doc_count"] == 1, bb
    assert "osprey" not in b.text.lower(), f"osprey named anywhere in bob's response: {b.text}"


def test_documents_endpoint_reports_freshness_that_is_current_not_composed():
    """The stale-badge half. The crawl finished during test_compose..., so this must say
    `ingested@`, not the `syncing` the compose response carried."""
    r = client.get("/router/stores/legal-archive/documents", headers=ALICE).json()
    assert r["freshness"].startswith("ingested@"), (
        f"freshness is still the compose-time snapshot: {r['freshness']!r}")
    assert "unreadable" in r, r   # #725: the count travels even at zero


def test_documents_endpoint_hides_a_store_the_caller_cannot_enumerate():
    """Gate #1, verbatim from the schema endpoint's rule: an invisible store answers 404, the
    same answer a store that does not exist gets. 403 would confirm it exists."""
    r = client.get("/router/stores/fin-ledger/documents", headers=BOB)
    assert r.status_code == 404, (r.status_code, r.text)
    assert "fin-ledger" not in r.text or "no such store" in r.text, r.text


def test_sync_endpoint_delta_crawls():
    r0 = client.post("/router/stores/legal-archive/sync", headers=ALICE)
    assert r0.status_code == 202, r0.text        # #454: submitted, not performed
    j0 = _await_ingest(r0.json())[0]
    assert j0["docs_done"] == 0, j0                       # nothing changed since compose
    newf = FOLDER_ROOT / "all-staff" / "merger.txt"
    newf.write_text("the acme merger closed in october")
    future = time.time() + 5
    os.utime(newf, (future, future))                      # past the mtime cursor
    r1 = client.post("/router/stores/legal-archive/sync", headers=ALICE)
    assert r1.status_code == 202, r1.text
    _await_ingest(r1.json())
    s1 = _catalog_store("legal-archive")
    assert s1["freshness"].startswith("ingested@"), s1
    # routing is description-driven (E2 prefilter): ask with the store's vocabulary
    a = client.post("/router/ask", headers=BOB,
                    json={"question": "what does the legal archive say about the merger"}).json()
    assert "october" in a["answer"], a["answer"]


def test_sync_unknown_or_non_connector_store_404():
    r = client.post("/router/stores/hr-wiki/sync", headers=ALICE)   # local kind: no cursor
    assert r.status_code == 404, r.text


def test_conversational_setup_talks_a_folder_live():
    # Phase C/D C1 (#116): TALK -> composed federation -> cited answer, one API.
    root = Path(tempfile.mkdtemp(prefix="dbse2e-setup-"))
    (root / "all-staff").mkdir()
    (root / "all-staff" / "wfh.txt").write_text(
        "work from home is allowed two days per week")
    conv = {"conv_id": "setup-e2e"}
    t1 = client.post("/router/setup/turn", headers=ALICE,
                     json=dict(conv, message=f"plug in the folder at {root} containing "
                               "work from home and holiday policies, for the "
                               "policy team, visible to all-staff")).json()
    assert t1["state"] == "gathering" and "Added" in t1["reply"], t1
    t2 = client.post("/router/setup/turn", headers=ALICE,
                     json=dict(conv, intent="ready")).json()
    assert t2["state"] == "confirming", t2
    assert not [v for v in t2["validation"] if v["level"] == "error"], t2["validation"]
    t3 = client.post("/router/setup/turn", headers=ALICE,
                     json=dict(conv, intent="apply")).json()
    assert t3["state"] == "applied", t3
    # #454: applying the setup composes, which SUBMITS the crawl. The conversational flow
    # reports the same `ingesting` handles as /router/compose, so a caller (and this test)
    # waits on the job rather than assuming the folder is already searchable.
    assert t3["result"]["stores"][0]["freshness"].startswith("syncing"), t3["result"]
    _await_ingest(t3["result"])
    assert _catalog_store(t3["result"]["stores"][0]["store_id"])["freshness"].startswith(
        "ingested@")
    a = client.post("/router/ask", headers=BOB,
                    json={"question": "what is the work from home policy"}).json()
    assert "two days" in a["answer"], a["answer"]
    # Phase D: guided verify runs a suggested question through the REAL ask path
    t4 = client.post("/router/setup/turn", headers=ALICE,
                     json=dict(conv, intent="verify")).json()
    assert t4["state"] == "applied" and "routed to" in t4["reply"], t4
    assert "two days" in t4["reply"], t4["reply"]


def test_setup_uses_llm_entry_parser_when_model_supports_it():
    """C3 (#116): when the edition's default chat model can extract entries, /setup/turn
    parses via the LLM — capability/key-gated exactly like the #57 model split. (This
    suite's memory edition can't, so every test above ran on the keyword fallback.)"""
    from fastapi import FastAPI

    from dbsearch.server.app import _edition, current_user
    from dbsearch.server.router_api import build_router_api

    class _FakeLlm:
        def __init__(self):
            self.calls = []

        def extract_setup_entries(self, text):
            self.calls.append(text)
            return ('[{"kind": "folder", "config": {"path": "/tmp/llm-parsed"}, '
                    '"business_unit": "ops", "acl": ["all-staff"], '
                    '"description": "runbooks"}]')

    fake = _FakeLlm()
    old_models, old_default = _edition.chat_models, _edition.chat_model_default
    _edition.chat_models = dict(old_models, **{"fake-haiku": fake})
    _edition.chat_model_default = "fake-haiku"
    try:
        sub = FastAPI()
        sub.include_router(build_router_api(_edition, current_user))
        t = TestClient(sub).post(
            "/router/setup/turn", headers=ALICE,
            json={"conv_id": "wiring", "message": "hook up our runbooks"})
        assert t.status_code == 200, t.text
        # "runbooks" names no kind — only the LLM path can have produced this entry
        assert fake.calls == ["hook up our runbooks"], fake.calls
        assert "llm-parsed" in t.json()["reply"], t.json()
    finally:
        _edition.chat_models, _edition.chat_model_default = old_models, old_default


def test_probe_recommends_native_mode_upgrade():
    # #107 probe mode-upgrade: probing an index-mode sharepoint entry while the
    # tenant is Graph-licensed (GRAPH_TOKEN present) should RECOMMEND the zero-copy
    # native path (ADR 0008) — a suggestion, never a silent mode switch.
    entry = {"id": "sp-x", "kind": "sharepoint", "business_unit": "hr",
             "config": {}}
    os.environ["GRAPH_TOKEN"] = "dev-graph-token"
    try:
        r = client.post("/router/probe", headers=ALICE, json={"entry": entry}).json()
        rec = r.get("recommendation", "")
        assert "graph_search" in rec and "native" in rec, r
    finally:
        del os.environ["GRAPH_TOKEN"]
    r2 = client.post("/router/probe", headers=ALICE, json={"entry": entry}).json()
    assert "recommendation" not in r2, r2         # unlicensed tenant: no upsell noise
    # an already-native entry gets no recommendation either
    os.environ["GRAPH_TOKEN"] = "dev-graph-token"
    try:
        r3 = client.post("/router/probe", headers=ALICE,
                         json={"entry": {"id": "g", "kind": "graph_search",
                                         "business_unit": "hr", "config": {}}}).json()
        assert "recommendation" not in r3, r3
    finally:
        del os.environ["GRAPH_TOKEN"]


def test_compose_accepts_delegation_block():
    # #107 OBO wiring: a store entry may carry a `delegation:` sibling of config —
    # compose registers it on the broker and still answers; a bad kind 400s honestly.
    demo = client.get("/router/demo", headers=ALICE).json()["manifest"]
    # #368 final review (IMPORTANT 2): a `static` delegation's token map holds raw bearer
    # tokens, so the plaintext-credential guard now covers it - a literal here is refused with
    # a 400 exactly like a literal `config.password`. Use the ${ENV} form, which is what a dev
    # rig should have been using all along: it is one of the three legal manifest value forms
    # (ADR 0010 s2) and resolve_env substitutes it server-side, so the delegation still works.
    os.environ["DEV_STATIC_TOKEN"] = "dev-tok"
    try:
        for s in demo["stores"]:
            if s["kind"] == "csv":
                s["delegation"] = {"kind": "static",
                                   "tokens": {"alice": "${DEV_STATIC_TOKEN}"}}
        r = client.post("/router/compose", headers=ALICE, json={"manifest": demo})
        assert r.status_code == 200, r.text
        a = client.post("/router/ask", headers=ALICE,
                        json={"question": "total amount by region"}).json()
        assert a["routing"]["stores"], a["routing"]      # delegated store still answers
    finally:
        del os.environ["DEV_STATIC_TOKEN"]
    bad = dict(demo)
    bad["stores"] = [dict(demo["stores"][0], delegation={"kind": "wat"})]
    r2 = client.post("/router/compose", headers=ALICE, json={"manifest": bad})
    assert r2.status_code == 400 and "wat" in r2.text, r2.text
    # restore the plain demo catalog for the tests that follow
    client.post("/router/compose", headers=ALICE,
                json={"manifest": client.get("/router/demo",
                                             headers=ALICE).json()["manifest"]})


def test_ask_stamps_sql_proof_and_rerun_roundtrip():
    # #165: sales-figures (csv federated SQL) answers "total amount by region"; every
    # SQL citation carries a typed proof + a user-bound rerun token that re-executes.
    r = client.post("/router/ask", headers=ALICE,
                    json={"question": "total amount by region"})
    assert r.status_code == 200, r.text
    sqls = [c["proof"] for c in r.json()["citations"]
            if c.get("proof", {}).get("kind") == "sql"]
    assert sqls, r.json()["citations"]
    p = sqls[0]
    assert p["store_id"] == "sales-figures", p
    assert p["rerun_token"], p                                  # stamped, user-bound
    rr = client.post("/router/rerun", headers=ALICE,
                     json={"store_id": p["store_id"], "sql": p["sql"],
                           "token": p["rerun_token"]})
    assert rr.status_code == 200, rr.text
    body = rr.json()
    assert body["count"] >= 1 and body["cols"], body
    assert len(body["rows"]) <= 50, body

    # tampered SQL → 403 (only server-issued statements run)
    rr2 = client.post("/router/rerun", headers=ALICE,
                      json={"store_id": p["store_id"], "sql": p["sql"] + " --x",
                            "token": p["rerun_token"]})
    assert rr2.status_code == 403, rr2.text

    # foreign user with alice's token → 403 (user-bound, LAW 2)
    rr3 = client.post("/router/rerun", headers=BOB,
                      json={"store_id": p["store_id"], "sql": p["sql"],
                            "token": p["rerun_token"]})
    assert rr3.status_code == 403, rr3.text


def test_rerun_unknown_and_invisible_store_same_404():
    rr = client.post("/router/rerun", headers=ALICE,
                     json={"store_id": "no-such-store", "sql": "SELECT 1", "token": "t"})
    assert rr.status_code == 404, rr.text
    # gate #1: a store bob can't see answers EXACTLY like one that doesn't exist
    rr2 = client.post("/router/rerun", headers=BOB,
                      json={"store_id": "fin-ledger", "sql": "SELECT 1", "token": "t"})
    assert rr2.status_code == 404, rr2.text
    assert rr.json()["detail"] == rr2.json()["detail"], (rr.json(), rr2.json())


def test_ask_has_origin_and_resolvable_footnotes():
    r = client.post("/router/ask", headers=ALICE,
                    json={"question": "total amount by region", "store": "sales-figures"}).json()
    # every citation carries a human origin naming its system
    assert r["citations"], r
    assert all("origin" in c and c["origin"] for c in r["citations"]), r["citations"]
    assert any("Local CSV" in c["origin"] for c in r["citations"]), r["citations"]
    # footnotes exist, are 1-indexed in order, and cover every [n] used in the answer
    fns = r.get("footnotes")
    assert fns and fns[0]["n"] == 1, fns
    assert [f["n"] for f in fns] == list(range(1, len(fns) + 1)), fns
    used = [int(m) for m in set(_re.findall(r"\[(\d+)\]", r["answer"]))]
    assert (not used) or max(used) <= len(fns), (used, len(fns))
    assert all(f.get("origin") and "snippet" in f for f in fns), fns
    # #729(a): every footnote carries the map that says how to READ the values in its snippet.
    # Asserted over HTTP because the map is built from the schema deep inside the store and has
    # a whole serialisation boundary to fall off - and asserted as ALWAYS PRESENT, empty or not,
    # so the renderer has one shape to handle rather than two. `sales-figures` types nothing
    # (its `amount` column is whole numbers, so it loads as INTEGER, and an integer is never
    # grouped) - which makes this the empty case, and the empty case is the one that has to
    # survive the wire without becoming null.
    assert all(isinstance(f.get("column_types"), dict) for f in fns), fns


def test_footnote_snippets_never_cut_mid_word():
    """#748: on the live site a snippet rendered '...receive 18 weeks of fully pa' - a hard
    [:160] with no ellipsis, so the truncation looked like the DOCUMENT'S OWN text. Over the
    wire (the only place the real server-built string exists): a snippet either fits whole
    with no ellipsis, or ends with a real ellipsis at a word boundary - the character before
    the ellipsis is not a space, and the cut never splits a word (dropping the ellipsis
    plus at most one trailing space must land on a whitespace boundary of the source or
    consume it entirely)."""
    r = client.post("/router/ask", headers=ALICE,
                    json={"question": "total amount by region", "store": "sales-figures"}).json()
    fns = r.get("footnotes")
    assert fns, r
    for f in fns:
        snippet = f["snippet"]
        if not snippet:
            continue
        if snippet.endswith("…"):
            body = snippet[:-1]
            assert body == body.rstrip(), (
                f"ellipsis glued to trailing space: {snippet!r}")
            assert len(body) <= 160, f"truncated snippet overruns the cap: {len(body)}"
        else:
            assert len(snippet) <= 160, (
                f"an un-ellipsized snippet longer than the cap means the cut is gone "
                f"or silent again: {len(snippet)} chars")
    # The load-bearing half: a >160-char document driven through the endpoint, its
    # footnote pinned against the KNOWN source text. Self-contained on purpose - later
    # tests re-compose the workspace, so leaning on an earlier test's store made this
    # assert against whatever manifest happened to be live. And it must exist at all
    # because the first version of this test checked only short snippets, and a reverted
    # hard-cut SURVIVED mutation - nothing over the wire ever crossed the cap (the #788
    # lesson, again: the fixture must be able to go red).
    sab_root = Path(tempfile.mkdtemp(prefix="dbse2e-748-"))
    (sab_root / "all-staff").mkdir()
    (sab_root / "all-staff" / "sabbatical.txt").write_text(LONG_POLICY)
    demo = client.get("/router/demo", headers=ALICE).json()["manifest"]
    demo["stores"].append({
        "id": "policy-archive-748", "kind": "folder", "mode": "index",
        "business_unit": "people", "acl": ["all-staff"], "title": "Policy archive",
        "description": "sabbatical policy archive",
        "config": {"path": str(sab_root)}})
    c = client.post("/router/compose", headers=ALICE, json={"manifest": demo})
    assert c.status_code == 200, c.text
    _await_ingest(c.json())
    r2 = client.post("/router/ask", headers=ALICE,
                     json={"question": "what is the sabbatical policy?"}).json()
    long_fns = [f for f in r2.get("footnotes", [])
                if (f["snippet"] or "").startswith("Sabbatical policy")]
    assert long_fns, r2.get("footnotes")
    s = long_fns[0]["snippet"]
    assert s.endswith("…"), f"a truncated snippet must carry the ellipsis: {s!r}"
    body = s[:-1]
    assert LONG_POLICY.startswith(body), (body, LONG_POLICY[:170])
    assert LONG_POLICY[len(body)] == " ", (
        f"cut mid-word: ...{body[-12:]!r} then {LONG_POLICY[len(body):len(body)+8]!r}")


def test_a_long_evidence_content_truncates_at_a_word_boundary():
    """Drive _snippet directly with the #748 string shape: >160 chars whose 160th char
    falls mid-word. The wire test above proves the endpoint uses it; this proves the
    helper's own contract, including that the pre-fix output (mid-word, no ellipsis) is
    impossible."""
    from dbsearch.server.router_api import _snippet

    long = ("Primary carers receive 18 weeks of fully paid parental leave, and secondary "
            "carers receive four weeks, both available flexibly within the first year "
            "after the birth or placement of the child in the family home.")
    assert len(long) > 160
    out = _snippet(long)
    assert out.endswith("…"), out
    body = out[:-1]
    assert len(body) <= 160, len(body)
    assert not body.endswith(" "), f"trailing space survived: {out!r}"
    # the body must be a whole-word prefix: the next character in the source is a space
    assert long.startswith(body), (body, long[:170])
    assert long[len(body)] == " ", (
        f"cut mid-word: ...{body[-12:]!r} then {long[len(body):len(body)+8]!r}")

    short = "fits whole"
    assert _snippet(short) == short, "a fitting snippet must pass through untouched"

    unbroken = "x" * 300
    out2 = _snippet(unbroken)
    assert out2 == "x" * 160 + "…", "no whitespace in reach: hard cut plus ellipsis"


def test_canvas_page_served():
    """#678: rewritten for the post-#643 architecture, NOT relaxed.

    This asserted `"stores.yml" in r.text` for a plain GET of /canvas. That became
    unsatisfiable on 260811 when #643 folded the canvas into the shell: /canvas is now in
    SHELL_PATHS (app.py) and serves index.html like every other surface, so the string moved
    into the canvas SURFACE MODULE and is rendered client-side. The test then failed on the
    second half of its `and` and reported a bare `200`, which reads like an HTTP problem and
    is why it sat red and unexplained for a day.

    The original intent was "the canvas page is reachable AND offers its stores.yml export",
    so both halves are still checked - just at the two places they now live. Asserting only
    the 200 would keep the test green while testing nothing: every SHELL_PATH returns the same
    shell, including ones that do not exist as surfaces at all."""
    r = client.get("/canvas")
    assert r.status_code == 200, r.status_code
    assert "/static/js/main.js" in r.text, (
        "/canvas no longer serves the app shell - the SHELL_PATHS wiring changed: "
        f"{r.text[:200]!r}")
    surface = client.get("/static/js/surfaces/canvas.js")
    assert surface.status_code == 200, surface.status_code
    assert "stores.yml" in surface.text, (
        "the canvas surface no longer offers the stores.yml export the manifest round-trip "
        "depends on")


def main():
    print("#109 /router API e2e:")
    test_kinds_lists_real_and_planned()
    test_route_before_compose_is_409()
    test_demo_compose_reports_stores_and_skips_cloud_kinds()
    test_probe_single_entry()
    test_alice_routes_and_asks_across_both_stores()
    test_bob_never_learns_finance_exists_over_http()
    test_identity_is_header_only()
    test_advisor_why_catalog_and_manual_override()
    test_compound_question_spans_semantic_and_sql()
    test_compose_with_folder_store_drives_connector_rail()
    test_folder_store_answers_with_law2_trim()
    # #939: registered HERE and not merely defined - this file's runner calls its tests by
    # name, so a new test that is only written is a new test that never runs. Three of these
    # sat green and unexecuted for exactly that reason before this line was added.
    test_documents_endpoint_names_the_files_and_trims_them_per_caller()
    test_documents_endpoint_reports_freshness_that_is_current_not_composed()
    test_documents_endpoint_hides_a_store_the_caller_cannot_enumerate()
    test_sync_endpoint_delta_crawls()
    test_sync_unknown_or_non_connector_store_404()
    test_conversational_setup_talks_a_folder_live()
    test_setup_uses_llm_entry_parser_when_model_supports_it()
    test_probe_recommends_native_mode_upgrade()
    test_compose_accepts_delegation_block()
    test_ask_stamps_sql_proof_and_rerun_roundtrip()
    test_rerun_unknown_and_invisible_store_same_404()
    test_ask_has_origin_and_resolvable_footnotes()
    test_footnote_snippets_never_cut_mid_word()
    test_a_long_evidence_content_truncates_at_a_word_boundary()
    test_canvas_page_served()
    print("  PASS  kinds / 409 pre-compose / compose+skip / probe / alice ask / "
          "bob no-leak / header auth / compound semantic+sql (#103) / "
          "folder compose+trim+delta-sync (#111) / setup-by-chat (#116) / "
          "snippet word-boundary (#748) / doc inventory + LAW2 filename trim (#939) / "
          "canvas served")
    print("\n#109 ROUTER API E2E PASSED.")


if __name__ == "__main__":
    main()
