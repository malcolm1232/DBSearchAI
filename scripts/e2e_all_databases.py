#!/usr/bin/env python3
"""#713 — cumulative routing-test matrix across ALL databases.

Implements the owner's protocol exactly:
  - databases are tested in a fixed order (legs);
  - running leg N re-composes and re-runs EVERY leg <= N (so a fix to leg k
    automatically retests A..k, never k alone);
  - the final run is `--upto all` (+ --cross) — everything checked together.

Usage:
  python3 matrix_driver.py --upto azure_sql          # legs up to and incl azure_sql
  python3 matrix_driver.py --upto all --cross        # the full final matrix
  python3 matrix_driver.py --upto all --cross --chat # ...asked on the Ask surface too
  python3 matrix_driver.py --list                    # show legs

--chat is ADR 0025's stated acceptance suite for #689: every transferable check is asked a
second time through /chat/stream and judged by the SAME assertions, so "the Ask surface
reaches the same stores with the same tallies" is a command rather than a sentence. It
needs DBSEARCH_ASK_ROUTES=1 on the server under test and asserts that first.

Runs against the 8081 dev-header rig (X-DBSearch-User). The canvas/Chrome pass
on 8080 is a separate, final gate — this driver is the L1 mechanics loop.
"""
import argparse
import json
import os
import sys
import urllib.request

BASE = os.environ.get("MATRIX_BASE", "http://127.0.0.1:8081")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDER_DOCS = os.path.join(REPO, "scratchpad", "matrix_folder_docs")
# AWS legs are parameterized: the bucket/workgroup are session-provisioned throwaways
# (see docs/HANDOVER + card #713). Unset -> those legs are skipped loudly.
S3_BUCKET = os.environ.get("MATRIX_S3_BUCKET", "")
RS_WORKGROUP = os.environ.get("MATRIX_REDSHIFT_WORKGROUP", "")

ALICE, BOB = "alice", "bob"

PASS, FAIL = 0, 0
FAILURES = []


