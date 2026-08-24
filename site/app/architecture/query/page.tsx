import Link from "next/link";
import type { Metadata } from "next";
import { ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SectionHeading } from "@/components/sections/section-heading";
import { CtaBand } from "@/components/sections/cta-band";
import { ArchToc } from "@/components/arch-toc";
import { ChapterTabs } from "@/components/chapter-tabs";
import { START_URL } from "@/lib/nav";
import { FileLink } from "@/components/file-link";

export const metadata: Metadata = {
  title: "Life of a query - DBSearch.AI",
  description:
    "Chapter two of the architecture: one question followed end to end through the router - identity, visibility, classification, embedding, profile matching, selection, budgeted dispatch, the SQL rail's honesty ladder, and synthesis - quoted straight from the codebase.",
};

/*
 * Every snippet below is quoted VERBATIM from the codebase (including the
 * original comments), so that when the repository goes public these blocks
 * can become links to source without a single character changing. Do not
 * paraphrase them to fit house copy style.
 */

const SNIPPET_IDENTITY = `def resolve_identity(get_header: HeaderGetter,
                     get_cookie: "CookieGetter | None" = None) -> str:
    """Return the trusted user oid for this request, or raise AuthError.

    THE identity chokepoint (#184): REST and GraphQL both call exactly this, so they cannot
    drift apart. Precedence: verified session cookie -> \`Bearer dbk_...\` API key -> dev
    header / verified bearer jwt.`;

const SNIPPET_ROUTE_ENTRY = `def route(self, user_oid: str, question: str,
          store_override: str | None = None) -> RoutingDecision:
    principals = self._identity.expand_groups(user_oid)
    # E8 cache: keyed by the PRINCIPAL SET (not the oid — same visibility, same
    # decision) + catalog revision, so recompose/registration invalidates.
    key = (tuple(sorted(principals)), question, store_override or "",
           self._catalog.revision)`;

const SNIPPET_VISIBILITY = `# --- gate #1: hereditary catalog visibility trim ---
def _path_visible(self, node: CatalogNode, principals: set[str]) -> bool:
    cur: CatalogNode | None = node
    seen: set[str] = set()
    while cur is not None:
        if cur.id in seen:
            return False   # #114 defense in depth: a cyclic path DENIES, never spins
        seen.add(cur.id)
        if not (set(cur.acl) & principals):
            return False
        cur = self._nodes.get(cur.parent_id) if cur.parent_id else None
    return True

def visible_stores(self, principals: list[str]) -> list[CatalogNode]:
    pset = set(principals)
    return [n for n in self.stores() if self._path_visible(n, pset)]`;

const SNIPPET_NOT_VISIBLE = `empty_catalog = not self._catalog.stores()
return RoutingDecision(
    query_type=query_type, method="fallback",
    reason=("no store is composed yet" if empty_catalog
            else "no accessible store for this user"))`;

const SNIPPET_CLASSIFY = `_ANALYTICAL = re.compile(
    r"\\b(how many|count|number of|total|sum|average|avg|mean|median|"
    r"trend|growth|per (month|quarter|year|region|unit)|by (month|quarter|year|region|unit))\\b",
    re.I,
)
_EXACT = re.compile(r"(#\\s*\\d+|\\b(id|invoice|record|ticket|order|employee)\\b[^?]*\\b\\d+\\b)", re.I)
_COMPOUND = re.compile(r"\\b(versus|vs\\.?|compare|compared to)\\b|\\b\\w+\\s+and\\s+\\w+.*\\?", re.I)


def classify_query(question: str) -> str:
    q = question.strip()
    if _COMPOUND.search(q):
        return "compound"
    if _EXACT.search(q):
        return "exact"
    if _ANALYTICAL.search(q):
        return "analytical"
    return "semantic"`;

const SNIPPET_RESCUE = `else:
    # #134 under-trigger rescue: keyword dominance (e.g. "total ... amount")
    # can mislabel an "and"-joined question, silently dropping the other
    # half. If the decomposer finds a clean split AND the halves route to
    # DIFFERENT stores, the question genuinely spans stores — treat it as
    # compound. Same-store splits (idioms like "terms and conditions") fall
    # through to plain single routing.
    subs = self._decomposer(question)
    if len(subs) >= 2:
        compound = self._route_compound(principals, subs)
        routed_ids = {sq.decision.stores[0].store_id
                      for sq in compound.sub_queries if sq.decision.stores}
        if len(routed_ids) >= 2:
            return compound`;

const SNIPPET_EMBED = `qv = self._embedder.embed([question])[0]                # the question becomes ONE vector,
                                                        # same embedder the profiles used
nodes = visible                                         # pool = visibility-trimmed catalog
                                                        # (LAW 2: unseen stores never enter)
bus = {n.profile.business_unit for n in visible if n.profile is not None}
if len(bus) >= self._coarse_min_bus:                    # E8 coarse→fine: at scale (6+ BUs),
    nodes = coarse_prune(qv, visible, self._embedder, self._coarse_floor_frac)
                                                        # drop whole BUs by their BEST store
candidates = score_stores(qv, nodes, self._embedder, question)
                                                        # fine pass: one cosine per survivor`;

const SNIPPET_PROFILE = `def profile_text(profile: StoreProfile) -> str:
    parts = [profile.title, profile.description, profile.business_unit, *profile.topics]
    parts.extend(_schema_terms(profile.schema))          # #296: structure routes an undescribed store
    return " ".join(p for p in parts if p)


def ensure_profile_vector(profile: StoreProfile, embedder: EmbeddingPort) -> list[float]:
    if profile.profile_vector is None:
        profile.profile_vector = embedder.embed([profile_text(profile)])[0]
    return profile.profile_vector`;

const SNIPPET_WARM = `# E8: warm every store's profile vector now — user-independent, and the first
# real query shouldn't pay the whole catalog's embedding cost.
for n in catalog.stores():
    if n.profile is not None:
        ensure_profile_vector(n.profile, embedder)`;

const SNIPPET_COARSE = `#321: this scored a BU by the CENTROID (mean) of its store vectors, which dilutes a
single strong match with unrelated siblings. The demo's \`sales\` BU (deals + storefront
+ warehouse) lost its one revenue+product store to the gate, so a 'revenue per product'
question fell to a finance DOCUMENT holding only a single total. The coarse gate's
question is 'could ANY store in this BU answer?' — a MAX over stores, not a mean. Max
also prunes strictly less, which is the safe direction (a BU with any relevant store
survives; a BU where nothing matches is still dropped)."""`;

