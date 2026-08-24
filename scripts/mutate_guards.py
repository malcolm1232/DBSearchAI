#!/usr/bin/env python3
"""Break the product on purpose, one edit at a time, and check the guard goes red.

    python3 scripts/mutate_guards.py                 # the whole matrix
    python3 scripts/mutate_guards.py -k 793          # only entries whose card or id matches
    python3 scripts/mutate_guards.py --list          # what is in the matrix, run nothing

#785. WHY THIS EXISTS, and it is not tidiness.

`CONTRIBUTING.md` already asks for this by hand: "if you are adding a guard, try breaking the
code on purpose and confirm the guard goes red. A guard that cannot fail is not a guard." The
trouble is that the RESULT of doing it lived in prose. #760's commit message claimed "9 CSS + 3
JS mutations, every one caught" and no such matrix existed anywhere - nothing re-ran it, nothing
could contradict it, and two of those guards were later shown to survive the mutation they
named. The 260817 verifier round then found 15 of 22 mutations surviving, and that number lives
in a handover nobody can execute either.

A claim about guards has to be a command. This is that command.

HOW IT AVOIDS LYING IN THE OTHER DIRECTION. A mutation "caught" for the wrong reason is worse
than one that survives, because it reads as coverage. Two rules:

  * EVERY RUN STARTS WITH A CONTROL. Each guard is run UNMUTATED first, and must be green. If it
    is not - a missing jsdom, a broken fixture, an unrelated failure - the whole matrix aborts.
    Without this, #792's change would make every DOM mutation report CAUGHT on a machine with no
    node_modules, having proved nothing at all.
  * EVERY ENTRY DECLARES WHAT IT EXPECTS. `expect="caught"` is a guard we believe in;
    `expect="survives"` is an open defect with the card that will fix it. The script fails only
    on a SURPRISE, so it is a regression gate today and a scoreboard for the tail at the same
    time. Flipping an entry to "caught" is part of the commit that fixes its card.

MUTATE ON A COPY, NEVER IN THE REPO. `git archive HEAD` into a scratch tree with
tests/node_modules symlinked. Two agents mutating the live working tree collided on 260817 and
produced a phantom `262/263` that belonged to another process's injected edit. The copy also
means an interrupted run cannot leave a mutation behind.

A MUTATION MUST BE FAITHFUL. Recreate a past defect from `git show <ref>:<path>`, not from memory
of its shape: a "revert" that used a placeholder value made the collision that WAS the defect
impossible to arise, and the guard looked green for a reason that had nothing to do with the
guard.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CANVAS = "src/dbsearch/server/static/js/surfaces/canvas.js"
CANVAS_CSS = "src/dbsearch/server/static/css/canvas.css"
# #924
SPL_PATH = "src/dbsearch/connectors/sharepoint_link.py"
SPL_GUARD = "tests/selftest_924_sharepoint_link.py"
CONNECTOR_PATH = "src/dbsearch/router/providers/connector.py"
# #689: the Sources rail and its helpers live here now, shared by /canvas and /ask.
PROOFS_JS = "src/dbsearch/server/static/js/ui/proofs.js"
ANSWER_SURFACE = "tests/selftest_715_729_answer_surface.py"
DOC_BLOCK = "tests/selftest_724_doc_block.py"

# Every entry: the card it belongs to, the exact edit, the guard that owns it, and what we
# currently believe. `old` must appear EXACTLY ONCE in the file, which is checked - a mutation
# that silently hit three call sites, or none, proves nothing about the one under test.
MUTATIONS = [
    # ---- #883: the registry's sync-state must outlive the process ---------------------------
    dict(id="883-register-blind-overwrite", card="#883",
         path="src/dbsearch/connectors/registry.py",
         guard="tests/selftest_883_source_sync_survives_restart.py",
         old="""        if self._store is not None:
            self._merge_persisted(desc)
            self._persist(desc)
        self._sources[desc.source_id] = desc""",
         new="""        if self._store is not None:
            self._persist(desc)
        self._sources[desc.source_id] = desc""",
         expect="caught",
         why="THE #883 DEFECT with a table under it, which is worse than the defect. Register "
             "stays a blind overwrite, so build_edition's unconditional boot seed writes its "
             "virgin descriptor over the durable row - the count is not merely lost on restart, "
             "it is durably ZEROED. Measured on prod twice: /admin/sources showed the seeded "
             "'sharepoint' with doc_count 0 while /admin/documents listed 6 and Ask answered "
             "with citations from them."),
    dict(id="883-cursor-not-written-at-commit", card="#883",
         path="src/dbsearch/connectors/registry.py",
         guard="tests/selftest_883_source_sync_survives_restart.py",
         old="""        if self._store is not None:
            self._persist(d)
        return d.summary()""",
         new="""        return d.summary()""",
         expect="caught",
         why="record_sync is the one place a completed crawl's result becomes durable (ADR "
             "0016: at _commit, after the batch is written, never earlier). Drop the "
             "write-through and the row keeps whatever register() first put there, so the node "
             "reports the count it had at connect time forever and a resync re-pays for the "
             "whole library."),
    # NO entry for the _schema_done ordering here, and the omission is deliberate. It was
    # written, run, and SURVIVED - correctly. In PgManifestStore that ordering is load-bearing
    # because its _run RAISES, so a flag left True after a rolled-back DDL poisons the process
    # forever. This store swallows and resets the flag to False in the same except, and
    # _ensure_schema holds _schema_lock across the whole DDL so no other thread can observe the
    # True-but-not-created window. Both properties together make the earlier assignment benign,
    # so an entry claiming otherwise would be an unfaithful mutation dressed as a guard.

    # ---- #872 / #875: whose principals a caller expands to, and what a failure means --------
    dict(id="872-expansion-asks-for-groups-only", card="#872",
         path="src/dbsearch/server/user_auth.py",
         guard="tests/selftest_group_resolution.py",
         old='            f"{GRAPH}/users/{urllib.parse.quote(user_oid)}/getMemberObjects",',
         new='            f"{GRAPH}/users/{urllib.parse.quote(user_oid)}/getMemberGroups",',
         expect="caught",
         why="THE #872 DEFECT as prod showed it. getMemberGroups returns groups ONLY. Every "
             "file in the owner's SharePoint library was ACL'd to a DIRECTORY ROLE (Global "
             "Administrator, which SharePoint reports under the identitySet's `group` key), so "
             "the trim denied him his own documents and Ask answered 'I do not have that "
             "information' about his own letter of employment. Measured both ways on prod: "
             "getMemberGroups returned 3 oids without it, getMemberObjects returned 4 with it."),
    dict(id="872-role-cannot-be-named", card="#872",
         path="src/dbsearch/server/user_auth.py",
         guard="tests/selftest_group_resolution.py",
         old='_NAMEABLE_TYPES = ["group", "user", "directoryRole"]',
         new='_NAMEABLE_TYPES = ["group", "user"]',
         expect="caught",
         why="The half that is easy to forget because nothing errors: getByIds returns only the "
             "types it is asked for and answers a PARTIAL list silently. A role-ACL'd document "
             "then expands correctly and its principal sits unnamed, which list_directory drops "
             "- access granted and invisible to the operator who manages it."),
    dict(id="875-signin-caches-a-failed-lookup", card="#875",
         path="src/dbsearch/server/app.py",
         guard="tests/selftest_group_resolution.py",
         old="        _resolve_groups_if_unknown(ident, u[\"oid\"], u[\"tid\"], session_tid=u[\"tid\"],\n"
             "                                   force=True, self_name=u.get(\"name\", \"\"))",
         new="        groups = _with_tenant_principal(\n"
             "            user_auth.fetch_member_principals(u[\"tid\"], u[\"oid\"]) or [], u[\"tid\"])\n"
             "        ident.set_user_groups(u[\"oid\"], groups)",
         expect="caught",
         why="THE #875 DEFECT verbatim, restored from `git show` rather than from memory of its "
             "shape: `or []` writes a Graph FAILURE into the cache as 'resolved, belongs to "
             "nothing', and set_user_groups then makes knows_groups true so the chokepoint's "
             "retry can never fire. One transient 403 becomes a permanent silent denial. It "
             "happened to the owner on prod on 2026-08-20."),
    dict(id="875-signin-trusts-a-stale-cache", card="#875",
         path="src/dbsearch/server/app.py",
         guard="tests/selftest_group_resolution.py",
         old="    if ident.knows_groups(oid) and not force:",
         new="    if ident.knows_groups(oid):",
         expect="caught",
         why="The OTHER clause of the same fix, mutated separately so neither can be rescued by "
             "the other (the 260818 lesson). Without `force` a fresh sign-in skips on the cache, "
             "so a user whose groups changed - or whose entry was poisoned before the fix - "
             "keeps the stale expansion for the life of the process."),
    # ---- #880 / #879: the ingest modal, and the picker it opens from -----------------------
    dict(id="880-succeed-on-the-202", card="#880",
         path='src/dbsearch/server/static/js/surfaces/canvas.js',
         guard='tests/e2e_880_ingest_modal.py',
         old="    spRun.jobId=r.job_id;\n    spWatchJob(spRun);",
         new="    spRun.status='succeeded'; spRun.docs=r.docs_indexed; renderSpRun(); closeSpPicker();",
         expect="caught",
         why="THE #880 DEFECT verbatim. Since #569 (LAW 4) /connectors/sharepoint/finish returns "
             "202 the moment the job is QUEUED, and api() treats any r.ok as success - so the old "
             "succeed(r) fired on the acknowledgement, closed the modal before the crawl started, "
             "killed the poller and wrote r.docs_indexed (a field the 202 does not carry) onto the "
             "node. The owner saw the modal flash, read '0 documents', concluded it had failed and "
             "ran the entire crawl a second time."),
    dict(id="880-skipping-blanks-the-stepper", card="#880",
         path='src/dbsearch/server/static/js/surfaces/canvas.js',
         guard='tests/e2e_880_ingest_modal.py',
         old="discovering:0, fetching:1, skipping:1,",
         new="discovering:0, fetching:1,",
         expect="caught",
         why="The runner emits `skipping` for an unchanged document, STEP_OF had no entry for it, "
             "and paint(-1) then cleared EVERY dot - so a resumed crawl looked like it had "
             "restarted from nothing, in the middle of running correctly. Mutated apart from the "
             "modal-lifetime clause because a fixture rescued by both proves neither."),
    dict(id="880-count-not-re-read", card="#880",
         path='src/dbsearch/server/static/js/surfaces/canvas.js',
         guard='tests/e2e_880_ingest_modal.py',
         # re-anchored across #917 (syncDocumentsNode() rides the same line); the mutation
         # still drops ONLY the SharePoint re-read this clause is about.
         old="    await syncSharePointNodes(); syncDocumentsNode();\n    const ing=spIngested[run.tenant];",
         new="    syncDocumentsNode();\n    const ing=spIngested[run.tenant];",
         expect="caught",
         why="The count was read once at mount and never again, so the node kept the pre-crawl 0 "
             "while the ingest ran. This mutation reproduces the exact string the owner reported: "
             "'0 documents indexed and searchable'. /admin/sources commits doc_count BEFORE the job "
             "is published terminal, so re-reading on terminal cannot race it."),
    dict(id="879-drives-awaited-before-paint", card="#879",
         path='src/dbsearch/server/static/js/surfaces/canvas.js',
         guard='tests/e2e_880_ingest_modal.py',
         old='    paintShell();   // #879: BEFORE any network call \u2014 the folder-link path needs no enumeration',
         new='    body.innerHTML=\'<div class="qmeta">Loading libraries\\u2026</div>\';\n'
             '    api("/connectors/sharepoint/drives?tenant="+encodeURIComponent(tenant))\n'
             '      .then(d=>{ spDrives=d; paintShell(); });',
         expect="caught",
         why="THE #879 DEFECT restored: nothing but 'Loading libraries...' until the enumeration "
             "returns, and only then the dialog. Measured at 11.2s on the owner's tenant, every "
             "second of it spent on a list the folder-link path - the one he used - does not need. "
             "Written as a faithful single edit after a first attempt merely flipped the function "
             "to `async`, which changes no behaviour at all and which the guard correctly SURVIVED. "
             "A mutation that does not reproduce the defect proves nothing about the guard."),
    dict(id="879-rows-labelled-by-drive", card="#879",
         path='src/dbsearch/server/static/js/surfaces/canvas.js',
         guard='tests/e2e_880_ingest_modal.py',
         old='    if(site && lib && site.toLowerCase()!==lib.toLowerCase()) return site+" \u2014 "+lib;\n    return site||lib;',
         new="    return lib||site;",
         expect="caught",
         why="Every default library in every tenant is called 'Documents', so seven rows read the "
             "same word in bold with the site name - the only distinguishing fact, and already in "
             "the payload - demoted to grey 11px. The owner read a list of tenant sites as a "
             "statement about his own files: 'since when i have so many docs?'"),
    dict(id="879-system-sites-as-peers", card="#879",
         path='src/dbsearch/server/static/js/surfaces/canvas.js',
         guard='tests/e2e_880_ingest_modal.py',
         old="    const real=spDrives.filter(d=>!d.system), sys=spDrives.filter(d=>d.system);",
         new="    const real=spDrives, sys=[];",
         expect="caught",
         why="contentTypeHub, the Viva group site and the AllCompany sites are plumbing nobody "
             "ingests, and shown as peers of real sites they are most of what made the tenant look "
             "unrecognisable. They are FOLDED, not dropped - a list that silently omits rows gives "
             "nobody a way to tell filtering from absence."),
    # ---- #885: a status poll metered as if it were the crawl --------------------------------
    dict(id="885-job-status-metered-as-ingest", card="#885",
         path="src/dbsearch/server/rate_limit.py",
         guard="tests/selftest_rate_limit.py",
         old='    "/ingest/jobs",',
         new='    # "/ingest/jobs",',
         expect="caught",
         why="THE PROD DEFECT, found by driving the owner's own folder link. GET "
             "/ingest/jobs/{id} is a status read but matched the '/ingest' costly prefix, so it "
             "shared a 30-per-minute per-IP budget with the crawl submit. The arithmetic makes "
             "the feature impossible rather than slow: one watcher spends the whole "
             "application's allowance. It presented as a LIE - #880's modal ate the budget in 18 "
             "seconds and reported 'That ingest did not finish' about a crawl that finished with "
             "5 documents at 06:59:18Z."),
    dict(id="885-exemption-too-broad", card="#885",
         path="src/dbsearch/server/rate_limit.py",
         guard="tests/selftest_rate_limit.py",
         old='    "/ingest/jobs",',
         new='    "/ingest",',
         expect="caught",
         why="The direction that would turn a correction into a hole. Exempting '/ingest' "
             "unmeters the expensive POST that submits documents - the thing the limiter exists "
             "for. Mutated apart from the entry above because a single guard passing both ways "
             "would prove only that SOMETHING is exempt, not that the right thing is."),
    # ---- #904: the canvas's own probes metered as if they were asks ------------------------
    dict(id="904-probe-metered-as-ask", card="#904",
         path="src/dbsearch/server/rate_limit.py",
         guard="tests/selftest_rate_limit.py",
         old='    "/router/probe",',
         new='    # "/router/probe",',
         expect="caught",
         why="THE PROD DEFECT, and the same shape as #885 above on a different route - which is "
             "why it is filed next to it. /router/probe opens a connection and reads a schema: no "
             "retrieval, no embedding, no LLM. It matched the '/router/' costly prefix and shared "
             "a 30-per-minute budget with /router/ask. The canvas probes ONCE PER STORE, so a "
             "15-source fleet spends half the application's allowance rendering itself, and the "
             "refusals were painted as red 'not connected' nodes over healthy stores. Measured on "
             "prod 260821: five stores that had probed available=true minutes earlier all "
             "returned 'rate limit exceeded - this is a public demo' in one sweep. Worse than "
             "#885 because the cost scales with how many sources a customer has."),
    dict(id="904-signed-in-shares-the-anonymous-bucket", card="#904",
         path="src/dbsearch/server/rate_limit.py",
         guard="tests/selftest_rate_limit.py",
         old='            retry = self.limiter.check(f"u:{oid}", limit=self.limiter.per_user)',
         new='            retry = self.limiter.check(f"ip:{client_ip(Request(scope))}")',
         expect="caught",
         why="The routing half. Every caller used to share ONE per-IP bucket sized for an "
             "anonymous demo visitor, so a signed-in owner walking his own canvas was throttled "
             "by an abuse rule and told 'this is a public demo' while it happened. NOTE the "
             "guard that catches this had to be REWRITTEN to drive the middleware: the first "
             "version called FixedWindowLimiter directly and passed against this very mutant, "
             "because it could not reach the code the fix changed."),
    dict(id="904-unverified-cookie-buys-the-bigger-budget", card="#904",
         path="src/dbsearch/server/rate_limit.py",
         guard="tests/selftest_rate_limit.py",
         old='                sess = user_auth.read_session(value)',
         new='                sess = {"oid": (value or "x")[:8]}',
         expect="caught",
         why="The security direction. The larger budget is granted on a VERIFIED session; if the "
             "HMAC check were skipped, anyone could mint a cookie and buy the bigger budget, "
             "turning an availability fix into an abuse hole. Mutated apart from the entry above "
             "because a guard that passed both ways would prove only that SOME identity was "
             "read, not that it was verified."),
    dict(id="904-global-cap-stops-binding", card="#904",
         path="src/dbsearch/server/rate_limit.py",
         guard="tests/selftest_rate_limit.py",
         old='            if self._global_count >= self.global_limit:',
         new='            if False and self._global_count >= self.global_limit:',
         expect="caught",
         why="The spend-hole control. This file's own docstring says the global cap 'must never "
             "be removed as redundant' - it is the actual bill bound and holds even when the "
             "per-IP key is forged. Being signed in must buy a BIGGER budget, never an unmetered "
             "one, so the global cap has to keep binding an authenticated caller too."),
    dict(id="885-429-read-as-a-lost-job", card="#885",
         path="src/dbsearch/server/static/js/surfaces/canvas.js",
         guard="tests/e2e_880_ingest_modal.py",
         old='        if(/\\b429\\b|too many/i.test(msg)){ run.backoff=Math.min((run.backoff||0)+1,10); renderSpRun(); return; }\n',
         new='',
         expect="caught",
         why="The client half. A 429 is the server saying SLOW DOWN - a statement about the "
             "poller, saying nothing at all about the job - and counting it as a miss is how the "
             "surface came to announce a successful crawl as a failure. The guard MEASURES the "
             "backoff (requests made across 12s of 429s) rather than only checking that no "
             "failure is shown: a first version did the latter, and this mutation SURVIVED it, "
             "because an un-backed-off poller simply had not yet reached its miss threshold."),
    # ---- #881: who may read the directory, and what a principal is CALLED ------------------
    dict(id="881-identities-ungated", card="#881",
         path="src/dbsearch/server/app.py",
         guard="tests/selftest_549_admin_metadata_gate.py",
         old='@app.get("/admin/identities", dependencies=[Depends(_require_operator)])',
         new='@app.get("/admin/identities")',
         expect="caught",
         why="THE #881 DEFECT. The route was exempted from #549's operator gate because the "
             "upload form's 'visible to groups' selector read it and refused to submit empty - "
             "but #539 DELETED that selector, and the exemption outlived the journey it "
             "protected. #872 then registered directory ROLES as principals, so the response "
             "stopped describing group membership and started naming who holds Global "
             "Administrator, with member_count saying how few of them there are. Measured on "
             "prod 2026-08-20: 6 groups and 3 users to any signed-in caller."),
    dict(id="881-graph-type-discarded", card="#881",
         path="src/dbsearch/server/user_auth.py",
         guard="tests/selftest_group_resolution.py",
         old='            out[o["id"]] = {"name": label, "kind": _kind_of(o.get("@odata.type"))}',
         new='            out[o["id"]] = {"name": label, "kind": "group"}',
         expect="caught",
         why="Link 1 of the kind chain, and the original sin: getByIds' response is the ONLY "
             "place in the product where a principal's type is ever visible (getMemberObjects "
             "returns bare oids, and a role GUID is shaped like a group's). It was parsed and "
             "dropped one line below where it arrived, which is why the ACL picker offered the "
             "tenant's Global Administrator role as a 'group'."),
    dict(id="881-kind-not-persisted", card="#881",
         path="src/dbsearch/adapters/local/__init__.py",
         guard="tests/selftest_group_resolution.py",
         old="        self._principal_kinds[oid] = kind",
         new="        pass",
         expect="caught",
         why="Link 2, mutated separately from links 1 and 3 because all three look correct in "
             "isolation and the defect lived in the JOIN between them - one mutation per link, "
             "or a fixture rescued by two of them at once proves none."),
    dict(id="881-directory-hardcodes-group", card="#881",
         path="src/dbsearch/adapters/local/__init__.py",
         guard="tests/selftest_group_resolution.py",
         old='                                              kind=self._principal_kinds.get(g, "group")))',
         new='                                              kind="group"))',
         expect="caught",
         why="Link 3, the literal as it stood before #881: every non-user principal flatly "
             "declared a group at the moment an operator picks who a store is shared with. "
             "Note the fallback is deliberately still 'group' - an untyped principal must keep "
             "its old label rather than vanish from the picker, which is the outcome #872 spent "
             "a whole card preventing. A separate guard holds that direction."),
    # ---- #689 / #851: Ask routes, and what a share does with a routed turn -----------------
    dict(id="859-rail-renders-everything-retrieved", card="#859",
         path="src/dbsearch/server/static/js/surfaces/ask.js",
         guard="tests/selftest_859_referenced_rail.py",
         old="  const fns = ref.length ? all.filter((f) => ref.includes(f.n)) : all;",
         new="  const fns = all;",
         expect="caught",
         why="THE #859 DEFECT as prod showed it: a revenue question rendering Sources (8) "
              "over an answer that references [1][3][5], five of them HR documents #856 now "
              "retrieves on every routed turn. A Sources list is a provenance claim."),
    dict(id="859-trim-renumbers-the-survivors", card="#859",
         path="src/dbsearch/server/static/js/surfaces/ask.js",
         guard="tests/selftest_859_referenced_rail.py",
         old="  const fns = ref.length ? all.filter((f) => ref.includes(f.n)) : all;",
         new=("  const fns = (ref.length ? all.filter((f) => ref.includes(f.n)) : all)\n"
              "    .map((f, i) => ({ ...f, n: i + 1 }));"),
         expect="caught",
         why="The tidy-looking version of the same fix: close the gaps so the rail reads 1, 2 "
              "instead of 1, 3. It points [3] at the row [1] describes - #855's lie one "
              "surface out - and the guard checks the SNIPPET behind each surviving number, "
              "not merely how many rows there are."),
    dict(id="859-empty-referenced-empties-the-rail", card="#859",
         path="src/dbsearch/server/static/js/surfaces/ask.js",
         guard="tests/selftest_859_referenced_rail.py",
         old="  const fns = ref.length ? all.filter((f) => ref.includes(f.n)) : all;",
         new="  const fns = all.filter((f) => ref.includes(f.n));",
         expect="caught",
         why="The over-reach: an answer that cites nothing - a cautious model, an extractive "
              "fallback - loses its whole rail and the reader has nothing to check. #724 kept "
              "its honest line for this reason rather than deleting the block."),
    dict(id="859-routed-response-omits-referenced", card="#859",
         path="src/dbsearch/server/app.py",
         guard="tests/selftest_859_referenced_rail.py",
         old='                "referenced": final.get("referenced", []),',
         new="",
         expect="caught",
         why="The server half. Without the key the client cannot tell 'cited nothing' from "
              "'not told' - Array.isArray fails and it falls back to rendering everything, "
              "which is the defect wearing the fix's clothes."),
    dict(id="856-documents-only-a-candidate", card="#856",
         path="src/dbsearch/router/router_service.py",
         guard="tests/selftest_856_documents_always_consulted.py",
         old="        decision = self._also_consult(user_oid, question, decision, report, top_k, timeout_s)",
         new="        pass    # the caller's documents compete for the route like any database",
         expect="caught",
         why="THE #856 DEFECT, restored: with the documents node ranked instead of asked, a "
              "composed folder holding an abbreviated copy of the leave policy took the whole "
              "ask (0.1333 vs 0.0556 on prod) and the caller's own fuller upload contributed "
              "nothing. The guard's PRECONDITION asserts the folder really does outrank the "
              "documents node, so a green cannot mean 'documents happened to win'."),
    dict(id="856-always-consulted-ignores-visibility", card="#856",
         path="src/dbsearch/server/ask_router.py",
         guard="tests/selftest_689_ask_routes.py",
         old="""        if self._node is not None and self._owner in set(principals):
            return [self._node]
        return []""",
         new="""        return [self._node] if self._node is not None else []""",
         expect="caught",
         why="Gate #1 through the new door. `always_consulted` is a SECOND place the documents "
              "node can enter a routed ask, and a visibility check only on `visible_stores` "
              "would let one caller's documents be consulted inside another caller's turn - "
              "the same leak #689's overlay tests pin on the first door."),
    dict(id="856-manual-pin-widened", card="#856",
         path="src/dbsearch/router/router_service.py",
         guard="tests/selftest_856_documents_always_consulted.py",
         old='        if always is None or decision.method == "manual":',
         new="        if always is None:",
         expect="caught",
         why="E7 exists so a user can say 'answer from THIS store'. Consulting a plane they "
              "did not pin overrules an explicit choice, and it is the one case where always "
              "must not mean always."),
    dict(id="855-proof-snippet-joined-onto-every-citation", card="#855",
         path="src/dbsearch/server/router_api.py",
         guard="tests/selftest_855_reopened_markers.py",
         old='        cite["snippet"] = rows[i] if i < len(rows) else _SNIPPET_JOIN.join(rows)',
         new='        cite["snippet"] = _SNIPPET_JOIN.join(rows)',
         expect="caught",
         why="The FIRST half of #855, restored exactly as it shipped. Joining every result row "
              "onto every citation of one (store, sql) makes those citations byte-identical, "
              "which is what let the persisted list dedupe them away. One mutation per CLAUSE: "
              "this one must be caught by the snippet assertions while the length assertion "
              "stays green, or the two halves are one guard in two costumes."),
    dict(id="855-persisted-citations-deduped", card="#855",
         path="src/dbsearch/query/conversation.py",
         guard="tests/selftest_855_reopened_markers.py",
         old="""    def _keep(row: dict) -> None:
        out.append(row)""",
         new="""    _seen: set = set()

    def _keep(row: dict) -> None:
        import json
        _k = json.dumps(row, sort_keys=True, default=str)
        if _k in _seen:
            return
        _seen.add(_k)
        out.append(row)""",
         expect="caught",
         why="The SECOND half. The answer's [n] markers index this list positionally, so a "
              "dedupe renumbers every marker after the first collapsed row - which is how a "
              "reopened prod turn came to say 'AMER 195,000.00[2]' with no [2] on screen. "
              "Caught by the length assertion alone; the snippet assertions stay green."),
    dict(id="855-unclassifiable-citation-dropped", card="#855",
         path="src/dbsearch/query/conversation.py",
         guard="tests/selftest_855_reopened_markers.py",
         old="""        if not c.get("store_id"):
            _keep({})            # neither shape: hold the slot, say nothing
            continue""",
         new="""        if not c.get("store_id"):
            continue""",
         expect="caught",
         why="The same defect by the other door: a row that is neither a document nor a proof "
              "still OCCUPIES a marker, and dropping it shifts every later one left. Separate "
              "entry from the dedupe because it is a separate line and the dedupe mutation "
              "leaves it green."),
    dict(id="689-gate1-through-the-overlay", card="#689",
         path="src/dbsearch/server/ask_router.py",
         guard="tests/selftest_689_ask_routes.py",
         old="        base = self._base.visible_stores(principals)",
         new="        base = self._base.stores()",
         expect="caught",
         why="Gate #1 bypassed by the ONE structure #689 puts in front of a live catalog. The "
              "overlay wraps a workspace catalog per request, so a visibility check it skipped "
              "would leak another user's store into a routed answer - and the guard's fixture "
              "composes ONE shared workspace holding two stores under different ACLs, because "
              "with per-user workspaces bob's catalog never contains alice's store and the "
              "same assertion passes whether or not the overlay honours visibility."),

    dict(id="689-documents-read-as-the-owner", card="#689",
         path="src/dbsearch/server/ask_router.py",
         guard="tests/selftest_689_ask_routes.py",
         old="        hits = self._qs.retrieve(user_oid, question, **kwargs)",
         new='        hits = self._qs.retrieve("alice", question, **kwargs)',
         expect="caught",
         why="LAW 2 on the DOCUMENT plane of a routed ask. The edition index is shared across "
              "accounts, so the trim is the only thing between two people's uploads and the "
              "overlay must run it as the CALLER."),

    dict(id="689-overlay-drops-the-partition", card="#689",
         path="src/dbsearch/server/ask_router.py",
         guard="tests/selftest_689_ask_routes.py",
         old="                         qs, edition.identity, tenant_id=scope)",
         new="                         qs, edition.identity, tenant_id=None)",
         expect="caught",
         why="#439's defect on the new seam: an ask must read the CALLER's ADR 0012 partition, "
              "not the deployment constant, or a foreign owner's documents answer from the "
              "home tenant."),

    dict(id="689-empty-store-still-composed", card="#689",
         path="src/dbsearch/server/ask_router.py",
         guard="tests/selftest_689_ask_routes.py",
         old="    if not qs.has_visible_content(user_oid, tenant_id=scope):\n        return None\n",
         new="",
         expect="caught",
         why="A documents store that exists and answers nothing can be ROUTED TO instead of a "
              "database that would have answered - #808's defect, invented fresh on this seam."),

    dict(id="689-streamed-draft-is-the-record", card="#689",
         path="src/dbsearch/router/synthesizer.py",
         guard="tests/selftest_689_ask_routes.py",
         old='        answer = strip_instruction_markers(generated["answer"])',
         new='        answer = generated["answer"]',
         expect="caught",
         why="The streamed tokens are a DRAFT: the marker sweep, the echo strip, the #493 "
              "condensed pass and the #474 rescue all rewrite after the last token. A client "
              "that renders its accumulator shows text the product already rejected (#257)."),

    dict(id="689-ask-renders-the-accumulator", card="#689",
         path="src/dbsearch/server/static/js/surfaces/ask.js",
         guard="tests/selftest_689_ask_proofs_dom.py",
         old="            { answer: done.answer || acc, citations: done.citations,",
         new="            { answer: acc, citations: done.citations,",
         expect="caught",
         why="The same rule at the surface. #257 was tidy before #689 and is load-bearing "
              "now that two post-passes can replace the answer wholesale."),

    dict(id="689-two-provenance-surfaces", card="#689",
         path="src/dbsearch/server/static/js/surfaces/ask.js",
         guard="tests/selftest_689_ask_proofs_dom.py",
         old="    if (!feedback) return;\n    return appendFeedback(block);\n  }",
         new="  }",
         expect="caught",
         why="The router's footnotes already cover BOTH planes, so letting the citation pill "
              "render beside the rail puts two provenance surfaces with two numbering schemes "
              "on one answer - #755's defect re-created on a new surface."),

    dict(id="689-flag-ignored", card="#689", path="src/dbsearch/server/app.py",
         guard="tests/selftest_689_chat_delegation.py",
         old="    if not ask_routes_enabled():\n        return None\n",
         new="",
         expect="caught",
         why="The feature ships dark. A flag that does not gate is a flag that cannot be "
              "turned off on prod without a rebuild."),

    dict(id="689-shared-thread-delegates", card="#689", path="src/dbsearch/server/app.py",
         guard="tests/selftest_689_chat_delegation.py",
         old="    if _edition.conversation_shares.live_share_for(conv_id, user) is not None:\n"
             "        return None\n",
         new="",
         expect="caught",
         why="A share widens the reader's DOCUMENT scope for one conversation (ADR 0020) and "
              "every rule around it is defined over documents. A recipient asking inside a "
              "shared thread must stay on the plane the share was built for."),

    dict(id="689-content-titles-raw-scope", card="#849",
         path="src/dbsearch/query/service.py",
         guard="tests/selftest_689_chat_delegation.py",
         old="        scope = as_read_scope(tenant_id, self._tenant_id)\n"
             "        return fn(scope.partition)[:limit] if callable(fn) else []",
         new="        return fn(tenant_id or self._tenant_id)[:limit] if callable(fn) else []",
         expect="caught",
         why="#849, the pre-fix line. `distinct_titles` compares its argument to "
              "`chunk.tenant_id` as a STRING, so a ReadScope matched nothing and a document "
              "store lost its whole content routing signal - silently, with no error and no "
              "log, reading from outside as the router declining a question it can answer."),

    dict(id="851-consent-ignored-on-read", card="#851", path="src/dbsearch/server/app.py",
         guard="tests/selftest_689_chat_delegation.py",
         old="        if _turn_blocks_share(t, share.shared_stores):\n            break\n",
         new="",
         expect="caught",
         why="The read-side half of the consent rule. It earns its keep on a share ROW THAT "
              "ALREADY EXISTS - one minted before #851, carrying no recorded consent - which "
              "no amount of correctness at create time can reach. The guard fabricates exactly "
              "that row, because a freshly-minted share cannot falsify any single one of the "
              "three enforcement sites."),

    dict(id="851-store-consent-never-stored", card="#851",
         path="src/dbsearch/query/conversation.py",
         guard="tests/selftest_689_ask_routes.py",
         old='_PROOF_KEYS = ("kind", "store_id", "sql", "origin", "snippet")',
         new='_PROOF_KEYS = ("kind", "store_id", "sql", "origin", "snippet", "rerun_token")',
         expect="caught",
         why="A rerun token binds (store, sql, USER). Persisted, it is either a credential "
              "minted for somebody else or one outliving the identity it was bound to - and "
              "the transcript re-signs per reader precisely so it never has to be stored."),

    dict(id="851-unticked-source-still-shared", card="#851",
         path="src/dbsearch/server/static/js/surfaces/ask.js",
         guard="tests/selftest_606_share_modal_ui.py",
         old="        { audience, email, expiresInDays, excludeDocs, excludeStores });",
         new="        { audience, email, expiresInDays, excludeDocs });",
         expect="caught",
         why="The share silently WIDENS: the owner unticks a warehouse, the modal shows it "
              "unticked, and the request hands it over anyway. The narrowing direction is the "
              "only one this modal is allowed to fail in."),

    dict(id="689-proofs-snippet-unescaped", card="#689",
         path="src/dbsearch/server/static/js/ui/proofs.js",
         guard="tests/selftest_715_729_answer_surface.py",
         old='  const out=esc(String(s||""));',
         new='  const out=String(s||"");',
         expect="caught",
         why="These strings carry MODEL OUTPUT and DATABASE VALUES into an authenticated page. "
              "The guard this replaces asserted the literal text `esc(` appeared in the "
              "function body, which a rewrite calling it on the wrong half would sail through; "
              "it now runs the real function over hostile input on both paths."),

    dict(id="790-partition-fail-open", card="#790", path="src/dbsearch/ports/base.py",
         guard="tests/selftest_790_two_transports_agree.py",
         old="return ReadScope(partition=default_partition if value is None else value)",
         new="return ReadScope(partition=value or default_partition)",
         expect="caught",
         why="The EXACT pre-fix statement, taken from `git show <ref>:<path>` rather than from "
              "memory of its shape. `\"\"` is the fail-closed partition and is falsy, so it "
              "became the deployment constant: REST=[] but GraphQL=['hr-policy'] for one "
              "identity, one key, one question."),

    dict(id="799-disclosure-duplicate-store", card="#799",
         path="src/dbsearch/router/synthesizer.py",
         guard="tests/selftest_799_disclosure_names_a_store_once.py",
         old='        named = ", ".join(_once(qualify(o.store_id, o.business_unit) for o in declined))',
         new='        named = ", ".join(qualify(o.store_id, o.business_unit) for o in declined)',
         expect="caught",
         why="The line prod actually rendered: 'not used: bigquery-1, bigquery-1.' A compound "
              "ask produces one outcome per store PER SUB-QUESTION, so a store that declined "
              "both halves was named twice. Found in a BROWSER, not by any test."),

    dict(id="799-dedup-by-store-id-instead", card="#799",
         path="src/dbsearch/router/synthesizer.py",
         guard="tests/selftest_799_disclosure_names_a_store_once.py",
         old="    seen, out = set(), []\n    for f in fragments:\n        if f not in seen:",
         new="    seen, out = set(), []\n    for f in fragments:\n        if f.split(' ')[0] not in seen:",
         expect="caught",
         why="The OBVIOUS fix, which is wrong: deduping by store id silently drops a second row "
              "count or a second cross-source note, trading a cosmetic duplicate for real "
              "information loss. The control test exists precisely to refuse this."),

    dict(id="731-delete-no-server-call", card="#731", path=CANVAS,
         guard="tests/selftest_731_canvas_delete_dom.py",
         old='    fetch("/router/stores/"+encodeURIComponent(node.id),\n'
             '          {method:"DELETE", headers:idHeaders(), keepalive:true})\n'
             '      .then(r=>{\n'
             '        if(!r.ok) throw new Error("HTTP "+r.status);\n'
             '        if(alive) undoToast(node);\n'
             '      })\n'
             '      .catch(e=>{\n'
             '        if(!alive) return;                 // navigated away; the row is the truth either way\n'
             '        state.push(node); renderAll();     // never show a deletion the server refused\n'
             '        toast("Could not remove "+node.id+" - "+(e.message||e));\n'
             '      });\n',
         new="",
         expect="caught",
         why="THE #731 defect: delete was client-only, so the stored manifest resurrected "
              "every node on the next page load and boot's composeUp re-committed them."),

    dict(id="731-hydrate-empty-as-absent", card="#731", path=CANVAS,
         guard="tests/selftest_731_canvas_delete_dom.py",
         old="if(m&&Array.isArray(m.stores)){",
         new="if(m&&m.stores&&m.stores.length){",
         expect="caught",
         why="The latent half: `stores: []` read as NO manifest, falling back to a stale "
              "localStorage copy - so delete-ALL could never persist. The fixture poisons "
              "localStorage with a stale non-empty save at remount, because an honest "
              "empty save let restoreCanvas's own fix rescue this gate (the "
              "rescued-by-both-halves shape, caught by the matrix in this session)."),

    dict(id="731-restore-empty-as-absent", card="#731", path=CANVAS,
         guard="tests/selftest_731_canvas_delete_dom.py",
         old="if(!saved || !Array.isArray(saved.nodes)) return false;",
         new="if(!saved || !Array.isArray(saved.nodes) || !saved.nodes.length) return false;",
         expect="caught",
         why="The sibling gate, discriminated on the ONLY surface where it is load-bearing "
              "- the dev-rig loadLiveDemo fallback, where a saved-but-empty canvas used to "
              "read as no-save and resurrect the demo manifest."),

    dict(id="731-refused-delete-stays-deleted", card="#731", path=CANVAS,
         guard="tests/selftest_731_canvas_delete_dom.py",
         old='      .catch(e=>{\n'
             '        if(!alive) return;                 // navigated away; the row is the truth either way\n'
             '        state.push(node); renderAll();     // never show a deletion the server refused\n'
             '        toast("Could not remove "+node.id+" - "+(e.message||e));\n'
             '      });',
         new='      .catch(()=>{});',
         expect="caught",
         why="Never show a deletion the server refused: a swallowed failure leaves the "
              "canvas lying about the workspace until the next reload resurrects the node "
              "anyway - the original defect wearing a success face."),

    dict(id="731-endpoint-skips-stored-row", card="#731",
         path="src/dbsearch/server/router_api.py",
         guard="tests/selftest_731_store_delete.py",
         old='                    if removed_entry is not None:\n'
             '                        manifest_store.put(key, dict(\n'
             '                            m, stores=[s for s in stores if s.get("id") != store_id]))\n'
             '                        removed = True',
         new='                    if removed_entry is not None:\n'
             '                        removed = True',
         expect="caught",
         why="The durable half: without the row edit, the delete works until the next "
              "rebuild - which is exactly the original resurrect, one layer down."),

    dict(id="731-endpoint-skips-live-catalog", card="#731",
         path="src/dbsearch/server/router_api.py",
         guard="tests/selftest_731_store_delete.py",
         old="            if st.catalog.remove(store_id):",
         new="            if False:",
         expect="caught",
         why="The live half: the row is edited but the warm catalog still serves the "
              "store until a recompose - deleted in the record, alive on the wire."),

    dict(id="731-endpoint-rebuilds-on-cold", card="#731",
         path="src/dbsearch/server/router_api.py",
         guard="tests/selftest_731_store_delete.py",
         old="        st = _pool.get_if_warm(key)",
         new="        st = _pool.get(key)",
         expect="caught",
         why="The cheapness clause: get() rebuilds a cold workspace from the stored row, "
              "re-firing connector crawls (40MB/>1h for the #536 pack) to serve an "
              "operation whose whole contract is being cheap."),

    dict(id="731-service-not-invalidated", card="#731",
         path="src/dbsearch/server/router_api.py",
         guard="tests/selftest_731_store_delete.py",
         old='                                if s.get("id") != store_id])\n'
             '                st.service = None',
         new='                                if s.get("id") != store_id])',
         expect="caught",
         why="The cached RouterQueryService still routes to the removed store; the route "
              "advisor keeps offering it until some other compose resets the service."),

    dict(id="719-hint-never-set", card="#719",
         path="src/dbsearch/router/providers/redshift.py",
         guard="tests/selftest_719_redshift_cold_start.py",
         old="            if (not wake and was_cold and not self._cold_hint\n"
             "                    and time.monotonic() - started >= self._cold_hint_after):\n"
             "                self._cold_hint = (\"this source's serverless warehouse was likely waking \"\n"
             "                                   \"from idle - ask again in about a minute\")\n",
         new="",
         expect="caught",
         why="The pre-fix behavior: a paused warehouse's first ask timed out with nothing "
              "attached - 'looks broken, is just cold'. MEASURED: cold 11.8s vs warm 2.2s "
              "against the executor's 8s budget."),

    dict(id="719-hint-ignores-idle", card="#719",
         path="src/dbsearch/router/providers/redshift.py",
         guard="tests/selftest_719_redshift_cold_start.py",
         old="        was_cold = (self._last_finished is None\n"
             "                    or started - self._last_finished > self._cold_idle_s)",
         new="        was_cold = True",
         expect="caught",
         why="The discriminator ALONE (one mutation per clause): without idleness, a "
              "merely-slow WARM query gets stamped 'waking' - the #727 mislabel pointed "
              "the other way."),

    dict(id="719-hint-on-wake-path", card="#719",
         path="src/dbsearch/router/providers/redshift.py",
         guard="tests/selftest_719_redshift_cold_start.py",
         old="if (not wake and was_cold and not self._cold_hint",
         new="if (was_cold and not self._cold_hint",
         expect="caught",
         why="The wake/ask separation: introspection and health ride the 180s poll and "
              "must never set an ask-facing hint."),

    dict(id="719-hint-not-cleared-on-finish", card="#719",
         path="src/dbsearch/router/providers/redshift.py",
         guard="tests/selftest_719_redshift_cold_start.py",
         old="        self._cold_hint = \"\"\n"
             "        self._last_finished = time.monotonic()",
         new="        self._last_finished = time.monotonic()",
         expect="caught",
         why="A statement that FINISHED explains no timeout; a stale hint would label some "
              "later, unrelated failure as a cold start."),

    dict(id="719-timeout-remedy-dropped", card="#719",
         path="src/dbsearch/router/executor.py",
         guard="tests/selftest_719_redshift_cold_start.py",
         old="                report.outcomes.append(StoreOutcome(routed.store_id, routed.business_unit,\n"
             "                                                    TIMEOUT, remedy=hint))",
         new="                report.outcomes.append(StoreOutcome(routed.store_id, routed.business_unit,\n"
             "                                                    TIMEOUT))",
         expect="caught",
         why="The executor half: the engine's hint must actually reach the outcome the "
              "disclosure renders, or the engine knows and the user still does not."),

    dict(id="727-empty-schema-declines", card="#727",
         path="src/dbsearch/router/structured.py",
         guard="tests/selftest_727_empty_schema_honest.py",
         old='            raise SchemaUnavailable(\n'
             '                "this source\'s schema could not be read - introspection returned 0 tables. "\n'
             '                "Check the delegated credential\'s privileges on the source, and that any "\n'
             '                "`tables:` allowlist entries are schema-qualified (a bare name only matches "\n'
             '                "the default schema).")',
         new='            raise CannotAnswerFromSchema(\n'
             '                "no table in this source matches the question")',
         expect="caught",
         why="THE #727 defect: an empty schema falling into the same decline an honest "
              "retrieval miss produces, so the disclosure told the owner his freight store "
              "holds no freight. This restores exactly that collapse."),

    dict(id="727-no-refresh-before-raise", card="#727",
         path="src/dbsearch/router/structured.py",
         guard="tests/selftest_727_empty_schema_honest.py",
         old="        if not schema:\n"
             "            self._engine.refresh_schema()\n"
             "            self._schema_index = None\n"
             "            schema = self.described_schema() if described else self._engine.schema()\n",
         new="",
         expect="caught",
         why="The retry clause ALONE (one mutation per clause): without the single "
              "refresh_schema() retry, a transient empty - an expired STS session since "
              "repaired, a GRANT fixed after compose - errors instead of recovering."),

    dict(id="727-executor-remedy-dropped", card="#727",
         path="src/dbsearch/router/executor.py",
         guard="tests/selftest_727_empty_schema_honest.py",
         old='                                                    error=f"SchemaUnavailable: {exc}",\n'
             '                                                    remedy=str(exc)))',
         new='                                                    error=f"SchemaUnavailable: {exc}",\n'
             '                                                    remedy=""))',
         expect="caught",
         why="The remedy IS the user's instructions (check privileges / schema-qualify the "
              "allowlist); dropping it leaves an ERROR the user can do nothing about."),

    dict(id="727-disclosure-says-not-connected", card="#727",
         path="src/dbsearch/router/synthesizer.py",
         guard="tests/selftest_727_empty_schema_honest.py",
         old='"not connected" if o.unlinked else o.status',
         new='"not connected" if o.remedy else o.status',
         expect="caught",
         why="The pre-fix qualifier: ANY remedied drop rendered as 'not connected' - the "
              "#680 unlinked-cloud phrasing - sending a schema-fault user to re-link a "
              "cloud that was already linked."),

    dict(id="811-raw-40613-resurfaces", card="#811",
         path="src/dbsearch/router/providers/azure_sql.py",
         guard="tests/selftest_811_wake_retry_bounds.py",
         # CLAUSE 1. Restores the pre-fix exhaustion: re-raise the driver error unchanged.
         # The shared deadline and the config keys stay, so only the message assertions go
         # red - the other two clauses cannot rescue this one.
         old="                if time.monotonic() >= deadline:\n"
             "                    raise AzureDatabaseUnavailable(",
         new="                if time.monotonic() >= deadline:\n"
             "                    raise exc\n"
             "                if False:\n"
             "                    raise AzureDatabaseUnavailable(",
         expect="caught",
         why="The #780 prod finding: Test connection on a NONEXISTENT database burned the "
              "full 120s and then showed the driver's raw (40613, b'...') tuple, telling a "
              "user who mistyped a name to wait for a resume that is never coming."),

    dict(id="811-reconnect-restarts-the-budget", card="#811",
         path="src/dbsearch/router/providers/azure_sql.py",
         guard="tests/selftest_811_wake_retry_bounds.py",
         # CLAUSE 3. The reconnect computes a FRESH budget again. The honest message and the
         # config keys are untouched, so only the elapsed-time assertion can catch it.
         old="            self._conn = self._open(deadline=deadline)\n"
             "            cur = self._conn.cursor()\n"
             "            cur.execute(sql)\n"
             "        return cur",
         new="            self._conn = self._open()\n"
             "            cur = self._conn.cursor()\n"
             "            cur.execute(sql)\n"
             "        return cur",
         expect="caught",
         why="Both _open sites computed their own deadline, so one statement that opened a "
              "connection and then lost it could spend the resume_timeout TWICE - 240s of a "
              "caller's time, long after the ask path's own 8s budget had expired."),

    dict(id="811-budget-not-configurable", card="#811",
         path="src/dbsearch/router/providers/azure_sql.py",
         guard="tests/selftest_811_wake_retry_bounds.py",
         # CLAUSE 2. from_config stops threading the keys. Exhaustion is still honest and the
         # deadline is still shared, so only the configurability assertions go red.
         old="        return cls(connect, tables=config.get(\"tables\"), user_connect=user_connect, **kw)",
         new="        return cls(connect, tables=config.get(\"tables\"), user_connect=user_connect)",
         expect="caught",
         why="The wake budget was constructor-only, so nothing in the product could set it: "
              "every deployment got 120s at 5s steps whatever its database actually was."),

    dict(id="807-azure-sql-caches-empty", card="#807",
         path="src/dbsearch/router/providers/azure_sql.py",
         guard="tests/selftest_727_empty_schema_honest.py",
         old="        if out:\n"
             "            self._schema_cache = out\n"
             "        return out",
         new="        self._schema_cache = out\n"
             "        return out",
         expect="caught",
         why="azure_sql cached an empty introspection for the engine's lifetime - the same "
              "defect #727 fixed on redshift, still live here (and on SYNAPSE, which reuses "
              "this engine verbatim) until #807."),

    dict(id="807-postgres-caches-empty", card="#807",
         path="src/dbsearch/router/providers/postgres.py",
         guard="tests/selftest_727_empty_schema_honest.py",
         old="        if out:\n"
             "            self._schema_cache = out\n"
             "        return out",
         new="        self._schema_cache = out\n"
             "        return out",
         expect="caught",
         why="postgres cached an empty introspection, so a fixed GRANT needed a recompose to "
              "be seen and _read_schema's one refresh retry re-read the cached []."),

    dict(id="807-mysql-caches-empty", card="#807",
         path="src/dbsearch/router/providers/mysql.py",
         guard="tests/selftest_727_empty_schema_honest.py",
         old="        if out:\n"
             "            self._schema_cache = out\n"
             "        return out",
         new="        self._schema_cache = out\n"
             "        return out",
         expect="caught",
         why="mysql cached an empty introspection - same contract, third home. #727 fixed "
              "only the two engines its prod incident happened to involve."),

    dict(id="808-no-allowlist-warning", card="#808",
         path="src/dbsearch/router/structured.py",
         guard="tests/selftest_808_allowlist_warning.py",
         # CLAUSE 1. The store computes no warning at all - the pre-fix silence. The wire
         # key still exists and the canvas still renders whatever it is given, so only the
         # warning-content assertions can catch this.
         old="    if schema:\n"
             "        return []\n"
             "    if getattr(engine, _ALLOWLIST_ATTR, None):",
         new="    if schema:\n"
             "        return []\n"
             "    if False:",
         expect="caught",
         why="The pre-fix compose: a store whose `tables:` allowlist matched NOTHING turned "
              "green and said nothing, so the owner only found out by asking a question and "
              "reading #727's failure."),

    dict(id="808-warning-dropped-on-the-wire", card="#808",
         path="src/dbsearch/server/router_api.py",
         guard="tests/selftest_808_allowlist_warning.py",
         # CLAUSE 3. The warning is still COMPUTED - clause 1's assertions stay green - it
         # just never leaves the process, which is the same silence with extra steps.
         old='            "business_unit": p.business_unit, "freshness": p.freshness,\n'
             '            "warnings": list(getattr(p, "warnings", None) or [])}',
         new='            "business_unit": p.business_unit, "freshness": p.freshness}',
         expect="caught",
         why="Server-side warnings that never reach the compose response cannot be rendered; "
              "the canvas had no `warnings` key to read."),

    dict(id="808-bigquery-rejects-qualified", card="#808",
         path="src/dbsearch/router/providers/bigquery.py",
         guard="tests/selftest_808_allowlist_warning.py",
         # CLAUSE 4. Restores the bare-only membership test - the inverse trap - while the
         # warning machinery stays intact, so only the bigquery assertions go red.
         old="        t = table.lower()\n"
             "        return (t in self._allow\n"
             "                or f\"{self._dataset}.{t}\".lower() in self._allow\n"
             "                or f\"{self._project}.{self._dataset}.{t}\".lower() in self._allow)",
         new="        return table.lower() in self._allow",
         expect="caught",
         why="The pre-fix bigquery filter was a BARE-name membership test, so an operator who "
              "wrote the honest `analytics.orders` - the shape redshift requires and #727's "
              "remedy recommends - matched nothing and silently emptied the store."),

    dict(id="808-canvas-drops-the-warning", card="#808",
         path="src/dbsearch/server/static/js/surfaces/canvas.js",
         guard="tests/selftest_808_allowlist_warning.py",
         # CLAUSE 5. The wire still carries it and the store still computes it; the CARD just
         # never renders it, which is exactly where #781 found the last one of these.
         old="      ((node.warnings&&node.warnings.length)\n"
             "        ? '<div class=\"nwarn\" title=\"'+esc(node.warnings.join(\" \"))+'\">'+\n"
             "            esc(node.warnings[0])+'</div>'\n"
             "        : '')+",
         new="      ''+",
         expect="caught",
         why="A warning that reaches the client and is dropped by the render layer is the "
              "#781 defect repeated: the answer sat unread on the node while the card showed "
              "nothing."),

    dict(id="842-ingest-unmetered", card="#842",
         path="src/dbsearch/server/edition.py",
         guard="tests/selftest_842_ingest_is_metered.py",
         # CLAUSE 1 of 3. The size stops riding the seed, so the document indexes with
         # doc_bytes defaulting to 0. Both gates still RUN - they just measure an account
         # whose usage never grows - so only the usage assertion can catch this.
         old='        seed = [{"external_id": external_id, "title": title, "uri": uri, "acl": acl,\n'
             '                 "text": text, "doc_bytes": len(text.encode("utf-8"))}]',
         new='        seed = [{"external_id": external_id, "title": title, "uri": uri, "acl": acl,\n'
             '                 "text": text}]',
         expect="caught",
         why="The pre-fix blind spot: doc_bytes had exactly one producer (the upload "
              "connector), so /ingest indexed at the model default 0 and usage_bytes read "
              "~0 however much had been stored - the 402 could never fire."),

    dict(id="842-ingest-skips-quota", card="#842",
         path="src/dbsearch/server/app.py",
         guard="tests/selftest_842_ingest_is_metered.py",
         # CLAUSE 2 of 3. Drops ONLY the quota call; metering and the disk guard stay, so
         # the usage and 507 assertions stay green and only the 402 guard can catch it.
         # RE-ANCHORED by #852. The two calls were adjacent when this was written; #844 put a
         # three-line comment between them and gave the quota call `replaces_doc_id`, so the
         # old two-line anchor stopped existing. Anchoring each call on its OWN line is also
         # what keeps the two #842 clauses independent - a shared anchor is one guard wearing
         # two costumes.
         old="    _enforce_storage_quota(request, user, _incoming, replaces_doc_id=req.external_id)",
         new="    pass  # MUTANT: no quota check on this path",
         expect="caught",
         why="/ingest reached _edition.ingest_document with no quota check at all, so a "
              "free-tier caller could store past their tier indefinitely on a path the "
              "upload endpoint refuses with 402."),

    dict(id="842-ingest-skips-disk-guard", card="#842",
         path="src/dbsearch/server/app.py",
         guard="tests/selftest_842_ingest_is_metered.py",
         # CLAUSE 3 of 3. Drops ONLY the disk guard. The quota still refuses over-quota
         # callers, so only the full-disk scenario - a caller INSIDE their quota - goes red.
         # RE-ANCHORED by #852, on its own call rather than the pair (see clause 2).
         old="    _enforce_disk_headroom(_incoming)\n",
         new="    pass  # MUTANT: no disk-headroom guard on this path\n",
         expect="caught",
         why="The #831 guard protects the volume for EVERY account, and the path that "
              "bypassed it could fill the disk while every individual caller stayed "
              "comfortably inside their own quota."),

    dict(id="844-replacement-double-counted", card="#844",
         path="src/dbsearch/server/app.py",
         guard="tests/selftest_844_replacement_quota.py",
         # CLAUSE 1: the upload path stops declaring what it replaces. The index still
         # SUPPORTS the exclusion and /ingest still passes its key, so only the upload
         # replacement assertion goes red.
         old="    _enforce_storage_quota(request, user, len(data),\n"
             "                           replaces_uri=f\"upload://{file.filename or 'document'}\")",
         new="    _enforce_storage_quota(request, user, len(data))",
         expect="caught",
         why="The pre-fix check counted the version this upload SUPERSEDES plus the incoming "
              "bytes, so a re-upload that nets ~zero was refused 402 'upgrade your plan' - a "
              "wrong denial with a payment demand attached."),

    dict(id="844-exclusion-ignored-by-the-index", card="#844",
         path="src/dbsearch/adapters/local/__init__.py",
         guard="tests/selftest_844_replacement_quota.py",
         # CLAUSE 2: the index accepts the parameter and ignores it - the shape where a fix
         # reads as present at every call site and does nothing at the one place it matters.
         old="            if exclude_uri is not None and getattr(c, \"uri\", None) == exclude_uri:\n"
             "                continue",
         new="            if False:\n"
             "                continue",
         expect="caught",
         why="A signature that takes the exclusion and does not apply it looks correct at "
              "every caller and changes nothing - the double count survives intact."),

    dict(id="838-stale-event-rewinds", card="#838",
         path="src/dbsearch/server/billing.py",
         guard="tests/selftest_838_webhook_ordering.py",
         # CLAUSE 1: drop the stale guard. The subscription guard and the persisted timestamp
         # both stay, so only the retried-older-event assertion goes red.
         old="    if event_at and last_at and event_at < int(last_at):",
         new="    if False:",
         expect="caught",
         why="Stripe does not guarantee delivery order and retries a failed event for days, "
              "so a retried .updated(active) landing after the .deleted that cancelled it "
              "restored paid entitlement PERMANENTLY - no further event follows a deletion."),

    dict(id="838-foreign-cancel-downgrades", card="#838",
         path="src/dbsearch/server/billing.py",
         guard="tests/selftest_838_webhook_ordering.py",
         # CLAUSE 2: drop the subscription-identity guard. Staleness still applies, so the
         # clause-1 assertion stays green and only the resubscribe case can catch this.
         old="        if recorded and subscription_id and recorded != subscription_id:",
         new="        if False:",
         expect="caught",
         why="Cancel-then-resubscribe is an ordinary journey: at the OLD subscription's "
              "period end Stripe emits .deleted for it, which applied blindly dropped a "
              "customer paying on a DIFFERENT subscription to free enforcement."),

    dict(id="838-timestamp-not-persisted", card="#838",
         path="src/dbsearch/server/billing.py",
         guard="tests/selftest_838_webhook_ordering.py",
         # CLAUSE 3: stop persisting the timestamp. Both guards remain in the source, but the
         # stale one silently becomes a no-op because there is never anything to compare to -
         # the shape where a fix reads as present and does nothing.
         old="        last_event_at=event_at or None,",
         new="        last_event_at=None,",
         expect="caught",
         why="Without the recorded timestamp the ordering comparison has no left-hand side, "
              "so the stale guard is dead code that still LOOKS like protection."),

    dict(id="843-crawl-skips-headroom", card="#843",
         path="src/dbsearch/pipeline/runner.py",
         guard="tests/selftest_843_crawl_headroom.py",
         # The pre-#843 crawl: straight to the put, no headroom question asked. The upload
         # endpoint keeps its own guard, so only the crawl assertions go red.
         old="            low = disk_shortfall(store, len(raw))\n"
             "            if low is not None:",
         new="            low = None\n"
             "            if low is not None:",
         expect="caught",
         why="Every connector crawl wrote each fetched file's raw bytes to the blob volume "
              "with no headroom check, and prod measured 211MB of connector blobs against "
              "199KB of uploads - the guarded path was the rounding error and the unguarded "
              "one was the volume."),

    dict(id="845-failed-upload-strands-blobs", card="#845",
         path="src/dbsearch/server/edition.py",
         guard="tests/selftest_845_failed_upload_reclaims.py",
         # CLAUSE 1: the upload path stops reclaiming on failure. The /ingest home and the
         # orphanhood guard both stay, so only the upload assertion goes red.
         # anchored by the `return self._finish_file_ingest(...)` that follows the UPLOAD
         # path's handler (#917 moved the post-success tail there) - the /ingest home's
         # handler is followed by `ts = self._now()` and the async home carries its own
         # comment lines, so this matches exactly one.
         old="        except Exception:\n"
             "            self._reclaim_orphan_blobs(tid, external_id)\n"
             "            raise\n"
             "        return self._finish_file_ingest(tid, external_id, uri, owner_oid, result)",
         new="        except Exception:\n"
             "            raise\n"
             "        return self._finish_file_ingest(tid, external_id, uri, owner_oid, result)",
         expect="caught",
         why="OBSERVED ON PROD: a rejected upload's raw blob stayed on the live disk with no "
              "index row, and both blob-deleting paths enumerate FROM the index - so nothing "
              "in the product could ever reclaim it."),

    dict(id="845-reclaim-ignores-orphanhood", card="#845",
         path="src/dbsearch/server/edition.py",
         guard="tests/selftest_845_failed_upload_reclaims.py",
         # CLAUSE 2, the DANGEROUS direction: reclaim on the exception alone. Both call sites
         # keep reclaiming, so the two leak assertions stay green and ONLY the live-document
         # control can catch it - which is the whole reason that control exists.
         old="        if self._indexed(partition, doc_id):\n"
             "            return\n"
             "        self._reclaim_blobs(partition, doc_id)",
         new="        self._reclaim_blobs(partition, doc_id)",
         expect="caught",
         why="run_ingestion deletes-before-indexing for the id it is writing, so a failure "
              "can land with an EXISTING id half-written. Reclaiming because an exception "
              "happened, rather than because the id is orphaned, would destroy a document "
              "that is still readable - trading a disk leak for data loss."),

    dict(id="841-supersede-before-ingest", card="#841",
         path="src/dbsearch/server/edition.py",
         guard="tests/selftest_841_supersede_after_success.py",
         # CLAUSE 1 of 2. Restores the pre-fix ORDER verbatim (supersede, then ingest) and
         # nothing else - `_reclaim_blobs` still exists and is still called, so only the
         # ordering guard can catch this. Taken from the pre-fix file, not from memory.
         # anchored by the `# strict:` comment that follows ingest_file's connector -
         # #917's submit_file_ingest duplicates the tid/connector pair, so the trailing
         # comment line is what pins this to the SYNC home the guard drives.
         old="        tid = tenant_id or self.tenant_id\n"
             "        connector = UploadConnector(tid, external_id, title, data, mime, acl, uri,\n"
             "                                    owner_oid=owner_oid)\n"
             "        # strict: one interactive file, so an unparseable upload must surface (415/422) rather",
         new="        tid = tenant_id or self.tenant_id\n"
             "        if uri:\n"
             "            for doc in self.index.list_doc_acls(as_read_scope(tid)):\n"
             "                if doc.uri == uri and doc.doc_external_id != external_id \\\n"
             "                        and doc.owner_oid == owner_oid:\n"
             "                    self.index.delete(tid, doc.doc_external_id)\n"
             "                    self._reclaim_blobs(tid, doc.doc_external_id)\n"
             "        connector = UploadConnector(tid, external_id, title, data, mime, acl, uri,\n"
             "                                    owner_oid=owner_oid)\n"
             "        # strict: one interactive file, so an unparseable upload must surface (415/422) rather",
         expect="caught",
         why="The pre-fix order: prior versions were deleted BEFORE run_ingestion, which "
              "runs strict=True, so an edited re-upload that failed to parse raised 415/422 "
              "with the user's previous good version already destroyed - an error and a "
              "data loss for one ordinary action."),

    dict(id="841-supersede-orphans-blobs", card="#841",
         path="src/dbsearch/server/edition.py",
         guard="tests/selftest_841_supersede_after_success.py",
         # CLAUSE 2 of 2. Drops ONLY the blob reclaim; the ordering fix stays, so the
         # failed-re-upload guard stays green and only the orphan guard can catch this.
         old="                    self.index.delete(tid, doc.doc_external_id)\n"
             "                    self._reclaim_blobs(tid, doc.doc_external_id)",
         new="                    self.index.delete(tid, doc.doc_external_id)",
         expect="caught",
         why="Superseding removed only the INDEX rows. DELETE /documents and the retention "
              "sweep both enumerate FROM the index, so a superseded version's raw/segments/"
              "chunk blobs became unreachable forever - every edited re-upload leaked its "
              "predecessor's bytes onto the volume the #831 headroom guard defends."),

    dict(id="727-redshift-caches-empty", card="#727",
         path="src/dbsearch/router/providers/redshift.py",
         guard="tests/selftest_727_empty_schema_honest.py",
         old="        if out:\n"
             "            self._schema_cache = out\n"
             "        return out",
         new="        self._schema_cache = out\n"
             "        return out",
         expect="caught",
         why="The pre-fix cache: an empty introspection cached for the engine's lifetime, "
              "so the ONE-refresh retry (and every later ask) read [] forever and a fixed "
              "GRANT needed a recompose to be seen."),

    dict(id="727-bigquery-caches-empty", card="#727",
         path="src/dbsearch/router/providers/bigquery.py",
         guard="tests/selftest_727_empty_schema_honest.py",
         old="        if out:\n"
             "            self._schema_cache = out\n"
             "        return out",
         new="        self._schema_cache = out\n"
             "        return out",
         expect="caught",
         why="The same rule's SECOND home (the #799 lesson): bigquery has the identical "
              "cache and introspect_as seam, and fixing only redshift would leave the "
              "sibling rail with the exact defect."),

    dict(id="781-tooltip-status-word-only", card="#781", path=CANVAS,
         guard="tests/selftest_781_compose_reason.py",
         old='esc(isUncomposed(node) ? "draft: "+UNCOMPOSED_HINT\n'
             '                                 : node.status+(node.reason?": "+node.reason:""))',
         new='esc(isUncomposed(node) ? "draft" : node.status)',
         expect="caught",
         why="The tooltip loses the REASON and becomes the status WORD again - the #781 defect: "
              "the owner hovered a red node on prod and read 'planned' while the compose "
              "response's reason sat unread on node.reason. RE-ANCHORED 260823 (#941 rewrote "
              "this line to report the catalog rather than the probe); the mutation is the same "
              "loss expressed against the line as it stands now, recovered from the file."),

    dict(id="781-reason-line-removed", card="#781", path=CANVAS,
         guard="tests/selftest_781_compose_reason.py",
         old='      (node.status==="planned"&&node.reason\n'
             '        ? \'<div class="nreason" title="\'+esc(node.reason)+\'">\'+esc(node.reason)+\'</div>\'\n'
             '        : \'\')+',
         new="",
         expect="caught",
         why="Removes the visible reason from the card, leaving only the tooltip and the "
              "status bar. Each of the three surfacing clauses must be load-bearing ALONE - "
              "the 260817 lesson, one mutation per clause."),

    dict(id="781-statusbar-segment-removed", card="#781", path=CANVAS,
         guard="tests/selftest_781_compose_reason.py",
         old="      failedSeg+\n",
         new="",
         expect="caught",
         why="Restores the status bar prod showed: '5 sources · 3 connected', counts only, "
              "naming neither the failing stores nor the cause."),

    dict(id="781-esc-dropped-from-tooltip", card="#781", path=CANVAS,
         guard="tests/selftest_781_compose_reason.py",
         old='esc(isUncomposed(node) ? "draft: "+UNCOMPOSED_HINT\n'
             '                                 : node.status+(node.reason?": "+node.reason:""))',
         new='(isUncomposed(node) ? "draft: "+UNCOMPOSED_HINT\n'
             '                                 : node.status+(node.reason?": "+node.reason:""))',
         expect="caught",
         why="The reason is server text entering an ATTRIBUTE sink; a `\"` breaks out of an "
              "unescaped title and mints event-handler attributes. The hostile fixture "
              "carries `\"` AND `<` so no single sink can rescue it (#786's lesson). "
              "RE-ANCHORED 260823: #941 moved the sink inside a ternary, so the esc() to strip "
              "is the outer one - recovered from the file, not from memory of its shape."),

    dict(id="781-esc-dropped-from-reason-line", card="#781", path=CANVAS,
         guard="tests/selftest_781_compose_reason.py",
         old="'\">'+esc(node.reason)+'</div>'",
         new="'\">'+node.reason+'</div>'",
         expect="caught",
         why="Same string, other sink class: unescaped element CONTENT executes `<img "
              "onerror>`. The probe counts the elements the payload managed to create."),

    dict(id="781-esc-dropped-from-bar-title", card="#781", path=CANVAS,
         guard="tests/selftest_781_compose_reason.py",
         old='esc(failed.map(n=>n.id+": "+n.reason).join("\\n"))',
         new='failed.map(n=>n.id+": "+n.reason).join("\\n")',
         expect="caught",
         why="The third home of the same server string - the status bar's title attribute. "
              "Six homes was #799's count for one rule; this fix has three, and each escapes "
              "independently, so each is mutated independently."),

    dict(id="781-clamp-bottom-padding-window", card="#781", path=CANVAS_CSS,
         guard="tests/selftest_781_compose_reason.py",
         old=".canvas-surface .node .nreason {padding:0 12px 0 15px;margin:0 0 10px;",
         new=".canvas-surface .node .nreason {padding:0 12px 10px 15px;",
         expect="caught",
         why="The EXACT first-shipped declaration (03d3e72): bottom PADDING under a line "
              "clamp. overflow:hidden clips at the padding edge, so the clipped third line's "
              "top pixels painted through the 10px window - a sliver of 'principals who may "
              "query it' visible under the ellipsis on prod. Found by the browser pass, "
              "invisible to jsdom; the guard is static and says so."),

    dict(id="787-media-wrapped-display", card="#787", path=CANVAS_CSS, guard=ANSWER_SURFACE,
         old=".canvas-surface .tracefoot summary .tri {display:inline-block;transition:transform .12s ease}",
         new=".canvas-surface .tracefoot summary .tri {display:inline-block;transition:transform .12s ease}\n@media (min-width:1px){.canvas-surface .tracefoot summary .tri {display:inline}}",
         expect="caught",
         why="#745 round one hidden inside a media query. The old flat _rules scan could not "
              "cross an inner brace, so it skipped the at-rule and handed the guard the INNER "
              "rule as if it were top level; the guard's any() was then satisfied by the "
              "untouched display:inline-block elsewhere. jsdom cannot help - evaluateMediaList "
              "matches only a query that is literally `all` or `screen`."),

    dict(id="787-rotation-by-a-turn", card="#787", path=CANVAS_CSS, guard=ANSWER_SURFACE,
         old="transform:rotate(90deg)",
         new="transform:rotate(1turn)",
         expect="caught",
         why="360 degrees, i.e. no rotation at all. _transform_moves applied its periodicity "
              "rule only when the argument contained the literal string 'deg', so turn/grad/rad "
              "all judged as movement - and because the helper is SHARED, one bug defeated the "
              "static and the computed guard at once. Both now go red."),

    dict(id="782-webkit-marker", card="#782", path=CANVAS_CSS, guard=ANSWER_SURFACE,
         old="summary::-webkit-details-marker {display:none}",
         new="summary::-webkit-details-marker {display:block}",
         expect="caught",
         why="Un-suppresses the native disclosure marker, restoring #745 round one on Safari "
              "and older Chromium: two triangles for one control. The only guard that mentions "
              "the rule tests `\"...::-webkit-details-marker\" in css`, a raw substring, so the "
              "declaration body can say anything."),

    dict(id="784-label-dedup-removed", card="#784", path=CANVAS, guard=ANSWER_SURFACE,
         old="const lbl=seg.filter((s,i)=>s&&seg.indexOf(s)===i).join(\" \u00b7 \");",
         new="const lbl=seg.join(\" \u00b7 \");",
         expect="caught",
         why="#761's rendered symptom, reinstated: a business unit that matches an origin "
              "segment prints twice - 'azure_sql-1 \u00b7 Azure SQL \u00b7 finance \u00b7 finance'. "
              "#761's own guard could no longer catch this because #753's dedup meant the "
              "duplicate was never composed; it needed a fixture whose segments actually collide."),

    dict(id="786-esc-quote", card="#786", path=PROOFS_JS, guard=ANSWER_SURFACE,
         old="""  return String(s).replace(/[&<>"]/g, (c) => ({""",
         new="""  return String(s).replace(/[&<>]/g, (c) => ({""",
         expect="caught",
         why="Drops the double quote from esc()'s character class, which is the entire defence "
              "of eleven attribute sinks - including the footnote `title=` fed straight from "
              "model text. Seven suites stay green because every check merely greps that the "
              "string `esc(` appears inside a function.\n"
              "RE-ANCHORED for #689: `esc` moved from canvas.js to ui/proofs.js when the "
              "Sources rail was shared with /ask, and the full matrix reported this UNANCHORED "
              "- the #810 lesson, which is that a mutation detaches silently and its guard "
              "keeps passing happily as a control. The guard itself was strengthened in the "
              "same move: it now imports and CALLS the real module instead of slicing the "
              "function out of canvas.js's source text."),

    dict(id="786-esc-dropped-from-outcome-row", card="#786", path=CANVAS,
         guard=ANSWER_SURFACE,
         old="""return '<div class="qmeta">'+(o.status==="ok"?"✓":"✗")+' '+esc(lbl)+""",
         new="""return '<div class="qmeta">'+(o.status==="ok"?"✓":"✗")+' '+lbl+""",
         expect="caught",
         why="esc() dropped from the one line #753/#761 rewrote. Invisible to both existing row "
              "guards because they read textContent, and an injected element contributes ZERO "
              "characters to it - so the guard's string gets CLEANER as the surface gets less "
              "safe. Measured: the row's textContent is byte-identical either way."),

    dict(id="788-outcome-status", card="#788", path=CANVAS, guard=ANSWER_SURFACE,
         old='(o.status==="ok"?"✓":"✗")',
         new='(o.status!=="error"?"✓":"✗")',
         expect="caught",
         why="A timed-out store then renders as `✓ ... timeout`. Every outcome fixture is "
              "status:\"ok\", so no fixture can reach the other side of this branch, and no "
              "guard reads the glyph at all."),

    dict(id="788-all-document-rail", card="#788", path=CANVAS, guard=ANSWER_SURFACE,
         old="(kinds.size===1&&kinds.has('document'))?' from your documents'",
         new="(kinds.size===1&&kinds.has('document'))?' from your databases'",
         expect="caught",
         why="Reinstates #750/#728 - a folder calling itself a database. There is no "
              "all-document rail fixture, so this arm is never taken."),

    dict(id="788-outcome-count-on-failure", card="#788", path=CANVAS, guard=ANSWER_SURFACE,
         old="""(o.status==="ok"?' · '+o.count+' result'+(o.count===1?'':'s'):'')""",
         new="""(' · '+o.count+' result'+(o.count===1?'':'s'))""",
         expect="caught",
         why="The SECOND read of the same branch. The glyph could stay correct while the count "
              "leaks onto a failed row: `✗ bigquery-1 · declined · 7 results`. Two independent "
              "reads need two mutations, or fixing one hides the other."),

    dict(id="788-unassigned-printed", card="#788", path=CANVAS, guard=ANSWER_SURFACE,
         old='if(o.business_unit&&o.business_unit!=="unassigned") seg.push(o.business_unit);',
         new='if(o.business_unit) seg.push(o.business_unit);',
         expect="caught",
         why="The placeholder the canvas itself writes for 'no business unit' becomes a label "
              "shown to the reader. One of four JS homes for this rule; the only guard on it "
              "walked python files with python f-string regexes."),

    dict(id="788-numbered-at-ten", card="#788", path=CANVAS, guard=ANSWER_SURFACE,
         old="const _NUMBERED=/^\\s*(\\d{1,2})[.)]\\s+/;",
         new="const _NUMBERED=/^\\s*(\\d)[.)]\\s+/;",
         expect="caught",
         why="A list breaks at item 10. Was unfalsifiable while the only ordered fixture had "
              "items `1.` and `2.`; #793's numbers_in_prose fixture runs 12-13, so a "
              "single-digit marker now stops that run being a list at all."),

    dict(id="793-year-eaten", card="#793", path=CANVAS, guard=ANSWER_SURFACE,
         old="const _NUMBERED=/^\\s*(\\d{1,2})[.)]\\s+/;",
         new="const _NUMBERED=/^\\s*(\\d+)[.)]\\s+/;",
         expect="caught",
         why="The digit cap removed. `2008. Revenue was 4.2M` becomes an <ol> item reading "
              "`Revenue was 4.2M` - the year DELETED from the screen and a counter saying `1.` "
              "in its place. This is the regression as shipped on 260817."),

    dict(id="793-lone-number-is-a-list", card="#793", path=CANVAS, guard=ANSWER_SURFACE,
         old="const bullet=_BULLET.test(t), numbered=!bullet&&ordered&&_NUMBERED.test(t);",
         new="const bullet=_BULLET.test(t), numbered=!bullet&&_NUMBERED.test(t);",
         expect="caught",
         why="The run requirement removed, so a single numbered line with no sibling is a list "
              "item again and its marker is deleted. The second half of #793 - either narrowing "
              "alone still loses content."),

    dict(id="793-start-dropped", card="#793", path=CANVAS, guard=ANSWER_SURFACE,
         old='const at=(tag==="ol"&&start&&start!==1)?\' start="\'+start+\'"\':"";',
         new='const at="";',
         expect="caught",
         why="`12. reconcile / 13. archive` renders as `1. / 2.` - the counter beside the text "
              "contradicting an answer that may name step 12 three lines later."),

    dict(id="788-loose-paragraph-break", card="#788", path=CANVAS, guard=ANSWER_SURFACE,
         old='for(const block of html.split(/\\n\\s*\\n/)){',
         new='for(const block of html.split(/\\n\\n/)){',
         expect="caught",
         why="Models emit '\\n \\n' - a blank line that is not quite blank - constantly. "
              "Narrowed, a real answer stops splitting into paragraphs and runs together."),

    dict(id="788-continuation-phantom", card="#788", path=CANVAS, guard=ANSWER_SURFACE,
         old="else if(items.length && _INDENTED.test(line)){",
         new="else if(_INDENTED.test(line)){",
         expect="caught",
         why="With no bullet open the line writes to items[-1], a property `length` never sees, "
              "so flushList emits nothing and the line VANISHES from the answer. Every "
              "continuation fixture follows a bullet, so the phantom path is unreachable."),

    dict(id="789-origin-fallback", card="#789", path="src/dbsearch/server/router_api.py",
         guard=ANSWER_SURFACE,
         old='    return " \u00b7 ".join(parts) if parts else fallback',
         new='    return " \u00b7 ".join(parts)',
         expect="caught",
         why="A footnote with no system, location or object loses its fallback and renders an "
              "EMPTY origin, which canvas.js:2057 turns into the literal word 'Source' as the "
              "system name. Invisible while the guard derived its expectation from the same "
              "function AND every fixture supplied both fields."),

    dict(id="789-wrapped-bullet-space", card="#789", path=CANVAS, guard=ANSWER_SURFACE,
         old='items[items.length-1]+=" "+t;',
         new='items[items.length-1]+=t;',
         expect="caught",
         why="Fuses the wrapped line onto the bullet: `43 962.79including the fuel surcharge`. "
              "The guard is a substring test, so the fused string still contains it."),

    dict(id="789-br-to-space", card="#789", path=CANVAS, guard=ANSWER_SURFACE,
         old='out.push("<p>"+buf.join("<br>")+"</p>");',
         new='out.push("<p>"+buf.join(" ")+"</p>");',
         expect="caught",
         why="Collapses every hard line break inside a paragraph. `grep -rn '<br>' tests/` "
              "returns zero: no guard in the repo mentions it, and the probe reports answer "
              "blocks as element children only, so the change is structurally invisible."),

    dict(id="803-reset-visible-to-live-user", card="#803", path=CANVAS,
         guard="tests/selftest_803_canvas_reset_guard_dom.py",
         old='if(rb) rb.style.display=realLoginConfigured()?"none":"";',
         new='if(rb) rb.style.display=demo?"none":"";',
         expect="caught",
         why="The pre-fix visibility, verbatim: reset rode the demo-only loop, so display was "
              "`demo?\"none\":\"\"` and a signed-in owner saw 'Live demo' in the toolbar - one "
              "click from composing the demo manifest over their stored workspace row. Only "
              "the live_reset_hidden scenario can catch this: the guard clause keeps the "
              "click inert, so every effect-based assertion stays green."),

    dict(id="803-loadLiveDemo-unguarded", card="#803", path=CANVAS,
         guard="tests/selftest_803_canvas_reset_guard_dom.py",
         old="    if(isLiveUser()) return;\n",
         new="",
         expect="caught",
         why="The pre-fix loadLiveDemo had no caller guard at all - deleting the clause is the "
              "faithful revert. Only the live_reset_click scenario can catch this: jsdom "
              "dispatches clicks on display:none elements, so the hidden button cannot rescue "
              "the click path, and the demo fetch + demo compose fire exactly as prod would."),

    dict(id="818-no-autosave-on-mutation", card="#818", path=CANVAS,
         guard="tests/selftest_818_canvas_autosave_dom.py",
         old="    rowSaveSoon();                    // #818: the server row mirrors every mutation too\n",
         new="",
         expect="caught",
         why="The pre-#818 world, faithfully: saveCanvas wrote localStorage only, the row "
              "was written only by compose, and an added-but-never-composed node was durably "
              "lost on every reload (the owner's prod repro: postgres-1, hard refresh, gone). "
              "draft_autosave_survives remounts from the row and goes red."),

    dict(id="818-dirty-check-gone", card="#818", path=CANVAS,
         guard="tests/selftest_818_canvas_autosave_dom.py",
         old="    if(snap===lastRowSave) return Promise.resolve(true);   // saved IS true - nothing changed\n",
         new="",
         expect="caught",
         why="Without the dirty check every page load PUTs back the row it just hydrated "
              "from - a write storm that also races compose. clean_no_put counts PUTs on an "
              "untouched mount and goes red."),

    dict(id="818-live-gate-gone", card="#818", path=CANVAS,
         guard="tests/selftest_818_canvas_autosave_dom.py",
         old="    if(!isLiveUser()) return Promise.resolve(false);\n",
         new="",
         expect="caught",
         why="The 'never PUT for a non-live user' rule deliberately has ONE home, "
              "pushRowSave (a redundant copy in rowSaveSoon was removed as an "
              "equivalent-mutant home, the #799 lesson) - so deleting it is a real behavior "
              "change: a no-login dev rig starts writing server rows. dev_no_put goes red."),

    dict(id="818-move-never-saves", card="#818", path=CANVAS,
         guard="tests/selftest_818_canvas_autosave_dom.py",
         old="        if(el._moved) saveCanvas();\n",
         new="",
         expect="caught",
         why="The pre-#818 drag handler, faithfully: `up` only removed listeners - nothing "
              "rendered after a pure move, so the position died with the tab (persistence "
              "depended on some LATER render happening to fire saveCanvas). move_saves_layout "
              "drags via the real pointer path and counts PUTs."),

    dict(id="818-layout-dropped-from-manifest", card="#818", path=CANVAS,
         guard="tests/selftest_818_canvas_autosave_dom.py",
         old="    return {tenant:state.tenant||\"acme\",\n"
             "            stores:state.filter(n=>!n.derived).map(n=>entryOf(n)),\n"
             "            layout:layoutOf()};",
         new="    return {tenant:state.tenant||\"acme\",\n"
             "            stores:state.filter(n=>!n.derived).map(n=>entryOf(n))};",
         expect="caught",
         why="The pre-layout shape of liveManifest: stores only (re-anchored across #917's "
              "action-kind filter, dropping ONLY the layout key). Every PUT and every "
              "compose then writes a row with no layout, so a move survives nothing. "
              "move_saves_layout asserts the PUT body carries the moved coordinates."),

    dict(id="818-hydrate-ignores-server-layout", card="#818", path=CANVAS,
         guard="tests/selftest_818_canvas_autosave_dom.py",
         old="          const p=lay[id];\n"
             "          if(Array.isArray(p)&&p.length===2&&isFinite(p[0])&&isFinite(p[1]))\n"
             "            return {x:p[0],y:p[1]};\n",
         new="",
         expect="caught",
         why="The pre-#818 posOf consulted only the localStorage cache. The scenario mounts "
              "with an EMPTY localStorage and a row layout - the position can only have come "
              "from the server, so ignoring it lands the node on the default grid."),

    dict(id="818-put-endpoint-skips-the-guard", card="#818",
         path="src/dbsearch/server/router_api.py",
         guard="tests/selftest_818_draft_save.py",
         old="        manifest, key = _guarded_manifest(\n"
             "            req.manifest, user,\n"
             "            owner_tenant=tenant_resolver(request) if tenant_resolver else None)\n",
         new="        manifest, key = dict(req.manifest), _workspace_key(user)\n",
         expect="caught",
         why="THE drift the shared prelude exists to prevent: a second row writer that "
              "forgets the compose guards would store plaintext credentials (LAW 1) and skip "
              "the caller-powers check (#423). test_secret_literal_refused_and_row_untouched "
              "goes red."),

    dict(id="809-redshift-not-always-delegated", card="#809", path=CANVAS,
         guard="tests/selftest_809_canvas_delegation_dom.py",
         old='  const _ALWAYS_DELEGATED=new Set(["s3","redshift","rds_postgres","rds_mysql"]);\n',
         new='  const _ALWAYS_DELEGATED=new Set(["s3","rds_postgres","rds_mysql"]);\n',
         expect="caught",
         why="The #809 clause alone reverted (the anchor grew the rds kinds in #814): "
              "redshift attached aws_keys only when require_signin was toggled, and prod "
              "has no ambient AWS - a palette-added redshift store could only ever compose "
              "to 'Unable to locate credentials'. palette_redshift_delegates flushes the "
              "row and goes red on the missing block; the yaml clause cannot rescue it "
              "(the PUT body is read, not the preview)."),

    dict(id="809-preview-signin-only", card="#809", path=CANVAS,
         guard="tests/selftest_809_canvas_delegation_dom.py",
         old='              delegation:(signin||_ALWAYS_DELEGATED.has(n.kind))?(n.delegation||delegationFor(n.kind)):null};\n',
         new='              delegation:signin?(n.delegation||delegationFor(n.kind)):null};\n',
         expect="caught",
         why="The pre-#809 preview rule, faithfully: manifest() claimed 'same rule as "
              "entryOf' while omitting _ALWAYS_DELEGATED, so the drawer showed no delegation "
              "line for a store that composes with one (false even for s3 since #673). "
              "yaml_preview reads the drawer text and goes red; the entryOf clause cannot "
              "rescue it (the preview builds its own stores list from manifest())."),

    dict(id="809-panel-signin-switch-back", card="#809", path=CANVAS,
         guard="tests/selftest_809_canvas_delegation_dom.py",
         old='    redshift:  {label:"Redshift",    mono:"RS",  cap:"analytical", fields:[{k:"description",ph:"what this warehouse holds (routing signal) — e.g. sales facts by region, quarter",secret:false},{k:"workgroup",ph:"your Redshift Serverless workgroup — e.g. default-workgroup",secret:false},{k:"database",ph:"dev",secret:false},{k:"region",ph:"us-east-1",secret:false},{k:"tables",ph:"optional: scope to these tables — e.g. public.orders, public.customers",secret:false}]},\n',
         new='    redshift:  {label:"Redshift",    mono:"RS",  cap:"analytical", fields:[{k:"description",ph:"what this warehouse holds (routing signal) — e.g. sales facts by region, quarter",secret:false},{k:"workgroup",ph:"your Redshift Serverless workgroup — e.g. default-workgroup",secret:false},{k:"database",ph:"dev",secret:false},{k:"region",ph:"us-east-1",secret:false},{k:"tables",ph:"optional: scope to these tables — e.g. public.orders, public.customers",secret:false},{k:"require_signin",ph:"yes → queries run as the signed-in user (your AWS keys)",secret:false}]},\n',
         expect="caught",
         why="The pre-#809 panel row, faithfully (git show): a require_signin switch on an "
              "always-delegated kind does nothing - the hollow-offer shape (#654/#656/#660). "
              "panel_switch selects the node and reads the rendered fields; only this clause "
              "can turn it red (the delegation clauses never touch renderPanel)."),

    dict(id="810-stale-reason-survives-compose", card="#810", path=CANVAS,
         guard="tests/selftest_810_stale_reason_dom.py",
         # RE-ANCHORED by #808, which added the warnings assignment to this same branch and
         # left this entry matching 0x - the harness reported UNANCHORED, which is the whole
         # reason it counts hits instead of trusting a replace. The mutation still removes
         # ONLY `n.reason=""`, leaving #808's clause intact, so it isolates #810 exactly as
         # before. (The lesson this cost: when you edit a line, ask which mutations were
         # anchored to it - a guard can be made unfalsifiable by an improvement.)
         old='          if(byId[n.id]){ n.status="connected"; n.freshness=byId[n.id].freshness||""; n.reason="";\n'
             '                          n.warnings=byId[n.id].warnings||[]; }\n',
         new='          if(byId[n.id]){ n.status="connected"; n.freshness=byId[n.id].freshness||"";\n'
             '                          n.warnings=byId[n.id].warnings||[]; }\n',
         expect="caught",
         why="The pre-#810 composeUp success branch, faithfully (git show): n.reason is "
              "per-node state the node object carries across composes, so a store that "
              "failed once and then composed clean kept 'connected: build/probe failed...' "
              "in its dot tooltip until a reload. recompose_clears_reason drives fail-then-"
              "fix and reads the title attribute; the still-failing control pins the clear "
              "to the success branch alone. ONE home deliberately: adoptApplied rebuilds "
              "nodes fresh, so a clear there would be an equivalent-mutant home (#799)."),

    dict(id="814-rds-demands-a-password", card="#814",
         path="src/dbsearch/router/providers/postgres.py",
         guard="tests/selftest_814_rds_iam_auth.py",
         old='        missing = [k for k in ("host", "database", "user") if not config.get(k)]\n'
             '        if missing:\n'
             '            raise ValueError(f"rds_postgres config missing {missing}")\n',
         new='        missing = [k for k in ("host", "database", "user", "password")\n'
             '                   if not config.get(k)]\n'
             '        if missing:\n'
             '            raise ValueError(f"postgres config missing {missing}")\n',
         expect="caught",
         why="The pre-#814 dead end, faithfully: the base validator demanded the password "
              "the panel does not collect AND named the wrong kind (the owner's live 260813 "
              "wall). test_password_is_not_required and the kind-leak test both go red."),

    dict(id="814-mint-skipped-triple-as-password", card="#814",
         path="src/dbsearch/router/providers/postgres.py",
         guard="tests/selftest_814_rds_iam_auth.py",
         old='            token = mint_token(config, triple, port, rds_client_factory)\n'
             '            return _open(config["user"], token)\n',
         new='            return _open(config["user"], triple)\n',
         expect="caught",
         why="The Entra rail's shape misapplied: the raw credential used AS the password "
              "(what user_connect does for Azure). RDS would refuse the triple; "
              "test_delegated_query_authenticates_with_an_iam_token asserts the opened "
              "password IS the minted token and goes red."),

    dict(id="814-schema-cache-crosses-callers", card="#814",
         path="src/dbsearch/router/providers/postgres.py",
         guard="tests/selftest_814_rds_iam_auth.py",
         old='        if credential != self._introspect_credential:\n'
             '            self._schema_cache = None\n'
             '        self._introspect_credential = credential\n'
             '\n'
             '    def _run(self, sql: str):\n'
             '        cred = self._introspect_credential\n'
             '        if cred:\n'
             '            conn = self._user_conns.get(cred)\n'
             '            if conn is None:\n'
             '                conn = self._user_conns[cred] = call_user_connect(\n'
             '                    self._user_connect, cred, None)\n'
             '            cur = conn.cursor()\n'
             '            cur.execute(sql)\n'
             '            return cur\n'
             '        return super()._run(sql)\n'
             '\n'
             '\n'
             'class PostgresProvider(StoreProviderPort):\n',
         new='        self._introspect_credential = credential\n'
             '\n'
             '    def _run(self, sql: str):\n'
             '        cred = self._introspect_credential\n'
             '        if cred:\n'
             '            conn = self._user_conns.get(cred)\n'
             '            if conn is None:\n'
             '                conn = self._user_conns[cred] = call_user_connect(\n'
             '                    self._user_connect, cred, None)\n'
             '            cur = conn.cursor()\n'
             '            cur.execute(sql)\n'
             '            return cur\n'
             '        return super()._run(sql)\n'
             '\n'
             '\n'
             'class PostgresProvider(StoreProviderPort):\n',
         expect="caught",
         why="ADR 0022's named residue: a schema cache keyed on the store rather than the "
              "credential serves caller B caller A's schema (LAW 2). "
              "test_introspect_as_reads_the_schema_as_the_caller re-introspects as bob and "
              "counts the second mint; without the drop it never happens."),

    dict(id="814-provider-defaults-to-base-engine", card="#814",
         path="src/dbsearch/router/providers/postgres.py",
         guard="tests/selftest_814_rds_iam_auth.py",
         old='    def __init__(self, **kw) -> None:\n'
             '        super().__init__(**kw)\n'
             '        if not kw.get("engine_factory"):\n'
             '            self._engine_factory = RdsPostgresEngine.from_config\n'
             '\n'
             '    def _make(self, config: dict, credential: "str | None" = None) -> FederatedSqlStore:\n',
         new='    def _make(self, config: dict, credential: "str | None" = None) -> FederatedSqlStore:\n',
         expect="caught",
         why="Without the default override the provider inherits PostgresEngine.from_config "
              "- prod registers with engine_factory=None, so every palette RDS store would "
              "be built on the password engine. test_providers_default_to_the_rds_engines "
              "goes red."),

    dict(id="814-build-as-drops-the-credential", card="#814",
         path="src/dbsearch/router/providers/postgres.py",
         guard="tests/selftest_814_rds_iam_auth.py",
         old='        engine = self._engine_factory(config)\n'
             '        if credential and hasattr(engine, "introspect_as"):\n'
             '            engine.introspect_as(credential)\n'
             '        return FederatedSqlStore(\n'
             '            store_id=config["id"], business_unit=config.get("business_unit", ""),\n'
             '            title=config.get("title", config["id"]),\n'
             '            description=config.get("description", ""),\n'
             '            engine=engine,\n'
             '            sql_generator=self._gen, authorizer=authorizer,\n'
             '            topics=config.get("topics") or [], embedder=self._embedder,\n'
             '            value_llm=self._value_llm)\n'
             '\n'
             '    def probe_as(self, config: dict, credential: "str | None" = None) -> StoreProfile:\n'
             '        """ADR 0022: introspect as the caller when the store declares a delegation."""\n',
         new='        engine = self._engine_factory(config)\n'
             '        return FederatedSqlStore(\n'
             '            store_id=config["id"], business_unit=config.get("business_unit", ""),\n'
             '            title=config.get("title", config["id"]),\n'
             '            description=config.get("description", ""),\n'
             '            engine=engine,\n'
             '            sql_generator=self._gen, authorizer=authorizer,\n'
             '            topics=config.get("topics") or [], embedder=self._embedder,\n'
             '            value_llm=self._value_llm)\n'
             '\n'
             '    def probe_as(self, config: dict, credential: "str | None" = None) -> StoreProfile:\n'
             '        """ADR 0022: introspect as the caller when the store declares a delegation."""\n',
         expect="caught",
         why="probe_as/build_as that accept and DROP the credential are the #665 shape - "
              "the compose path would introspect ambient while claiming delegation. "
              "test_provider_build_as_threads_the_credential records introspect_as on a "
              "fixture engine and goes red."),

    dict(id="814-mysql-mint-skipped", card="#814",
         path="src/dbsearch/router/providers/mysql.py",
         guard="tests/selftest_814_rds_iam_auth.py",
         old='            token = mint_token(config, triple, port, rds_client_factory)\n'
             '            return _open(config["user"], token)\n',
         new='            return _open(config["user"], triple)\n',
         expect="caught",
         why="The mysql twin of 814-mint-skipped-triple-as-password - the twins share "
              "mint_token (one home) but each has its own user_connect closure that could "
              "independently skip it. test_mysql_twin_mints_and_connects goes red."),

    dict(id="814-rds-not-always-delegated", card="#814", path=CANVAS,
         guard="tests/selftest_814_rds_canvas_dom.py",
         old='  const _ALWAYS_DELEGATED=new Set(["s3","redshift","rds_postgres","rds_mysql"]);\n',
         new='  const _ALWAYS_DELEGATED=new Set(["s3","redshift"]);\n',
         expect="caught",
         why="The pre-#814 set, faithfully: a palette-added RDS entry carried no delegation "
              "at all, so the server had no caller to mint for. palette_rds_delegates "
              "flushes the row and goes red on the missing blocks."),

    dict(id="814-rds-resource-collapses-to-redshift", card="#814", path=CANVAS,
         guard="tests/selftest_814_rds_canvas_dom.py",
         old='  const _AWS_RESOURCE={s3:"s3",redshift:"redshift",rds_postgres:"rds",rds_mysql:"rds"};\n',
         new='  const _AWS_RESOURCE={s3:"s3",redshift:"redshift",rds_postgres:"redshift",rds_mysql:"redshift"};\n',
         expect="caught",
         why="The pre-#814 delegationFor said kind===\"s3\"?\"s3\":\"redshift\" - every "
              "non-s3 AWS kind labeled redshift. Equivalent mutant of that expression in "
              "map form; the resource:\"rds\" assertion in palette_rds_delegates goes red."),

    dict(id="814-panel-password-back", card="#814", path=CANVAS,
         guard="tests/selftest_814_rds_canvas_dom.py",
         old='    rds_postgres:{label:"RDS Postgres", mono:"RDS", cap:"analytical", fields:[{k:"description",ph:"what this DB holds (routing signal) — e.g. orders: sku, qty, revenue",secret:false},{k:"host",ph:"your-db.abc123.ap-southeast-1.rds.amazonaws.com",secret:false},{k:"database",ph:"postgres",secret:false},{k:"user",ph:"db user granted rds_iam — connects with your AWS keys",secret:false},{k:"port",ph:"5432",secret:false},{k:"tables",ph:"optional: scope to these tables — e.g. public.orders, public.customers",secret:false}]},\n',
         new='    rds_postgres:{label:"RDS Postgres", mono:"RDS", cap:"analytical", fields:[{k:"description",ph:"what this DB holds (routing signal) — e.g. orders: sku, qty, revenue",secret:false},{k:"host",ph:"your-db.abc123.ap-southeast-1.rds.amazonaws.com",secret:false},{k:"database",ph:"postgres",secret:false},{k:"user",ph:"your database user",secret:false},{k:"password",ph:"your database password",secret:true},{k:"port",ph:"5432",secret:false},{k:"tables",ph:"optional: scope to these tables — e.g. public.orders, public.customers",secret:false}]},\n',
         expect="caught",
         why="The pre-#814 panel row, faithfully (git show): a password field on a kind "
              "whose engine no longer wants one collects a secret with no purpose. "
              "rds_panel_no_password reads the rendered fields and goes red."),

    dict(id="791-supersede-owner-blind", card="#791", path="src/dbsearch/server/edition.py",
         guard="tests/selftest_ingest_file.py",
         old="                if doc.uri == uri and doc.doc_external_id != external_id \\\n"
             "                        and doc.owner_oid == owner_oid:",
         new="                if doc.uri == uri and doc.doc_external_id != external_id:",
         expect="caught",
         why="THE #791 defect, exactly as it stood (git show): supersede-by-uri deleted any "
              "doc sharing the filename across the WHOLE tenant partition with no owner "
              "check. `uri` is filename-only, so bob uploading his own Report.pdf to the "
              "shared org partition destroyed alice's Report.pdf. Data loss, silent."),

    dict(id="791-docacl-owner-not-surfaced", card="#791",
         path="src/dbsearch/adapters/local/__init__.py",
         guard="tests/selftest_ingest_file.py",
         old="                allowed_principals=list(c.allowed_principals), owner_oid=c.owner_oid,",
         new="                allowed_principals=list(c.allowed_principals),",
         expect="caught",
         why="The other half of the fix, mutated ALONE (one mutation per CLAUSE): the gate "
              "reads `doc.owner_oid`, so an adapter that does not surface it leaves every "
              "owner None. Then None == None supersedes ACROSS owners again for the local "
              "backend - and the SAME-owner test also goes red, which is the point: the "
              "gate and the field each have to be load-bearing on their own."),

    dict(id="791-admin-documents-asdict", card="#791", path="src/dbsearch/server/app.py",
         guard="tests/selftest_594_delete_your_own_document.py",
         old='    rows = [{"doc_external_id": d.doc_external_id, "title": d.title, "uri": d.uri,\n'
             '             "allowed_principals": list(d.allowed_principals or [])}\n'
             '            for d in _edition.list_documents(\n'
             '                user, scope, unrestricted=is_operator((sess or {}).get("oid", "")))]',
         new='    rows = [asdict(d) for d in _edition.list_documents(\n'
             '        user, scope, unrestricted=is_operator((sess or {}).get("oid", "")))]',
         expect="caught",
         why="The line as it stood BEFORE #791, which was correct until DocACL grew a field: "
              "serializing the domain object wholesale publishes every uploader's OID to each "
              "colleague who can read the document. The trap is that this mutation is a "
              "REVERT to code that was fine - the leak is created by the new field, not by "
              "the asdict, which is exactly why the guard has to assert on the RESPONSE."),

    dict(id="823-dev-rig-gets-gated", card="#823", path=CANVAS,
         guard="tests/selftest_823_canvas_gating_dom.py",
         old="    if(!realLoginConfigured()) return null;         // dev rig: unchanged, deliberately\n",
         new="",
         expect="caught",
         why="The tempting simplification: drop the dev-rig escape and gate everyone on "
              "sign-in state. A dev rig has signed_in permanently false and carries identity "
              "in X-DBSearch-User, so this locks every local rig and every selftest out of "
              "the palette. Same trap the openUploadPicker comment names."),

    dict(id="823-unlinked-provider-opens-anyway", card="#823", path=CANVAS,
         guard="tests/selftest_823_canvas_gating_dom.py",
         old="    if((authState.linked||[]).indexOf(need)>=0) return null;",
         new="    return null;",
         expect="caught",
         why="The defect #823 exists to fix, as prod stands today: bob is signed in with "
              "nothing linked and Azure/Google/AWS/M365 all render fully clickable, so every "
              "one of those tiles composes to a credential error (#551's always-fails tile, "
              "with a real credential prompt available and never offered)."),

    dict(id="823-files-group-gated-too", card="#823", path=CANVAS,
         guard="tests/selftest_823_canvas_gating_dom.py",
         old="    if(!need) return null;                          // Files & Links: an account is enough\n",
         new="",
         expect="caught",
         why="The over-broad gate: sweeping Files & Links in with the cloud providers leaves "
              "an ordinary hosted user who has linked nothing unable to add ANY source at "
              "all, which is the entire product. upload/csv/local need no third party."),

    dict(id="823-rail-panel-name-drift", card="#823", path=CANVAS,
         guard="tests/selftest_823_canvas_gating_dom.py",
         old='     link:"entra",  who:"Microsoft", flag:"enabled",        connect:"/auth/entra/link"},\n'
             '    {key:"google"',
         new='     link:"entra",  who:"Azure", flag:"enabled",        connect:"/auth/entra/link"},\n'
             '    {key:"google"',
         expect="caught",
         why="Proves the drift guard is not vacuous. The canvas keeps its own copy of the "
              "four link facts (this surface has no cross-surface imports), so the ONLY "
              "thing making that copy trustworthy is a test that fails when it disagrees "
              "with the account panel's ROSTER. Renaming one provider must go red."),

    dict(id="833-missing-blob-writes-zero", card="#833",
         path="scripts/prod_833_backfill_doc_bytes.py",
         guard="tests/selftest_833_doc_bytes_backfill.py",
         old='skips.append((tenant, doc, "raw blob missing - leaving NULL, never 0"))',
         new='updates.append((tenant, doc, 0))',
         expect="caught",
         why="The empty-success shape inside the backfill itself: an upload whose raw blob "
              "is gone must stay NULL and be NAMED, because writing 0 turns 'unknown' into "
              "a metering claim the billing surface then presents as fact."),

    dict(id="833-upload-discriminator-inverted", card="#833",
         path="scripts/prod_833_backfill_doc_bytes.py",
         guard="tests/selftest_833_doc_bytes_backfill.py",
         # RE-ANCHORED by #852. #840 hoisted `uri = row.get("uri") or ""` onto its own line
         # (to add the absent-uri branch), so the inline form this anchored to is gone.
         old="        if uri.startswith(UPLOAD_PREFIX):",
         new="        if not uri.startswith(UPLOAD_PREFIX):",
         expect="caught",
         why="Inverting the upload:// discriminator makes the backfill meter CONNECTOR "
              "documents by their raw blob size - exactly the ADR 0027 rule 3 violation "
              "the rig's SharePoint fixture (with a deliberately present, deliberately "
              "unread blob) exists to catch."),

    dict(id="832-old-key-becomes-primary", card="#832",
         path="src/dbsearch/adapters/local/secrets.py",
         guard="tests/selftest_832_key_rotation.py",
         old="keys = [key, *old_keys]",
         new="keys = [*old_keys, key]",
         expect="caught",
         why="MultiFernet encrypts with keys[0]. With the OLD key first, every write during "
              "the rotation window lands under the key about to be dropped, so the rotation "
              "quietly re-creates the orphaned-ciphertext defect it exists to end. The "
              "new-writes-encrypt-under-the-primary-alone test drops the old key and reads."),

    dict(id="832-unreadable-reports-as-set", card="#832",
         path="src/dbsearch/adapters/local/secrets.py",
         guard="tests/selftest_832_key_rotation.py",
         old='return {"exists": True, "readable": False, "hint": ""}',
         new='return {"exists": True, "hint": ""}',
         expect="caught",
         why="The pre-fix behaviour, restored exactly: an undecryptable blob answers the "
              "same shape as a short-but-healthy secret, so the operator who rotated the "
              "key sees 'is set' on every surface while every USE fails."),

    dict(id="832-reencrypt-swallows-failure", card="#832",
         path="src/dbsearch/adapters/local/secrets.py",
         guard="tests/selftest_832_key_rotation.py",
         old='report["unreadable"].append(handle)',
         new="pass",
         expect="caught",
         why="A re-encrypt pass that swallows a blob no key can read reports a clean "
              "rotation, and the operator drops the old key believing --verify's silence. "
              "The report must NAME what it could not carry across."),

    dict(id="832-canvas-unreadable-branch-inverted", card="#832",
         path=CANVAS,
         guard="tests/selftest_832_key_rotation.py",
         old="d.readable===false",
         new="d.readable===true",
         expect="caught",
         why="Inverting the canvas branch makes the HEALTHY credential render the scary "
              "message and the broken one render 'is set' - the exact inversion of #832's "
              "honesty fix. The structural guard pins the literal condition and its order "
              "before the hint branch."),

    dict(id="831-incoming-bytes-ignored", card="#831",
         # RE-ANCHORED by #852, and it MOVED FILE: #843 lifted the floor, its env override and
         # this arithmetic into core.headroom so the ingest runner could share one definition
         # of "is there room". The guard still lives in app.py; the rule being mutated does not.
         path="src/dbsearch/core/headroom.py",
         guard="tests/selftest_831_disk_headroom.py",
         old="    return None if free >= floor + incoming else (free, floor)",
         new="    return None if free >= floor else (free, floor)",
         expect="caught",
         why="Without the incoming term the guard accepts the very upload that crosses the "
              "floor - accepted-then-regretted, which on a full disk is the outage this "
              "card exists to prevent. The two same-free-space, different-size tests are "
              "what make the plus-incoming clause observable."),

    dict(id="831-fail-open-removed", card="#831",
         # RE-ANCHORED by #852, same #843 move as the clause above: the fail-open now lives
         # in core.headroom.shortfall, which returns None for "cannot tell, do not enforce".
         path="src/dbsearch/core/headroom.py",
         guard="tests/selftest_831_disk_headroom.py",
         old="    try:\n        free = store.free_bytes()\n    except NotImplementedError:\n        return None",
         new="    free = store.free_bytes()\n    if False:\n        return None",
         expect="caught",
         why="A store that cannot fill the local disk must not be enforced against: the "
              "in-memory store raises NotImplementedError from free_bytes(), and without "
              "the catch every upload on a memory rig 500s under an absurd floor - the "
              "cannot-measure test drives exactly that."),

    dict(id="748-hard-cut-restored", card="#748",
         path="src/dbsearch/server/router_api.py",
         guard="tests/e2e_router_api.py",
         old='"snippet": _snippet(ev.get("content") or ""),',
         new='"snippet": (ev.get("content") or "")[:160],',
         expect="caught",
         why="The EXACT pre-fix line from `git show HEAD~1:...router_api.py:1318`, not a "
              "memory of its shape: the hard cut that rendered '...fully pa' on the live "
              "site, mid-word with no ellipsis, indistinguishable from the document's own "
              "text. The helper-contract test drives a >160-char content through the "
              "endpoint's own _snippet and the wire test rejects any over-cap snippet "
              "without an ellipsis."),

    dict(id="834-emb-blob-writes-return", card="#834",
         path="src/dbsearch/pipeline/runner.py",
         guard="tests/selftest_834_no_emb_blobs.py",
         old="                    embedding=vec,",
         new='                    embedding_ref=store.put(f"emb/{doc.tenant_id}/{doc.external_id}/{n}", json.dumps(vec).encode()),',
         expect="caught",
         why="The faithful pre-fix behaviour (blob + ref instead of in-message), and a "
              "DISCRIMINATING one: retrieval still works through the ref fallback, so only "
              "the zero-emb-keys assertion can see the regression - the store silently "
              "grows 20% again while every answer stays correct."),

    dict(id="834-legacy-fallback-removed", card="#834",
         path="src/dbsearch/core/models.py",
         guard="tests/selftest_834_no_emb_blobs.py",
         old="        if self.embedding_ref:",
         new="        if False:",
         expect="caught",
         why="Without the ref fallback an old-shaped chunk (only embedding_ref, the shape "
              "every blob-backed rig and half-migrated caller still produces) refuses to "
              "index at all - the legacy-chunk test upserts exactly that shape."),

    dict(id="834-naked-chunk-indexed-silently", card="#834",
         path="src/dbsearch/core/models.py",
         guard="tests/selftest_834_no_emb_blobs.py",
         old="        raise ValueError(\n"
             "            f\"chunk {self.chunk_id} carries neither an embedding nor an embedding_ref\")",
         new="        return []",
         expect="caught",
         why="Returning an empty vector instead of refusing indexes a row retrieval can "
              "never rank - the silent-data-loss shape (#719 family: an infra defect must "
              "never masquerade as a content answer). The refuses-loudly test upserts a "
              "chunk with neither field and demands the exception."),

    dict(id="805-reset-before-register-restored", card="#805",
         path="src/dbsearch/server/router_api.py",
         guard="tests/selftest_805_compose_broker_window.py",
         old="        _staging = state.broker.staging()",
         new="        state.broker.reset()\n        _staging = state.broker",
         expect="caught",
         why="The faithful pre-fix behaviour: register onto the freshly-reset LIVE broker. "
              "Both tests go red - the deterministic mid-registration probe sees an empty "
              "broker (the concurrent-ask window), and a failed recompose strips the old "
              "manifest's delegations while its stores keep serving."),

    dict(id="805-adopt-before-handlers", card="#805",
         path="src/dbsearch/server/router_api.py",
         guard="tests/selftest_805_compose_broker_window.py",
         old="            r.register_delegations(spec, _staging,",
         new="            state.broker.adopt(_staging)\n            r.register_delegations(spec, state.broker,",
         expect="caught",
         why="Adopting the EMPTY staging broker before registering into the live one is "
              "reset-before-register wearing the new clothes: the mid-registration probe "
              "sees an empty live broker again, and a failed block still strips the old "
              "delegations. Pins that adopt must come AFTER a successful registration."),

    dict(id="815-missing-dir-reads-as-empty", card="#815",
         path="src/dbsearch/connectors/folder.py",
         guard="tests/selftest_815_folder_probe_honesty.py",
         old="        if not self._root.is_dir():\n            raise FileNotFoundError(",
         new="        if False:\n            raise FileNotFoundError(",
         expect="caught",
         why="The faithful pre-fix behaviour: rglob's empty iterator makes a typo'd path "
              "probe 'reachable; schema read' and exercise 'may be empty' - a hollow green "
              "indistinguishable from a healthy empty folder. Both failure tests demand "
              "status=failed and the path named in the verdict."),

    dict(id="804-failure-keeps-the-button", card="#804",
         path=CANVAS,
         guard="tests/selftest_804_compose_failed_button.py",
         old='.catch(e=>{ btn.textContent="Compose failed: "+(e.message||e);\n'
             '                  setTimeout(restoreComposeBtn,4000); });',
         new='.catch(e=>{ btn.textContent="Compose failed: "+(e.message||e); });',
         expect="caught",
         why="The faithful pre-fix catch: the error string becomes the button's permanent "
              "name, so there is no retry affordance until a full reload. The release test "
              "outwaits the failure-path timer and demands 'Compose up' back."),

    dict(id="816-rail-offers-graph-search-again", card="#816",
         path=CANVAS,
         guard="tests/selftest_816_graph_search_not_self_serve.py",
         # #924 added sharepoint_link to this row; the anchor follows the line as written.
         old='kinds:["upload","gdrive","sharepoint_link","sharepoint","local","folder"]',
         new='kinds:["upload","gdrive","sharepoint_link","sharepoint","local","folder","graph_search"]',
         expect="caught",
         why="The faithful pre-fix rail: a signed-in customer is offered a kind with no "
              "self-serve path, whose Test connection can only ever fail, and whose panel "
              "used to collect a credential nothing reads."),

    # #893 is TWO clauses on two sides of the wire, and a fixture rescued by both at once
    # would prove neither (#788). One mutation each.
    dict(id="893-foreign-shapes-pass-the-server", card="#893",
         path="src/dbsearch/query/service.py",
         guard="tests/selftest_893_foreign_citation_tokens.py",
         old="        return _FOREIGN_MARK.sub(\"\", answer)",
         new="        return answer",
         expect="caught",
         why="The whitelist as it stood: every guard in the pipeline tests a citation's "
              "NUMBER, and the shapes gpt-oss also emits - a filename, a chunk name, the "
              "private-use sentinel run - carry no number to test, so they passed the sweep "
              "AND the renderer's whitelist and reached the reader on every surface."),

    dict(id="893-preview-shows-raw-model-output", card="#893",
         path="src/dbsearch/server/static/js/surfaces/ask.js",
         guard="tests/selftest_893_foreign_citation_tokens.py",
         old="        (tok) => { acc += tok; answerEl.textContent = previewText(acc); stickToBottom(false); },",
         new="        (tok) => { acc += tok; answerEl.textContent = acc; stickToBottom(false); },",
         expect="caught",
         why="The line the leak was actually on, restored exactly: the streamed answer went "
              "to the page as the model's RAW output and only the final string went through "
              "answerNodes, so 'confirmed\u30109\u2020L1-L4\u3011' was on screen for the whole "
              "length of every generation. This is the string the owner read off prod."),

    dict(id="918-blank-link-accuses-the-reader", card="#918",
         path="src/dbsearch/connectors/gdrive.py",
         guard="tests/selftest_712_gdrive_connector.py",
         old='    if not s:\n'
             '        raise ValueError(\n'
             '            "no folder link yet - open this source and paste the folder\'s share link "\n'
             '            "(in Drive: Share -> General access -> \'Anyone with the link\', then Copy link)")\n',
         new="",
         expect="caught",
         why="The message as it stood on prod: an EMPTY link fell through to the malformed "
              "branch, so the owner's gdrive-1 - whose link field had never been filled - "
              "carried the red note \"that does not look like a Drive folder link\". A node "
              "accusing the reader of input they never gave is exactly the launch gate's own "
              "standard failing."),

    dict(id="893-strip-eats-the-real-marker", card="#893",
         path="src/dbsearch/query/service.py",
         guard="tests/selftest_893_foreign_citation_tokens.py",
         old='    r"\\s?\\u3010(?!\\s*\\d+\\s*(?:\\u2020[^\\u3011]*)?\\u3011)[^\\u3011]*\\u3011"',
         new='    r"\\s?\\u3010[^\\u3011]*\\u3011"',
         expect="caught",
         why="The over-broad strip - the same fix without its negative lookahead. It deletes "
              "the marker the reader CAN resolve along with the ones they cannot, which is "
              "destroying real provenance to remove fake provenance: #257's own failure "
              "arriving from the opposite side."),

    dict(id="816-dev-copy-returns", card="#816",
         path="src/dbsearch/router/native_search.py",
         guard="tests/selftest_816_graph_search_not_self_serve.py",
         old='f"native Microsoft search is not configured on this deployment ({var} is "\n'
             '                "operator-set, not self-serve). Use a SharePoint source instead, or ask "\n'
             '                "the operator to configure native search"',
         new='f"native store has no credential: env {var} is not set "\n'
             '                "(dev spike; E5 OBO replaces this)"',
         expect="caught",
         why="The faithful pre-fix string from git show: developer vocabulary on a "
              "customer verdict, no remedy the user can act on, no owner named."),

    dict(id="861-routed-markers-unvalidated", card="#861",
         path="src/dbsearch/server/router_api.py",
         guard="tests/selftest_861_routed_marker_validation.py",
         old='    result["answer"] = QueryService._drop_dangling_markers(\n'
             '        result.get("answer") or "", len(footnotes))',
         new='    result["answer"] = result.get("answer") or ""',
         expect="caught",
         why="CLAUSE A, restored exactly as the routed path shipped it: the answer reaches the "
              "reader unchecked, so a model writing [9] over three footnotes gets [9] rendered. "
              "Verified by hand to redden test_a_marker_past_the_last_footnote_is_dropped ALONE "
              "while both clause-B tests stay green - one mutation per clause, or the two halves "
              "are one guard in two costumes."),

    dict(id="861-referenced-on-one-surface-only", card="#861",
         path="src/dbsearch/server/router_api.py",
         guard="tests/selftest_861_routed_marker_validation.py",
         old='    result["referenced"] = QueryService._referenced(result["answer"], len(footnotes))',
         new='    pass',
         expect="caught",
         why="The asymmetry #859 actually shipped: `referenced` computed in the /chat/stream "
              "delegate only, so /router/ask returned no key at all - and absent and empty are "
              "different states to a client keying on Array.isArray. Reddens the two referenced "
              "tests while both drop tests stay green, which is what proves the drop clause "
              "stands on its own."),

    dict(id="861-citations-from-deduped", card="#861",
         path="src/dbsearch/router/synthesizer.py",
         guard="tests/selftest_861_routed_marker_validation.py",
         old="""    out: list[dict] = []
    for ev in evidence:
        prov = ev.provenance or {}
        cite = {"store_id": ev.store_id, "kind": ev.kind}""",
         new="""    out: list[dict] = []
    seen: set[tuple] = set()
    for ev in evidence:
        prov = ev.provenance or {}
        key = (ev.store_id, ev.kind, prov.get("doc"), prov.get("table"),
               str(prov.get("row_ids")))
        if key in seen:
            continue
        seen.add(key)
        cite = {"store_id": ev.store_id, "kind": ev.kind}""",
         expect="caught",
         why="CLAUSE B, the dedupe restored verbatim. This is #855's defect at its THIRD home: "
              "two chunks of one document become two footnotes and one citation, so a marker "
              "valid on the live rail dangles on reopen - measured on prod as [1,3,5,7] stored "
              "against 6 citations. Reddens all three clause-B tests with every clause-A test "
              "green."),

    dict(id="861-reopen-renumbers-survivors", card="#861",
         path="src/dbsearch/server/static/js/surfaces/ask.js",
         guard="tests/selftest_602_owner_reopens_conversation.py",
         old="""  const footnotes = stored
    // Number FIRST, against the list the answer's markers were written against...
    .map((c, i) => ({ c, n: i + 1 }))
    // ...then drop the rows this builder cannot render, each survivor keeping its own number.
    .filter(({ c }) => c.store_id && !c.doc)""",
         new="""  const footnotes = stored
    .filter((c) => c.store_id && !c.doc)
    .map((c, i) => ({ c, n: i + 1 }))""",
         expect="caught",
         why="The pre-fix ORDER, restored: filter the document rows out and only then number "
              "what is left, so every proof after the first gap is renumbered. Measured on "
              "prod - an answer reading 'Singapore 137[1] London 92[4] Berlin 78[7] Austin "
              "65[10]' reopened with its rows labelled [1][2][3][4], so [4] opened Austin's "
              "row under a line that says London. A dangling marker looks broken; a moved one "
              "looks sourced. Caught by the reopened-numbering DOM probe."),

    dict(id="865-mainjs-matches-a-class-itself", card="#865",
         path="src/dbsearch/server/static/js/main.js",
         guard="tests/selftest_631_rail_slot.py",
         old="  if (grid && surface && !railMounted(grid)) {",
         new='  if (grid && surface && !grid.querySelector(".rail")) {',
         expect="caught",
         why="THE CALL-SITE CLAUSE, restored exactly as it shipped: main.js holding its own "
              "opinion about how the rail is built, and getting it wrong - nothing in the tree "
              "carries the class `rail` (the root is `navrail`; a class selector matches whole "
              "tokens), so the condition was always true and the mount was never guarded. "
              "Caught by the source assertion alone; the behavioural test stays green, which "
              "is the point of splitting them - the probe drives railMounted and cannot see "
              "whether anybody calls it."),

    dict(id="865-railmounted-wrong-class", card="#865",
         path="src/dbsearch/server/static/js/ui/rail.js",
         guard="tests/selftest_631_rail_slot.py",
         old='const RAIL_CLASS = "navrail";',
         new='const RAIL_CLASS = "rail";',
         expect="caught",
         why="THE BEHAVIOURAL CLAUSE: the predicate and renderRail disagree about the root "
              "class, so railMounted() cannot see a rail it just built and the mount step "
              "stops being idempotent. Caught by the DOM probe alone; the source assertion "
              "stays green."),

    dict(id="861-cjk-marker-form-unvalidated", card="#861",
         path="src/dbsearch/query/service.py",
         guard="tests/selftest_861_routed_marker_validation.py",
         old='        answer = re.sub(r"\\s?\\[(\\d+)\\]|\\s?【\\s*(\\d+)\\s*(?:†[^】]*)?】",\n'
             '                        lambda m: m.group(0)\n'
             '                        if 1 <= int(m.group(1) or m.group(2)) <= n_citations else "",\n'
             '                        answer)',
         new='        answer = re.sub(r"\\s?\\[(\\d+)\\]",\n'
             '                        lambda m: m.group(0) if 1 <= int(m.group(1)) <= n_citations else "",\n'
             '                        answer)',
         expect="caught",
         why="The pre-#861 helper, restored exactly: it validated the PLAIN spelling only. The "
              "model's own convention is the CJK bracket form, which components.js renders to "
              "the reader as a [n] footnote just like the plain one and which _MARK_ANY has "
              "always counted - so the guard was checking the spelling the model uses LEAST. "
              "Found on prod while verifying #861's own first deploy: four consecutive routed "
              "answers came back in that form. Reddens the two spelling tests while the "
              "plain-form and clause-B tests stay green."),

    # #920 moved the gate from the provider row to the kind. That is FOUR clauses, and a
    # fixture rescued by two of them at once proves neither (the standing lesson from #788),
    # so each one is mutated on its own.
    dict(id="920-gdrive-demands-google-again", card="#920", path=CANVAS,
         guard="tests/selftest_920_files_and_links.py",
         old='    gdrive:    {label:"Google Drive", mono:"GD",  cap:"documents",',
         new='    gdrive:    {label:"Google Drive", mono:"GD",  cap:"documents", needs:"google",',
         expect="caught",
         why="The defect this card was opened for, re-expressed at the kind level: mex3woof "
              "signed in, asked for a PUBLIC Drive folder, and was told to connect a Google "
              "account that slice 1 never uses (it reads the folder with the deployment's own "
              "API key). The gate lying about its own requirement."),

    dict(id="920-sharepoint-gate-deleted", card="#920", path=CANVAS,
         guard="tests/selftest_920_files_and_links.py",
         old='cap:"semantic",   needs:"entra", fields:[]},',
         new='cap:"semantic",   fields:[]},',
         expect="caught",
         why="The over-broad version of this fix, and the one a 'can I add gdrive now?' test "
              "alone would pass: the regroup deletes the requirement instead of moving it. "
              "SharePoint ingests on the caller's OWN Microsoft consent wherever the tile is "
              "filed, so an unlinked caller gets a tile that can only ever fail."),

    dict(id="920-row-gates-if-any-kind-does", card="#920", path=CANVAS,
         guard="tests/selftest_920_files_and_links.py",
         old="    return gates.every(Boolean) ? gates[0] : null;",
         # `.some(...) ? gates[0]` was the first attempt and it SURVIVED: gates[0] is
         # upload's null, so the row opened anyway and the mutation changed nothing on
         # screen. The faithful shape of the old behaviour is the FIRST REAL gate, which is
         # what a provider-level gate returned.
         new="    return gates.find(Boolean) || null;",
         expect="caught",
         why="The mixed row collapsing back to a whole-row gate: because SharePoint now sits "
              "under Files & Links, one gated kind would shut the row on upload, CSV and "
              "Local too - an ordinary hosted user who linked nothing could add nothing, "
              "which is #823's own over-broad-gate defect arriving by a new route."),

    dict(id="920-kind-inherits-no-requirement", card="#920", path=CANVAS,
         guard="tests/selftest_920_files_and_links.py",
         old="    const p=PROVIDERS.find(p=>p.kinds.indexOf(kind)>=0);\n    return p ? p.link : null;",
         new="    return null;",
         expect="caught",
         why="The kind gate with its inheritance dropped: every database kind declares no "
              "`needs` of its own and relies on the row's, so this makes Azure SQL, BigQuery "
              "and Redshift addable with nothing vaulted - ADR 0022/0024's as-you query with "
              "no credential to run as."),

    # ---- #937: /ask counted the uploaded-document index and spoke for both planes -----------
    # Six entries, and the count is the point: the first fixture pair let THREE of these
    # survive, because `indexed:false` and `authorized_docs:0` were true together in both
    # scenarios, so no single clause was ever the thing that decided. The guard now carries
    # `unshared`, `norows` and `unknown` for exactly that reason.
    dict(id="937-boot-banner-guard-removed", card="#937",
         path="src/dbsearch/server/static/js/surfaces/ask.js",
         guard="tests/selftest_937_ask_corpus_contradiction_dom.py",
         old="    if (s.connected_sources !== 0) return;\n    if (!s.indexed) {",
         new="    if (!s.indexed) {",
         expect="caught",
         why="THE DEFECT AS PROD SHIPPED IT. With the connected-sources plane gone, /ask reads "
             "the uploaded-document count alone and tells a caller whose Drive folder is "
             "connected, ingested and answering that they have indexed nothing and should go "
             "connect a source. Measured on dbsearch.ai 260823, still on screen six seconds "
             "after a hard reload."),
    dict(id="937-boot-banner-fires-unconditionally", card="#937",
         path="src/dbsearch/server/static/js/surfaces/ask.js",
         guard="tests/selftest_937_ask_corpus_contradiction_dom.py",
         old="    if (s.connected_sources !== 0) return;",
         new="    return;",
         expect="caught",
         why="THE CHEAP WRONG FIX: delete the copy instead of qualifying it. Every "
             "contradiction test goes green and the product loses the one sentence that tells "
             "a genuinely new user what to do next - which is the #392 defect restored. The "
             "`empty` scenario is the control that refuses it."),
    dict(id="937-unmeasured-workspace-read-as-empty", card="#937",
         path="src/dbsearch/server/static/js/surfaces/ask.js",
         guard="tests/selftest_937_ask_corpus_contradiction_dom.py",
         old="    if (s.connected_sources !== 0) return;",
         new="    if (s.connected_sources > 0) return;",
         expect="caught",
         why="`> 0` treats null - the workspace store was unreachable - exactly like a MEASURED "
             "zero, so a caller whose sources we simply could not read is told they have none. "
             "#392's own rule is that an unmeasured corpus is silence, not emptiness. Both the "
             "connected and empty scenarios pass under this; only `unknown` refuses it."),
    dict(id="937-panel-denies-its-own-sources", card="#937",
         path="src/dbsearch/server/static/js/ui/components.js",
         guard="tests/selftest_937_ask_corpus_contradiction_dom.py",
         old="  if (retrieved && !corpus.authorized_docs) {",
         new="  if (false) {",
         expect="caught",
         why="The panel half of the defect: `provenanceNote` reaches its no-source sentences "
             "with three sources rendered underneath, so the Sources panel says 'there was "
             "nothing to search' as a header on top of what it searched. This is the "
             "screenshot the user sent."),
    dict(id="937-panel-guard-ignores-retrieval", card="#937",
         path="src/dbsearch/server/static/js/ui/components.js",
         guard="tests/selftest_937_ask_corpus_contradiction_dom.py",
         old="  if (retrieved && !corpus.authorized_docs) {",
         new="  if (!corpus.authorized_docs) {",
         expect="caught",
         why="Drops the only clause that makes this a CONTRADICTION rule rather than a blanket "
             "suppression. With nothing retrieved the counters are all anyone has and the "
             "denial is TRUE; without `retrieved` the surface answers 'Grounded in 0 documents' "
             "instead. Only the `norows` scenario can fail on this."),
    dict(id="937-panel-guard-reads-the-wrong-counter", card="#937",
         path="src/dbsearch/server/static/js/ui/components.js",
         guard="tests/selftest_937_ask_corpus_contradiction_dom.py",
         old="  if (retrieved && !corpus.authorized_docs) {",
         new="  if (retrieved && !corpus.indexed) {",
         expect="caught",
         why="THE NEAR-MISS FIX, and the one I actually wrote first. Reading `indexed` catches "
             "the connector case and falls through on `indexed:true, authorized_docs:0` to "
             "'None of the indexed documents are shared with you yet' - a permissions "
             "accusation printed above sources the caller can plainly see. Only `unshared` "
             "isolates it."),
    dict(id="937-dry-run-tells-you-to-connect-what-you-connected", card="#937",
         path="src/dbsearch/server/static/js/ui/components.js",
         guard="tests/selftest_937_ask_corpus_contradiction_dom.py",
         old="  if (!corpus.indexed && corpus.connected_sources) {\n    return sentence(\"Nothing you can access matched this question\");\n  }\n",
         new="",
         expect="caught",
         why="ROUND 2, AND THE ONLY ONE PROD FOUND RATHER THAN THE SUITE. The first fix keyed "
             "on `retrieved`, so a question that matched NOTHING skipped it entirely and the "
             "note still read 'Connect a source to get started' - printed one line under an "
             "answer saying 'The source is there and readable'. A fix that names an asymmetry "
             "owes a probe on both sides of it; `dryrun` is that probe."),

    # ---- #939 / #895: a node that could not say what it holds -------------------------------
    dict(id="939-inventory-forwards-the-admin-listing", card="#939",
         path="src/dbsearch/query/service.py",
         guard="tests/selftest_939_document_inventory.py",
         old="            if not (principals & allowed):\n                continue                      # LAW 2 - see the docstring above",
         new="            pass",
         expect="caught",
         why="Drops the LAW 2 trim. `list_doc_acls` is the ADMIN permission-tester surface and "
             "returns EVERY document in the partition, so without this intersection the file "
             "list publishes the NAMES of documents the caller cannot read - a disclosure with "
             "no query attached, invisible to every retrieval test because retrieval never "
             "touches this call. Bob is all-staff and must never learn osprey.txt exists."),
    dict(id="939-node-keeps-the-compose-snapshot", card="#939",
         path="src/dbsearch/server/static/js/surfaces/canvas.js",
         guard="tests/selftest_939_store_documents_dom.py",
         old="        freshnessPill(node)+",
         new="        (node.freshness?'<span class=\"pill\" title=\"freshness\">'+esc(node.freshness)+'</span>':'')+",
         expect="caught",
         why="THE PROD DEFECT, restored: the node paints the freshness captured AT COMPOSE, so "
             "a crawl that finished afterwards leaves the badge reading `syncing` forever and "
             "no doc count anywhere. Measured on prod 260823 - catalog ingested@08:58:31, badge "
             "syncing. This is #895's failing clause."),
    dict(id="939-count-printed-mid-crawl", card="#939",
         path="src/dbsearch/server/static/js/surfaces/canvas.js",
         guard="tests/selftest_939_store_documents_dom.py",
         old="    if(node.docsKnown && !syncing && typeof node.docCount===\"number\"){",
         new="    if(node.docsKnown && typeof node.docCount===\"number\"){",
         expect="caught",
         why="Prints a count while the crawl is still running, where it is 0 and will not be 0 "
             "in a minute - a moving number stated as the answer, which is #392's error in "
             "miniature. Only the `syncing` scenario can fail on this clause."),
    dict(id="939-unknown-rendered-as-an-empty-store", card="#939",
         path="src/dbsearch/server/static/js/surfaces/canvas.js",
         guard="tests/selftest_939_store_documents_dom.py",
         old="    if(!node.docsKnown) return '';        // unknown says nothing at all (#392)",
         new="",
         expect="caught",
         why="A store that CANNOT list - a SQL store, or a listing that errored - renders the "
             "empty-file-list placeholder, stating as fact something nobody measured. #392 "
             "exists because an unmeasured corpus was rendered as an empty one."),
    dict(id="939-unreadable-files-vanish-silently", card="#939",
         path="src/dbsearch/server/static/js/surfaces/canvas.js",
         guard="tests/selftest_939_store_documents_dom.py",
         old="    const unread=node.unreadable",
         new="    const unread=false",
         expect="caught",
         why="#725: files the crawl listed and could not fetch (a per-file 403, an export cap) "
             "disappear from the list with nothing said. A file list makes this WORSE than the "
             "old silence - the file is in the user's folder, absent from a list that claims to "
             "be the folder, and no surface explains it."),

    # ---- #944: composing an unchanged store re-crawled the whole library -------------------
    dict(id="944-recompose-rebuilds-and-recrawls", card="#944",
         path="src/dbsearch/router/providers/connector.py",
         guard="tests/selftest_944_compose_reuses_a_built_store.py",
         old="        if (built is not None and sid in self._pipes\n                and self._recipes.get(sid) == recipe\n                and not _crawl_failed(self.sources, sid)):",
         new="        if False:",
         expect="caught",
         why="THE PROD DEFECT. The canvas composes on every mount, and build_as built a new "
             "empty index and submitted a full crawl each time - measured on the live box as "
             "docs_done 2, docs_total 2, docs_skipped 0 for a trip from /ask to Connectors. "
             "Drive API quota and the empty-index window, per page visit, scaling with the "
             "size of the customer's library."),
    dict(id="944-reuse-ignores-a-changed-recipe", card="#944",
         path="src/dbsearch/router/providers/connector.py",
         guard="tests/selftest_944_compose_reuses_a_built_store.py",
         old="                and self._recipes.get(sid) == recipe\n",
         new="",
         expect="caught",
         why="Reuses a store whose ENTRY changed - a new folder link keeps serving the old "
             "folder's content, and an acl change leaves the previous audience stamped on "
             "every chunk (the audience is written at ingest), so a permission change the "
             "product reports as applied has not been applied. LAW 2."),
    dict(id="944-reuse-strands-a-failed-store", card="#944",
         path="src/dbsearch/router/providers/connector.py",
         guard="tests/selftest_944_compose_reuses_a_built_store.py",
         old="                and not _crawl_failed(self.sources, sid)):",
         new="                ):",
         expect="caught",
         why="Reuses a store whose last crawl ERRORED, which makes recompose - the user's only "
             "recovery gesture - a permanent no-op and strands the store empty. That is the "
             "#941 family exactly: a gesture that looks like it worked."),

    # ---- #940: a store still reading was reported as a source that holds nothing ------------
    dict(id="940-warming-store-reported-as-empty", card="#940",
         path="src/dbsearch/router/synthesizer.py",
         guard="tests/selftest_940_warming_store_is_not_empty.py",
         old="        if any(getattr(o, \"warming\", False) for o in outcomes):\n            return WARMING_ANSWER\n",
         new="",
         expect="caught",
         why="THE PROD DEFECT, twice, once per deploy. A connector store's index lives in the "
             "api process, so a container restart empties it and it re-crawls - and during "
             "that window the question that had just answered with three citations was told "
             "'The source is there and readable - it simply holds nothing that fits'. Three "
             "positive claims, the last one false about a file that contains the answer."),
    dict(id="940-executor-never-reads-freshness", card="#940",
         path="src/dbsearch/router/executor.py",
         guard="tests/selftest_940_warming_store_is_not_empty.py",
         old="                fresh = store.freshness() if hasattr(store, \"freshness\") else \"\"",
         new="                fresh = \"\"",
         expect="caught",
         why="The OTHER half. Filed first as expect=survives, because the guard only exercised "
             "no_evidence_answer and could not see the executor that SETS `warming` - deleting "
             "this line would have left every sentence assertion green while prod behaved "
             "exactly as before. Closed the same session with a dispatch-level fixture (a fake "
             "store reporting syncing freshness through the real `execute`), so the entry is "
             "now `caught` and the gap is gone rather than merely documented."),

    # ---- #941: "the endpoint probed OK" was rendered as "this store holds data" -------------
    dict(id="941-probe-result-rendered-as-composed", card="#941",
         path="src/dbsearch/server/static/js/surfaces/canvas.js",
         guard="tests/selftest_941_uncomposed_store_dom.py",
         old="  function isUncomposed(node){\n    return !node.derived && node.status===\"connected\" && !composedIds.has(node.id);\n  }",
         new="  function isUncomposed(node){\n    return false;\n  }",
         expect="caught",
         why="THE DEFECT AS PROD SHIPPED IT. testConn and composeUp both write "
             "status='connected'; one means the endpoint answered a probe, the other means the "
             "store is in the catalog. The owner re-added a Drive folder, pressed Test "
             "connection, and got a green dot, '1 connected' and 'content is retrievable' over "
             "a store holding nothing - for twenty minutes."),
    dict(id="941-uploads-node-libelled-as-draft", card="#941",
         path="src/dbsearch/server/static/js/surfaces/canvas.js",
         guard="tests/selftest_941_uncomposed_store_dom.py",
         old="    return !node.derived && node.status===\"connected\" && !composedIds.has(node.id);",
         new="    return node.status===\"connected\" && !composedIds.has(node.id);",
         expect="caught",
         why="Drops the derived-node exemption. 'Your documents' (#917) comes from "
             "/admin/documents and is never in any manifest, so it is never in a compose "
             "response - and the honesty fix would then accuse the one node that is genuinely "
             "working, on every canvas that has an upload. The `derived` scenario is the only "
             "one that can fail on this."),
    dict(id="941-status-bar-still-counts-drafts", card="#941",
         path="src/dbsearch/server/static/js/surfaces/canvas.js",
         guard="tests/selftest_941_uncomposed_store_dom.py",
         old="    const conn=state.filter(s=>s.status===\"connected\"&&!isUncomposed(s)).length;",
         new="    const conn=state.filter(s=>s.status===\"connected\").length;",
         expect="caught",
         why="The status bar read '1 connected' in the same viewport as an answer saying 'No "
             "data sources are connected yet'. 'Connected' in that bar has always meant "
             "askable, and an uncomposed store is not."),
    dict(id="941-panel-still-claims-content-is-retrievable", card="#941",
         path="src/dbsearch/server/static/js/surfaces/canvas.js",
         guard="tests/selftest_941_uncomposed_store_dom.py",
         old="    const probe = isUncomposed(node)",
         new="    const probe = false",
         expect="caught",
         why="Restores the panel line that reported REACHABILITY and was read as readiness - "
             "'Connection healthy - a record round-tripped', 'exercise: content is retrievable' "
             "- about a store with no content at all."),
    dict(id="941-test-connection-composes-nothing", card="#941",
         path="src/dbsearch/server/static/js/surfaces/canvas.js",
         guard="tests/selftest_941_uncomposed_store_dom.py",
         old="        if(v.status!==\"failed\") composeUp();",
         new="",
         expect="caught",
         why="Removes the auto-compose half. The surface stays honest but the user is left "
             "hunting for a button called 'Compose up', which is what happened - the owner "
             "asked how to tell whether it was even ingesting."),
    dict(id="941-composes-even-when-the-probe-refused", card="#941",
         path="src/dbsearch/server/static/js/surfaces/canvas.js",
         guard="tests/selftest_941_uncomposed_store_dom.py",
         old="        if(v.status!==\"failed\") composeUp();",
         new="        composeUp();",
         expect="caught",
         why="Auto-composes a store the probe just REFUSED, submitting a crawl already known to "
             "fail and burying the remediation the user needs under a compose error. Only the "
             "`probefail` scenario can fail on this clause."),
    # #948: connector docs in the Admin listing. Two clauses that must hold together - the
    # merge happens, and it is ACL-trimmed - so one mutation each (the #788 lesson).
    dict(id="948-admin-drops-the-connector-plane", card="#948",
         path="src/dbsearch/server/app.py",
         guard="tests/selftest_948_connector_docs_in_admin.py",
         old="    if connector_docs:\n        seen = {r.get(\"doc_external_id\") for r in rows}",
         new="    if False and connector_docs:\n        seen = {r.get(\"doc_external_id\") for r in rows}",
         expect="caught",
         why="The pre-#948 behaviour: /admin/documents shows the upload plane only, so a caller "
             "whose only source is a connector sees an empty Admin while the node shows a count."),
    dict(id="948-merge-skips-the-acl-trim", card="#948",
         path="src/dbsearch/server/router_api.py",
         guard="tests/selftest_948_connector_docs_in_admin.py",
         old="                docs = lister(store.authorize(scope.principal))",
         new="                docs = lister(store.authorize(\"\"))",
         expect="caught",
         why="Trimming to an empty principal is the LAW 2 breach this card's whole risk is "
             "about: it would publish every document in the store to a caller the store's own "
             "authorize() would refuse - bob would see the deal-team connector doc in Admin."),
    # #947: connector delete is destructive. Two clauses: purge does the work, and
    # delete_store wires it in. One mutation each.
    dict(id="947-purge-is-a-noop", card="#947",
         path="src/dbsearch/router/providers/connector.py",
         guard="tests/selftest_947_delete_purges_connector_data.py",
         old="        pipe = self._pipes.get(store_id)\n        if pipe is not None:",
         new="        return False\n        pipe = self._pipes.get(store_id)\n        if pipe is not None:",
         expect="caught",
         why="The pre-#947 non-destructive behaviour: purge does nothing, so the chunks and "
             "the built store survive and a re-add reuses the stale index (#944's residual) "
             "instead of re-crawling the changed folder."),
    dict(id="947-delete-skips-the-purge", card="#947",
         path="src/dbsearch/server/router_api.py",
         guard="tests/selftest_947_delete_purges_connector_data.py",
         old="            provider = st.connector_source(store_id) if hasattr(st, \"connector_source\") else None",
         new="            provider = None",
         expect="caught",
         why="Delete removes the store from the catalog but never purges its data: the "
             "endpoint's re-add then serves the pre-delete content (doc_count stays 3, not 5), "
             "which is the exact 'deleted that isn't' the card was opened for."),
    # #949: a gated brand row reveals its services. Two clauses: the services are listed
    # (not hidden), and none is addable (the #823 property that must survive). One each.
    dict(id="949-gated-row-hides-its-services-again", card="#949", path=CANVAS,
         guard="tests/selftest_949_brand_rows_reveal_services.py",
         old="    provmenu.innerHTML='<div class=\"mh\">'+esc(p.label)+'</div>'+banner+kinds.map(k=>{",
         new="    if(gate){ provmenu.innerHTML='<div class=\"mh\">'+esc(p.label)+'</div>'+banner; provmenu.classList.add('show'); const _b=provmenu.querySelector('.gate-banner button.gate-cta'); if(_b) _b.onclick=()=>{closeProvMenu();}; placeProvMenu(row); return; }\n    provmenu.innerHTML='<div class=\"mh\">'+esc(p.label)+'</div>'+banner+kinds.map(k=>{",
         expect="caught",
         why="The pre-#949 (#823) behaviour restored: a fully-gated row renders only the connect "
             "banner and hides its services, so the owner's whole point - showing a user what "
             "Azure/Google/AWS can do before they connect - is lost."),
    dict(id="949-revealed-tiles-become-addable", card="#949", path=CANVAS,
         guard="tests/selftest_949_brand_rows_reveal_services.py",
         old="      const d=KINDS[k], g=kindGate(k);\n      const sub = !g ? d.cap : (gate ? d.cap : esc(g.cta||g.msg));",
         new="      const d=KINDS[k], g=null;\n      const sub = !g ? d.cap : (gate ? d.cap : esc(g.cta||g.msg));",
         expect="caught",
         why="Revealing the services must not make them ADDABLE to an unlinked caller - that is "
             "the #551/#823 always-403 tile the reveal was careful to avoid. Dropping the per-tile "
             "gate would let a click add an Azure node that can only fail."),
    dict(id="949-flyout-width-left-to-its-content", card="#949", path=CANVAS_CSS,
         guard="tests/selftest_949_brand_rows_reveal_services.py",
         old=".canvas-surface .provmenu {position:fixed;z-index:60;min-width:190px;max-width:300px;",
         new=".canvas-surface .provmenu {position:fixed;z-index:60;min-width:190px;",
         expect="caught",
         why="The pixel defect the owner caught on prod within minutes of the #949 ship, restored "
             "exactly: with no cap on the MENU, the banner's max-width:none let one long line of "
             "prose stretch the flyout to ~800px and pull every service tile out with it. jsdom "
             "does no layout, so only a static guard on the parent's rule can see this."),
    # #950: the node's Upload files button must actually upload. Two clauses - it opens the
    # picker, and both buttons carry the same name - so one mutation each.
    dict(id="950-node-button-only-selects", card="#950", path=CANVAS,
         guard="tests/selftest_923_upload_node_lifecycle.py",
         old="    if(upBtn){ upBtn.addEventListener(\"click\",e=>{ e.stopPropagation();\n      selected=node.uid; renderAll(); openUploadPicker(); }); }",
         new="    if(upBtn){ upBtn.addEventListener(\"click\",e=>{ e.stopPropagation(); selected=node.uid; renderAll(); }); }",
         expect="caught",
         why="The defect as the owner met it, restored byte-for-byte from the pre-fix line: the "
             "button's whole behaviour is to select the node, and #923 already auto-selects it on "
             "add - so the panel is open already and the click does nothing visible. 'i click "
             "upload files, nth happens then im like huh?'"),
    dict(id="950-panel-grows-a-second-upload-button", card="#950", path=CANVAS,
         guard="tests/selftest_923_upload_node_lifecycle.py",
         old="      (all.length>8\n        ? '<div class=\"up-row\"><input type=\"text\" class=\"updoc-filter\"",
         new="      '<div class=\"up-row\" style=\"margin:8px 0\"><button class=\"btn primary updoc-add\" '+\n        'style=\"flex:1;justify-content:center\">Upload files</button></div>'+\n      (all.length>8\n        ? '<div class=\"up-row\"><input type=\"text\" class=\"updoc-filter\"",
         expect="caught",
         why="The panel's upload button restored - a SECOND 'Upload files' on screen at the same "
             "moment as the node's, which is the duplication the owner ruled out: 'ONE single "
             "upload files under canvas node is good enough. the right side just shows what files "
             "are present.' Note this mutation is the shape the code had BEFORE the removal, so a "
             "guard that only checked the node's button would stay green on it."),
    # #951: a mount that never hydrated must never write the row. Two clauses - the gate
    # exists, and a FAILED read stays unhydrated - so one mutation each.
    dict(id="951-unhydrated-mount-may-write-the-row", card="#951", path=CANVAS,
         guard="tests/selftest_951_unmount_before_hydrate.py",
         old="    if(!rowHydrated) return Promise.resolve(false);\n",
         new="",
         expect="caught",
         why="The prod data loss restored exactly: unmountCanvas flushes a row save before alive "
             "drops (#818), state=[] is set synchronously at wire-up, and lastRowSave is still "
             "null - so a surface torn down before GET /router/manifest lands PUTs stores:[] with "
             "keepalive and destroys the workspace. The owner's gdrive + sharepoint_link nodes "
             "vanished while Admin still listed their documents."),
    dict(id="951-a-failed-read-still-adopts", card="#951", path=CANVAS,
         guard="tests/selftest_951_unmount_before_hydrate.py",
         old="    }).catch(()=>{\n      // #951: the read FAILED (store outage, network). rowHydrated stays FALSE, so this mount\n      // renders from localStorage but may never write the row back - overwriting a row we\n      // could not read is how a transient outage becomes permanent data loss.\n      loadLiveUserFromLocal();\n    });",
         new="    }).catch(()=>{ rowHydrated=true; loadLiveUserFromLocal(); });",
         expect="survives",
         why="OPEN TAIL, deliberately expect=survives: marking a FAILED manifest read as hydrated "
             "lets a transient store outage overwrite a row nobody could read - the same data loss "
             "through a different door. The probe holds the read open rather than failing it, so "
             "no fixture drives this path yet. Flipping this to 'caught' is the commit that adds "
             "a read-failure scenario."),
    # #952: a failure inside an SSE body must become a terminal event, sanitized, and the
    # client must settle on every ending. Three clauses, one mutation each.
    dict(id="952-stream-dies-silently", card="#952",
         path="src/dbsearch/server/app.py",
         guard="tests/selftest_952_stream_error_event.py",
         old="            if type(exc).__name__ == \"RateLimitError\":\n                msg = (\"The model is rate-limited right now - wait a few seconds and ask \"\n                       \"again.\")\n            else:\n                msg = (\"Answer generation failed mid-stream. Your documents are fine - \"\n                       \"ask the question again.\")\n            yield f\"data: {json.dumps({'type': 'error', 'message': msg})}\\n\\n\"",
         new="            return",
         expect="caught",
         why="The pre-fix behaviour byte-for-byte: the exception is logged and the stream just "
             "ends - no token, no done, no error - and the Ask box shows typing dots forever. "
             "The owner: 'suddenly the chat cant type cos i think its processing'."),
    dict(id="952-provider-text-on-the-wire", card="#952",
         path="src/dbsearch/server/app.py",
         guard="tests/selftest_952_stream_error_event.py",
         old="                msg = (\"Answer generation failed mid-stream. Your documents are fine - \"\n                       \"ask the question again.\")",
         new="                msg = str(exc)",
         expect="caught",
         why="The obvious 'helpful' fix that leaks: forwarding the provider's own message puts "
             "the Groq org id, an upsell URL - and for other providers, api keys - into every "
             "reader's network tab (LAW 1). The real prod 429 carried all three shapes."),
    dict(id="952-client-swallows-the-abrupt-end", card="#952",
         path="src/dbsearch/server/static/js/api.js",
         guard="tests/selftest_952_stream_error_event.py",
         old="  if (!sawDone) throw new Error(\"the answer stream ended before the reply - ask again\");\n",
         new="",
         expect="caught",
         why="The wedge's client half: a stream that ends with neither done nor error resolves "
             "silently, onDone never fires, and the typing dots sit over a dead stream. The "
             "server now always sends a terminal event, but an infra-level cut (proxy, deploy "
             "mid-stream) still ends a body abruptly - the client owns its own honesty."),
    dict(id="953-node-id-by-count", card="#953", path=CANVAS,
         guard="tests/selftest_953_node_id_collision.py",
         old="    let idn=1; while(state.some(s=>s.id===kind+\"-\"+idn)) idn++;\n    let id=kind+\"-\"+idn;",
         new="    let id=kind+\"-\"+(state.filter(s=>s.kind===kind).length+1);",
         expect="caught",
         why="The pre-#953 allocation byte-for-byte: counting hands an add-after-delete a LIVE "
             "node's id (gdrive-2 twice on one canvas), welding two nodes to one server store - "
             "and #947's destructive delete then purges the other node's data. The id-recycling "
             "half of the owner's vanished-nodes incident."),
    # #550: the admin directory + source registry are scoped to the home tenant. Two clauses
    # (principals gate, sources gate) - one mutation each.
    dict(id="550-principals-not-scoped", card="#550", path="src/dbsearch/server/app.py",
         guard="tests/selftest_550_admin_metadata_tenant_scope.py",
         old="    if not _caller_owns_home_directory(request):\n        return {\"available\": False, \"principals\": [],\n                \"reason\": \"sign in with your organization account to resolve names here, \"\n                          \"or paste an oid \u2014 a personal account shares only with itself\"}\n",
         new="",
         expect="caught",
         why="The #550 leak restored: with the tenant gate gone, a solo account (a Google/email "
             "signup, acct:<oid>) or a foreign tenant enumerates the HOME org's directory - group "
             "and people NAMES incl. 'Global Administrator'. Metadata, not content, but the leak "
             "that most undercuts a permission-faithful product."),
    dict(id="550-sources-not-scoped", card="#550", path="src/dbsearch/server/app.py",
         guard="tests/selftest_550_admin_metadata_tenant_scope.py",
         old="    if not _caller_owns_home_directory(request):\n        return []\n    return [asdict(s) for s in _edition.admin_service.sources()]",
         new="    return [asdict(s) for s in _edition.admin_service.sources()]",
         expect="caught",
         why="A solo/foreign caller reads the deployment's source names + doc counts + tenant "
             "metadata - the sibling half of the same disclosure."),
    # #924: a SharePoint "Anyone with the link" folder with NO Microsoft identity. Thirteen
    # clauses, one mutation each (the #788 lesson: a fixture rescued by two clauses at once
    # proves neither). Every one is the shape the connector would have had if a rule from the
    # probe, or from gdrive.py's history (#767, #918), had been skipped.
    dict(id="924-root-read-off-the-link-not-the-redirect", card="#924", path=SPL_PATH, guard=SPL_GUARD,
         old="        self._root = root\n",
         new="        self._root = urllib.parse.urlparse(self._link).path\n",
         expect="caught",
         why="The load-bearing rule from the probe: a typed or derived path that is out of the "
             "badge's scope lists as 200 with an EMPTY value, so a store built on it syncs "
             "'successfully' with nothing in it. The only root is the one the 302 names."),
    dict(id="924-no-badge-carries-on", card="#924", path=SPL_PATH, guard=SPL_GUARD,
         old="        if not badge or resp.status_code not in (301, 302, 303, 307, 308):\n",
         new="        if resp.status_code not in (301, 302, 303, 307, 308):\n",
         expect="caught",
         why="A revoked / expired / org-only link also answers 302 - to a Microsoft sign-in, "
             "with no badge. Accepting the redirect without the badge is a crawl that either "
             "403s or builds an empty store; the reader must be told the LINK is the problem."),
    dict(id="924-badge-not-sent", card="#924", path=SPL_PATH, guard=SPL_GUARD,
         old='            url, headers={"Cookie": f"FedAuth={self._badge}",\n                          "Accept": "application/json;odata=nometadata"},\n',
         new='            url, headers={"Accept": "application/json;odata=nometadata"},\n',
         expect="caught",
         why="Measured on prod: `no_cookie status=403`. A REST call without the badge is refused "
             "on every route, so the guard that every call carries it is the whole mechanism."),
    dict(id="924-cursor-tie-skips", card="#924", path=SPL_PATH, guard=SPL_GUARD,
         old="                if cursor and stamp < cursor:\n",
         new="                if cursor and stamp <= cursor:\n",
         expect="caught",
         why="gdrive.py's rule for the same reason: no changes feed for an anonymous caller, so "
             "the cursor is the newest stamp seen and never regresses - a tie skipped is a "
             "document invisible to every future crawl."),
    dict(id="924-no-recursion", card="#924", path=SPL_PATH, guard=SPL_GUARD,
         old='                queue.append(f.get("ServerRelativeUrl") or f"{folder}/{f.get(\'Name\', \'\')}")\n',
         new="                pass\n",
         expect="caught",
         why="A crawl that never descends is a store that looks full and is not (s3.py's words)."),
    dict(id="924-first-page-only", card="#924", path=SPL_PATH, guard=SPL_GUARD,
         old='            url = body.get("odata.nextLink") or body.get("@odata.nextLink") or ""\n',
         new='            url = ""\n',
         expect="caught",
         why="The real API caps a page at 100 rows and continues via odata.nextLink; a crawl "
             "that stops at the first page silently drops the 101st document onward."),
    dict(id="924-failed-listing-returns-partial", card="#924", path=SPL_PATH, guard=SPL_GUARD,
         old='            if resp.status_code != 200:\n                raise RuntimeError(\n                    f"SharePoint listing failed ({resp.status_code}) for folder {folder!r}")\n',
         new='            if resp.status_code != 200:\n                return rows\n',
         expect="caught",
         why="A half-listed folder that reports success is the worst available failure shape: "
             "the cursor advances past documents that were never seen."),
    dict(id="924-403-believed-first-time", card="#924", path=SPL_PATH, guard=SPL_GUARD,
         old="        if resp.status_code == 403:\n            self._mint()\n            resp = self._get(url)\n            if resp.status_code == 403:\n",
         new="        if resp.status_code == 403:\n",
         expect="caught",
         why="An expired badge mid-crawl answers 403 exactly like a per-file restriction. "
             "Believing the first 403 turns an expiry into a silently-partial store, counted as "
             "'unreadable' with the cursor already past every skipped item (#767's data loss)."),
    dict(id="924-5xx-counted-unreadable", card="#924", path=SPL_PATH, guard=SPL_GUARD,
         old='            raise RuntimeError(\n                f"SharePoint download failed ({resp.status_code}) for {title!r} - failing the "\n',
         new='            raise ItemUnreadable(\n                f"SharePoint download failed ({resp.status_code}) for {title!r} - failing the "\n',
         expect="caught",
         why="#767's asymmetry: a transient 429/5xx on the skip-and-count path is never re-listed "
             "(the cursor advanced at listing time). A wrongly-failed crawl retries; a "
             "wrongly-skipped document is gone."),
    dict(id="924-empty-link-accuses-the-reader", card="#924", path=SPL_PATH, guard=SPL_GUARD,
         old='    if not s:\n        raise ValueError(\n            "no sharing link yet - open this source and paste the folder\'s share link "\n            "(in SharePoint or OneDrive: Share -> Anyone with the link -> Copy link)")\n',
         new="",
         expect="caught",
         why="#918 verbatim: an EMPTY link falling through to the malformed branch tells a reader "
             "who typed nothing that what they typed does not look like a link."),
    dict(id="924-file-link-accepted", card="#924", path=SPL_PATH, guard=SPL_GUARD,
         old='    if kind != "f":\n',
         new='    if False:\n',
         expect="caught",
         why="A :b:/:w:/:x: link shares ONE file; its redirect names no folder, so the failure "
             "would surface later as a confusing 'did not name a folder' at Test connection "
             "instead of 'share the folder instead' at paste time."),
    dict(id="924-empty-acl-composes", card="#924", path=CONNECTOR_PATH, guard=SPL_GUARD,
         old='    if not acl:\n        raise ValueError(\n            "a sharepoint_link store needs an audience: no acl is set, so every ingested "\n            "document would be visible to nobody. Set \'Who can see this store\' before composing")\n',
         new="",
         expect="caught",
         why="#673's rule: a store with no audience ingests documents nobody can see (#200), and "
             "composes green while doing it."),
    dict(id="924-ui-demands-microsoft", card="#924", path=CANVAS, guard=SPL_GUARD,
         old='    sharepoint_link:{label:"SharePoint link", mono:"SPL", cap:"documents",',
         new='    sharepoint_link:{label:"SharePoint link", mono:"SPL", cap:"documents", needs:"entra",',
         expect="caught",
         why="The gate lying about its own requirement (#920's defect): this kind reads an "
             "anonymous link with no Microsoft identity, so a Connect-your-Microsoft-account "
             "tile would shut the door on exactly the user it exists for."),
]