def call(path, payload=None, user=ALICE, timeout=180):
    req = urllib.request.Request(BASE + path, method="POST" if payload is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if user:
        req.add_header("X-DBSearch-User", user)
    data = json.dumps(payload).encode() if payload is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as resp:
            return resp.getcode(), json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def sse_done(path, payload, user=ALICE, timeout=180):
    """POST an SSE endpoint and return (code, the `done` event). The conversational
    surface streams; #713's assertions are written against a single result dict, and the
    `done` event IS that dict — so the chat drive below can reuse the SAME assertion core
    the /router/ask drive uses rather than growing a second copy of it."""
    req = urllib.request.Request(BASE + path, method="POST")
    req.add_header("Content-Type", "application/json")
    if user:
        req.add_header("X-DBSearch-User", user)
    data = json.dumps(payload).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as resp:
            final = {}
            for raw in resp:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:].strip() or "{}")
                if ev.get("type") == "done":
                    final = ev
            return resp.getcode(), final
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def check(leg, name, ok, detail=""):
    global PASS, FAIL
    mark = "✓" if ok else "✗"
    print(f"  {mark} [{leg}] {name}" + (f"  — {detail}" if (detail and not ok) else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"[{leg}] {name} — {detail}")
    return ok


def vault_secret(store_id, field, value):
    """ADR 0010: literal credentials are refused at compose - store once via the
    credential panel's endpoint and reference the returned secret:// handle,
    exactly as the canvas does."""
    code, r = call("/secrets", {"store_id": store_id, "field": field,
                                        "value": value})
    if code != 200 or not r.get("handle"):
        raise SystemExit(f"FATAL: vaulting {store_id}.{field} failed: {code} {r}")
    return r["handle"]


def env_of(name):
    """Read one value from the repo .env (the driver itself runs env-less)."""
    for line in open(os.path.join(REPO, ".env")):
        line = line.strip()
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise KeyError(name)


def setup_cosmos_check():
    """Independent source tally: cosmos record proofs carry no rerun token
    (only kind=='sql' proofs do), so the driver counts the container directly
    and the check asserts the ANSWER carries the same number."""
    from azure.cosmos import CosmosClient
    c = CosmosClient(env_of("COSMOS_ENDPOINT"), env_of("COSMOS_KEY"))
    cont = c.get_database_client(env_of("COSMOS_DATABASE")).get_container_client("reviews")
    n = list(cont.query_items("SELECT VALUE COUNT(1) FROM c",
                              enable_cross_partition_query=True))[0]
    if n != 84:
        raise SystemExit(f"FATAL: cosmos reviews count changed: {n} != 84 — "
                         "update the leg's ground truth before trusting the matrix")


# ---------------------------------------------------------------- leg setups
def setup_folder_docs():
    os.makedirs(FOLDER_DOCS, exist_ok=True)
    p = os.path.join(FOLDER_DOCS, "facilities_badge_policy.txt")
    with open(p, "w") as f:
        f.write(
            "Facilities policy — visitor badges.\n"
            "Visitor badges at the Tuas office must be returned at the end of each visit.\n"
            "The replacement fee for a lost visitor badge is 35 dollars.\n"
            "Contractors keep their badges for the duration of the engagement.\n")


# ---------------------------------------------------------------- the legs
# Each leg: id -> {stores: [entries], setup: fn|None, checks: [check dicts]}
# check dict keys:
#   q             the question
#   pin           optional store id to pin (deterministic tally asks)
#   as_user       who asks (default alice)
#   expect_store  outcomes[] must contain this store with status ok
#   forbid_store  this store must NOT appear in outcomes with results
#   contains      list of fragments the answer text must contain (case-insens)
#   tally_rows    expected sorted [(str,int)] rows from re-running the sql proof
#   deny          True -> answer must contain no content from expect_store's data
#                 and the store must not be named (LAW 2 negative)

LEGS = []


def leg(id, **kw):
    kw["id"] = id
    LEGS.append(kw)


_DEMO_STORES = {}


def demo_store(store_id):
    """The product's own demo fixture entry for `store_id` — canonical shapes."""
    if not _DEMO_STORES:
        code, demo = call("/router/demo")
        if code != 200:
            raise SystemExit(f"FATAL: /router/demo -> {code}")
        for s in demo["manifest"]["stores"]:
            _DEMO_STORES[s["id"]] = s
    return _DEMO_STORES[store_id]


# --- A: local in-memory doc stores (the demo pair: hr-wiki + fin-ledger) ---
leg("local_docs",
    stores_fn=lambda: [demo_store("hr-wiki"), demo_store("fin-ledger")],
    checks=[
        {"q": "what is our parental leave policy?", "expect_store": "hr-wiki",
         "contains": ["sixteen weeks"]},
        # non-vacuous LAW 2 pair: alice (deal-team) SEES the ledger...
        {"q": "confidential revenue ledger invoices", "expect_store": "fin-ledger"},
        # ...and bob (all-staff only) must not see content OR existence
        {"q": "confidential revenue ledger invoices", "as_user": BOB, "deny": True,
         "deny_markers": ["fin-ledger", "four point two million"]},
    ])

# --- B: local structured (csv -> sqlite), the demo sales-figures fixture ---
leg("csv_sql",
    stores_fn=lambda: [demo_store("sales-figures")],
    checks=[
        {"q": "total amount by region", "pin": "sales-figures",
         "tally_rows": [("apac", 60), ("emea", 140)]},
    ])

# --- C: folder (real files on disk; operator-only — fine on the dev rig) ---
leg("folder_docs",
    setup=setup_folder_docs,
    stores=[
        {"id": "facilities-folder", "kind": "folder", "mode": "index",
         "business_unit": "facilities", "acl": ["all-staff"], "title": "Facilities policies",
         "description": "facilities documents: visitor badges, office access",
         "config": {"path": FOLDER_DOCS}},
    ],
    checks=[
        {"q": "what is the replacement fee for a lost visitor badge?",
         "expect_store": "facilities-folder", "contains": ["35"]},
    ])

# --- D: Azure SQL (live dbslivesqlcf5535 / dbslivesql / dbo.sales fixture) ---
leg("azure_sql",
    stores=[
        {"id": "azure-deals", "kind": "azure_sql", "mode": "pushdown",
         "business_unit": "sales", "acl": ["deal-team"], "title": "Azure SQL deals",
         "description": "closed deals revenue amount by region product",
         "config": {"server": "${AZURE_SQL_SERVER}", "database": "${AZURE_SQL_DATABASE}",
                    "user": "${AZURE_SQL_USER}", "password": "${AZURE_SQL_PASSWORD}",
                    "use_odbc": True, "tables": ["sales"]}},
    ],
    checks=[
        {"q": "what is the total closed deal amount by region?",
         "expect_store": "azure-deals",
         "tally_rows": [("amer", 195000), ("apac", 205000), ("emea", 125000)]},
        # LAW 2: bob is not deal-team; the deals store must stay invisible to him
        {"q": "what is the total closed deal amount by region?", "as_user": BOB,
         "deny": True, "deny_markers": ["azure-deals", "205,000", "205000"]},
    ])

# --- E: Azure SQL / dbsampleaw (AdventureWorksLT — the real-question DB) ---
# ground truth from seed_azure_fleet --verify 260813: top seller
# BK-T79U-60 Touring-1000 Blue, 60 units ($37,191); 24 products sold, 30 customers
leg("azure_sql_aw",
    stores=[
        {"id": "aw-sales", "kind": "azure_sql", "mode": "pushdown",
         "business_unit": "sales", "acl": ["all-staff"], "title": "AdventureWorks sales",
         "description": "transactional customer sales orders (AdventureWorks): order line items, units sold and revenue per product, customer accounts",
         "config": {"server": "${AZURE_SQL_SERVER}", "database": "dbsampleaw",
                    "user": "${AZURE_SQL_USER}", "password": "${AZURE_SQL_PASSWORD}",
                    "use_odbc": True,
                    "tables": ["SalesLT.SalesOrderHeader", "SalesLT.SalesOrderDetail",
                               "SalesLT.Product", "SalesLT.ProductCategory",
                               "SalesLT.Customer"]}},
    ],
    checks=[
        # ground truth VERIFIED against the source 260813: by units it is
        # Classic Vest, S (87). PINNED: once the Synapse DW exists, "most units"
        # legitimately fans out (sales_daily also holds units) — the tally must
        # not depend on winning that race.
        {"q": "which product sold the most units?", "pin": "aw-sales",
         "contains": ["classic vest", "87"]},
        # the seeder's designed TRANSACTIONAL routing question (1 order, verified).
        # KNOWN GAP #715: 4 stores score within 0.04, the selector single-routes to
        # mysql-orders, it declines, and no rescue follows. Runs every pass; reported
        # loudly; does not gate the matrix while the selector design call is pending.
        {"q": "which orders did customer 29485 place?", "expect_store": "aw-sales",
         "known_gap": "#715"},
    ])

# --- F: Azure Postgres (support.tickets, 132 rows — verified 260813) ---
leg("postgres",
    stores=[
        {"id": "support-tickets", "kind": "postgres", "mode": "pushdown",
         "business_unit": "support", "acl": ["all-staff"], "title": "Support tickets (Postgres)",
         "description": "customer support tickets: product, status, priority, assigned team",
         "config": {"host": "${AZURE_PG_HOST}", "database": "${AZURE_PG_DATABASE}",
                    "user": "${AZURE_PG_USER}", "password": "${AZURE_PG_PASSWORD}",
                    "tables": ["support.tickets"]}},
    ],
    checks=[
        {"q": "how many support tickets do we have in total?",
         "expect_store": "support-tickets", "tally_scalar": 132},
    ])

# --- G: Azure MySQL — TWO databases on one server (between-database routing
#        within one engine). `storefront` (page_views 552 + cart_events 84,
#        seeded 260813) is a SEPARATE database from .env's dbslivemysql, which
#        holds the old `orders` fixture (revenue by channel, runbook §3).
leg("mysql",
    stores=[
        {"id": "storefront", "kind": "mysql", "mode": "pushdown",
         "business_unit": "ecommerce", "acl": ["all-staff"], "title": "Storefront analytics (MySQL)",
         "description": "storefront web analytics: product page views and shopping cart events",
         "config": {"host": "${AZURE_MYSQL_HOST}", "database": "storefront",
                    "user": "${AZURE_MYSQL_USER}", "password": "${AZURE_MYSQL_PASSWORD}",
                    "tables": ["page_views", "cart_events"]}},
        {"id": "mysql-orders", "kind": "mysql", "mode": "pushdown",
         "business_unit": "ecommerce", "acl": ["all-staff"], "title": "Online orders (MySQL)",
         "description": "small online widget shop orders: revenue and quantity per sku (widgets, gadgets, gizmos)",
         "config": {"host": "${AZURE_MYSQL_HOST}", "database": "${AZURE_MYSQL_DATABASE}",
                    "user": "${AZURE_MYSQL_USER}", "password": "${AZURE_MYSQL_PASSWORD}",
                    "tables": ["orders"]}},
    ],
    checks=[
        {"q": "how many product page views did the storefront get in total?",
         "expect_store": "storefront", "tally_scalar": 552},
        # runbook §3's "revenue by channel" ground truth is STALE — the live
        # `orders` table is (id, sku, qty, revenue), no channel column, and the
        # store correctly DECLINED the channel question (no-fabrication, #211).
        # True ground truth verified at the source 260813:
        {"q": "what is the revenue for each product sku in our online orders?",
         "expect_store": "mysql-orders",
         "tally_rows": [("gadget-b", 8800), ("gizmo-c", 4800), ("widget-a", 7500)]},
    ])

# --- H: Cosmos DB (reviews container, 84 docs — verified 260813; .env's
#        COSMOS_CONTAINER=tickets is STALE, the seeder recreated it as `reviews`) ---
leg("cosmos_db",
    stores=[
        {"id": "product-reviews", "kind": "cosmos_db", "mode": "pushdown",
         "business_unit": "product", "acl": ["all-staff"], "title": "Product reviews (Cosmos)",
         "description": "customer product reviews with star ratings and comments",
         "config": {"endpoint": "${COSMOS_ENDPOINT}", "database": "${COSMOS_DATABASE}",
                    "container": "reviews", "key": "${COSMOS_KEY}"}},
    ],
    setup=setup_cosmos_check,
    checks=[
        # no rerun tally: record proofs are un-rerunnable (kind!='sql'); the
        # setup counted the container at the source instead (84).
        {"q": "how many customer reviews have we received in total?",
         "expect_store": "product-reviews", "contains": ["84"]},
    ])

# --- I: Synapse (dwpool RECREATED + reseeded this session; delete after!) ---
# ground truth verified at source 260813: 96 spend rows, 192 sales_daily rows,
# spend by channel email 79,520 / paid-search 69,600 / social 89,440.
# (runbook §4's "AMER $1.56M" is from the OLD sales_fact table — stale.)
leg("synapse",
    stores=[
        {"id": "marketing-dw", "kind": "synapse", "mode": "pushdown",
         "business_unit": "marketing", "acl": ["all-staff"], "title": "Marketing DW (Synapse)",
         "description": "marketing analytics warehouse: advertising spend by channel (email, paid-search, social) and region, plus daily aggregated sales revenue facts",
         "config": {"server": "${SYNAPSE_SERVER}", "database": "${SYNAPSE_POOL}",
                    "user": "${SYNAPSE_USER}", "password": "${SYNAPSE_PASSWORD}",
                    "use_odbc": True,
                    "tables": ["dw.marketing_spend", "dw.sales_daily"]}},
    ],
    checks=[
        {"q": "how much did we spend on marketing by channel?",
         "expect_store": "marketing-dw",
         "tally_rows": [("email", 79520), ("paid-search", 69600), ("social", 89440)]},
        # the seeder's designed AGGREGATE half of the 'sales' overlap pair.
        # KNOWN GAP #718: half 1 routes to the demo csv (near-tie, #715 shape),
        # its emea/apac vocabulary poisons the key-carry bind for half 2, and
        # marketing-dw - which holds BOTH halves - answers empty. Pinned, the
        # sub-question tallies perfectly (UK 140,880 / US 97,680).
        {"q": "how did sales revenue compare to marketing spend by region?",
         "expect_store": "marketing-dw", "known_gap": "#718"},
    ])

# --- J: S3 (fresh bucket this session; ambient-identity self-host path on 8081;
#        the vaulted-keys product path is the Chrome phase, as #673 proved it) ---
leg("s3",
    stores=[
        {"id": "warranty-docs", "kind": "s3", "mode": "index",
         "business_unit": "support", "acl": ["alice"], "title": "Warranty policies (S3)",
         "description": "hardware warranty policy documents: coverage periods, claims",
         "config": {"bucket": S3_BUCKET, "prefix": "policies/",
                    "region": "ap-southeast-1"}},
    ],
    wait_ingested=["warranty-docs"],
    checks=[
        {"q": "what is the standard hardware warranty period for the APAC region?",
         "expect_store": "warranty-docs", "contains": ["18 months"]},
        # LAW 2 slice-1: S3 docs are ACL'd to the linking user ALONE
        {"q": "what is the standard hardware warranty period for the APAC region?",
         "as_user": BOB, "deny": True, "deny_markers": ["18 months", "warranty-docs"]},
    ])

# --- K: Redshift Serverless (workgroup created this session; Data API, ambient) ---
# ground truth seeded+verified 260813: SIN-HKG 8000 / SIN-NRT 15000 / SIN-SYD 12000
leg("redshift",
    stores=[
        {"id": "freight-costs", "kind": "redshift", "mode": "pushdown",
         "business_unit": "logistics", "acl": ["all-staff"], "title": "Freight costs (Redshift)",
         "description": "freight shipping costs in usd per shipping lane",
         "config": {"workgroup": RS_WORKGROUP, "database": "dev",
                    "region": "ap-southeast-1", "tables": ["freight_costs"]}},
    ],
    checks=[
        {"q": "what is the total freight cost per shipping lane?",
         "expect_store": "freight-costs",
         "tally_rows": [("sin-hkg", 8000), ("sin-nrt", 15000), ("sin-syd", 12000)]},
    ])

# --- L/M: the rds_* alias rails (#672). LIVE RDS IS IAM-BLOCKED for this
# account's user (no ec2:AuthorizeSecurityGroupIngress -> instances unreachable),
# so these run the EXACT rds_* kinds/engines with literal human-typed creds over
# TCP against reachable hosts, on tables seeded for this purpose. When the owner
# unblocks IAM, flip host/creds to the dbsearch-e2e-{pg,mysql} endpoints and
# re-run the same checks.
leg("rds_postgres",
    stores_fn=lambda: [
        {"id": "rds-headcount", "kind": "rds_postgres", "mode": "pushdown",
         "business_unit": "hr", "acl": ["all-staff"], "title": "HR headcount (RDS Postgres rail)",
         "description": "employee headcount per department",
         "config": {"host": env_of("AZURE_PG_HOST"), "database": env_of("AZURE_PG_DATABASE"),
                    "user": env_of("AZURE_PG_USER"),
                    "password": vault_secret("rds-headcount", "password",
                                             env_of("AZURE_PG_PASSWORD")),
                    "tables": ["hr_headcount"]}},
    ],
    checks=[
        {"q": "what is the employee headcount per department?",
         "expect_store": "rds-headcount",
         "tally_rows": [("engineering", 12), ("operations", 4), ("sales", 7)]},
    ])

leg("rds_mysql",
    stores_fn=lambda: [
        {"id": "rds-suppliers", "kind": "rds_mysql", "mode": "pushdown",
         "business_unit": "procurement", "acl": ["all-staff"], "title": "Suppliers (RDS MySQL rail)",
         "description": "component suppliers: country and lead time in days",
         "config": {"host": env_of("AZURE_MYSQL_HOST"), "database": env_of("AZURE_MYSQL_DATABASE"),
                    "user": env_of("AZURE_MYSQL_USER"),
                    "password": vault_secret("rds-suppliers", "password",
                                             env_of("AZURE_MYSQL_PASSWORD")),
                    "tables": ["suppliers"]}},
    ],
    checks=[
        {"q": "what is the lead time in days for each of our suppliers?",
         "expect_store": "rds-suppliers",
         "tally_rows": [("acme metals", 21), ("borneo plastics", 35), ("cheng precision", 14)]},
    ])

# ------------------------------------------------ cross-database checks (--cross)
# Federation + doc×sql traversal — every entry names the legs it needs so the
# runner skips (loudly) any check whose stores aren't composed yet.
CROSS_CHECKS = [
    {"needs": ["local_docs", "azure_sql"],
     "q": "What is our parental leave policy, and what is the total closed deal "
          "amount by region?",
     "expect_stores": ["hr-wiki", "azure-deals"]},
    # revenue legitimately lives in THREE stores (aw-sales transactional,
    # marketing-dw aggregate, mysql-orders widgets) — the federation must reach
    # the tickets/views store plus ANY revenue source, not one anointed pair.
    {"needs": ["postgres", "azure_sql_aw"],
     "q": "which products generate the most support tickets, and how much revenue "
          "do they bring in?",
     "expect_stores": ["support-tickets"],
     "expect_any": ["aw-sales", "marketing-dw", "mysql-orders"]},
    {"needs": ["mysql", "azure_sql_aw"],
     "q": "which products are viewed a lot on the storefront but rarely bought?",
     "expect_stores": ["storefront"],
     "expect_any": ["aw-sales", "marketing-dw", "mysql-orders"]},
    # KNOWN GAP #718 (referent-carry instance): cosmos receives 'what do customers
    # say about THIS PRODUCT' — the best-seller sku is never substituted, so the
    # one store that holds reviews declines a dangling referent.
    {"needs": ["cosmos_db", "azure_sql_aw"],
     "q": "what do customers say in reviews about our best-selling product?",
     "expect_stores": ["product-reviews"], "known_gap": "#718"},
    # doc (AWS S3) × sql (Azure SQL) — cross-cloud, cross-modality
    {"needs": ["s3", "azure_sql"],
     "q": "What is the standard hardware warranty period for APAC, and what is the "
          "total closed deal amount for the apac region?",
     "expect_stores": ["warranty-docs", "azure-deals"]},
    # sql (AWS Redshift) × sql (Azure Synapse) — cross-cloud federation
    # KNOWN GAP #718 (bind-poisoning instance, cross-cloud): the freight lanes
    # (SIN-*) from half 1 get bound into marketing-dw's query -> honest empty.
    {"needs": ["redshift", "synapse"],
     "q": "how do total freight costs per shipping lane compare with marketing "
          "spend by channel?",
     "expect_stores": ["freight-costs", "marketing-dw"], "known_gap": "#718"},
]


def wait_ingested(store_ids, timeout=180):
    """Poll the catalog until each store reports non-ingesting freshness (#454:
    ingest is async; asking during ingest legitimately answers from nothing)."""
    import time
    deadline = time.time() + timeout
    pending = set(store_ids)
    while pending and time.time() < deadline:
        _, cat = call("/router/catalog")
        for bu in cat.get("business_units", []):
            for src in bu.get("sources", []):
                for st in src.get("stores", []):
                    if st.get("store_id") in pending and st.get("freshness") != "ingesting":
                        pending.discard(st.get("store_id"))
        if pending:
            time.sleep(3)
    return not pending

# ---------------------------------------------------------------- runner
def leg_stores(lg):
    if "stores" not in lg and "stores_fn" in lg:
        lg["stores"] = lg["stores_fn"]()
    return lg["stores"]


def build_manifest(active):
    stores = []
    for lg in active:
        stores.extend(leg_stores(lg))
    return {"tenant": "acme", "stores": stores}


def probe_all(active):
    ok_all = True
    for lg in active:
        for st in leg_stores(lg):
            code, r = call("/router/probe", {"entry": st})
            ok = code == 200 and r.get("available") is True
            ok_all &= check(lg["id"], f"CONNECT probe {st['id']}", ok,
                            f"code={code} resp={json.dumps(r)[:160]}")
    return ok_all


GAPS = []


CHAT_SURFACE = [False]      # --chat: also drive every transferable check through Ask
NOT_TRANSFERABLE = []


def _one_surface(lg, c, surface):
    if c.get("known_gap"):
        global PASS, FAIL
        p0, f0 = PASS, FAIL
        ok = _run_check_inner(lg, c, surface)
        if not ok:
            # roll back the FAIL counts this check added; record as a gap instead
            n_new_fails = FAIL - f0
            FAIL = f0
            del FAILURES[len(FAILURES) - n_new_fails:]
            GAPS.append(f"[{lg['id']}/{surface}] {c['q'][:60]!r} — known gap {c['known_gap']}")
            print(f"  ⚠ [{lg['id']}] KNOWN-GAP {c['known_gap']}: {c['q'][:60]!r} still fails "
                  f"on {surface}")
        else:
            print(f"  ★ [{lg['id']}] known gap {c['known_gap']} PASSES on {surface} — "
                  "consider closing the card")
        return ok
    return _run_check_inner(lg, c, surface)


def run_check(lg, c):
    """#689 / ADR 0025: every check is asked on BOTH surfaces, and the assertions are the
    same object — `_run_check_inner` — so the two surfaces cannot drift on what counts as
    routed, contained or tallied. A divergence between them IS the defect this card is
    about, so it must not be possible to satisfy one drive's expectations with the other's
    code."""
    ok = _one_surface(lg, c, "router")
    if CHAT_SURFACE[0]:
        if c.get("pin"):
            # A conversational turn has no store field (ChatRequest is conv_id+question+
            # model, by LAW 2's own rule that nothing in the body chooses scope), so a
            # PINNED check cannot be re-asked here as written. Named out loud rather than
            # dropped: an un-run check that prints nothing reads as a passing one.
            NOT_TRANSFERABLE.append(f"[{lg['id']}] {c['q'][:60]!r} (pin={c['pin']})")
            print(f"  ~ [{lg['id']}] chat: NOT TRANSFERABLE — pinned to {c['pin']}, and the "
                  "Ask surface cannot pin")
        else:
            ok &= _one_surface(lg, c, "chat")
    return ok


def assert_chat_routes(active):
    """The PRECONDITION for every chat check below: is this server's Ask surface actually
    routing at all?

    Without this, `DBSEARCH_ASK_ROUTES=0` makes every chat check fail with 'did not route
    to X' — which reads exactly like a routing defect and is not one. The document path's
    done event has no `outcomes` key; the routed one always does, even when every store
    declined. So the KEY's presence discriminates the flag, and its absence is reported as
    what it is."""
    q = next((c["q"] for lg in active for c in lg["checks"]
              if not c.get("pin") and not c.get("deny")), "what data do you have?")
    code, ev = sse_done("/chat/stream", {"conv_id": "matrix-ask-precondition",
                                         "question": q})
    routed = code == 200 and "outcomes" in ev
    check("precondition", "Ask surface routes (DBSEARCH_ASK_ROUTES on)", routed,
          f"code={code} keys={sorted(ev)[:12]} — the done event carries no `outcomes`, so "
          f"this server answers Ask from documents alone. Every chat check below would fail "
          f"for THAT reason and not for a routing defect. Set DBSEARCH_ASK_ROUTES=1 on "
          f"{BASE} and re-run.")
    return routed


def _drive_router(c, user):
    """The canvas's own surface: POST /router/ask, which may PIN a store."""
    q = {"question": c["q"]}
    if c.get("pin"):
        q["store"] = c["pin"]
    return call("/router/ask", q, user=user)


CHAT_CONV = [0]


def _drive_chat(c, user):
    """The Ask surface (#689 / ADR 0025): the SAME question, asked conversationally.

    A FRESH conv_id per check, deliberately. A reused thread condenses the new question
    against the previous turn, so a check would be asking a question this matrix never
    wrote down — and the follow-up behaviour it would then be measuring belongs in a
    check that says so, not in every check by accident."""
    CHAT_CONV[0] += 1
    conv = f"matrix-ask-{CHAT_CONV[0]}"
    return sse_done("/chat/stream", {"conv_id": conv, "question": c["q"]}, user=user)


DRIVES = {"router": _drive_router, "chat": _drive_chat}


def _run_check_inner(lg, c, surface="router"):
    user = c.get("as_user", ALICE)
    code, r = DRIVES[surface](c, user)
    blob = json.dumps(r).lower()
    answer = (r.get("answer") or "").lower()
    label = f"{surface}[{user}] {c['q'][:48]!r}"

    if c.get("deny"):
        leaked = [m for m in c.get("deny_markers", []) if m.lower() in blob]
        ok = check(lg["id"], f"{label} DENIED (LAW 2)", code == 200 and not leaked,
                   f"code={code} leaked={leaked} answer={answer[:100]}")
        if surface == "chat":
            # A denial is only evidence if the routed plane ANSWERED this caller. When the
            # delegate declines (nothing this caller can see), the turn falls back to the
            # document path and leaks nothing — a green that says nothing about LAW 2, and
            # the exact shape of an empty success hiding an outage. `outcomes` present ==
            # the router produced this turn.
            ok &= check(lg["id"], f"{label} denial is NON-VACUOUS (routed turn)",
                        "outcomes" in r,
                        f"the delegate declined for {user}, so the document path answered "
                        f"and nothing was trimmed by the router at all; keys={sorted(r)[:12]}")
        return ok

    ok = check(lg["id"], f"{label} answered", code == 200 and bool(answer),
               f"code={code} resp={json.dumps(r)[:200]}")
    if not ok:
        return False
    if c.get("expect_store"):
        outs = r.get("outcomes", [])
        hit = any(o.get("store_id") == c["expect_store"] and o.get("status") == "ok"
                  for o in outs)
        ok &= check(lg["id"], f"{label} ROUTED -> {c['expect_store']}", hit,
                    f"outcomes={[(o.get('store_id'), o.get('status')) for o in outs]}")
    for frag in c.get("contains", []):
        ok &= check(lg["id"], f"{label} answer contains {frag!r}",
                    frag.lower() in answer, f"answer={answer[:160]}")
    if c.get("tally_rows") is not None or c.get("tally_scalar") is not None:
        sqlp = [p for cit in r.get("citations", [])
                for p in [cit.get("proof") or cit] if p.get("sql")]
        ok &= check(lg["id"], f"{label} has sql proof", bool(sqlp),
                    json.dumps(r.get("citations", []))[:160])
        if sqlp:
            # The proof belonging to the store this check is ABOUT, not merely the first one
            # in the list. A federated answer carries a proof per store, and the Ask surface
            # cannot pin — so "citations[0]" silently tallies whichever store happened to be
            # cited first and reads as a pass or a fail for the wrong store entirely.
            want = c.get("expect_store") or c.get("pin")
            p = next((x for x in sqlp if x.get("store_id") == want), sqlp[0])
            _, x = call("/router/rerun", {"store_id": p["store_id"], "sql": p["sql"],
                                           "token": p.get("rerun_token")}, user=user)
            rows = x.get("rows", [])
            if c.get("tally_rows") is not None:
                # A row of a different WIDTH is a failing tally, not a crashed matrix: the
                # driver re-runs the proof's own SQL, and a query returning three columns is
                # a real (and interesting) answer to compare against a two-column expectation.
                shaped = [row for row in rows if len(row) == 2]
                got = (sorted((str(a).lower(), int(round(float(b)))) for a, b in shaped)
                       if shaped else [])
                exp = sorted((s, v) for s, v in c["tally_rows"])
                if len(shaped) != len(rows):
                    ok &= check(lg["id"], f"{label} re-run rows are (label, value) pairs",
                                False, f"{len(rows) - len(shaped)} row(s) of another width; "
                                       f"store={p.get('store_id')} rows={rows[:3]}")
                ok &= check(lg["id"], f"{label} re-run rows TALLY", got == exp,
                            f"got={got} expected={exp}")
            else:
                flat = [v for row in rows for v in row
                        if isinstance(v, (int, float)) or str(v).replace(".", "").isdigit()]
                got = int(round(float(flat[0]))) if len(flat) == 1 and len(rows) == 1 else None
                ok &= check(lg["id"], f"{label} re-run scalar TALLY", got == c["tally_scalar"],
                            f"got={got} rows={rows[:5]} expected={c['tally_scalar']}")
    return ok


def run_cross(active_ids):
    global PASS, FAIL
    for c in CROSS_CHECKS:
        missing = [n for n in c["needs"] if n not in active_ids]
        label = f"cross {c['q'][:60]!r}"
        if missing:
            print(f"  ~ SKIP {label} (needs legs {missing})")
            continue
        p0, f0 = PASS, FAIL
        code, r = call("/router/ask", {"question": c["q"]})
        answer = (r.get("answer") or "").lower()
        outs = r.get("outcomes", [])
        got = {o.get("store_id") for o in outs if o.get("status") == "ok"}
        detail = f"ok-outcomes={sorted(got)} all={[(o.get('store_id'), o.get('status')) for o in outs]}"
        check("cross", f"{label} answered", code == 200 and bool(answer),
              f"code={code} resp={json.dumps(r)[:160]}")
        for sid in c["expect_stores"]:
            check("cross", f"{label} traversed {sid}", sid in got, detail)
        if c.get("expect_any"):
            check("cross", f"{label} traversed any of {c['expect_any']}",
                  bool(got.intersection(c["expect_any"])), detail)
        if c.get("known_gap") and FAIL > f0:
            n_new = FAIL - f0
            FAIL = f0
            del FAILURES[len(FAILURES) - n_new:]
            GAPS.append(f"[cross] {c['q'][:60]!r} — known gap {c['known_gap']}")
            print(f"  ⚠ [cross] KNOWN-GAP {c['known_gap']}: {c['q'][:50]!r} still fails")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upto", default="all", help="leg id (inclusive) or 'all'")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--cross", action="store_true", help="run cross-database checks too")
    ap.add_argument("--chat", action="store_true",
                    help="ALSO ask every transferable check through /chat/stream — the Ask "
                         "surface (#689 / ADR 0025). Needs DBSEARCH_ASK_ROUTES=1 on the "
                         "server under test; the run asserts that before it starts.")
    args = ap.parse_args()
    CHAT_SURFACE[0] = args.chat

    if args.list:
        for lg in LEGS:
            print(lg["id"])
        return 0

    ids = [lg["id"] for lg in LEGS]
    upto = len(LEGS) if args.upto == "all" else ids.index(args.upto) + 1
    active = LEGS[:upto]
    for lg in list(active):
        if lg["id"] == "s3" and not S3_BUCKET:
            print("~ SKIP leg s3: MATRIX_S3_BUCKET unset (provision a bucket first)")
            active.remove(lg)
        if lg["id"] == "redshift" and not RS_WORKGROUP:
            print("~ SKIP leg redshift: MATRIX_REDSHIFT_WORKGROUP unset")
            active.remove(lg)
    print(f"matrix: legs {ids[:upto]} (cumulative — every earlier leg re-runs)\n")

    for lg in active:
        if lg.get("setup"):
            lg["setup"]()

    # CONNECT: probe every store of every active leg
    probe_all(active)

    # COMPOSE: everything active at once (this is the routing reality: all stores
    # visible together, so a later leg can steal an earlier leg's questions — that
    # regression is exactly what cumulative re-runs exist to catch)
    manifest = build_manifest(active)
    code, r = call("/router/compose", {"manifest": manifest})
    skipped = r.get("skipped", [])
    composed = {s.get("store_id") for s in r.get("stores", [])}
    wanted = {s["id"] for s in manifest["stores"]}
    check("compose", f"compose {len(manifest['stores'])} stores, none skipped",
          code == 200 and not skipped and wanted <= composed,
          f"code={code} skipped={json.dumps(skipped)[:200]} composed={sorted(composed)}")

    # async ingest (#454): any leg that declares wait_ingested blocks here
    for lg in active:
        if lg.get("wait_ingested"):
            ok = wait_ingested(lg["wait_ingested"])
            check(lg["id"], f"ingest complete {lg['wait_ingested']}", ok)

    # ASK: every leg's checks, in leg order — on the router surface, and (with --chat)
    # on the Ask surface too, from the same question and the same assertions.
    if CHAT_SURFACE[0]:
        assert_chat_routes(active)
    for lg in active:
        for c in lg["checks"]:
            run_check(lg, c)

    if args.cross:
        print("\ncross-database checks (federation + doc×sql):")
        run_cross({lg["id"] for lg in active})

    print(f"\n{PASS} passed, {FAIL} failed" + (f", {len(GAPS)} known gap(s)" if GAPS else "")
          + (f", {len(NOT_TRANSFERABLE)} not transferable to Ask" if NOT_TRANSFERABLE else ""))
    for g in GAPS:
        print(f"  ⚠ {g}")
    for n in NOT_TRANSFERABLE:
        print(f"  ~ not asked on the Ask surface: {n}")
    if FAILURES:
        print("failures:")
        for f in FAILURES:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