const SNIPPET_SELECTOR = `pool = relative_floor(scored, floor_frac)
if not pool:
    return [], "fallback", 0.0

conf = pool[0].score

# dominant single winner — no LLM needed
if len(pool) == 1 or (pool[0].score - pool[1].score) >= margin:
    return [pool[0]], "prefilter", conf

capped = pool[:fanout_cap]

if tiebreak is not None:
    by_id = {s.store_id: s for s in capped}
    picked_ids = [sid for sid in tiebreak([s.store_id for s in capped]) if sid in by_id]
    if picked_ids:
        return [by_id[sid] for sid in picked_ids], "llm", conf

return capped, "prefilter", conf`;

const SNIPPET_NARROW = `# #288: a prefilter fan-out whose question DISTINCTIVELY names one tied store is scoped
# to it (embedding ties on shared tokens like "amount"/"region"; the distinctive token,
# e.g. "deal"/"order", disambiguates). A true cross-store ask keeps its fan-out.
if method == "prefilter" and len(selected) > 1:
    narrowed = distinctive_narrow(question, selected, nodes)
    if len(narrowed) < len(selected):
        selected = narrowed`;

const SNIPPET_OUTCOMES = `OK = "ok"
EMPTY = "empty"
ERROR = "error"
TIMEOUT = "timeout"
BUDGET = "budget"    # E8: not dispatched — the query's dispatch ceiling was reached
DECLINED = "declined"   # #211: the store was asked, and it does NOT hold this kind of data.
# NOT the same as EMPTY. EMPTY means "I looked and found no matching rows"; DECLINED means "this
# question is not about anything I hold." Conflating them is how the fabrication happened: asked
# for support tickets, a sales database counted order lines and CALLED them support tickets.`;

const SNIPPET_ISOLATION = `try:
    evidence = fut.result(timeout=remaining)
except _FutureTimeout:
    report.outcomes.append(StoreOutcome(routed.store_id, routed.business_unit,
                                        TIMEOUT))
    continue
except CannotAnswerFromSchema as exc:
    # #211: NOT an error — the store is healthy and did the honest thing. It simply
    # does not hold this kind of data, and said so instead of inventing a column.
    report.outcomes.append(StoreOutcome(routed.store_id, routed.business_unit,
                                        DECLINED, error=str(exc)))
    continue
except Exception as exc:  # noqa: BLE001 — any store fault is a drop, never fatal
    report.outcomes.append(StoreOutcome(routed.store_id, routed.business_unit,
                                        ERROR, error=f"{type(exc).__name__}: {exc}"))
    continue`;

const SNIPPET_VALIDATE_SQL = `def validate_sql(sql: str, visible_tables: list) -> None:
    """Read-only, single-statement, visible-schema-only guard. Raises ValueError."""
    s = sql.strip()
    if "--" in s or "/*" in s:
        raise ValueError("SQL comments are not allowed")
    if ";" in s.rstrip(";"):
        raise ValueError("multiple SQL statements are not allowed")
    first = s.split(None, 1)[0].lower() if s else ""
    if first not in ("select", "with"):
        raise ValueError("only SELECT/WITH queries are allowed")
    if _FORBIDDEN.search(s):
        raise ValueError("write/DDL/introspection keywords are not allowed")
    allowed = {_norm_table(t) for t in visible_tables}
    allowed |= {m.group(1).lower() for m in _CTE_NAME.finditer(s)}
    for m in _TABLE_REF.finditer(s):
        if _norm_table(m.group(1)) not in allowed:
            raise ValueError(f"table {m.group(1)!r} is not in the visible schema")`;

const SNIPPET_CANNOT_ANSWER = `"The one thing you must NEVER do is answer with a DIFFERENT THING than the one asked for. "
"Do not count or alias an unrelated measure to stand in for a missing one (e.g. counting "
"sales order lines and calling them support tickets), do not substitute the 'closest' "
"column for an entity that simply is not here, and do not fall back to a broad SELECT. "
"ONLY when the thing asked about has no counterpart at all in this schema, output exactly "
"CANNOT_ANSWER and nothing else. A confident wrong answer is far worse than none — another "
"database in the federation usually holds what was asked for, and one fabricated column "
"poisons the whole federated result."`;

const SNIPPET_ACCESS = `def access_for(self, user_oid: str, store_id: str) -> AccessContext:
    principals = self._identity.expand_groups(user_oid)
    delegation = self._delegations.get(store_id)
    if delegation is not None:
        exchange, resource = delegation
        return AccessContext(user_oid=user_oid, principals=principals,
                             delegated_credential=exchange.exchange(user_oid, resource))
    policy = self._row_policies.get(store_id)
    if policy is not None:
        return AccessContext(user_oid=user_oid, principals=principals,
                             row_policy=policy(user_oid, principals))
    return AccessContext(user_oid=user_oid, principals=principals)`;

const SNIPPET_DICTIONARY = `def resolve_literal(written: str, candidates: list, embedder=None,
                    llm=None) -> "str | None":
    """Map the user's wording onto one stored value, or None to decline.

    The ladder, first hit wins: exact, case-fold, normalized, then - only with a dense
    embedder - nearest by cosine subject to MIN_SIMILARITY and MIN_MARGIN, and finally -
    only with an IN-TENANT model - one LLM disambiguation call over the shortlist. None
    at every step means "I could not tell", which the caller turns into an honest
    decline; it never means "no rows"."""`;

const SNIPPET_MERGE = `def merge_evidence(per_store: list[list[Evidence]], cap: int = 12) -> list[Evidence]:
    """Round-robin by per-store rank: every store's #1 outranks any store's #2."""
    merged: list[Evidence] = []
    rank = 0
    while len(merged) < cap:
        row = [lst[rank] for lst in per_store if rank < len(lst)]
        if not row:
            break
        merged.extend(row)
        rank += 1
    return merged[:cap]`;

const SNIPPET_NO_ANSWER = `if not decision.stores:
    reason = (decision.reason or "").lower()
    if "composed" in reason:                 # catalog genuinely empty - safe to say so
        return NOT_COMPOSED_ANSWER
    return NO_EVIDENCE_ANSWER                # invisible-or-nonexistent: stay generic (LAW 2)
if outcomes and all(o.status == DECLINED for o in outcomes):
    return DECLINED_ANSWER                   # #211: healthy store, honestly holds no such data
if outcomes and all(o.status in (ERROR, TIMEOUT) for o in outcomes):
    return FAILED_ANSWER                     # the disclosure names which store and why
if outcomes and any(o.status in (OK, EMPTY) for o in outcomes):
    return EMPTY_RESULT_ANSWER               # it ran, it just matched nothing
return NO_EVIDENCE_ANSWER`;