def _sh(argv, cwd, env=None):
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                          env=env or os.environ.copy())


def _workspace(dest: Path) -> None:
    """HEAD, exactly as a clean clone has it, plus the one thing a clean clone lacks.

    The symlink is deliberate and load-bearing: without site/node_modules every DOM guard
    would fail for a reason that is not the mutation, and the matrix would report a wall of
    CAUGHT while proving nothing. See the control run.
    """
    dest.mkdir(parents=True, exist_ok=True)
    tar = subprocess.run(["git", "archive", "HEAD"], cwd=str(ROOT), capture_output=True)
    if tar.returncode != 0:
        sys.exit(f"git archive failed: {tar.stderr.decode(errors='replace')}")
    if subprocess.run(["tar", "-x", "-C", str(dest)], input=tar.stdout).returncode != 0:
        sys.exit("could not unpack the archive")
    src_nm = ROOT / "tests/node_modules"
    if src_nm.exists():
        os.symlink(src_nm, dest / "tests/node_modules")


def _run_guard(ws: Path, guard: str) -> tuple[bool, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ws / "src")
    # An opt-out here would let a missing jsdom read as a pass and quietly hollow out the whole
    # matrix, which is the #792 defect wearing a different hat.
    env.pop("DBSEARCH_ALLOW_DOM_SKIP", None)
    r = _sh([sys.executable, guard], ws, env)
    tail = (r.stderr or r.stdout or "").strip().splitlines()
    return r.returncode == 0, (tail[-1] if tail else f"exit {r.returncode}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-k", metavar="SUBSTRING", default="",
                    help="only entries whose id or card contains SUBSTRING")
    ap.add_argument("--list", action="store_true", help="print the matrix and exit")
    ap.add_argument("--keep", action="store_true", help="keep the scratch tree for inspection")
    args = ap.parse_args()

    picked = [m for m in MUTATIONS if args.k in m["id"] or args.k in m["card"]]
    if args.list:
        for m in picked:
            print(f"{m['card']:<6} {m['id']:<28} expect={m['expect']:<9} {m['guard']}")
        return 0
    if not picked:
        sys.exit(f"no matrix entry matches -k {args.k!r}")

    ws = Path(tempfile.mkdtemp(prefix="dbsearch-mutate-"))
    print(f"workspace: {ws}")
    _workspace(ws)

    # ---- CONTROL. Without this the whole run is unfalsifiable. ------------------------------
    print("\ncontrol (unmutated, every guard must be green):")
    for guard in sorted({m["guard"] for m in picked}):
        ok, detail = _run_guard(ws, guard)
        print(f"  {'ok  ' if ok else 'RED '} {guard}")
        if not ok:
            print(f"\nABORTED: {guard} is not green BEFORE any mutation ({detail}).\n"
                  f"Every result below would read CAUGHT for a reason that is not the mutation.\n"
                  f"If jsdom is missing, run `npm ci --prefix tests` first.")
            return 2

    pristine = {p: (ws / p).read_text(encoding="utf-8")
                for p in sorted({m["path"] for m in picked})}

    print("\nmatrix:")
    rows, surprises = [], []
    for m in picked:
        target = ws / m["path"]
        src = pristine[m["path"]]
        hits = src.count(m["old"])
        if hits != 1:
            rows.append((m, "UNANCHORED", f"the `old` text appears {hits} times, expected 1"))
            surprises.append(m)
            print(f"  UNANCHORED {m['card']:<6} {m['id']:<28} (appears {hits}x - fix the entry)")
            continue
        try:
            target.write_text(src.replace(m["old"], m["new"], 1), encoding="utf-8")
            green, detail = _run_guard(ws, m["guard"])
        finally:
            target.write_text(src, encoding="utf-8")     # always restore, even on Ctrl-C
        got = "survives" if green else "caught"
        mark = "CAUGHT  " if got == "caught" else "SURVIVED"
        surprise = got != m["expect"]
        if surprise:
            surprises.append(m)
        print(f"  {mark} {m['card']:<6} {m['id']:<28} "
              f"{'<-- SURPRISE, expected ' + m['expect'] if surprise else ''}")
        rows.append((m, got, detail))

    caught = sum(1 for _, got, _ in rows if got == "caught")
    print(f"\n{'=' * 70}")
    print(f"{caught}/{len(rows)} mutations caught")
    open_defects = [m for m, got, _ in rows if got == "survives" and m["expect"] == "survives"]
    if open_defects:
        print(f"{len(open_defects)} still surviving BY DECLARATION (open cards): "
              f"{', '.join(sorted({m['card'] for m in open_defects}))}")
    if surprises:
        print(f"\nSURPRISES ({len(surprises)}) - a guard changed behaviour without its card:")
        for m in surprises:
            print(f"  {m['card']} {m['id']}: expected {m['expect']}")
        print("\nIf you just FIXED one of these, flip its `expect` in the same commit.")
    if not args.keep:
        shutil.rmtree(ws, ignore_errors=True)
    else:
        print(f"\nkept: {ws}")
    return 1 if surprises else 0


if __name__ == "__main__":
    sys.exit(main())
