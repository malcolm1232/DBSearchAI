"""/router API — the live seam between the DB-canvas and Phase E (#109).

Exposes the E1 provider/compose layer, the E2 router, and the E3 executor+synthesizer
over HTTP so the canvas stops simulating. Identity is ALWAYS the authenticated header
user (LAW 2) — route/ask answers are gate-#1-trimmed per caller, so this API can never
name a store the caller isn't cleared to see. Responses carry metadata and post-trim
content only (LAW 1). Cloud kinds without a real provider are reported honestly as
planned (E4/E9) — compose SKIPS them with a reason instead of faking a connection.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from dbsearch import router as r
from dbsearch.adapters.local import InMemoryIdentity
from dbsearch.api.auth import DEMO_PREFIX, real_login_enabled
from dbsearch.router.demo_backing import demo_fixture_path, fixture_or_cloud_factory
from dbsearch.router.provenance import sign_rerun, verify_rerun
from dbsearch.router.secret_fields import find_secret_literals
from dbsearch.router.secret_handles import ScopedSecretResolver
from dbsearch.server.manifest_store import ManifestStoreUnavailable
from dbsearch.server.operators import is_operator
from dbsearch.server.scope import RequestScope, make_scope_builder
from dbsearch.server.workspaces import SHARED_KEY, WorkspacePool


# Palette kinds the canvas offers. Only kinds present in the provider registry are real
# today; the rest land with E4 (federated SQL) / E9 (compose for everything, ADR 0008).
PLANNED_KINDS = ("sharepoint", "folder", "bigquery", "redshift", "azure_sql",
                 "databricks", "postgres", "mysql", "synapse", "cosmos_db", "csv",
                 "rds_postgres", "rds_mysql",   # #672
                 "s3",                          # #673
                 "gdrive",                      # #712
                 "sharepoint_link")             # #924


def _merged_config(entry: dict) -> dict:
    """The store config a provider is handed. ONE definition, deliberately (#674).

    This dict literal existed in THREE copies - /router/probe, /router/health and the setup
    agent's health check - and a fourth, DIFFERENT one in provisioning.load_manifest. The
    compose copy carried the store's `acl` (added by #551 so a connector can inherit its
    store's audience); the three probe copies did not. So a provider that reads config["acl"]
    saw it at compose and never at Test-connection, and the two paths quietly disagreed about
    what the same store IS.

    That is not a cosmetic difference: Test-connection exists to answer "will this work when
    I compose it?", and a probe running against a different config cannot answer that in
    either direction. It surfaced as an s3 store (#673) reporting `probe 0ms - no acl is set`
    on a node whose audience was set and which composed correctly, and it has been silently
    weakening the folder connector's default_acl fallback since #551.

    `acl` is not a credential and providers already receive it at compose, so passing it here
    exposes nothing new - it simply stops the two paths lying to each other. Keep this as the
    single source: three copies are what allowed the drift.
    """
    return {"id": entry.get("id", "store"),
            "business_unit": entry.get("business_unit", ""),
            "title": entry.get("title", entry.get("id", "store")),
            "description": entry.get("description", ""),
            "acl": entry.get("acl", []),
            **entry.get("config", {})}

# ONE source of truth for the demo principals' group memberships. The scope seam
# builds the demo IdentityPort from this exact map (#340: catalog visibility must
# use the same groups the composed stores were built with).
DEMO_USER_GROUPS = {"alice": ["all-staff", "deal-team"], "bob": ["all-staff"]}

# Demo manifest keyed to the DEV identities (edition users alice/bob):
# hr-wiki is all-staff (both see it); fin-ledger is deal-team (alice only) — so the
# canvas demo proves gate #1 with the real user switcher.
DEMO_MANIFEST = {
    "tenant": "acme-demo",
    "stores": [
        {"id": "hr-wiki", "kind": "local", "business_unit": "hr", "acl": ["all-staff"],
         "title": "HR Wiki",
         "description": "human resources parental leave holidays onboarding benefits",
         "config": {
             "seed": [{"external_id": "handbook", "title": "Staff Handbook", "uri": "u1",
                       "acl": ["all-staff"], "text": "parental leave is sixteen weeks"}],
             "user_groups": dict(DEMO_USER_GROUPS),
         }},
        {"id": "fin-ledger", "kind": "local", "business_unit": "finance",
         "acl": ["deal-team"], "title": "Finance Ledger",
         "description": "revenue invoices tax numbers ledger accounting "
                        "project falcon acquisition valuation deal",
         "config": {
             # Two deal-team-only docs, so the demo proves gate #1 on BOTH a revenue figure
             # and a named-deal valuation: alice (deal-team) sees them, bob (all-staff) is
             # denied. Distinct numbers on purpose - $4.2M revenue vs $4.2B valuation - so a
             # correct answer can't be a lucky echo of the other.
             "seed": [{"external_id": "q3", "title": "Q3 Ledger", "uri": "u2",
                       "acl": ["deal-team"],
                       "text": "confidential revenue four point two million"},
                      {"external_id": "falcon", "title": "Project Falcon Plan", "uri": "u3",
                       "acl": ["deal-team"],
                       "text": "Project Falcon is a confidential acquisition. Headline "
                               "valuation is 4.2 billion dollars, comprising 3.1 billion in "
                               "cash and 1.1 billion in stock, an 11.4x forward revenue "
                               "multiple. Expected to close in Q3. Deal team only."}],
             "user_groups": dict(DEMO_USER_GROUPS),
         }},
        {"id": "sales-figures", "kind": "csv", "business_unit": "sales",
         "acl": ["all-staff"], "title": "Sales figures (federated SQL)",
         "description": "sales figures totals sum average count amount by region owner",
         "config": {"tables": {"sales": {
             "columns": ["region", "owner", "amount"],
             "rows": [["emea", "alice", 100], ["emea", "bob", 40], ["apac", "alice", 60]],
         }}}},
    ],
}


# #279 Task 1 (3a): the badged fixture-backed demo fleet - four cloud SQL kinds, each
# answering from a bundled local fixture (SqliteEngine) instead of a real cloud connection
# (LAW 7: fixture-aware factories live only on the demo compose path, see demo_backing.py).
# All `all-staff` (both demo principals see them); the alice>bob contrast is carried by the
# doc stores (fin-ledger = deal-team = alice only), per the Slice-3 plan. Cosmos is
# intentionally NOT fixture-backed here - deferred to #282.
_fx = demo_fixture_path

DEMO_FLEET_STORES = [
    {"id": "azure-deals", "kind": "azure_sql", "business_unit": "sales", "acl": ["all-staff"],
     "title": "Azure SQL - deals", "description": "closed deals revenue amount by region product",
     "config": {"fixture": {"files": [_fx("azure_sql", "sales.csv")]},
                "user_groups": dict(DEMO_USER_GROUPS)}},
    {"id": "support-tickets", "kind": "postgres", "business_unit": "support", "acl": ["all-staff"],
     "title": "Azure Postgres - support tickets", "description": "support tickets resolution hours by region priority",
     "config": {"fixture": {"files": [_fx("postgres", "support_tickets.csv")]},
                "user_groups": dict(DEMO_USER_GROUPS)}},
    {"id": "storefront", "kind": "mysql", "business_unit": "sales", "acl": ["all-staff"],
     "title": "Azure MySQL - storefront", "description": "storefront orders amount by region category",
     "config": {"fixture": {"files": [_fx("mysql", "storefront_orders.csv")]},
                "user_groups": dict(DEMO_USER_GROUPS)}},
    {"id": "warehouse", "kind": "synapse", "business_unit": "sales", "acl": ["all-staff"],
     "title": "Azure Synapse - warehouse", "description": "warehouse units by region sku",
     "config": {"fixture": {"files": [_fx("synapse", "warehouse_sales.csv")]},
                "user_groups": dict(DEMO_USER_GROUPS)}},
]


def demo_fleet_display() -> list:
    """The pre-composed demo catalog as a SANITIZED display manifest for the canvas demo mode
    (#279): the two doc stores + the four badged fixture connectors, with only the fields the
    canvas needs to render a read-only node (id/kind/business_unit/title/description/acl). The
    `fixture:`/`seed:` internals are stripped - the demo visitor never sees or edits them. This
    is what a `demo:*` identity's /router/ask actually routes against, so display == ask target."""
    docs = [s for s in DEMO_MANIFEST["stores"] if s["id"] in ("hr-wiki", "fin-ledger")]
    out = []
    for s in docs + DEMO_FLEET_STORES:
        out.append({"id": s["id"], "kind": s["kind"],
                    "business_unit": s.get("business_unit", ""),
                    "title": s.get("title", s["id"]),
                    "description": s.get("description", ""),
                    "acl": s.get("acl", [])})
    return out


class ComposeRequest(BaseModel):
    manifest: dict


class SetupTurnRequest(BaseModel):
    conv_id: str
    message: str = ""
    intent: str = "chat"            # chat | ready | apply | cancel (C1, card #116)


class ProbeRequest(BaseModel):
    entry: dict


class QuestionRequest(BaseModel):
    question: str
    # E7 manual pin — only a VISIBLE store can match. Optional[] (not `str | None`):
    # pydantic must eval this at runtime and local python is 3.9.
    store: Optional[str] = None


class RerunRequest(BaseModel):
    store_id: str
    sql: str
    token: str


class _State:
    """Composed-catalog state for this server process (demo scope: one live catalog)."""

    def __init__(self, identity=None, on_sync=None, sql_generator=None,
                 cosmos_generator=None,
                 embedder=None, floor_vector_rescue=0.0,
                 fixture_backed: bool = False, shared_doc_qs=None,
                 value_llm=None, job_store=None, job_partition: str = "") -> None:
        # #279 (ADR 0009): the demo scope's registry hands the four SQL cloud providers a
        # fixture-aware engine factory - a store whose config carries a `fixture:` block
        # resolves to a local SqliteEngine instead of the real cloud, while keeping its
        # real `kind` so origins.py still badges it. LAW 7/LAW 2: this is opt-in and OFF
        # by default, so the live/user compose path is byte-identical to before - a
        # user-submitted `fixture:` there stays inert (see demo_backing.py, Slice 1/2).
        def _engine_factory(cloud_from_config):
            return fixture_or_cloud_factory(cloud_from_config) if fixture_backed else None

        self.registry = r.ProviderRegistry()
        # #107 OBO: ONE broker for the composed catalog — manifest `delegation:`
        # blocks register on it at compose; SQL stores authorize() through it.
        self.broker = r.IdentityBroker(identity) if identity is not None else None
        self.registry.register(r.LocalIndexProvider(
            embedder=embedder, floor_vector_rescue=floor_vector_rescue))
        # E3b: `mode: native` via Graph Search — real provider; queries need GRAPH_TOKEN
        # (dev spike) or the E5 OBO exchange; an uncredentialed store drops + discloses.
        self.registry.register(r.GraphSearchProvider())
        # E4: loose csv/inline tables as a federated SQL store (in-tenant sqlite engine).
        # #135: every SQL provider shares the capability-gated NL2SQL generator (real
        # chat model when available, else the naive keyword default inside the store).
        self.registry.register(r.CsvSqlProvider(broker=self.broker, sql_generator=sql_generator, value_llm=value_llm,
                                          embedder=embedder))
        # #107: cloud pushdown engines — creds/auth stay server-side (LAW 1: ${ENV}
        # refs for Azure SQL, ADC for BigQuery, SigV4 for Redshift); each cloud SDK
        # imports lazily at compose time (LAW 7). A store that fails to build/probe
        # is SKIPPED with its reason, never fatal (load_manifest skipped list).
        self.registry.register(r.AzureSqlProvider(broker=self.broker, sql_generator=sql_generator, value_llm=value_llm,
                                            embedder=embedder,
                                            engine_factory=_engine_factory(r.AzureSqlEngine.from_config)))
        self.registry.register(r.BigQueryProvider(broker=self.broker, sql_generator=sql_generator, value_llm=value_llm,
                                            embedder=embedder))
        self.registry.register(r.RedshiftProvider(broker=self.broker, sql_generator=sql_generator, value_llm=value_llm,
                                            embedder=embedder))
        self.registry.register(r.DatabricksProvider(broker=self.broker, sql_generator=sql_generator, value_llm=value_llm,
                                              embedder=embedder))
        # #155: Azure Database for PostgreSQL (and any PostgreSQL) on the SAME pushdown rail —
        # psycopg imports lazily (LAW 7); ${ENV} creds resolve server-side (LAW 1).
        self.registry.register(r.PostgresProvider(broker=self.broker, sql_generator=sql_generator, value_llm=value_llm,
                                            embedder=embedder,
                                            engine_factory=_engine_factory(r.PostgresEngine.from_config)))
        # #158: Azure Database for MySQL (and any MySQL) — PyMySQL imports lazily (LAW 7), TLS required.
        self.registry.register(r.MySqlProvider(broker=self.broker, sql_generator=sql_generator, value_llm=value_llm,
                                         embedder=embedder,
                                         engine_factory=_engine_factory(r.MySqlEngine.from_config)))
        # #672: Amazon RDS / Aurora on the SAME engines under truthful names. No new capability -
        # RDS Postgres is Postgres over TLS - but `postgres` was reachable only through a canvas
        # group labelled Azure, and origins.SYSTEM cited every such store as "Azure Postgres".
        # A distinct kind makes the stored manifest say what the store actually is.
        # ADR 0026 (#814): the RDS kinds ride their OWN engines - no password required,
        # the caller's vaulted aws_keys redeem into an IAM auth token at connect time.
        self.registry.register(r.RdsPostgresProvider(broker=self.broker, sql_generator=sql_generator,
                                            value_llm=value_llm, embedder=embedder,
                                            engine_factory=_engine_factory(r.RdsPostgresEngine.from_config)))
        self.registry.register(r.RdsMySqlProvider(broker=self.broker, sql_generator=sql_generator,
                                            value_llm=value_llm, embedder=embedder,
                                            engine_factory=_engine_factory(r.RdsMySqlEngine.from_config)))
        # #159: Azure Synapse (dedicated SQL pool) — same TDS engine as azure_sql, warehouse target.
        self.registry.register(r.SynapseProvider(broker=self.broker, sql_generator=sql_generator, value_llm=value_llm,
                                           embedder=embedder,
                                           engine_factory=_engine_factory(r.AzureSqlEngine.from_config)))
        # #160: Azure Cosmos DB (NoSQL / Core API) — its OWN FEDERATED_DOC store (not the SQL rail);
        # azure-cosmos imports lazily (LAW 7). Deterministic Cosmos-SQL generator for now.
        self.registry.register(r.CosmosProvider(broker=self.broker,
                                               generator=cosmos_generator))
        # #111: the document-connector rail on the SAME Lego surface — `mode: index`
        # kinds whose build() registers a connector, runs the initial crawl, and keeps
        # a delta cursor for POST /router/stores/{id}/sync. Identity = the edition's
        # real user->groups map, so LAW 2 trims composed doc stores per caller.
        # #565: both rails share ONE job store so a deployment with Postgres gets durable,
        # restart-surviving ingest jobs (#327); unset DSN -> each provider makes its own
        # in-memory one, which is today's behaviour. `job_partition` is the workspace key.
        self.connector_providers = [
            r.ConnectorStoreProvider("folder", r.folder_connector_factory,
                                     identity=identity, on_sync=on_sync,
                                     job_store=job_store, job_partition=job_partition),
            r.ConnectorStoreProvider("sharepoint", r.sharepoint_connector_factory,
                                     identity=identity, on_sync=on_sync,
                                     shared_query_service=shared_doc_qs,
                                     job_store=job_store, job_partition=job_partition),
            # #673: S3 as a document source. The FIRST connector whose content lives in the
            # CALLER's cloud account rather than the deployment's, so it is also the first to
            # take a delegated credential (ADR 0022's rule, extended to this rail via
            # ConnectorStoreProvider.probe_as/build_as).
            r.ConnectorStoreProvider("s3", r.s3_connector_factory,
                                     identity=identity, on_sync=on_sync,
                                     job_store=job_store, job_partition=job_partition),
            # #712: a public Google Drive folder. Slice 1 needs no delegated credential
            # (deployment API key, public content only); the factory's `credential`
            # parameter is the slice-2 seam, already bound by name via _build_connector.
            r.ConnectorStoreProvider("gdrive", r.gdrive_connector_factory,
                                     identity=identity, on_sync=on_sync,
                                     job_store=job_store, job_partition=job_partition),
            # #924: a SharePoint / OneDrive folder shared as "Anyone with the link". No
            # credential at all - the link mints its own anonymous badge - so, like gdrive
            # slice 1, any signed-in user can add one; the consent-based `sharepoint` kind
            # above stays for tenants whose IT disables anonymous sharing.
            r.ConnectorStoreProvider("sharepoint_link", r.sharepoint_link_connector_factory,
                                     identity=identity, on_sync=on_sync,
                                     job_store=job_store, job_partition=job_partition),
        ]
        for p in self.connector_providers:
            self.registry.register(p)
        self.catalog: "r.StoreCatalog | None" = None
        self.manifest: dict | None = None
        self.service: "r.RouterQueryService | None" = None   # E8: per-compose (owns the cache)

    def set_workspace_key(self, key: str) -> None:
        """#565: which partition this workspace's ingest job records belong to.

        Called by WorkspacePool on creation. With a durable job store there is ONE
        `ingest_jobs` table for the whole deployment, and a resume is looked up by
        (partition, source_id) - so the partition has to be the workspace, not the Entra
        tenant, or two people in one tenant who both connect a store called "hr-docs" resume
        each other's crawl and skip documents their own index never received."""
        for p in self.connector_providers:
            p.job_partition = key

    def set_doc_tenant(self, tenant_id: "str | None") -> None:
        """#439: the ADR 0012 partition this WORKSPACE's shared-index document stores read.

        A workspace is one owner, so it is one partition - which is why this lives on the
        state rather than on each store entry. Set from the caller's server-verified tid
        before composing; without it a foreign owner's connected SharePoint store queried
        the deployment constant and returned nothing through /router/ask."""
        for p in self.connector_providers:
            p.set_doc_tenant(tenant_id)

    def known(self, kind: str, mode: "str | None" = None) -> bool:
        try:
            self.registry.get(kind, mode)
            return True
        except KeyError:
            return False

    def connector_source(self, store_id: str) -> "r.ConnectorStoreProvider | None":
        for p in self.connector_providers:
            if p.owns(store_id):
                return p
        return None


def _profile_summary(p) -> dict:
    # #808: `warnings` rides the per-store entry because a warned store is LIVE - it composed,
    # it is in the catalog, it will be routed to. `skipped` cannot carry it: that list means
    # "refused, never built", and folding the two together would make an unusable store and a
    # working-but-misconfigured one indistinguishable to the canvas, which is the exact
    # confusion #200 and #680 each had to undo.
    return {"store_id": p.store_id, "title": p.title, "description": p.description,
            "kind": p.kind, "capabilities": sorted(p.capabilities),
            "business_unit": p.business_unit, "freshness": p.freshness,
            "warnings": list(getattr(p, "warnings", None) or [])}


# #176 legible origin — build a "system · location · object" string a person can pinpoint.
def _origin_str(origin: "dict | None", obj: str, fallback: str) -> str:
    parts = [p for p in ((origin or {}).get("system", ""), (origin or {}).get("location", "")) if p]
    if obj:
        parts.append(obj)
    return " · ".join(parts) if parts else fallback


def _evidence_kind(prov: dict) -> str:
    if "sql" in prov:
        return "sql"
    if "doc" in prov or "uri" in prov or "title" in prov:
        return "document"
    if "source" in prov:
        return "native"
    return ""


def _obj_for(kind: str, prov: dict) -> str:
    if kind == "sql":
        return f"table {prov['table']}" if prov.get("table") else ""
    return prov.get("doc") or prov.get("title") or ""


#: How a persisted proof's result rows read as one string. A separator a value can contain
#: would make two rows look like one; " | " is what the synthesizer already uses to join
#: proofs, so the transcript reads the way the answer's own evidence does.
_SNIPPET_JOIN = " | "


def pair_proof_snippets(done: dict) -> dict:
    """Give each PROOF citation the snippet its footnote already computed (#689 slice 2).

    A reopened transcript must not re-run the query to show its evidence: the database has
    moved on since, and #633 settled this exact question for document quotes - a reader shown
    freshly-fetched rows under an old answer is being shown evidence that answer never used.
    Grouped on (store_id, sql), which is what a SQL proof IS.

    PAIRED BY POSITION WITHIN THE GROUP, and this is the whole of #855. One SELECT returning
    three rows produces three evidence rows, three footnotes and three citations. A dict
    comprehension over them kept only the LAST snippet, so all three read "region=emea"; the
    fix for that JOINED every row onto every citation, which made the three citations
    byte-identical - and identical rows are exactly what `_slim_citations` then deduped away,
    leaving the answer's [2] and [3] pointing at nothing. The nth citation of a group gets the
    nth row: three distinct rows, three distinct citations, three markers that resolve.

    Gaps only - a citation whose producer already gave it a snippet keeps it.

    Running out of rows falls back to ALL of them joined rather than reusing one, because
    attaching row 1 to citation 4 is the invented provenance `_drop_dangling_markers` refuses
    to manufacture: "all the rows this query returned" is true of any of them, "row 1" would
    not be. Mutates and returns `done`."""
    snips: dict = {}
    for f in done.get("footnotes", []):
        if f.get("snippet"):
            snips.setdefault((f["store_id"], f.get("sql", "")), []).append(f["snippet"])
    taken: dict = {}
    for cite in done.get("citations", []):
        proof = cite.get("proof") or {}
        key = (cite.get("store_id"), proof.get("sql") or cite.get("sql") or "")
        rows = snips.get(key)
        if not rows or cite.get("snippet"):
            continue
        i = taken.get(key, 0)
        cite["snippet"] = rows[i] if i < len(rows) else _SNIPPET_JOIN.join(rows)
        taken[key] = i + 1
    return done


def decorate_ask_result(result: dict, catalog, user: str) -> dict:
    """Everything a raw `RouterResult.to_dict()` needs before a person can read it: a
    user-bound proof token on each SQL citation (#165), a legible origin (#176), and the
    footnote list the answer's `[n]` markers resolve into (#175/#177/#729).

    MODULE-LEVEL, and called by BOTH ask surfaces, because #689/ADR 0025 gives the router a
    second caller: the conversational `/chat/stream` delegate (server/ask_router.py) has to
    hand back the SAME shape /router/ask does, or the #713 acceptance matrix - which asks the
    same questions through both and expects the same tallies - would be comparing two
    different renderings rather than two answers. A second copy of this loop is a place for
    the two surfaces to drift silently apart.

    `catalog` is whatever the caller resolved (the live catalog, the demo one, or #689's
    documents overlay); `user` is the NAMESPACED identity, because that is what binds a rerun
    token. Mutates `result` in place and returns it."""

    def _origin_of(store_id: str):
        try:
            return catalog.get(store_id).profile.origin
        except Exception:
            return None

    # #165: user-bound proof tokens + #176: human origin on each citation
    for cite in result["citations"]:
        proof = cite.get("proof")
        if proof and proof.get("kind") == "sql":
            proof["rerun_token"] = sign_rerun(proof["store_id"], proof["sql"], user)
        prov = {k: cite[k] for k in ("sql", "table", "doc", "title", "uri", "source")
                if k in cite}
        k = (proof or {}).get("kind") or _evidence_kind(prov)
        cite["origin"] = _origin_str(_origin_of(cite["store_id"]), _obj_for(k, prov),
                                     cite.get("title") or cite["store_id"])

    # #175: footnotes resolve the answer's [n] — built from evidence (MERGED order)
    footnotes = []
    for i, ev in enumerate(result.get("evidence", [])):
        prov = ev.get("provenance") or {}
        k = _evidence_kind(prov)
        sql = prov.get("sql", "")
        footnotes.append({
            "n": i + 1, "kind": k, "store_id": ev["store_id"],
            "origin": _origin_str(_origin_of(ev["store_id"]), _obj_for(k, prov),
                                  ev.get("business_unit") or ev["store_id"]),
            "system": (_origin_of(ev["store_id"]) or {}).get("system", ""),
            "location": (_origin_of(ev["store_id"]) or {}).get("location", ""),
            # #729: WHICH OBJECT inside that store answered - "table SalesOrderHeader", or a
            # document's name. It was already being computed into `origin`, and the Sources
            # rail renders `system` + `location` and never `origin`, so it was thrown away at
            # the last step: three citations from one query rendered as three identical cards
            # reading "Azure SQL / host / database", distinguishable only by their snippets.
            # A citation that cannot say what it points at is doing half its job.
            "object": _obj_for(k, prov),
            "snippet": _snippet(ev.get("content") or ""),
            # #729(a): how to READ the values inside that snippet - {column: "num"|"date"|
            # "ts"}, from the DECLARED schema type, for the columns this query returned.
            # The rail renders `TotalDue=43962.7901` and cannot tell it from
            # `period=2024.06` by looking, because nothing in the string distinguishes
            # them; only the type does, and this is the last point that still knows it.
            # Absent for a column the schema cannot resolve, which renders it raw.
            "column_types": prov.get("column_types") or {},
            "uri": prov.get("uri", ""), "sql": sql,
            # #177: token so the Sources list can offer "Verify data" (re-run) inline
            "rerun_token": sign_rerun(ev["store_id"], sql, user) if k == "sql" and sql else "",
        })
    result["footnotes"] = footnotes

    # #861: VALIDATE THE ANSWER'S NUMBERS AGAINST THE LIST THE READER WILL SEE.
    #
    # The routed path stripped only [coverage]/[query]/[style] (`strip_instruction_markers`)
    # and nothing numeric, so a routed answer printing [9] over four footnotes printed it
    # because the model decided to. The document path has refused that since #257, and its
    # docstring is as true here: the model picks numbers up out of the CONTENT - a policy
    # with numbered headings produced "...refer to public holidays [4]" against ONE citation -
    # and "a marker that resolves to nothing is worse than no marker: it reads as
    # corroboration". #859 then keyed the whole Sources rail off which markers the answer
    # carries, which made an unchecked number a rendering instruction.
    #
    # HERE, rather than in `synthesize`, because this is where the footnote list is BUILT.
    # The denominator and the list it counts are the same object, so they cannot drift; a
    # copy in the synthesizer would be counting `merged` and hoping it still matched.
    #
    # BOTH SURFACES AT ONCE, which is the other half of the fix. `referenced` shipped with
    # #859 on the /chat/stream delegate only, so /router/ask returned no `referenced` key at
    # all - and #859's own rule is that absent and empty are different states to a client
    # keying on Array.isArray. This module-level function is the one thing both callers run.
    #
    # QueryService's helpers, not a second implementation: two planes that answered "what
    # does this answer actually cite" differently would be one product with two answers.
    from dbsearch.query.service import QueryService
    result["answer"] = QueryService._drop_dangling_markers(
        result.get("answer") or "", len(footnotes))
    # AFTER the drop, deliberately - `_referenced` documents that it reads the answer the
    # READER sees, so it can only ever name a marker that is really on screen.
    result["referenced"] = QueryService._referenced(result["answer"], len(footnotes))
    return result


def _snippet(text: str, limit: int = 160) -> str:
    """#748: the footnote snippet cap, trimmed to a WORD boundary with a real ellipsis.

    The old hard `[:160]` rendered '...receive 18 weeks of fully pa' on the live site - a
    cut that looks like the document's own text, which for a product whose whole claim is
    faithful citation is the worst possible place to look broken. Fits-whole passes through
    untouched with no ellipsis; a truncation cuts at the last whitespace inside the limit
    (rstripped, so the ellipsis never floats after a space); a single unbroken run longer
    than the limit hard-cuts and still gets the ellipsis, because the reader must always be
    able to tell OUR cut from the document's punctuation. The ellipsis rides outside the
    cap (161st char): no CSS clamp exists downstream (.osnip), and humanizeSnippet never
    touches a trailing ellipsis."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    boundary = re.search(r"\s\S*$", head)   # last whitespace run in reach, any kind
    cut = boundary.start() if boundary else -1
    return (head[:cut].rstrip() if cut > 0 else head) + "…"


def _default_wiring(edition) -> tuple:
    """The SQL/Cosmos NL2SQL generator + vector-rescue floor wiring, computed from the
    edition's default chat model + embedder. Factored out so the live state
    (`build_router_api`) and the demo state (`compose_demo_catalog`, #279) share the
    EXACT same wiring instead of two subtly-diverging copies."""
    # #135: wire the edition's default chat model as the NL2SQL generator when it can
    # generate SQL (capability gate, like C3's entry parser). llm_sql_generator guards
    # the output and falls back to the keyword generator on any failure, so this is
    # always safe; absent the capability, SQL stores keep the deterministic default.
    _sql_llm = edition.chat_models.get(edition.chat_model_default)
    # #254: memoized so one question over one schema always yields the SAME query. At
    # temperature 0.0 the model still alternated between INNER and LEFT JOIN for identical
    # asks (295 rows vs 142), so the user was told a different total each time they asked the
    # same thing. Stability is imposed here, above the model; the join SEMANTICS are left to
    # the question, since "include entities with no matching rows" is a real thing to ask.
    sql_gen = (r.memoized_sql_generator(r.llm_sql_generator(_sql_llm))
               if _sql_llm is not None and hasattr(_sql_llm, "generate_sql") else None)
    # #229: Cosmos gets the SAME treatment. It was the ONLY engine with no LLM seam - every SQL
    # store was handed llm_sql_generator while Cosmos alone answered real questions with a regex,
    # which is what produced the #228 fabrication (an ungrouped aggregate fell through to a blind
    # `SELECT TOP 20 *` sample and the synthesizer averaged it). Same capability gate, same
    # guard-and-fallback contract; absent a chat model, Cosmos keeps the deterministic default.
    cosmos_gen = (r.llm_cosmos_generator(_sql_llm)
                  if _sql_llm is not None and hasattr(_sql_llm, "generate_cosmos_query")
                  else None)
    # #143: a real semantic embedder (LlamaEmbedding) reaches the router's index stores and
    # earns the vector-rescue floor; the noisy lexical HashingEmbedding keeps it off (0.0).
    fvr = 0.9 if type(edition.embedder).__name__ == "LlamaEmbedding" else 0.0
    # #462: the RAW chat model also travels to the SQL stores as the literal-resolution
    # disambiguator. Passed unconditionally - the in_tenant gate lives inside
    # `dictionary._llm_pick` (absent-means-no), so an out-of-tenant model is carried but
    # never asked, and no wiring mistake here can leak a value.
    return sql_gen, cosmos_gen, fvr, _sql_llm


def _service_wiring(edition) -> tuple:
    """The two capability-gated collaborators every RouterQueryService in this process gets.

    ONE definition, because #689's ask delegate builds a service too (over the documents
    overlay) and a service wired differently would decompose or rescue differently - so the
    canvas and Ask would answer the same question differently, which IS the defect #689
    reports."""
    # #215: a compound question must reach each store as a STANDALONE sub-question that
    # still carries the join key. The regex splitter drops it ("...and how much revenue
    # do they bring" -> a subject-less fragment -> total company revenue instead of
    # revenue per product, which joins to nothing). An LLM decomposer goes behind the
    # same seam, and falls back to the regex split on any bad generation.
    chat = edition.chat_models[edition.chat_model_default]
    decomposer = (r.llm_decomposer(chat)
                  if hasattr(chat, "decompose_question") else None)
    # #474 (ADR 0014-B): the schema-aware cross-store planner - fires only when the plain
    # path declined, sees caller-visible metadata only. Capability-gated like the rest.
    planner = (r.llm_cross_store_planner(chat)
               if hasattr(chat, "plan_cross_store") else None)
    return decomposer, planner


def _build_service(state: "_State", edition, *, identity=None) -> "r.RouterQueryService":
    """ONE service per composed catalog (E8). Factored out so the live `_service()`
    closure and the demo compose seam (`compose_demo_catalog`, #279) build it identically.
    `identity` defaults to the edition's LIVE identity; the demo compose seam passes its
    OWN demo identity (#340: the router's gate-#1 visibility check lives inside
    RouterQueryService, keyed to whichever identity it was built with - pairing the demo
    catalog with the live identity there reproduced the exact #340 failure even after
    the read endpoints stopped doing it directly)."""
    decomposer, planner = _service_wiring(edition)
    return r.RouterQueryService(state.catalog, identity or edition.identity, edition.embedder,
                                decomposer=decomposer, cross_store_planner=planner)


def _has_env_ref(obj) -> bool:
    """True when any value in `obj` is a `${ENV}` reference.

    Deliberately the SAME predicate `resolve_env` applies (startswith "${" and endswith
    "}"), not a stricter regex: a gate that recognizes fewer strings than the resolver does
    is a gate with a hole in exactly the shape of the difference."""
    if isinstance(obj, str):
        return obj.startswith("${") and obj.endswith("}")
    if isinstance(obj, dict):
        return any(_has_env_ref(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_env_ref(v) for v in obj)
    return False


def _reads_local_files(entry: dict) -> bool:
    """True when this store entry points a provider at the SERVER's own filesystem.

    Determined by reading the providers, not by guessing from the kind name:
      - `folder` (providers/connector.py -> FolderConnector) rglobs `config.path` and reads
        every supported file under it into an index the caller can then query;
      - `csv` with `files:` (structured.py -> SqliteEngine.from_csv_files) opens those paths.
        `csv` with inline `tables:` is the caller's OWN data and stays allowed.
    Every other registered kind takes a network endpoint, or inline seed content."""
    kind = entry.get("kind", "")
    config = entry.get("config") or {}
    if kind == "folder":
        return True
    if kind == "csv":
        return bool(config.get("files"))
    return False


def _guard_caller_manifest(manifest: dict, user_oid: str) -> None:
    """Refuse the two manifest powers that belong to whoever RUNS this deployment (#423).

    Keyed on the caller at compose time, never on the manifest's history: a stored operator
    manifest replayed by `_rebuild` is not a request and is not checked here, so a workspace
    that was legitimately composed with `${ENV}` refs still rebuilds after a restart.

    1. `${ENV}` references (C2/C3). `resolve_env` resolves them SERVER-side, out of the
       server's own environment, so a signed-in stranger could read any variable the process
       holds by naming it - `AUTH_CLIENT_SECRET` as a `host` came back verbatim in the
       store's failure reason - and could probe which variables are set at all. The caller's
       own credentials have a path that does not touch the server's environment: ADR 0010's
       `secret://` handles.

       Checked over the WHOLE entry, not over `config:` and `delegation:` alone. The first
       version of this gate checked those two blocks, and `provisioning.load_manifest` also
       passes `id`, `business_unit`, `title` and `description` through `resolve_env` - so
       `title: "${AUTH_CLIENT_SECRET}"` composed successfully with the secret resolved into
       the store's own title, returned in the compose response and readable from
       /router/catalog forever after. Enumerating fields is how that hole was born; a
       superset is the only version that cannot drift when someone adds a field to the
       resolved dict. A `${...}` string in a field the server does NOT resolve (an `acl`
       entry, say) is refused too, which is a harmless false positive on a value that would
       never have worked as intended anyway.
    2. Local file sources (C4). `kind: folder` (and `csv` with `files:`) point the ingest
       rail at a path on OUR disk and then make its contents queryable - arbitrary
       server-side file read, dressed as a connector.

    Both messages are fixed strings: a refusal must not double as an oracle for what the
    env holds or what exists on the filesystem."""
    if is_operator(user_oid):
        return
    for e in manifest.get("stores", []) or []:
        if _has_env_ref(e):
            raise HTTPException(status_code=400, detail=(
                "environment references are operator-only on this deployment - use your own "
                "credentials (secret:// via the credential panel)"))
        if _reads_local_files(e):
            raise HTTPException(status_code=400, detail=(
                "local file sources are operator-only on this deployment"))


# #439: where a workspace's SERVER-VERIFIED tenant partition rests inside the stored manifest.
# Leading underscore because it is not part of the manifest a user authors: the compose path
# STRIPS whatever the client sent under this key and rewrites it from resolve_tenant, so a
# client can never choose the partition its documents are read from (ADR 0012's whole point).
OWNER_TENANT_KEY = "_owner_tenant"


def _compose_manifest(state: "_State", manifest: dict, subject_token_provider=None,
                      on_rotate=None, secrets=None, owner_tenant: "str | None" = None,
                      owner_oid: str = "") -> dict:
    """The ONE compose path - the /router/compose endpoint, the setup agent (#116), and
    the demo compose seam (`compose_demo_catalog`, #279) all use it, so there is never a
    second, divergently-behaving compose.

    `secrets` (ADR 0010 s2/s3): a `ScopedSecretResolver` bound to the requesting caller, or
    None on paths with no self-serve context (the demo seam, the setup agent today). Threaded
    into both `register_delegations` and `load_manifest` so a `secret://` handle in either
    `config:` or `delegation:` resolves to THIS caller's own stored credentials - never
    another tenant's or another user's (LAW 5)."""
    if "tenant" not in manifest:
        raise HTTPException(status_code=400, detail="manifest needs a tenant")
    # #439: BEFORE anything builds. `manifest["tenant"]` is the user's own label for their
    # catalog ("acme") and is client-authored; the ADR 0012 partition is a different thing
    # entirely and is server-verified. Set it on the state so shared-index document stores
    # read the OWNER's partition rather than the deployment constant.
    state.set_doc_tenant(owner_tenant)
    entries = manifest.get("stores", [])
    known = [e for e in entries if state.known(e.get("kind", ""), e.get("mode"))]
    # #200: LAW 2 is default-deny, so a store with an EMPTY ACL is visible to NOBODY.
    # Composing it anyway produced the worst failure we have: the node reports
    # "Connection healthy — a record round-tripped", turns green and `live`, and then
    # every ask answers "I couldn't find anything you have access to about that." Every
    # affirmative signal says WORKING while the store can never be reached. It is not a
    # connection fault, so refuse to compose it and SAY why — an unusable store must
    # never look like a healthy one.
    buildable = [e for e in known if e.get("acl")]
    skipped = [{"id": e.get("id", "?"), "kind": e.get("kind", "?"),
                "reason": (f"kind {e.get('kind', '?')!r} has no provider for mode "
                           f"{e['mode']!r} (supported: {state.registry.modes_for(e.get('kind', ''))})"
                           if state.known(e.get("kind", "")) and e.get("mode")
                           else "no provider for this kind yet — lands in E4/E9 (ADR 0008)")}
               for e in entries if not state.known(e.get("kind", ""), e.get("mode"))]
    skipped += [{"id": e.get("id", "?"), "kind": e.get("kind", "?"),
                 "reason": "no ACL — nobody can see this store (LAW 2 is default-deny). "
                           "Add the principals who may query it, e.g. the signed-in "
                           "user's OID."}
                for e in known if not e.get("acl")]
    spec = dict(manifest, stores=buildable)
    if state.broker is not None:
        # #107 OBO wiring: delegation blocks re-register per compose; a bad block is a
        # manifest error, never a silent no-delegation (LAW 2). #805: registration goes
        # into a STAGING broker and is adopted atomically on success — resetting the live
        # broker first opened a window where a concurrent ask saw no delegations at all,
        # and a compose failing mid-registration left the old manifest serving with its
        # delegations stripped. Staleness is still impossible: the staging broker starts
        # empty, so adopt() replaces everything the old manifest registered.
        _staging = state.broker.staging()
        try:
            r.register_delegations(spec, _staging,
                                   subject_token_provider
                                   or r.env_subject_token_provider,
                                   on_rotate=on_rotate, secrets=secrets)
        except PermissionError:
            # Task 5 policy: a foreign secret handle in a `delegation:` block is a 403, not a
            # 400 and never a silent skip - see the matching `load_manifest` handling below.
            # The detail is a FIXED string, deliberately never `str(exc)` or the handle - a
            # refusal must not teach a prober which handle it tried or whose scope it hit.
            raise HTTPException(status_code=403,
                                detail="not authorized for this secret handle")
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400,
                                detail=f"delegation error: {exc}")
        state.broker.adopt(_staging)
    try:
        # #107: per-store failures (bad creds, unset ${ENV}, unreachable engine)
        # land in `skipped` with their reason — one bad cloud store must never
        # take down the rest of the catalog. Manifest-level errors (#114 id
        # collisions) still 400.
        # #665 (ADR 0022): the owner's own delegated credential introspects the schema of a
        # store that declares a delegation, exactly as /router/probe and /router/health
        # already do. Reuses the same exchange_from_config + subject_token_provider pair
        # register_delegations was handed a few lines above, so compose and query can never
        # redeem different credentials, and _for_idp's cross-cloud binding guard applies here
        # too. Raises inside load_manifest's per-store try, so an unlinked cloud is an honest
        # skip rather than a failed compose.
        def _credential_for(entry: dict) -> "str | None":
            deleg = entry.get("delegation")
            if not deleg or not owner_oid:
                return None
            exchange, resource = r.exchange_from_config(
                r.resolve_env(dict(deleg), secrets=secrets),
                subject_token_provider or r.env_subject_token_provider)
            return exchange.exchange(owner_oid, resource)

        catalog = r.load_manifest(spec, registry=state.registry, skipped=skipped,
                                  secrets=secrets, credential_for=_credential_for)
    except PermissionError:
        # Task 5 policy (ADR 0010 s3, review finding on Task 4): a foreign secret handle in
        # `config:` must NOT be downgraded into `skipped` alongside ordinary build/probe
        # failures (that would be indistinguishable from "this store has bad credentials" -
        # an observability problem, and it would let compose answer 200 with the offending
        # store quietly missing). `load_manifest` re-raises PermissionError uncaught for
        # exactly this to catch and turn into a 403. Fixed detail string, same reasoning as
        # the delegation branch above - never echo the handle.
        raise HTTPException(status_code=403,
                            detail="not authorized for this secret handle")
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"manifest error: {exc}")
    state.catalog = catalog
    state.manifest = manifest
    state.service = None            # E8: new catalog -> new service (cache + vectors)
    stores = [_profile_summary(n.profile) for n in catalog.stores()]
    # #454: composing a connector-backed store SUBMITS its first crawl rather than running it
    # inside this request. The caller is told which jobs that started, because otherwise the
    # only honest thing compose could report is a store with no content and no explanation -
    # and "it looks empty" with nothing to poll is the failure this card exists to end.
    # Scoped to stores in THIS catalog that are still ingesting. A provider outlives a
    # compose, so its job table remembers every crawl the workspace ever submitted; listing
    # those would report a finished job for a store the new manifest does not even contain,
    # and a client polling it would see "succeeded" for something that never started.
    composed = {n.id for n in catalog.stores()}
    ingesting = [{"store_id": sid, "job_id": jid, "poll": f"/router/jobs/{jid}"}
                 for p in state.connector_providers
                 for sid, jid in p.active_jobs().items()
                 if sid in composed and getattr(p.jobs.get(jid), "status", "") not in
                 ("succeeded", "failed")]
    return {"tenant": manifest["tenant"], "stores": stores, "skipped": skipped,
            "ingesting": ingesting}


class DemoCatalog:
    """A composed demo-scope catalog + its live RouterQueryService (#279 Task 1/3a) - the
    seam tests (and later, 3b's demo scope routing) use instead of poking `_State`
    internals directly. `.catalog` is the composed `StoreCatalog` (visibility checks via
    `visible_stores`/`visible_tree`); `.ask` is a thin passthrough to the service."""

    def __init__(self, catalog: "r.StoreCatalog", service: "r.RouterQueryService",
                 skipped: list) -> None:
        self.catalog = catalog
        self.service = service
        self.skipped = skipped

    def ask(self, user_oid: str, question: str, llm, store_override: "str | None" = None):
        return self.service.ask(user_oid, question, llm, store_override=store_override)


def compose_demo_catalog(edition) -> DemoCatalog:
    """The demo-scope compose seam (#279 Task 1/3a): a fresh, fixture-backed `_State` over
    the alice/bob doc stores (hr-wiki, fin-ledger - from DEMO_MANIFEST; sales-figures is
    DROPPED here since azure-deals now covers sales) PLUS the four badged fixture SQL
    connectors (DEMO_FLEET_STORES). Reuses the SAME wiring `build_router_api` computes for
    the live state (`_default_wiring`) and the SAME compose path the live /router/compose
    endpoint uses (`_compose_manifest`) - never a second, divergent compose.
    #340: the demo catalog's OWN identity (DEMO_USER_GROUPS) drives BOTH the `_State`
    broker/connector wiring AND the composed `RouterQueryService`'s gate-#1 visibility -
    never the edition's live identity, which a hosted box may never have heard of alice/bob
    on."""
    sql_gen, cosmos_gen, fvr, value_llm = _default_wiring(edition)
    demo_identity = InMemoryIdentity(dict(DEMO_USER_GROUPS))
    state = _State(fixture_backed=True, identity=demo_identity, sql_generator=sql_gen,
                   cosmos_generator=cosmos_gen, embedder=edition.embedder,
                   floor_vector_rescue=fvr, value_llm=value_llm)
    doc_stores = [s for s in DEMO_MANIFEST["stores"] if s["id"] in ("hr-wiki", "fin-ledger")]
    manifest = {"tenant": DEMO_MANIFEST["tenant"], "stores": doc_stores + DEMO_FLEET_STORES}
    result = _compose_manifest(state, manifest)
    service = _build_service(state, edition, identity=demo_identity)
    return DemoCatalog(catalog=state.catalog, service=service, skipped=result["skipped"])


def build_router_api(edition, current_user, *, current_user_demo_ok=None,
                     subject_token_provider=None, on_rotate=None, secrets=None,
                     manifest_store=None, force_per_user_workspaces=None,
                     tenant_resolver=None, job_store=None) -> APIRouter:
    """`edition` supplies identity/embedder/LLM; `current_user` is the app's LIVE-only auth dep
    (it 403s a `demo:*` identity). `current_user_demo_ok` is the demo-safe dep used ONLY by the
    read endpoints that serve the demo scope (/ask, /route, /rerun, /catalog, /demo); it
    defaults to `current_user`, so a caller that does not wire it keeps the live-only behavior
    (default-deny). ADR 0009 / #279.

    `secrets` (ADR 0010 s2/s3, #319 Task 5): the server's SecretsPort, or None on a deployment
    with no DBSEARCH_SECRET_KEY configured. When set, /router/compose builds a per-caller
    `ScopedSecretResolver(secrets, edition.tenant_id, user)` so a `secret://` handle minted by
    THAT caller's own POST /secrets resolves during compose; None makes every handle a hard
    error (resolve_env's no-resolver-wired refusal), never a silent pass-through.

    `manifest_store` (#368): where each workspace's RAW manifest rests, or None for a
    memory-only lifecycle (today's behavior - a restart empties every workspace). When set,
    compose persists and a cold workspace rebuilds itself from the stored row.

    `force_per_user_workspaces` (#368): a TEST SEAM, not a deployment knob. None means decide
    per request from `real_login_enabled()`; True/False pin it. Production always passes
    None."""
    api = APIRouter(prefix="/router")
    if current_user_demo_ok is None:
        current_user_demo_ok = current_user

    def _emit_sync(summary) -> None:
        # LAW 8: metadata-only counters for composed connector-rail syncs.
        edition.agent.emit(
            "source.synced",
            counts={"sources_synced": 1, "docs_indexed": summary.doc_count},
            health={"last_index_ts": summary.last_sync_at},
            ts=summary.last_sync_at,
        )

    _sql_gen, _cosmos_gen, _fvr, _value_llm = _default_wiring(edition)

    def _make_state() -> "_State":
        return _State(identity=edition.identity, on_sync=_emit_sync, sql_generator=_sql_gen,
                      cosmos_generator=_cosmos_gen, value_llm=_value_llm,
                      job_store=job_store,
                      embedder=edition.embedder, floor_vector_rescue=_fvr,
                      # #304/#306: the router's SharePoint store shares the edition's ingested
                      # index, so health (has_content) + routing (doc-title topics) reflect the
                      # REAL docs.
                      shared_doc_qs=edition.query_service)

    def _workspace_key(user: str) -> str:
        # #368 spec revision: per-owner ONLY under a real login. Dev-header rigs share
        # one workspace so the alice/bob demo and e2edbs keep compose-as-one-query-as-
        # another. #183 makes the two modes mutually exclusive, so this cannot mix.
        per_user = (real_login_enabled() if force_per_user_workspaces is None
                    else force_per_user_workspaces)
        return user if per_user else SHARED_KEY

    def _resolver_for(owner: str):
        return (ScopedSecretResolver(secrets, edition.tenant_id, owner)
                if secrets is not None else None)

    def _introspect_credential_for(entry: dict, owner: str) -> "str | None":
        """ADR 0022: mint THIS caller's delegated access token for a store that declares a
        `delegation:` block, so Test-connection introspects the schema as them.

        Minted here, at the server layer, for the same reason `_resolver_for` lives here: this
        is where identity, the vault and the secret scope are known. The router layer receives
        an opaque token and never learns which cloud it belongs to.

        Reuses `exchange_from_config` + `subject_token_provider` - the exact pair
        /router/compose already uses to register delegations - so probe and query can never
        redeem a different credential than each other, and the `_for_idp` binding guard that
        stops one cloud's refresh token reaching another cloud's token endpoint applies here
        too, for free.

        No delegation block -> None -> the caller's provider falls back to its server identity
        (ADC for BigQuery), which is the unchanged operator-owned-warehouse path. A FAILURE to
        mint is deliberately NOT swallowed: `NotSignedIn` is the one outcome the user can act
        on ("connect Google"), and the endpoints' handlers already turn it into an honest
        failed verdict. Returning None there would hide it behind a default-credentials error
        about a machine identity the user has never heard of - which is precisely the #656
        wall this ADR removes."""
        deleg = entry.get("delegation")
        if not deleg:
            return None
        resolver = _resolver_for(owner)
        resolved = r.resolve_env(dict(deleg), secrets=resolver)
        exchange, resource = r.exchange_from_config(
            resolved, subject_token_provider or r.env_subject_token_provider)
        return exchange.exchange(owner, resource)

    def _rebuild(st: "_State", manifest: dict, owner: str) -> None:
        # The ONE compose path, replayed from the stored manifest. secret:// handles
        # resolve for the owning workspace only; on a SHARED_KEY rig they will land in
        # skipped-with-reason (dev rigs use ${ENV}, not handles - honest degradation).
        #
        # The plaintext-credential guard runs HERE TOO, not just on the write path. Every
        # row `_persisting_compose` writes is already clean, but that makes the invariant a
        # comment rather than a check: a row written out-of-band, by a migration, or before
        # this guard existed would otherwise compose a plaintext credential straight into a
        # live workspace (LAW 6). Self-enforcing beats documented.
        #
        # A hit must NOT crash the workspace - a rebuild is not a user action, and failing
        # the whole workspace would take the owner's OTHER, legitimate stores down with it.
        # So the offending stores are DROPPED (treated as unusable, the same shape as
        # compose's skipped-with-reason) and the rest of the manifest composes. The log
        # names store and field only, never the value.
        bad = find_secret_literals(manifest)
        if bad:
            offenders = {b["store_id"] for b in bad}
            logging.getLogger("dbsearch").error(
                "workspace %s: stored manifest carries plaintext credentials in %s - those "
                "stores are DROPPED from the rebuild as unusable; recompose them with a "
                "secret:// handle (ADR 0010 s2)", owner,
                ", ".join(sorted(f"{b['store_id']}.{b['field']}" for b in bad)))
            manifest = dict(manifest, stores=[s for s in manifest.get("stores", [])
                                              if s.get("id", "?") not in offenders])
        # #439: replay the partition that was verified when this manifest was COMPOSED. A
        # rebuild has no request behind it, so the tid cannot be resolved here - which is
        # exactly why compose persists it. Absent (a row written before #439) means None,
        # i.e. today's behavior: fall back to the QueryService's own tenant.
        _compose_manifest(st, manifest, subject_token_provider, on_rotate,
                          secrets=_resolver_for(owner),
                          owner_tenant=manifest.get(OWNER_TENANT_KEY),
                          # #665: a cold rebuild has no request behind it, but the vault is
                          # durable (#435) - so the owner's delegated credential is still
                          # mintable here, and a BigQuery store survives a restart instead of
                          # silently dropping out of the catalog until someone re-composes.
                          owner_oid=owner)

    _pool = WorkspacePool(_make_state, manifest_store=manifest_store, rebuild=_rebuild)

    def _workspace(user: str) -> "_State":
        try:
            return _pool.get(_workspace_key(user))
        except ManifestStoreUnavailable:
            # #200: an empty workspace served in place of a failing lookup reads to the
            # user as "your stores are gone". Fail closed and say so instead.
            raise HTTPException(status_code=503,
                                detail="workspace store unavailable - try again shortly")

    # Registry membership is identical across workspaces (every `_State` registers the same
    # providers in __init__), so ONE template state answers kind/mode capability questions.
    # It never composes and never holds a catalog - no workspace side effects for a
    # read-only capability listing.
    _kinds_state = _make_state()

    def _service_for(st: "_State") -> "r.RouterQueryService":
        if st.catalog is None:
            raise HTTPException(status_code=409,
                                detail="no catalog composed yet — POST /router/compose first")
        if st.service is None:
            # E8: ONE service per composed catalog — the route cache and warmed profile
            # vectors live as long as the catalog does; compose replaces both together.
            st.service = _build_service(st, edition)
        return st.service

    def ask_delegate(user: str, scope, llm):
        """#689 / ADR 0025: a routed answer producer for THIS caller, or None.

        This is the seam the conversational surface crosses. `/chat/stream` owns the turn -
        history, condense, recording, transcripts, shares, the SSE shell - and delegates only
        WHO PRODUCES THE ANSWER. What comes back is a callable taking the standalone question
        and yielding the same token/done events `QueryService.answer_stream` yields, so
        ConversationService never learns which plane answered.

        NONE, NOT AN ERROR, in three cases, because the conversational surface has a correct
        answer to fall back on and a broken Ask box is worse than a document-only one:
          - THIS CALLER CAN SEE NO COMPOSED STORE: the ADR's own degrade clause - the router
            path becomes the document path, which is today's behaviour, which is why the
            empty-state copy stays truthful and needs no change. Measured per caller against
            gate #1, not merely "is a catalog object present": on a dev-header rig every
            caller shares one workspace (#368), so a colleague who has connected nothing sits
            behind somebody else's catalog object and would otherwise have their answers
            quietly re-piped through the router's synthesizer - a different prompt and a
            different citation shape - for a question only their documents could answer. The
            flag's blast radius is people who actually connected something.
          - the workspace store is unavailable: `/router/compose` must fail closed and say so
            (#200), but an ASK still has documents to answer from, and 503-ing the whole Ask
            box because the manifest table is down would take a working surface offline.
          - a demo identity: the chat routes depend on the LIVE-only `current_user`, which
            403s `demo:*` before this is ever reached. Asserted rather than assumed - the
            #340 lesson is that a demo/live mispairing is invisible until it leaks.

        The service is built PER CALL, over a per-request catalog. That is not the E8 waste it
        looks like: the composed nodes' profile vectors are already warmed on the shared
        profile objects the overlay passes through, the documents profile's vector is cached
        on its own text (ask_router._VECTOR_CACHE), and the E8 route cache is per-turn here by
        construction because the overlay's `revision` includes this caller's document titles.
        The alternative - caching a service per (workspace, user) - would hold a QueryService
        bound to one request's ReadScope past the request that verified it, which is #439's
        defect with a longer fuse."""
        from dbsearch.server.ask_router import DocsOverlayCatalog, documents_node
        assert not user.startswith(DEMO_PREFIX), (
            "the ask delegate was reached by a demo identity - the chat routes are supposed "
            "to depend on the live-only current_user (#279/#340)")
        try:
            st = _workspace(user)
        except HTTPException:
            return None                      # workspace store down: documents still answer
        if st.catalog is None:
            return None                      # nothing composed at all
        if not st.catalog.visible_stores(edition.identity.expand_groups(user)):
            return None                      # nothing THIS caller can see: same degrade
        decomposer, planner = _service_wiring(edition)

        def produce(standalone: str):
            doc_node = documents_node(edition, user, scope, base_catalog=st.catalog)
            overlay = DocsOverlayCatalog(st.catalog, doc_node, user)
            svc = r.RouterQueryService(overlay, edition.identity, edition.embedder,
                                       decomposer=decomposer, cross_store_planner=planner)
            for ev in svc.ask_stream(user, standalone, llm):
                if ev.get("type") != "done":
                    yield ev
                    continue
                done = decorate_ask_result(dict(ev), overlay, user)
                # #689 slice 2: give each PROOF citation the snippet its footnote already
                # computed, so the turn recorded below carries what the answer was built from.
                # A reopened transcript must not re-run the query to show its evidence: the
                # database has moved on since, and #633 settled this exact question for
                # document quotes - a reader shown freshly-fetched rows under an old answer is
                # being shown evidence that answer never used. Joined on (store_id, sql),
                # which is what a SQL proof IS.
                # ACCUMULATED per query, not keyed last-wins. One SELECT returning three rows
                # produces three evidence rows, three footnotes and three citations, and a
                # dict comprehension over them keeps only the last snippet - so all three
                # proof rows recorded the SAME single row of the result and the transcript
                # showed "region=emea" three times for a query that returned three regions.
                pair_proof_snippets(done)
                # #859's `referenced` and #861's marker validation both live in
                # `decorate_ask_result` now, which ran above: it is where the footnote list is
                # built, so the denominator cannot drift from the list it counts, and it is
                # the one function BOTH ask surfaces call. This block used to compute
                # `referenced` here, which left /router/ask without the key entirely.
                # The document ids this turn actually drew on, in evidence order. `/chat` and
                # `/chat/stream` have always returned this and `record_query` counts it; a
                # routed turn that reported none would look to every downstream reader like a
                # turn that retrieved nothing.
                docs, seen = [], set()
                for e in done.get("evidence", []):
                    d = (e.get("provenance") or {}).get("doc")
                    if d and d not in seen:
                        seen.add(d)
                        docs.append(d)
                done["retrieved_docs"] = docs
                # #576's retention touch. Read off the recorder rather than out of the
                # evidence, because an owner oid is an ACCOUNT id and must not ride on
                # anything the client can see (#549) - `/chat/stream` strips this key from
                # the wire event, and it is the ONLY consumer.
                done["retrieved_owners"] = sorted(
                    getattr(getattr(doc_node, "store", None), "_qs", None).owners
                    if doc_node is not None else [])
                yield done

        return produce

    api.ask_delegate = ask_delegate

    # --- Demo/live scope (ADR 0009 / #279 Task 2, LAW 2) -------------------------------------
    # A `demo:*` identity is routed to a SEPARATE, pre-composed, fully-local demo catalog and
    # is de-namespaced to its bare principal (alice/bob) ONLY here, for authorization against
    # the demo catalog. The live `state` is NEVER consulted for a demo identity. The demo-safe
    # READ endpoints (/ask, /route, /rerun, /catalog, /demo) depend on `current_user_demo_ok`;
    # every other endpoint depends on the LIVE-only `current_user`, which 403s a demo identity
    # by construction - so a demo visitor can never compose, probe, health-check or sync the
    # live catalog, and no admin/mutating endpoint needs its own per-endpoint demo guard (#184).
    # The read endpoints below consume an injected `RequestScope` (#336) rather than picking
    # their own collaborators, so a demo identity can never mispair with a live one (#340).
    _demo_cache: dict = {}

    def _demo_catalog() -> "DemoCatalog":
        if "cat" not in _demo_cache:                # composed once, cached for the process
            _demo_cache["cat"] = compose_demo_catalog(edition)
        return _demo_cache["cat"]

    _build_scope = make_scope_builder(
        edition=edition, demo_catalog=_demo_catalog,
        live_catalog=lambda user: _workspace(user).catalog,
        live_service=lambda user: _service_for(_workspace(user)),
        demo_user_groups=DEMO_USER_GROUPS)

    def scoped(user: str = Depends(current_user_demo_ok)) -> RequestScope:
        return _build_scope(user)

    @api.get("/kinds")
    def kinds(user: str = Depends(current_user)) -> dict:
        reasons = {
            "local": "in-tenant index provider (E1)",
            "graph_search": "mode:native via Microsoft Graph Search (E3b) — set GRAPH_TOKEN",
            "folder": "mode:index via the document-connector rail (#111) — delta-synced",
            "azure_sql": "mode:pushdown federated SQL in Azure SQL (#107) — "
                         "${ENV} creds resolve server-side",
            "bigquery": "mode:pushdown federated SQL in BigQuery (#107) — "
                        "server-side ADC auth",
            "redshift": "mode:pushdown via the Redshift Data API (#107) — "
                        "server-side SigV4 auth",
            "databricks": "mode:pushdown SQL warehouse via databricks-sql-connector "
                          "(#107) — ${ENV} token resolves server-side",
            "postgres": "mode:pushdown federated SQL in Azure Database for PostgreSQL "
                        "(#155) — ${ENV} creds resolve server-side, TLS required",
            "mysql": "mode:pushdown federated SQL in Azure Database for MySQL "
                     "(#158) — ${ENV} creds resolve server-side, TLS required",
            "synapse": "mode:pushdown T-SQL in Azure Synapse (dedicated SQL pool) "
                       "(#159) — same TDS engine as azure_sql, ${ENV} creds server-side",
            "cosmos_db": "mode:pushdown Cosmos-SQL over JSON docs in Azure Cosmos DB "
                         "(#160, NoSQL) — its own FEDERATED_DOC store, ${ENV} creds server-side",
            "sharepoint": "mode:index via the document-connector rail (#111); "
                          "mode:native = kind graph_search (E3b)",
            "sharepoint_link": "mode:index over a folder shared as 'Anyone with the link' "
                               "(#924) - no Microsoft account, the link mints its own badge",
        }
        out = []
        for k in ("local", "graph_search") + PLANNED_KINDS:
            # The template state, not the caller's workspace: which kinds this build can
            # connect is a property of the provider registry, identical everywhere, and a
            # capability listing must not create (or touch) a workspace.
            if _kinds_state.known(k):
                out.append({"kind": k, "available": True,
                            "modes": _kinds_state.registry.modes_for(k),
                            "reason": reasons.get(k, "provider registered")})
            else:
                out.append({"kind": k, "available": False, "modes": [],
                            "reason": "provider lands in E4/E9 (ADR 0008)"})
        return {"kinds": out}

    @api.get("/demo")
    def demo(user: str = Depends(current_user_demo_ok)) -> dict:
        # `manifest` is the original 3-store demo manifest - UNCHANGED (the dev/self-host canvas
        # and e2edbs L1 compose it). `demo_fleet` (#279) is the pre-composed badged fleet the
        # canvas renders in demo mode, so what a demo visitor SEES matches what they can ask.
        return {"manifest": DEMO_MANIFEST, "tenant": DEMO_MANIFEST["tenant"],
                "demo_fleet": demo_fleet_display()}

    def _do_compose(st: "_State", manifest: dict, secret_resolver=None,
                    owner_tenant: "str | None" = None, owner_oid: str = "") -> dict:
        """The ONE compose path - the endpoint and the setup agent (#116) both reach it
        (as does the demo compose seam, `compose_demo_catalog`, via `_compose_manifest`).

        `owner_oid` (#665): whose delegated credential introspects a delegated store's
        schema. Empty on the demo seam, which has no real caller and no vault - so that path
        keeps the server-identity behaviour it has always had."""
        return _compose_manifest(st, manifest, subject_token_provider, on_rotate,
                                 secrets=secret_resolver, owner_tenant=owner_tenant,
                                 owner_oid=owner_oid)

    def _guarded_manifest(manifest: dict, user_oid: str,
                          owner_tenant: "str | None" = None) -> "tuple[dict, str]":
        """#368/#818: the ONE guard prelude for anything that writes the stored row -
        _persisting_compose and the draft save share it, so the two writers cannot drift
        apart on the guard. Returns the cleaned manifest and the caller's workspace key."""
        # #439: the partition is SERVER business. Drop whatever the client sent under this
        # key before doing anything else, then write the verified value - so a crafted
        # manifest cannot aim its document reads at another tenant's partition (ADR 0012).
        manifest = {k: v for k, v in manifest.items() if k != OWNER_TENANT_KEY}
        if owner_tenant:
            manifest[OWNER_TENANT_KEY] = owner_tenant
        bad = find_secret_literals(manifest)
        if bad:
            # Store+field only - find_secret_literals never carries the value (LAW 1).
            names = ", ".join(f"{b['store_id']}.{b['field']}" for b in bad)
            raise HTTPException(status_code=400, detail=(
                f"plaintext credential in {names} - store it once via the credential "
                "panel (POST /secrets) and reference the returned secret:// handle"))
        # #423: server-side operator powers (${ENV} resolution, local file sources). This
        # sits on the REQUEST path only - `_rebuild` replays stored manifests and must not
        # re-judge an operator's own stores by whoever happens to warm the workspace.
        _guard_caller_manifest(manifest, user_oid)
        return manifest, _workspace_key(user_oid)

    def _persisting_compose(manifest: dict, user_oid: str,
                            owner_tenant: "str | None" = None) -> dict:
        """#368: the ONE guarded, persisting compose - both compose surfaces (the endpoint
        and the setup agent) go through here, so they cannot drift apart on the guard or on
        durability."""
        manifest, key = _guarded_manifest(manifest, user_oid, owner_tenant)
        # #368 final review (IMPORTANT 3a): `get_for_replace`, NOT `_workspace`. On a cold key
        # `_workspace` lazily rebuilt the ENTIRE catalog from the stored row and this compose
        # then immediately overwrote it - two `_compose_manifest` calls per cold compose, i.e.
        # the first canvas load after a restart connected and probed every one of the owner's
        # cloud databases twice and ran a connector-rail store's initial full crawl twice
        # (double-firing `sources_synced`), with a real chance of timing out at the proxy.
        # `adopt` registers the workspace only once this compose has fully succeeded, so a
        # failed compose leaves the owner's stored stores intact and still rebuildable.
        st, adopt = _pool.get_for_replace(key)
        # ADR 0010 s2/s3: the resolver is scoped to THIS caller (tenant + owner) so a
        # `secret://` handle in the manifest can only ever resolve to a credential this same
        # caller stored - never another tenant's or another user's (LAW 5). None when this
        # deployment has no secret store configured, which makes every handle a hard error
        # rather than a silent pass-through (resolve_env's no-resolver-wired refusal).
        out = _do_compose(st, manifest, _resolver_for(user_oid), owner_tenant=owner_tenant,
                          owner_oid=user_oid)
        if manifest_store is not None:
            try:
                manifest_store.put(key, manifest)
            except ManifestStoreUnavailable:
                # The in-memory compose above succeeded; refuse to pretend it is durable.
                raise HTTPException(status_code=503,
                                    detail="workspace store unavailable - the catalog "
                                           "composed but was NOT saved; try again shortly")
        adopt()
        return out

    @api.post("/compose")
    def compose(request: Request, req: ComposeRequest,
                user: str = Depends(current_user)) -> dict:
        # #439: resolve the caller's partition HERE, on the request, and persist it with the
        # manifest - a later rebuild has no request to derive it from. `tenant_resolver` is
        # the app's own resolve_tenant chokepoint (ADR 0012); None on rigs that wire the
        # router without it, which keeps today's single-partition behavior.
        return _persisting_compose(req.manifest, user,
                                   owner_tenant=tenant_resolver(request) if tenant_resolver else None)

    @api.get("/manifest")
    def manifest(user: str = Depends(current_user)) -> dict:
        """#368: the server-held manifest for the caller's workspace - the canvas restores
        from THIS, not localStorage, so a stale client copy can never shadow the truth.
        Safe to serve: a stored manifest carries only ADR 0010 s2 legal forms (the guard
        on compose refuses plaintext), and handles are inert without their owner."""
        if manifest_store is None:
            return {"manifest": None}
        try:
            return {"manifest": manifest_store.get(_workspace_key(user))}
        except ManifestStoreUnavailable:
            raise HTTPException(status_code=503,
                                detail="workspace store unavailable - try again shortly")

    @api.put("/manifest")
    def save_manifest(request: Request, req: ComposeRequest,
                      user: str = Depends(current_user)) -> dict:
        """#818: a guarded ROW WRITE, nothing else - so an added-but-not-yet-composed node
        survives a reload. Before this, the row was written only by _persisting_compose, and
        the canvas rebuilds exclusively from the row (#368) - so a draft the user had just
        added was durably lost on every reload (localStorage is a display cache the remount
        itself overwrites). The owner hit exactly this on prod.

        The contract:
        - SAME GUARDS AS COMPOSE, SHARED NOT COPIED (_guarded_manifest): plaintext
          credential -> 400 naming store.field, row untouched (LAW 1); caller-powers
          guard (#423); the partition key is server business (#439).
        - NO COMPOSE IN-REQUEST (LAW 4): no engines built, no probes fired, a warm catalog
          is not touched - drafts are not queryable until a real compose reconciles them
          (boot's composeUp already does). A crash after this write converges on the row.
        - FAIL CLOSED, HONESTLY: store outage or no store -> 503 saying NOT saved, never a
          lying 200 (an empty success hides an outage).
        """
        manifest, key = _guarded_manifest(
            req.manifest, user,
            owner_tenant=tenant_resolver(request) if tenant_resolver else None)
        if manifest_store is None:
            raise HTTPException(status_code=503,
                                detail="no workspace store configured - the draft was "
                                       "NOT saved")
        try:
            manifest_store.put(key, manifest)
        except ManifestStoreUnavailable:
            raise HTTPException(status_code=503,
                                detail="workspace store unavailable - the draft was "
                                       "NOT saved; try again shortly")
        return {"saved": True, "stores": len(manifest.get("stores") or [])}

    @api.delete("/stores/{store_id}")
    def delete_store(store_id: str, user: str = Depends(current_user)) -> dict:
        """#731: remove ONE store from the caller's workspace - the stored row AND the live
        catalog. Before this, delete was client-only and the stored manifest resurrected
        every node on the next page load; the canvas was silently draft/commit and nothing
        said so.

        The contract, in order of what matters:
        - DURABLE FIRST, FAIL CLOSED: the row is edited before the live workspace; a store
          outage is a 503 with NOTHING changed anywhere. A crash between the two writes
          converges on the row at the next rebuild.
        - CHEAP: the live workspace is touched only if already WARM (`get_if_warm`) - a
          delete must NEVER rebuild a workspace, because a rebuild re-fires connector
          crawls. Catalog surgery + the same `service = None` invalidation compose uses;
          no store rebuilds; the broker is untouched (a delegation whose catalog node is
          gone is unreachable - access_for is keyed by store id).
        - NON-DESTRUCTIVE (#731's revertibility condition): documents, ingest jobs, secret
          handles, vault entries and grants all survive - re-adding the store restores it
          wholesale. The removed manifest entry rides back for the client's Undo (same
          disclosure argument as GET /manifest: ADR 0010 s2 forms only, handles inert).
        - IDEMPOTENT: deleting an absent id answers 200 {"deleted": false}.
        - EMPTY IS A STATE: deleting the last store leaves {tenant, stores: []} - never an
          absent row - so hydration can tell "authoritatively empty" from "never composed".
        """
        key = _workspace_key(user)
        removed_entry = None
        removed = False
        if manifest_store is not None:
            try:
                m = manifest_store.get(key)
                if m is not None:
                    stores = m.get("stores") or []
                    removed_entry = next(
                        (s for s in stores if s.get("id") == store_id), None)
                    if removed_entry is not None:
                        manifest_store.put(key, dict(
                            m, stores=[s for s in stores if s.get("id") != store_id]))
                        removed = True
            except ManifestStoreUnavailable:
                raise HTTPException(
                    status_code=503,
                    detail="workspace store unavailable - the store was NOT deleted; "
                           "try again shortly")
        st = _pool.get_if_warm(key)
        if st is not None and getattr(st, "catalog", None) is not None:
            if st.catalog.remove(store_id):
                removed = True
                if getattr(st, "manifest", None):
                    st.manifest = dict(
                        st.manifest,
                        stores=[s for s in st.manifest.get("stores", [])
                                if s.get("id") != store_id])
                st.service = None
            # #947: a CONNECTOR store's delete is DESTRUCTIVE - purge its ingested chunks and
            # forget the built store, so "deleted" is true of the data too and a re-add
            # re-crawls the external source rather than reusing stale content (the #944
            # residual). Safe only because the source of truth is external; uploads keep their
            # own #923 confirm-modal path and never reach here. Warm-only, like the catalog
            # edit above: a cold connector store holds no in-process index to purge (its
            # content did not survive the restart - #940), so there is nothing to reach.
            provider = st.connector_source(store_id) if hasattr(st, "connector_source") else None
            if provider is not None:
                try:
                    if provider.purge(store_id):
                        removed = True
                except Exception:
                    logging.getLogger("dbsearch").warning(
                        "delete: purge failed for %s", store_id, exc_info=True)
        return {"store_id": store_id, "deleted": removed, "entry": removed_entry}

    # C1 (#116): the conversational setup agent — gathers manifest entries by chat,
    # validates early, applies through _persisting_compose (never a side door).
    from dbsearch.agents.setup_session import SetupSessionService, llm_entry_parser

    def _setup_ask(user_oid: str, question: str) -> dict:
        # Phase D verify: the REAL routed ask path — same trim, same routing.
        llm = edition.chat_models[edition.chat_model_default]
        return _service_for(_workspace(user_oid)).ask(user_oid, question, llm).to_dict()

    def _setup_health(user_oid: str, entry: dict) -> dict:
        # #130 Phase G: round-trip health check on a just-composed store, as the admin.
        from dbsearch.router.health import ConnectionTest, default_strategies
        # C4 (#319 review): same resolver-threading as /router/compose (Task 5) - without
        # it a `secret://` handle in this entry's config could never resolve, and the
        # caller's own health check (`setup.turn`) would report the exact "manifest
        # references unset env var" -shaped failure ADR 0010 exists to fix. Any error here
        # (including a foreign-handle PermissionError) is already caught by the caller
        # (`setup_session.py`'s `except Exception` around `self._health(...)`) and turned
        # into a `{"status": "failed", ...}` verdict rather than a 500 - and that message
        # never contains the handle (ScopedSecretResolver's refusal deliberately omits it).
        resolver = _resolver_for(user_oid)
        merged = r.resolve_env(_merged_config(entry), secrets=resolver)
        ct = ConnectionTest(_workspace(user_oid).registry, default_strategies())
        return ct.run({"kind": entry.get("kind", ""), "mode": entry.get("mode"),
                       "id": merged["id"], "config": merged}, user_oid).to_dict()

    # C3 (#116): parse entries with the default chat model when it can extract them
    # (Anthropic/Groq — key-gated exactly like the #57 model split), keyword otherwise.
    _setup_llm = edition.chat_models[edition.chat_model_default]
    _parser = (llm_entry_parser(_setup_llm)
               if hasattr(_setup_llm, "extract_setup_entries") else None)
    setup = SetupSessionService(
        tenant=edition.tenant_id,
        # #368: the setup agent composes into the CALLER's workspace, through the same
        # guarded, persisting path the endpoint uses - never a second compose surface.
        compose=lambda manifest, user_oid: _persisting_compose(manifest, user_oid),
        modes_for=_kinds_state.registry.modes_for, ask=_setup_ask,
        entry_parser=_parser, health=_setup_health)

    @api.post("/setup/turn")
    def setup_turn(req: SetupTurnRequest, user: str = Depends(current_user)) -> dict:
        return setup.turn(user, req.conv_id, req.message, req.intent).to_dict()

    @api.post("/probe")
    def probe(req: ProbeRequest, user: str = Depends(current_user)) -> dict:
        # C4 (#319 review): the motivating bug of ADR 0010 - "Test connection" (this
        # endpoint) is the button a user presses FIRST after storing a credential, and
        # without this resolver every `secret://` handle in the entry hit resolve_env's
        # no-resolver-wired refusal, indistinguishable from the original unset-${ENV} bug
        # ADR 0010 opens by quoting. /router/compose has had this since Task 5; /probe did
        # not.
        resolver = _resolver_for(user)
        st = _workspace(user)
        entry = req.entry
        kind = entry.get("kind", "")
        if not st.known(kind):
            return {"available": False,
                    "reason": f"no provider for kind {kind!r} yet — lands in E4/E9"}
        provider = st.registry.get(kind)
        try:
            config = r.resolve_env(_merged_config(entry), secrets=resolver)
            cred = _introspect_credential_for(entry, user)
            prober = getattr(provider, "probe_as", None)
            profile = (prober(config, credential=cred)
                       if prober is not None and cred else provider.probe(config))
        except PermissionError:
            # Task 5 policy (ADR 0010 s3), applied here for the first time: a foreign
            # secret handle must be a clean 403, never downgraded into the same
            # available=False/reason shape as an ordinary unreachable store - that would
            # be indistinguishable from "this store has bad credentials" and would let the
            # prober keep guessing quietly. Fixed detail string - never `str(exc)` or the
            # handle (the refusal itself never names the handle it tried, see
            # ScopedSecretResolver.resolve).
            raise HTTPException(status_code=403,
                                detail="not authorized for this secret handle")
        except Exception as exc:
            # #107: an unreachable/miscredentialed store probes honestly-unavailable
            # (same isolation as the compose skip) instead of a 500.
            # #832: some exceptions stringify to "" (cryptography's InvalidToken), which
            # rendered the literal reason "probe failed: " - name the type as the fallback.
            return {"available": False,
                    "reason": f"probe failed: {str(exc) or type(exc).__name__}"}
        out = {"available": True, "profile": _profile_summary(profile)}
        # #107 probe mode-upgrade (ADR 0008): the tenant is Graph-licensed and this
        # entry would COPY SharePoint into an index — recommend the zero-copy native
        # path. A suggestion only, never a silent mode switch.
        import os as _os

        if (kind == "sharepoint" and entry.get("mode", "index") == "index"
                and _os.environ.get("GRAPH_TOKEN")):
            out["recommendation"] = (
                "tenant licenses Microsoft Graph Search — consider kind: "
                "graph_search (mode: native): zero-copy, no derived index to sync "
                "(ADR 0008); the index mode you probed still works")
        return out

    @api.post("/health")
    def health(req: ProbeRequest, user: str = Depends(current_user)) -> dict:
        """#130 Phase G: a graded ROUND-TRIP verdict (healthy|degraded|failed) — probe proves
        reachability, this proves a record flows back through retrieve() as the calling admin.
        Read-only on every rail; never a 500 (unreachable -> failed verdict, like /probe)."""
        from dbsearch.router.health import (
            ConnectionTest, HealthVerdict, StageResult, default_strategies,
        )

        # C4 (#319 review): same wiring as /probe above and /router/compose (Task 5) - this
        # is "Test connection", the button ADR 0010's motivating bug quote is about.
        resolver = _resolver_for(user)
        st = _workspace(user)
        entry = req.entry
        kind = entry.get("kind", "")
        mode = entry.get("mode")
        if not st.known(kind, mode):
            return HealthVerdict(
                status="failed",
                stages=[StageResult("probe", False, 0, f"no provider for {kind!r} yet")],
                summary=f"No provider for kind {kind!r} — lands in E4/E9.",
                remediation="connect this source's provider first").to_dict()
        try:
            merged = r.resolve_env(_merged_config(entry), secrets=resolver)
            ct = ConnectionTest(st.registry, default_strategies())
            verdict = ct.run({"kind": kind, "mode": mode, "id": merged["id"],
                              "config": merged}, user,
                             introspect_credential=_introspect_credential_for(entry, user))
        except PermissionError:
            # Same Task 5 policy as compose and /probe above: a foreign secret handle is a
            # clean 403, not downgraded into a "failed" verdict (which would look exactly
            # like an ordinary bad-credential health check, and would let a prober keep
            # guessing quietly). Fixed detail, never the handle.
            raise HTTPException(status_code=403,
                                detail="not authorized for this secret handle")
        except Exception as exc:
            # honest-unavailable: unresolved ${ENV} / bad config -> failed verdict, never a 500
            logging.getLogger("dbsearch").warning(
                "health %s (%s) could not prepare check: %s",
                entry.get("id", "store"), kind, exc)
            return HealthVerdict(
                status="failed",
                stages=[StageResult("probe", False, 0, f"could not prepare check: {exc}")],
                summary=f"Cannot check {kind!r}.",
                remediation=str(exc)).to_dict()
        return verdict.to_dict()

    @api.post("/stores/{store_id}/sync", status_code=202)
    def sync_store(store_id: str, user: str = Depends(current_user)) -> dict:
        """Delta re-sync one composed connector-backed store (#111). Only stores built by
        the connector rail have a cursor to resume; others 404 (metadata-only response).

        202, not 200 (#454, ADR 0016 §1). This used to call `provider.sync()` INLINE, so the
        whole crawl ran inside this request - a LAW 4 violation with a measured consequence:
        #536's 40.2MB / 4884-document pack never finished inside the 3600s timeout, so the
        flow the product is FOR could not complete at all. The crawl is now submitted and the
        caller is handed a job id to poll (`GET /router/jobs/{job_id}`)."""
        provider = _workspace(user).connector_source(store_id)
        if provider is None:
            raise HTTPException(status_code=404,
                                detail=f"no connector-backed store {store_id!r} composed")
        job = provider.start_sync(store_id)
        s = provider.summary(store_id)
        if job is None:
            # A shared-index store: the edition owns its ingestion, so there is no job to
            # watch. Said plainly rather than returning a job id that would never move.
            return {"store_id": s.source_id, "kind": s.kind, "job_id": None,
                    "status": s.status, "docs_synced": s.doc_count,
                    "last_sync_at": s.last_sync_at,
                    "detail": "this store's ingestion is managed by the edition, not the router"}
        return {"store_id": s.source_id, "kind": s.kind, "job_id": job.job_id,
                "status": "syncing", "docs_synced": s.doc_count,
                "last_sync_at": s.last_sync_at,
                "resumed": bool(job.docs_done or job.docs_skipped),
                "poll": f"/router/jobs/{job.job_id}"}

    @api.get("/jobs/{job_id}")
    def ingest_job(job_id: str, user: str = Depends(current_user)) -> dict:
        """How a long ingest stops being indistinguishable from a hang (ADR 0016 §4).

        Reports the phase, how many documents are done out of how many, how many a resume
        skipped because a previous attempt had already indexed them, and the terminal reason
        when there is one. `error` is an exception CLASS NAME by construction
        (jobs.JobCheckpoint.failed) - never a driver or connector message, which can quote
        document content or a credential (LAW 1).

        LAW 2 / ADR 0012: a job is only visible to a caller whose workspace owns the store it
        belongs to. Without that check a job id would be an oracle for other people's source
        names and document counts, which is #549's defect on a new surface."""
        ws = _workspace(user)
        for provider in ws.connector_providers:
            job = provider.jobs.get(job_id)
            if job is None:
                continue
            if not provider.owns(job.source_id):
                continue
            return {"job_id": job.job_id, "store_id": job.source_id, "status": job.status,
                    "phase": job.phase, "docs_done": job.docs_done,
                    "docs_total": job.docs_total, "docs_skipped": job.docs_skipped,
                    "error": job.error, "updated_at": job.updated_at}
        # 404 for both "no such job" and "not yours" - a distinguishable 403 would confirm
        # the id exists.
        raise HTTPException(status_code=404, detail=f"no ingest job {job_id!r}")

    @api.post("/route")
    def route(req: QuestionRequest, scope: RequestScope = Depends(scoped)) -> dict:
        return scope.service().route(scope.principal, req.question,
                                     store_override=req.store).to_dict()

    @api.post("/ask")
    def ask(req: QuestionRequest, scope: RequestScope = Depends(scoped)) -> dict:
        result = scope.service().ask(scope.principal, req.question, scope.chat_llm,
                                     store_override=req.store).to_dict()
        return decorate_ask_result(result, scope.catalog(), scope.user)

    @api.post("/rerun")
    def rerun(req: RerunRequest, scope: RequestScope = Depends(scoped)) -> dict:
        """#165: re-execute a server-issued SQL proof under the CALLER's guards.
        Gate #1: invisible store == nonexistent store (identical 404)."""
        _NOT_FOUND = HTTPException(status_code=404, detail="no such store")
        catalog = scope.catalog()
        if catalog is None:
            raise _NOT_FOUND
        visible = {n.id for n in catalog.visible_stores(scope.groups())}
        if req.store_id not in visible:
            raise _NOT_FOUND
        if not verify_rerun(req.store_id, req.sql, scope.user, req.token):
            raise HTTPException(status_code=403,
                                detail="proof token invalid — re-ask the question")
        store = catalog.get(req.store_id).store
        runner = getattr(store, "rerun_sql", None)
        if runner is None:
            raise HTTPException(status_code=409, detail="store does not support re-run")
        access = store.authorize(scope.principal)
        try:
            cols, rows, count = runner(access, req.sql)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"re-run failed: {exc}")
        return {"cols": cols, "rows": rows, "count": count, "capped": count > len(rows)}

    @api.get("/catalog")
    def catalog(scope: RequestScope = Depends(scoped)) -> dict:
        """E7 admin/advisor read surface: the CALLER-visible catalog tree with freshness
        (gate #1 — a user can never enumerate stores they aren't cleared to see)."""
        cat = scope.catalog()
        if cat is None:
            raise HTTPException(status_code=409,
                                detail="no catalog composed yet — POST /router/compose first")
        return cat.visible_tree(scope.groups())

    @api.get("/stores/{store_id}/schema")
    def store_schema(store_id: str, scope: RequestScope = Depends(scoped)) -> dict:
        """#562: what is actually IN one composed store — tables, columns, row counts.

        The Admin console could report on the document plane and nothing else, so the
        databases on the canvas had no answer surface at all.

        Resolved through the caller's own visible_stores(), never by id lookup: a store this
        caller cannot enumerate answers **404**, the same answer a store that does not exist
        gets. 403 would confirm it exists, which is the existence probe the catalog's
        hereditary trim (gate #1) is built to close. The scope injection is #336/#340 - an
        endpoint that picks its own catalog can pair the demo catalog with the live identity.

        No rows are returned. The schema comes off the profile the catalog already holds, so
        this costs no query, and row counts come from the engine's own row_counts() - which
        answers None when it cannot count. Reporting 0 there would tell an operator a full
        warehouse is empty (#392: unknown is not empty).
        """
        cat = scope.catalog()
        if cat is None:
            raise HTTPException(status_code=409,
                                detail="no catalog composed yet — POST /router/compose first")
        node = next((n for n in cat.visible_stores(scope.groups()) if n.id == store_id), None)
        if node is None:
            raise HTTPException(status_code=404, detail="no such store")
        profile = node.profile
        tables = list(getattr(profile, "schema", None) or [])
        engine = getattr(node.store, "_engine", None)
        counts = None
        if engine is not None and hasattr(engine, "row_counts"):
            try:
                counts = engine.row_counts()
            except Exception:
                # A store that cannot be counted right now is not a broken endpoint. Report
                # the schema we already have and say the counts are unknown.
                counts = None
        return {
            "store_id": node.id,
            "title": profile.title if profile else node.id,
            "kind": profile.kind if profile else "",
            "capabilities": sorted(profile.capabilities) if profile else [],
            "business_unit": profile.business_unit if profile else "",
            "freshness": profile.freshness if profile else "",
            "counts_known": counts is not None,
            "tables": [{"table": t.get("table", ""),
                        "columns": t.get("columns", []),
                        "row_count": (counts or {}).get(t.get("table", ""))}
                       for t in tables],
        }

    @api.get("/stores/{store_id}/documents")
    def store_documents(store_id: str, scope: RequestScope = Depends(scoped)) -> dict:
        """#939 / #895: WHAT landed in this store, and when - for THIS caller.

        The launch gate asks a connected node to show "synced + doc count". Nothing in the
        product could answer either: `/admin/sources` only ever knew about sharepoint rows, and
        the canvas node's freshness is a snapshot taken AT COMPOSE, so a crawl that finished
        afterwards left the badge reading `syncing` forever (measured on prod 260823 - catalog
        `ingested@08:58:31`, badge `syncing`).

        Resolved through the caller's own `visible_stores()` and 404 - never 403 - for a store
        this caller cannot enumerate, which is the schema endpoint's rule verbatim: 403 would
        confirm the store exists, and that is the existence probe gate #1 closes.

        `documents` is trimmed by the store's OWN authorize() (LAW 2 - see
        QueryService.document_inventory for why forwarding the admin listing would be a
        disclosure). Titles and uris only, never content (LAW 1).

        `freshness` comes off the LIVE descriptor rather than the catalog snapshot, which is
        the half of this that fixes the stale badge. `unreadable` is #725: files the crawl
        listed and could not fetch (a per-file 403, an export cap). A list that silently omits
        them would be a new way to mislead - the file is in the folder, absent from the list,
        and nothing says why - so the count travels with the list even when it is zero.
        """
        cat = scope.catalog()
        if cat is None:
            raise HTTPException(status_code=409,
                                detail="no catalog composed yet - POST /router/compose first")
        node = next((n for n in cat.visible_stores(scope.groups()) if n.id == store_id), None)
        if node is None:
            raise HTTPException(status_code=404, detail="no such store")
        store = node.store
        lister = getattr(store, "documents", None)
        if not callable(lister):
            # A SQL store has no document inventory, and saying "0 documents" about one would
            # be a number that is not false so much as meaningless. Unknown, not empty (#392).
            return {"store_id": node.id, "known": False, "documents": [],
                    "doc_count": None, "freshness": "", "unreadable": 0}
        try:
            docs = lister(store.authorize(scope.principal))
        except Exception:
            # A store that cannot list right now is not a broken endpoint - and it is NOT an
            # empty one either. Report unknown and let the surface stay quiet.
            return {"store_id": node.id, "known": False, "documents": [],
                    "doc_count": None, "freshness": "", "unreadable": 0}
        desc = getattr(store, "descriptor", None)
        fresh = getattr(store, "freshness", None)
        return {
            "store_id": node.id,
            "known": True,
            "documents": docs,
            "doc_count": len(docs),
            "freshness": fresh() if callable(fresh) else "",
            "unreadable": int(getattr(desc, "unreadable", 0) or 0),
        }

    def _composed_source_count(user: str) -> "int | None":
        """#937: how many sources this caller has COMPOSED - the other plane's answer to
        "is there anything here for me to search?".

        /ask needs this because `corpus_status` cannot answer it. That counter scans the
        uploaded-document index, and a connector store builds its own (see
        providers/connector.py - `index = InMemoryIndex(obj)`), so a caller whose only source
        is a Drive folder is reported as having an empty corpus forever. The page then told
        them to go and connect a source they had already connected.

        READ OFF THE STORED MANIFEST, NOT A BUILT CATALOG, and that is a deliberate cost
        decision rather than a shortcut: /ask calls this on every page load, and
        `_build_scope` materializes a workspace. Warming every caller's workspace to decide
        whether to print one sentence would spend the #731 cold-key property on a banner. A
        manifest read is the single key lookup the canvas already does to restore itself.

        None means UNKNOWN and never zero. A workspace store that is briefly unreachable must
        not render as "you have connected nothing" - that is #392's own rule (a corpus we did
        not measure is silence, not emptiness) applied to the plane #392 did not know about.
        Zero is returned only when it is MEASURED: no manifest rail on this deployment, so the
        document plane genuinely is the whole truth, or a manifest that really holds no stores.

        Counts only, never store ids or titles (LAW 1), and only ever the caller's OWN
        workspace key - this can neither enumerate nor reveal anybody else's sources.

        WHAT IT DELIBERATELY IS NOT: acl-aware. Under a real login `_workspace_key` is the
        caller's own oid, so their manifest is theirs and the count is exact. Under the
        shared-workspace rigs (dev headers, the alice/bob demo) every caller shares SHARED_KEY,
        so a store composed by one is counted for all - and a caller outside that store's acl
        would be counted as having a source they cannot query. Trimming by acl means building
        the catalog, which is the cost this function exists to avoid. The consequence is bounded
        and it falls the safe way: the only thing the number gates is whether /ask prints
        "connect a source", so the worst case is a rig user seeing one fewer suggestion, never
        a caller learning that a store exists or reaching content the trim would refuse.
        """
        if manifest_store is None:
            return 0
        try:
            stored = manifest_store.get(_workspace_key(user)) or {}
        except ManifestStoreUnavailable:
            return None
        return len(stored.get("stores") or [])

    def _composed_documents(user: str) -> "list | None":
        """#948: the CONNECTOR plane's documents for this caller, ACL-trimmed - the rows
        /admin/documents cannot see because a connector store indexes into its OWN in-process
        index (providers/connector.py), not the uploaded-document index list_documents reads.
        This is #937's two-plane split reaching the Admin surface.

        WARM-ONLY, deliberately (`get_if_warm`, never `_workspace`): /admin/documents is a
        page load, and `_pool.get` on a cold key REBUILDS the catalog from the manifest, which
        fires every connector's crawl. Materializing a workspace just to render a list would
        put a crawl behind every Admin visit. A caller who composed this session has a warm
        workspace and sees their connector docs; a cold one sees uploads only until they
        compose - the honest bound of #940 (connector content is not durable), surfaced rather
        than hidden behind a crawl. None = UNKNOWN (older build, or not warm), which the caller
        renders as 'nothing to add', never as a claim of emptiness (#392).

        ACL-TRIMMED by each store's OWN authorize() (LAW 2), the same call and the same trim
        /router/stores/{id}/documents uses - so this can never name a document a query would
        refuse. Titles and uris only (LAW 1). `source_store`/`source_kind` travel so the
        surface can say WHERE a document came from and withhold the upload-only actions
        (Download, Delete) that would 404 on a doc the upload index never held.
        """
        if user.startswith(DEMO_PREFIX):
            return None
        try:
            st = _pool.get_if_warm(_workspace_key(user))
        except Exception:
            return None
        if st is None or getattr(st, "catalog", None) is None:
            return None
        scope = _build_scope(user)
        groups = scope.groups()
        rows: list[dict] = []
        for node in st.catalog.visible_stores(groups):
            store = getattr(node, "store", None)
            lister = getattr(store, "documents", None)
            if not callable(lister):
                continue                      # a SQL store has no document inventory
            try:
                docs = lister(store.authorize(scope.principal))
            except Exception:
                continue                      # a store that cannot list now is not empty (#392)
            kind = getattr(getattr(store, "descriptor", None), "kind", "") or ""
            for d in docs:
                rows.append({
                    "doc_external_id": d.get("doc") or d.get("doc_external_id") or "",
                    "title": d.get("title", ""),
                    "uri": d.get("uri", ""),
                    # Private-to-the-adder is the only audience a connector store has today
                    # (#920), and the store already trimmed this list to what the caller may
                    # see, so naming the caller as the audience is true and not a disclosure.
                    # owned_by_you is deliberately ABSENT: a connector doc is removed by
                    # deleting its SOURCE node (#947), never one-by-one, so drawing a per-doc
                    # Delete would be the #551 always-404 tile.
                    "allowed_principals": [scope.principal],
                    "source_store": node.id,
                    "source_kind": kind,
                })
        return rows

    # #731: introspection seam - selftest_731 pins that a delete on a COLD key edits the
    # stored row without materializing a workspace (warm_keys() stays empty).
    api._workspace_pool = _pool
    # #948: the second plane's document listing, for /admin/documents to merge in. Attached,
    # not a module function, for the same reason as _composed_source_count: the workspace key,
    # the pool and the scope builder are all closure state of this builder.
    api._composed_documents = _composed_documents
    # #937: the seam /ask/suggestions reads. Attached rather than exported as a module function
    # because the workspace key and the manifest store are both closure state of this builder;
    # a second definition in app.py would be a second answer to "which workspace is this
    # caller's", which is exactly the drift `_workspace_key` exists to prevent.
    api._composed_source_count = _composed_source_count
    return api