/* The overview map: one row per step, each anchoring its section. */
const STEPS = [
  {
    id: "step-0",
    num: "0",
    title: "Identity",
    line: "A verified token becomes a principal set - the user plus every group they belong to.",
  },
  {
    id: "step-1",
    num: "1",
    title: "Visibility",
    line: "The catalog is trimmed to stores this caller may know exist. Invisible equals nonexistent.",
  },
  {
    id: "step-2",
    num: "2",
    title: "Classify",
    line: "Three regexes label the question's shape. The classifier is allowed to be wrong - both directions are rescued.",
  },
  {
    id: "step-3",
    num: "3",
    title: "Embed",
    line: "The question becomes one vector, using the same embedder the store profiles were built with.",
  },
  {
    id: "step-4",
    num: "4",
    title: "Match",
    line: "One cosine per visible store against profile vectors warmed at startup. No model on the happy path.",
  },
  {
    id: "step-5",
    num: "5",
    title: "Select",
    line: "A clear winner routes free; a tie fans out to at most three stores, then cheap rescues narrow it.",
  },
  {
    id: "step-6",
    num: "6",
    title: "Dispatch",
    line: "Selected stores run under a disclosed budget. One failing store is dropped, never the whole query.",
  },
  {
    id: "step-7",
    num: "7",
    title: "The SQL rail",
    line: "Schema in, SQL out, validated, executed as the user in your own engine - then an honesty ladder for misses.",
  },
  {
    id: "step-8",
    num: "8",
    title: "Synthesis",
    line: "Evidence merges by rank, never raw score. Every claim carries a citation and a typed proof.",
  },
] as const;

/* Rail data: grouped so the collapsed strip stays readable. */
const QUERY_TOC = [
  {
    id: "map",
    label: "Map",
    children: [
      { id: "map", label: "The map" },
      { id: "timelines", label: "Two timelines" },
    ],
  },
  {
    id: "step-0",
    label: "Gate",
    children: [
      { id: "step-0", label: "Identity" },
      { id: "step-1", label: "Visibility" },
      { id: "step-2", label: "Classify" },
    ],
  },
  {
    id: "step-3",
    label: "Route",
    children: [
      { id: "step-3", label: "Embed" },
      { id: "step-4", label: "Match" },
      { id: "step-5", label: "Select" },
    ],
  },
  {
    id: "step-6",
    label: "Run",
    children: [
      { id: "step-6", label: "Dispatch" },
      { id: "step-7", label: "SQL rail" },
    ],
  },
  {
    id: "step-8",
    label: "Answer",
    children: [
      { id: "step-8", label: "Synthesis" },
      { id: "invariants", label: "What moves" },
    ],
  },
] as const;

const INGEST_SIDE = [
  "Documents are chunked, and every chunk is stamped with the source system's ACL.",
  "Every composed store gets a profile vector - an embedding of its title, description, business unit, topics and schema vocabulary.",
  "SQL stores build a value dictionary of stored text values, and wide schemas get a table-level index.",
] as const;

const QUERY_SIDE = [
  "The nine steps on this page. Nothing here re-reads a source system's full contents.",
  "Routing compares one fresh question vector against profile vectors that already exist.",
  "Retrieval compares the caller's principals against ACLs that were stamped long before the question.",
] as const;

const FROZEN = [
  "The step order itself, and the security laws: the model never sees stored data values, and an invisible store is indistinguishable from a nonexistent one.",
  "Containment guarantees: a tiebreaker can only narrow an already-scored, already-visible pool; the lexical rescue never widens a fan-out; rerank layers only remove.",
  "Disclosure: budget caps, dropped stores, repaired literals and declined questions are all said out loud, never silent.",
] as const;

const CALIBRATION = [
  "The numbers: the 0.6 relative floor, the 0.15 margin, fan-out capped at three, a dispatch budget of eight, twelve merged evidence items, a sixty-second decision cache.",
  "These are settings, not commitments - each is a named constructor argument, and changing one is a config edit, not a redesign.",
] as const;

const EVOLVING = [
  "Which in-tenant model serves each rail, chosen by measured bake-offs rather than defaults.",
  "Chunking shape and the selection-quality heuristics - the current frontier of tuning.",
  "Every such change is gated by a fixed regression pack of routed questions, so a tweak that hurts routing is caught in minutes.",
] as const;

/* Per-step flow diagrams: the mechanism drawn as text, kept under ~66
 * columns so phones scroll the figure, never the page. */
const FLOW_0 = `request arrives
   │  Authorization header / session cookie - the TRANSPORT
   │  any "user" field in the request BODY is ignored:
   │  you cannot impersonate by sending "user": "alice"
   ▼
resolve_identity()        one function, every entry point
   ▼
user_oid  ("bob")
   │  expand_groups(user_oid)
   ▼
PRINCIPAL SET  ["bob", "grp-sales", "grp-emea", ...]
   the user + ALL transitive groups - computed ONCE,
   reused by step 1 (visibility) and step 7 (credentials)`;

const FLOW_1 = `all composed stores
   │  keep a store only if the caller's principals
   │  intersect its ACL - and every ancestor's ACL too
   ▼
VISIBLE CATALOG    the only stores steps 2-8 ever see
   │
   ├─ catalog truly empty      → "no store is composed yet"
   └─ stores exist, none visible → one generic reply
      (an invisible store must read as nonexistent)`;

const FLOW_2 = `question
   ▼
classify_query()   three regexes, priority order
   ├─ compound?    "versus / compare / X and Y ...?"
   ├─ exact?       "invoice #4471"
   ├─ analytical?  "how many / total / average by region"
   └─ else         → semantic (the default)

the label is only a guess. the real check: split the
question in two and see where the halves ROUTE.

  guess says compound:  "terms and conditions?"
     split → "terms" / "conditions"
     both halves route to the SAME store
     → it was ONE question → route it whole

  guess says single:  "total revenue and the refund policy"
     trial split → "total revenue" / "the refund policy"
     halves route to TWO different stores
     → it really was TWO questions → treat as compound`;

const FLOW_3 = `question ── embed ONCE ──▶ query vector
   self-host:  nomic-embed-text     768 dims (in-tenant)
   Azure:      text-embedding-3-small  1536 dims
   demo:       hashing embedder    4096 dims
   the SAME embedder that built the store profiles -
   otherwise the cosine in step 4 compares apples to pears`;

const FLOW_4 = `STARTUP (once, no user involved)   QUERY TIME (per question)
──────────────────────────────     ─────────────────────────
store profile text                 cosine(query vector,
  title + description + BU           profile vector)
  + topics + schema vocab            = ONE number per
      │  embed                       visible store
      ▼                                  │
profile VECTORS, warmed ──feed──────────▶│
                                         ▼
                                   ranked candidates
6+ business units? coarse pass first:
   score each unit by its BEST store (max, not mean),
   prune losing units, fine-score the survivors`;

const FLOW_5 = `ranked candidates   [0.82, 0.79, 0.31, 0.28]
   │  ① relative floor: drop < 0.6 x best → [0.82, 0.79]
   ▼
   ② margin test: leader ahead by ≥ 0.15?
   ├─ YES → ONE store, zero model cost   (most queries)
   └─ NO  → fan out, capped at 3 stores
        │  ③ tiebreak seam (unused in prod): may only
        │     pick FROM the pool, never outside it
        ▼
        ④ question distinctively names ONE tied store
           ("deal" vs "order")? → scope to that store`;

const FLOW_6 = `selected stores (all sub-queries pooled)
   │  ceiling: 8 dispatches - the overflow becomes a
   │  DISCLOSED "budget" outcome, never a silent skip
   ▼
run concurrently → exactly one outcome per store:
   ok · empty · declined · timeout · error · budget
   a failing store is dropped AND named in the answer;
   every other store's evidence still gets through`;

const FLOW_7 = `schema (wide? retrieve only the relevant tables)
   ▼
in-tenant model writes SQL     sees names + meanings,
   │                           NEVER data values (law 1);
   │                           may answer CANNOT_ANSWER
   ▼
validate_sql()                 read-only, one statement,
   │                           visible tables only
   ▼
EXECUTE AS THE USER            token exchanged for the
   │                           caller's own credential →
   │                           the engine's RLS applies
   ▼
miss? the honesty ladder:
   empty result  → probe: did a filter match nothing?
   wrong literal → value-dictionary repair → re-run,
                   substitution DISCLOSED
   still lost    → ONE bare-schema reprompt
   still lost    → honest decline, never a guess`;

const FLOW_8 = `evidence, ranked per store
   ▼
merge ROUND-ROBIN by rank      every store's #1 beats
   │  cap: 12 items            any store's #2 - raw scores
   ▼                           are never compared
answer + per-claim citation + typed proof
   row   → the SQL, the table, the row ids
   chunk → the document, its uri, a locator
   ▼
nothing survived? say WHY - five distinct replies:
   not composed · declined · failed · empty · no evidence`;

/* Real classifier cases, straight from tests/selftest_router_classify.py. */
const CLASSIFY_EXAMPLES = [
  {
    q: "HR attrition versus sales headcount growth",
    label: "compound",
    why: "comparison word",
  },
  {
    q: "compare AI revenue and the post-mortem findings",
    label: "compound",
    why: "two things joined",
  },
  { q: "show invoice #4471", label: "exact", why: "id-shaped number" },
  { q: "look up employee id 90210", label: "exact", why: "entity word + number" },
  {
    q: "What is the total revenue in Q3?",
    label: "analytical",
    why: "aggregate word",
  },
  {
    q: "average deal size by region",
    label: "analytical",
    why: "aggregate + group-by phrase",
  },
  {
    q: "what is our parental leave policy",
    label: "semantic",
    why: "no pattern - the default",
  },
  { q: "summarise the onboarding guide", label: "semantic", why: "default" },
] as const;

function Objective({
  children,
  example,
}: {
  children: React.ReactNode;
  example?: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-border p-5">
      <p className="text-micro font-mono text-fg-muted">Objective</p>
      <div className="mt-2 space-y-2 text-base leading-relaxed text-fg">
        {children}
      </div>
      {example && (
        <>
          <p className="mt-5 text-micro font-mono text-fg-muted">Example</p>
          <div className="mt-2 space-y-2 text-base leading-relaxed text-fg">
            {example}
          </div>
        </>
      )}
    </div>
  );
}

function FurtherExplanation({ children }: { children: React.ReactNode }) {
  return (
    <details className="group rounded-lg border border-border">
      <summary className="flex cursor-pointer list-none items-center justify-between px-5 py-3.5 font-mono text-micro text-fg-muted [&::-webkit-details-marker]:hidden">
        Further explanation
        <span
          aria-hidden
          className="transition-transform duration-200 group-open:rotate-90"
        >
          ›
        </span>
      </summary>
      <div className="space-y-4 border-t border-border px-5 pb-5 pt-4 text-base leading-relaxed text-fg">
        {children}
      </div>
    </details>
  );
}

function Flow({ chart }: { chart: string }) {
  return (
    // min-w-0: same guard as CodeRef - scroll inside the figure, not the page
    <figure className="min-w-0 max-w-full overflow-hidden rounded-lg border border-border bg-surface">
      <figcaption className="border-b border-border px-4 py-2.5 font-mono text-xs text-fg">
        the flow
      </figcaption>
      <pre className="overflow-x-auto p-4 font-mono text-[13px] leading-relaxed text-fg">
        {chart}
      </pre>
    </figure>
  );
}

function CodeRef({
  file,
  code,
  note,
}: {
  file: string;
  code: string;
  note?: string;
}) {
  return (
    // min-w-0: inside a flex/grid item the pre's min-content width would
    // otherwise widen the column past the viewport instead of scrolling
    <figure className="min-w-0 max-w-full overflow-hidden rounded-lg border border-border bg-surface">
      <figcaption className="border-b border-border px-4 py-2.5 font-mono text-xs text-fg">
        <FileLink file={file} />
      </figcaption>
      <pre className="overflow-x-auto p-4 font-mono text-[13px] leading-relaxed text-fg-muted">
        <code>{code}</code>
      </pre>
      {note && (
        <p className="border-t border-border px-4 py-2.5 text-xs text-fg-muted">
          {note}
        </p>
      )}
    </figure>
  );
}

function Prose({ children }: { children: React.ReactNode }) {
  return <p className="text-base leading-relaxed text-fg-muted">{children}</p>;
}

function Strong({ children }: { children: React.ReactNode }) {
  return <span className="font-medium text-fg">{children}</span>;
}

export default function QueryPage() {
  return (
    <main>
      <ArchToc items={QUERY_TOC} />

      {/* Hero */}
      <section className="vault-grid border-b border-border">
        <div className="mx-auto max-w-3xl px-6 py-20 text-center lg:py-28">
          <p className="text-micro font-mono text-fg-muted">Architecture</p>
          <div className="flex justify-center">
            <ChapterTabs active="query" />
          </div>
          <h1 className="mt-6 font-display text-display-2 text-fg">
            The life of a query
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-lg text-fg-muted">
            Chapter one walked the laws - the invariants the codebase must
            never break. This page follows one question through the machinery
            that obeys them: nine steps from a signed-in user to a cited,
            provable answer, each quoted from the actual code. The laws are
            the walls; this is what runs inside them.
          </p>
          <div className="mt-8 flex flex-wrap items-center justify-center gap-4">
            <Button asChild variant="pill">
              <Link href="/demo">Book a demo</Link>
            </Button>
            <Button asChild variant="quiet">
              <a href={START_URL}>Self-host free</a>
            </Button>
          </div>
          <nav aria-label="The steps" className="mt-10">
            <ul className="flex flex-wrap items-center justify-center gap-2">
              {STEPS.map(({ id, num, title }) => (
                <li key={id}>
                  <a
                    href={`#${id}`}
                    className="inline-block rounded-full border border-border px-3 py-1.5 font-mono text-xs text-fg-muted transition-colors hover:border-fg hover:text-fg"
                  >
                    {num} · {title}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        </div>
      </section>

      {/* The map */}
      <section id="map" className="mx-auto max-w-6xl scroll-mt-20 px-6 py-24">
        <SectionHeading
          kicker="The map"
          title="Nine steps, cheapest first"
          sub="Every step exists to answer one question as cheaply as it can be answered, so the expensive machinery only runs when the cheap machinery could not decide."
        />
        <ol className="mx-auto mt-16 max-w-3xl">
          {STEPS.map(({ id, num, title, line }) => (
            <li key={id} className="border-b border-border first:border-t">
              <a
                href={`#${id}`}
                className="group flex items-baseline gap-5 px-2 py-4 transition-colors hover:bg-surface"
              >
                <span className="w-6 shrink-0 text-right font-mono text-sm text-fg-muted">
                  {num}
                </span>
                <span className="w-24 shrink-0 font-medium text-fg">
                  {title}
                </span>
                <span className="min-w-0 text-sm text-fg-muted">{line}</span>
                <ArrowRight
                  className="ml-auto h-3.5 w-3.5 shrink-0 self-center text-fg-muted opacity-0 transition-opacity group-hover:opacity-100"
                  aria-hidden="true"
                />
              </a>
            </li>
          ))}
        </ol>
      </section>

      {/* Two timelines */}
      <section id="timelines" className="scroll-mt-20 border-y border-border bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <SectionHeading
            kicker="Before the steps"
            title="Two timelines, and why the query is fast"
            sub="Half of what makes a query work happened long before the question was asked. Separating the two timelines is the single most confusion-killing idea on this page."
          />
          <div className="mx-auto mt-16 grid max-w-4xl gap-6 lg:grid-cols-2">
            <div className="rounded-lg border border-border bg-bg p-6">
              <p className="font-mono text-xs font-semibold uppercase tracking-widest text-fg">
                Ingest time · once
              </p>
              <ul className="mt-5 space-y-3">
                {INGEST_SIDE.map((item) => (
                  <li key={item} className="flex items-start gap-3 text-sm text-fg-muted">
                    <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-fg" aria-hidden="true" />
                    {item}
                  </li>
                ))}
              </ul>
            </div>
            <div className="rounded-lg border border-border bg-bg p-6">
              <p className="font-mono text-xs font-semibold uppercase tracking-widest text-fg">
                Query time · every question
              </p>
              <ul className="mt-5 space-y-3">
                {QUERY_SIDE.map((item) => (
                  <li key={item} className="flex items-start gap-3 text-sm text-fg-muted">
                    <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-fg" aria-hidden="true" />
                    {item}
                  </li>
                ))}
              </ul>
            </div>
          </div>
          <p className="mx-auto mt-8 max-w-3xl text-center text-sm text-fg-muted">
            Everything ingest built - stamped ACLs, profile vectors, value
            dictionaries - is what the query steps below merely compare
            against. That is why routing is arithmetic, not inference.
          </p>
        </div>
      </section>

      {/* Step 0 */}
      <section id="step-0" className="mx-auto max-w-6xl scroll-mt-20 px-6 py-24">
        <SectionHeading
          kicker="Step 0 · identity"
          title="From sign-in to principal set"
          sub="Identity is resolved once, at the HTTP boundary, from a verified sign-in (eg. a Microsoft Entra session or an API key). Never from anything the request body claims."
        />
        <div className="mx-auto mt-16 max-w-4xl space-y-6">
          <Objective
            example={
              <>
                Bob from sales expands to bob plus grp-sales; Alice, a
                director, to alice plus grp-directors. Store visibility in
                step 1 and query credentials in step 7 both reuse that same
                list, so a directors-only store simply does not exist for
                Bob.
              </>
            }
          >
            Establish beyond doubt who is asking, and turn that one identity
            into the full list of principals - the user plus every group they
            belong to - that every later permission check will reuse.
          </Objective>
          <Flow chart={FLOW_0} />
          <Prose>
            Why it is built this way: if identity came from the request body,
            anyone could type someone else&apos;s name. So the body is ignored and
            identity comes only from the <Strong>transport</Strong> - a
            verified session cookie, an API key, or a verified bearer token,
            in that order. And because there is exactly{" "}
            <Strong>one function</Strong> doing this for both REST and
            GraphQL, the two entry points cannot develop different rules over
            time. Here is that function:
          </Prose>
          <CodeRef file="src/dbsearch/api/auth.py" code={SNIPPET_IDENTITY} />
          <Prose>
            The router&apos;s first act is the group expansion. The resulting{" "}
            <Strong>principal set</Strong> is computed once per query and then
            reused everywhere - it is what visibility checks against in step 1
            and what the credential broker uses in step 7. It even serves as
            the routing cache key: two users with identical visibility share a
            cached decision, and the catalog revision in the key means any
            recompose invalidates it instantly. Here is where that happens:
          </Prose>
          <CodeRef
            file="src/dbsearch/router/router_service.py"
            code={SNIPPET_ROUTE_ENTRY}
            note="Cached decisions expire after sixty seconds, and the catalog revision in the key means a recompose invalidates instantly."
          />
        </div>
      </section>

      {/* Step 1 */}
      <section id="step-1" className="scroll-mt-20 border-y border-border bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <SectionHeading
            kicker="Step 1 · visibility"
            title="It is physically impossible (byte-wise) to see data you are not allowed to see"
            sub="The permission trim runs first, before any ranking."
          />
          <div className="mx-auto mt-16 max-w-4xl space-y-6">
            <Objective
              example={
                <>
                  The directors-only store carries the ACL grp-directors.
                  Bob&apos;s principal set (bob, grp-sales) has no overlap, so the
                  store is dropped here - it can never be scored, queried, or
                  named for him, and even the no-results reply stays generic
                  so he cannot tell it exists. Alice&apos;s set includes
                  grp-directors, so for her it survives the trim.
                </>
              }
            >
              Cut the catalog down to the stores this caller is allowed to
              know exist - before anything is ranked, selected, or explained.
              An unauthorized store is physically impossible to leak into an
              answer because it never entered the pipeline to begin with.
            </Objective>
            <Flow chart={FLOW_1} />
            <Prose>
              Visibility is <Strong>hereditary</Strong>: a store is visible
              only if the caller&apos;s principals intersect the ACL of the store
              and of every ancestor above it. An empty ACL matches nobody -
              unknown means hidden, not visible. The source:
            </Prose>
            <CodeRef file="src/dbsearch/router/catalog.py" code={SNIPPET_VISIBILITY} />
            <Prose>
              <Strong>An error message can leak secrets too.</Strong> If Bob
              gets &quot;you do not have access to the sales store&quot;, he
              has just learned a sales store exists - the deny message itself
              leaked. So the system only admits &quot;nothing is connected
              yet&quot; when that is literally true, because an empty catalog
              cannot leak anything. The moment any store exists that Bob
              cannot see, he gets the same generic reply he would get if it
              did not exist. It is the same pattern as a login form never
              telling you whether the username or the password was wrong -
              that would let attackers enumerate accounts. The code that
              draws this line:
            </Prose>
            <CodeRef
              file="src/dbsearch/router/router_service.py"
              code={SNIPPET_NOT_VISIBLE}
            />
          </div>
        </div>
      </section>

      {/* Step 2 */}
      <section id="step-2" className="mx-auto max-w-6xl scroll-mt-20 px-6 py-24">
        <SectionHeading
          kicker="Step 2 · classify"
          title="What kind of question is this?"
          sub="Three regexes label the question's shape - compound, exact, analytical, or semantic - in one cheap pass, before any model is involved."
        />
        <div className="mx-auto mt-16 max-w-4xl space-y-6">
          <Objective
            example={
              <>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border text-left">
                        <th className="py-2 pr-4 font-mono text-xs font-semibold uppercase tracking-widest text-fg-muted">
                          Question
                        </th>
                        <th className="py-2 pr-4 font-mono text-xs font-semibold uppercase tracking-widest text-fg-muted">
                          Label
                        </th>
                        <th className="py-2 font-mono text-xs font-semibold uppercase tracking-widest text-fg-muted">
                          Why
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {CLASSIFY_EXAMPLES.map((r) => (
                        <tr key={r.q} className="border-b border-border last:border-0">
                          <td className="py-2 pr-4 text-fg">&quot;{r.q}&quot;</td>
                          <td className="py-2 pr-4 font-mono text-xs text-fg">
                            {r.label}
                          </td>
                          <td className="py-2 text-fg-muted">{r.why}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="text-xs text-fg-muted">
                  All eight are real cases from the classifier&apos;s test file,
                  tests/selftest_router_classify.py.
                </p>
              </>
            }
          >
            <p>Label the question&apos;s shape before routing:</p>
            <ul className="list-disc space-y-1 pl-5">
              <li>
                <Strong>compound</Strong> - a comparison or join spanning two
                things
              </li>
              <li>
                <Strong>exact</Strong> - a point lookup of one record by id
              </li>
              <li>
                <Strong>analytical</Strong> - an aggregate: count, total,
                average, trend
              </li>
              <li>
                <Strong>semantic</Strong> - a plain search (the default)
              </li>
            </ul>
            <p>
              The one label that changes the pipeline is compound: the
              question is split, and each half runs through routing on its
              own - embedded, matched against store profiles, and sent to its
              own best store (steps 3 to 5). So a compound question can end
              up querying two databases where a single question queries one.
              Picking which store, database or document, is never the
              label&apos;s job: that is the vector match in steps 4 and 5.
            </p>
          </Objective>
          <Flow chart={FLOW_2} />
          <Prose>
            Priority order matters: &quot;compare total revenue and
            refunds&quot; contains an aggregate word, but compound is checked
            first, so it splits instead of being treated as one aggregate.
            The whole classifier:
          </Prose>
          <CodeRef file="src/dbsearch/router/classify.py" code={SNIPPET_CLASSIFY} />
          <Prose>
            The classifier can be wrong in two directions, and both mistakes
            are caught the same way:{" "}
            <Strong>run the split anyway and look at where the halves
            route</Strong>. A false alarm - &quot;terms and conditions?&quot;
            matches the &quot;X and Y&quot; pattern - collapses back to a
            single question, because both halves land on the same store. A
            miss - &quot;total revenue and the refund policy&quot;, where the
            aggregate word dominates the label - gets promoted to compound,
            because the trial split lands its halves on two different stores.
            The label is never trusted; the routing is:
          </Prose>
          <CodeRef
            file="src/dbsearch/router/router_service.py"
            code={SNIPPET_RESCUE}
            note="Single-versus-multi-store is not one decision - it is a cheap guess plus a check on where the halves route."
          />
        </div>
      </section>

      {/* Step 3 */}
      <section id="step-3" className="scroll-mt-20 border-y border-border bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <SectionHeading
            kicker="Step 3 · embed"
            title="The question becomes one vector"
            sub="One embedding call per routing pass, with the same embedder object the store profiles were built with - so the comparison in the next step is apples to apples."
          />
          <div className="mx-auto mt-16 max-w-4xl space-y-6">
            <Objective>
              Turn the question into the one vector that all of the routing
              arithmetic in steps 4 and 5 will compare against.
            </Objective>
            <Flow chart={FLOW_3} />
            <CodeRef
              file="src/dbsearch/router/router_service.py"
              code={SNIPPET_EMBED}
            />
            <Prose>
              The embedder is the edition&apos;s choice, not the router&apos;s: in-tenant{" "}
              <Strong>nomic-embed-text at 768 dimensions</Strong> under Ollama
              for self-host, <Strong>text-embedding-3-small at 1536</Strong> on
              Azure, and a 4096-dimension hashing embedder for the offline
              demo. Routing code never knows which one it holds - which is
              exactly what makes the choice swappable.
            </Prose>
          </div>
        </div>
      </section>

      {/* Step 4 */}
      <section id="step-4" className="mx-auto max-w-6xl scroll-mt-20 px-6 py-24">
        <SectionHeading
          kicker="Step 4 · match"
          title="Routing is arithmetic against profiles built at startup"
          sub="Every composed store carries a profile vector. Scoring a question is one cosine per visible store - no model call on the happy path."
        />
        <div className="mx-auto mt-16 max-w-4xl space-y-6">
          <Objective
            example={
              <>
                For &quot;what was Q3 revenue?&quot;, one cosine per visible
                store ranks the catalog: sales_sql scores 0.82 (its table
                vocabulary shares revenue, deal, region), finance_sql 0.79,
                hr_docs 0.31, legal_docs 0.28. That ranked list - 0.82,
                0.79, 0.31, 0.28 - is exactly what the selector in step 5
                decides over. Four stores ranked, no model called.
              </>
            }
          >
            Rank the visible stores by how much each one&apos;s profile resembles
            the question - using vectors that already exist, so scoring costs
            one cosine per store.
          </Objective>
          <Flow chart={FLOW_4} />
          <Prose>
            A store&apos;s profile text is its title, description, business unit,
            topics, and - for SQL stores - its table and column vocabulary, so
            structure routes even an undescribed store. Document stores fold
            their titles under a word budget, so one chatty store cannot
            dominate the embedding.
          </Prose>
          <CodeRef file="src/dbsearch/router/profiles.py" code={SNIPPET_PROFILE} />
          <Prose>
            Profile vectors are <Strong>warmed when the service starts</Strong>,
            because they depend on no user and no question - the first real
            query should not pay the whole catalog&apos;s embedding cost:
          </Prose>
          <CodeRef
            file="src/dbsearch/router/router_service.py"
            code={SNIPPET_WARM}
          />
          <Prose>
            At scale - six or more business units - a coarse pass prunes whole
            units before fine scoring. It scores each unit by its{" "}
            <Strong>best store, not its average</Strong>, a lesson learned from
            a real misroute and preserved in the code where it was learned:
          </Prose>
          <CodeRef
            file="src/dbsearch/router/profiles.py"
            code={SNIPPET_COARSE}
            note="The coarse gate asks 'could ANY store in this unit answer?' - a max. A mean lets strong stores drown among weak siblings."
          />
          <FurtherExplanation>
            <p>
              The gate&apos;s question per unit is &quot;could anything in
              here answer?&quot; - and &quot;any&quot; is a best-case
              question, not an average-case one. Here is the misroute that
              taught the lesson, with the real cast:
            </p>
            <pre className="overflow-x-auto rounded-lg bg-surface p-4 font-mono text-[13px] leading-relaxed text-fg">
              {`for "revenue per product":

Sales BU     deals         0.9   ← the right store
             storefront    0.2
             warehouse     0.1
             ─────────────────
             mean = 0.4          ← the unit's score before #321
             max  = 0.9          ← the unit's score today

Finance BU   one document  0.5     (its only store)

mean:  Sales 0.4 < Finance 0.5 → the WHOLE Sales unit is
       pruned, its 0.9 store never reaches fine scoring,
       and the question lands on a finance document
       holding a single revenue total
max:   Sales 0.9 survives → the fine pass picks deals`}
            </pre>
            <p>
              The right store existed, scored highest of anything in the
              catalog, and lost - because its two irrelevant siblings dragged
              the unit&apos;s average down. That is what &quot;a mean lets
              strong stores drown among weak siblings&quot; means.
            </p>
            <p>
              The safety argument in the docstring - max{" "}
              <Strong>prunes strictly less</Strong> - is about the two
              mistakes not being symmetric. Keep a unit too generously and
              the fine pass wastes a few cosines ranking its stores low.
              Prune a unit wrongly and the right store is gone from the pool
              - nothing downstream can bring it back. Max is always at least
              the mean, so it only ever keeps <em>more</em> units: the gate
              is deliberately biased toward the recoverable mistake. A unit
              where genuinely nothing matches is still dropped, because even
              its best store scores low.
            </p>
          </FurtherExplanation>
        </div>
      </section>

      {/* Step 5 */}
      <section id="step-5" className="scroll-mt-20 border-y border-border bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <SectionHeading
            kicker="Step 5 · select"
            title="How many stores get this query - and at what cost"
            sub="Step 4 produced a ranked list of scores. Step 5 turns that list into the actual routing decision: usually one store and no model call, at most three on a genuine tie."
          />
          <div className="mx-auto mt-16 max-w-4xl space-y-6">
            <Objective
              example={
                <>
                  The ranked list from step 4: 0.82, 0.79, 0.31, 0.28. The
                  relative floor keeps only stores within 0.6 of the best
                  (0.82 &times; 0.6 = 0.49), dropping 0.31 and 0.28. The
                  margin test then asks: does 0.82 lead 0.79 by at least
                  0.15? It does not - 0.03 apart is a genuine tie - so the
                  query goes to both stores and the merge in step 8 settles
                  it on evidence. Had finance_sql scored 0.60 instead, the
                  margin test would have passed and sales_sql alone gets the
                  query: decision made, nothing but arithmetic spent.
                </>
              }
            >
              Decide which stores actually receive the query, using nothing
              but the ranked scores from step 4. Two threshold checks - a
              relative floor, then a margin test - settle most questions on
              a single obvious winner at zero model cost. Only a genuine
              near-tie fans out, capped at three stores. And everything that
              runs after the scores (the tiebreak seam, the lexical rescue)
              is only allowed to <Strong>shrink</Strong> that tied set -
              never to grow it, never to reach outside it.
            </Objective>
            <Flow chart={FLOW_5} />
            <Prose>
              The scored candidates pass a <Strong>relative floor</Strong> -
              anything below six tenths of the best score is out - then a{" "}
              <Strong>margin test</Strong>: if the leader beats the runner-up
              by at least 0.15, it wins outright. Otherwise the tied pool fans
              out, capped at three stores.
            </Prose>
            <CodeRef file="src/dbsearch/router/selector.py" code={SNIPPET_SELECTOR} />
            <Prose>
              The <Strong>tiebreak is an injected seam</Strong>, not a baked-in
              model call: the served build passes none, so every production
              pick today is pure vector arithmetic. And the seam is contained
              by construction - a returned id that was not already in the
              scored, visible pool is simply ignored, so no tiebreaker, however
              wrong, can ever widen access.
            </Prose>
            <Prose>
              One cheap rescue runs after selection. A fan-out whose question
              distinctively names exactly one of the tied stores is scoped to
              it - shared tokens tie the vector scores, the distinctive token
              breaks the tie:
            </Prose>
            <CodeRef
              file="src/dbsearch/router/router_service.py"
              code={SNIPPET_NARROW}
              note="The rescue never widens the set and never reaches outside it - it can only make a fan-out smaller."
            />
          </div>
        </div>
      </section>

      {/* Step 6 */}
      <section id="step-6" className="mx-auto max-w-6xl scroll-mt-20 px-6 py-24">
        <SectionHeading
          kicker="Step 6 · dispatch"
          title="A disclosed budget, and no store can sink the query"
          sub="Selected stores - across all sub-queries - dispatch concurrently up to a ceiling of eight. Everything past the ceiling is reported, not silently skipped."
        />
        <div className="mx-auto mt-16 max-w-4xl space-y-6">
          <Objective>
            Run every selected store without letting any single one of them
            stall, sink, or silently shrink the query.
          </Objective>
          <Flow chart={FLOW_6} />
          <Prose>
            Every dispatched store ends in exactly one named outcome, and the
            vocabulary itself encodes a hard-won distinction:{" "}
            <Strong>empty is not the same as declined</Strong>. A store that
            holds no such data and says so is being honest, not failing.
          </Prose>
          <CodeRef file="src/dbsearch/router/executor.py" code={SNIPPET_OUTCOMES} />
          <Prose>
            Failure is isolated per store: a timeout, an honest decline, and a
            crash each record their outcome and move on. The remaining stores&apos;
            evidence still reaches the answer, and the dropped store is named
            in the disclosure.
          </Prose>
          <CodeRef file="src/dbsearch/router/executor.py" code={SNIPPET_ISOLATION} />
        </div>
      </section>

      {/* Step 7 */}
      <section id="step-7" className="scroll-mt-20 border-y border-border bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <SectionHeading
            kicker="Step 7 · the SQL rail"
            title="Schema in, SQL out, executed as you - then honesty"
            sub="Inside a SQL store the in-tenant model sees table signatures and authored meanings, never a single data value. What comes back is validated, executed under the caller's own authority, and repaired honestly when it misses."
          />
          <div className="mx-auto mt-16 max-w-4xl space-y-6">
            <Objective>
              Turn the question into safe SQL, run it with the caller&apos;s own
              authority inside the caller&apos;s own engine - and when it misses,
              repair or decline honestly instead of guessing.
            </Objective>
            <Flow chart={FLOW_7} />
            <Prose>
              Wide schemas are narrowed first: a table-level index retrieves
              the relevant slice of the schema into the prompt instead of all
              of it. The prompt hands over signatures and meanings - and one
              non-negotiable instruction about what to do when the data simply
              is not there:
            </Prose>
            <CodeRef
              file="src/dbsearch/adapters/anthropic/__init__.py"
              code={SNIPPET_CANNOT_ANSWER}
              note="Quoted from the system prompt itself. Declining is cheap - another store in the federation usually holds what was asked."
            />
            <Prose>
              Whatever the model writes must pass a{" "}
              <Strong>read-only, single-statement, visible-schema-only</Strong>{" "}
              guard before it touches an engine:
            </Prose>
            <CodeRef file="src/dbsearch/router/structured.py" code={SNIPPET_VALIDATE_SQL} />
            <Prose>
              Execution is pushdown, <Strong>as the user</Strong>: where a
              delegation exists, the caller&apos;s token is exchanged for a
              credential in their own name, so the engine&apos;s own row-level
              security applies at the source. Where it does not, a proven
              row-policy predicate wraps the query. Only aggregates come back -
              never raw dumps.
            </Prose>
            <CodeRef
              file="src/dbsearch/router/identity_broker.py"
              code={SNIPPET_ACCESS}
            />
            <Prose>
              Then the <Strong>honesty ladder</Strong> for misses. An empty
              aggregate is probed to learn whether a filter matched nothing. A
              literal the model guessed wrong - a casing, a spelling, a
              notation - is repaired through the value dictionary and re-run
              under the same credential, with the substitution disclosed in
              the answer&apos;s provenance. If repair cannot decide, one bare-schema
              reprompt is allowed; a second miss is an honest decline, never a
              guess.
            </Prose>
            <CodeRef
              file="src/dbsearch/router/dictionary.py"
              code={SNIPPET_DICTIONARY}
              note="None never means 'no rows' - it means 'I could not tell', which becomes a decline instead of a fabricated filter."
            />
          </div>
        </div>
      </section>

      {/* Step 8 */}
      <section id="step-8" className="mx-auto max-w-6xl scroll-mt-20 px-6 py-24">
        <SectionHeading
          kicker="Step 8 · synthesis"
          title="Merged by rank, cited, and honest about nothing"
          sub="Evidence from a SQL engine and a vector index carry incomparable scores - so the merge trusts each store's own ranking, never the raw numbers."
        />
        <div className="mx-auto mt-16 max-w-4xl space-y-6">
          <Objective>
            Compose one cited, provable answer from whatever survived - or
            tell the user exactly why there is none.
          </Objective>
          <Flow chart={FLOW_8} />
          <CodeRef file="src/dbsearch/router/synthesizer.py" code={SNIPPET_MERGE} />
          <Prose>
            Twelve items survive the merge; only the top few passages
            (currently three) are quoted into the model&apos;s prompt, and the trim
            is disclosed. Every claim in the composed answer carries a
            citation with a <Strong>typed proof</Strong>: for a database row,
            the exact SQL, table and row ids; for a document chunk, the
            document, its location and a locator. The answer also reports each
            store&apos;s outcome - including what was dropped, capped or declined.
          </Prose>
          <Prose>
            And when nothing survives, the user gets the{" "}
            <Strong>true reason</Strong>, not a generic shrug. Five distinct
            no-answer messages, chosen by what actually happened - with the one
            deliberate exception that a permission denial stays generic, so an
            invisible store leaks nothing even here:
          </Prose>
          <CodeRef
            file="src/dbsearch/router/synthesizer.py"
            code={SNIPPET_NO_ANSWER}
            note="Blaming permissions for an empty catalog once sent users hunting for an access problem that did not exist. Now the reply says why."
          />
        </div>
      </section>

      {/* What moves, what doesn't */}
      <section id="invariants" className="scroll-mt-20 border-y border-border bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <SectionHeading
            kicker="The honest close"
            title="What is frozen, what is calibration, what is moving"
            sub="An architecture page that pretends everything is final is lying. Here is the actual state of each layer."
          />
          <div className="mx-auto mt-16 grid max-w-5xl gap-6 lg:grid-cols-3">
            <div className="rounded-lg border border-border bg-bg p-6">
              <p className="font-mono text-xs font-semibold uppercase tracking-widest text-fg">
                Frozen
              </p>
              <ul className="mt-5 space-y-3">
                {FROZEN.map((item) => (
                  <li key={item} className="flex items-start gap-3 text-sm text-fg-muted">
                    <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-fg" aria-hidden="true" />
                    {item}
                  </li>
                ))}
              </ul>
            </div>
            <div className="rounded-lg border border-border bg-bg p-6">
              <p className="font-mono text-xs font-semibold uppercase tracking-widest text-fg">
                Calibration
              </p>
              <ul className="mt-5 space-y-3">
                {CALIBRATION.map((item) => (
                  <li key={item} className="flex items-start gap-3 text-sm text-fg-muted">
                    <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-fg" aria-hidden="true" />
                    {item}
                  </li>
                ))}
              </ul>
            </div>
            <div className="rounded-lg border border-border bg-bg p-6">
              <p className="font-mono text-xs font-semibold uppercase tracking-widest text-fg">
                Moving
              </p>
              <ul className="mt-5 space-y-3">
                {EVOLVING.map((item) => (
                  <li key={item} className="flex items-start gap-3 text-sm text-fg-muted">
                    <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-fg" aria-hidden="true" />
                    {item}
                  </li>
                ))}
              </ul>
            </div>
          </div>
          <p className="mx-auto mt-10 max-w-3xl text-center text-sm text-fg-muted">
            For the invariants these steps obey - data residency and
            permission-faithful retrieval, in the code that enforces them -
            go back to{" "}
            <Link
              href="/architecture"
              className="text-fg underline underline-offset-4 hover:text-accent"
            >
              chapter one: the two laws
            </Link>
            .
          </p>
        </div>
      </section>

      <CtaBand />
    </main>
  );
}
