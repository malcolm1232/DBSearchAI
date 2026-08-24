"""Anthropic Claude via the Messages API (LAW 9, pluggable model) — powers the two-phase
conversational proposal draft (#57): cheap **Haiku** for the gather chat, strong **Sonnet**
for the final proposal.

ONE class, parameterised by `model`, so the same code is the Haiku and the Sonnet instance.

⚠ LAW 1 (data residency): pointed at the PUBLIC api.anthropic.com (the default), retrieved
customer content leaves the tenant. That is acceptable for the DEV/DEMO only and is logged
once at startup. The product-faithful path (#58) keeps `base_url` the SAME shape but points it
at Claude hosted on Azure / inside the customer VNet — no other code changes. `base_url` IS
that seam. Like every LlmPort, this MUST only ever receive post-trim content (LAW 2).

Optional dep:  pip install '.[anthropic]'
"""
from __future__ import annotations

import re

from dbsearch.ports.base import LlmPort

from dbsearch.ports.prompts import ANSWER_SYSTEM as _ANSWER_SYSTEM  # noqa: E402  (#403)
from dbsearch.ports.prompts import CONDENSE_SYSTEM as _CONDENSE_SYSTEM  # noqa: E402
from dbsearch.core.copy import NO_EVIDENCE_ANSWER
_ELICIT_SYSTEM = (
    "You are helping a consultant scope a client proposal. Ask ONE sharp clarifying question at "
    "a time to pin down the client, their need, the scope, constraints, timeline and budget. "
    "When the essentials are covered, say so and invite them to generate the draft. Be brief."
)
_SUMMARY_SYSTEM = (
    "Summarise the conversation into a short, deduplicated bullet list of the proposal's "
    "confirmed requirements (client, need, scope, constraints, timeline, budget where stated). "
    "Output ONLY the bullets, one '- ' per line — no preamble."
)
_PLAN_SYSTEM = (
    "Decompose the opportunity brief into ONE focused retrieval sub-question per proposal "
    "section, in order. Output ONLY the sub-questions, one per line, no numbering."
)
_DRAFT_SYSTEM = (
    "Write ONE proposal section in a confident consulting voice, grounded ONLY in the provided "
    "context passages (cite them by [n]). If a passage doesn't support a claim, omit the claim. "
    "Return prose only — no headings, no citation list."
)
_SETUP_SYSTEM = (
    "Extract enterprise data-source entries from an admin's setup message. Output ONLY a JSON "
    "array — no prose, no code fences. One object per source described: "
    '{"kind": one of folder|csv|sharepoint|postgres|bigquery|redshift|azure_sql|graph_search, '
    '"id": a short kebab-case slug, '
    '"business_unit": the team/unit it is for (omit if unsaid), '
    '"acl": [the groups the admin EXPLICITLY said may see it] — NEVER invent or guess an acl; '
    "omit it when the admin did not say who can see the source, "
    '"description": the topics/content the admin said it contains (omit if unsaid), '
    '"config": connection details as given (folder: {"path"}; csv: {"files": [..]}; '
    'bigquery: {"project", "dataset"})}. '
    "Extract only what is stated — never fabricate paths, projects, or groups. "
    "Output [] if no data source is described."
)
_DECOMPOSE_SYSTEM = (
    "You split a COMPOUND question into standalone sub-questions — one per data domain — so "
    "that each can be sent to a DIFFERENT database and the results joined afterwards.\n"
    "Output ONLY a JSON array of strings. No prose, no code fences.\n"
    "Rules:\n"
    "1. If the question asks about ONE thing, return it unchanged as a single-element array.\n"
    "2. Every sub-question must STAND ALONE: resolve pronouns (they/it/those) into the noun "
    "they refer to.\n"
    "3. CRITICAL — carry the shared entity and grain into EVERY sub-question (e.g. 'per "
    "product SKU', 'by region', 'for customer 29485'). That grain is the JOIN KEY used to "
    "line up results from different databases; a sub-question that loses it produces an "
    "aggregate that cannot be joined to anything.\n"
    "4. At most 3 sub-questions.\n"
    "Example — 'Which products generate the most support tickets, and how much revenue do "
    "they bring?' becomes:\n"
    '["How many support tickets are there for each product, by product SKU?", '
    '"What is the total revenue for each product, by product SKU?"]'
)

_SQL_SYSTEM = (
    "You translate an analytics question into ONE SQL query over the given schema. "
    "Output ONLY the SQL — no prose, no code fences, no explanation, no trailing semicolon. "
    "Rules: a SINGLE read-only SELECT (or WITH ... SELECT) statement; reference ONLY tables "
    "and columns present in the schema; never write, alter, or use multiple statements or "
    "comments. Prefer aggregates/GROUP BY/ORDER BY when the question asks for totals, "
    "rankings, or breakdowns.\n"
    # #271: a MEASURE per entity must not be padded with entities that have no measure. Asked
    # "total revenue for each product sku" the model wrote a LEFT JOIN from Product, returning
    # 295 rows of which 153 had TotalRevenue = NULL — only 142 products have any revenue. Two
    # harms, both silent: the answer quotes 295 as the denominator ("a partial sample of 5 out
    # of 295 total product SKUs"), and the empty rows compete for a truncated result's row
    # budget. It also led the model to state outright that "the complete result set contains
    # revenue data for all products", which was false of a majority of its own rows.
    # An outer join is still correct when the question ASKS for the entities with none — that
    # is a real thing to want, so this narrows the default rather than forbidding the shape.
    "For a MEASURE per entity (revenue per product, orders per customer, tickets per team), "
    "include only entities that HAVE matching rows - use an inner join. An entity with no "
    "matching rows has no measure, and padding the result with empty rows makes the row count "
    "misleading. Use an outer join ONLY when the question explicitly asks to include entities "
    "with none (\"including products that never sold\", \"even if zero\").\n"
    # #230 value grounding: the model gets the SCHEMA ONLY - names and types, never a row (LAW 1:
    # customer data must not leave the tenant). So it CANNOT know how a value is spelled or cased.
    # Asked for "touring bikes" it wrote `= 'touring bikes'` while the column holds 'Touring Bikes'
    # - zero rows, and zero rows reads to the user as "I don't have that information". A WRONG
    # FILTER IS INDISTINGUISHABLE FROM MISSING DATA: the worst kind of failure, because it hides
    # itself. Compare case-insensitively and the guess stops mattering.
    "You are given names and types but NEVER any data values, so you CANNOT know how a value is "
    "cased or spelled in the table. When filtering on a TEXT value taken from the question, "
    "always compare case-insensitively - WHERE LOWER(col) = LOWER('the value') - never a bare "
    "= 'the value'. A filter that misses on casing returns zero rows, which is silently "
    "indistinguishable from the data not existing.\n"
    # #211: this rule used to read "if the question can't be answered from the schema, return
    # the safest broad SELECT over the most relevant table" — i.e. answer anyway. Asked which
    # products had the most SUPPORT TICKETS, a sales schema duly returned
    # COUNT(SalesOrderDetailID) AS support_tickets: order lines, RELABELLED as tickets. The
    # number was real, the label invented, and it shipped with citations. Declining is the
    # only honest move, and it is cheap — another store in the catalog usually does hold it.
    "DEFAULT TO ANSWERING, and DO map the question's wording onto the schema's names. Column "
    "and table names will rarely match the question word-for-word: `product_number` IS the "
    "product SKU, a `tickets` table DOES answer a question about support tickets, `opened_on` "
    "IS the date it was raised. Recognising that two names mean the SAME THING is reading the "
    "schema, not inventing — do it, and write the query.\n"
    "The one thing you must NEVER do is answer with a DIFFERENT THING than the one asked for. "
    "Do not count or alias an unrelated measure to stand in for a missing one (e.g. counting "
    "sales order lines and calling them support tickets), do not substitute the 'closest' "
    "column for an entity that simply is not here, and do not fall back to a broad SELECT. "
    "ONLY when the thing asked about has no counterpart at all in this schema, output exactly "
    "CANNOT_ANSWER and nothing else. A confident wrong answer is far worse than none — another "
    "database in the federation usually holds what was asked for, and one fabricated column "
    "poisons the whole federated result."
)


def sql_user_prompt(question: str, schema: list, dialect: str = "") -> str:
    """The user half of the NL2SQL prompt, shared by every LlmPort that can generate SQL.

    Public and central on purpose. #461: `LlamaLlm` shipped without `generate_sql` at all,
    so `router_api._model_wiring`'s `hasattr` gate silently fell back to the regex
    `keyword_sql_generator` on every self-host rig — a whole class of wrong answers that
    no test caught because each adapter carried its OWN private copy of this formatting
    and nothing tied them together. One function, one prompt shape, one place to change.

    LAW 1: schema METADATA ONLY — names and types, never a row, never a value. See the
    #230 note in `_SQL_SYSTEM` for why the model is told to compare case-insensitively
    instead of being shown how values are spelled.

    #468: types are included because `_SQL_SYSTEM` PROMISES them ("You are given names
    and types but NEVER any data values") and this function emitted names only. A model told it
    has type information it never received cannot tell REAL from TEXT, and the defensible
    guess — cast everything to text before comparing — silently breaks numeric filters:

        WHERE CAST(customer_id AS TEXT) = '29485'   ->  '29485.0' != '29485'  ->  no rows

    and a wrong filter is indistinguishable from missing data. Measured with a
    context-free generator over the golden pack's 18 failing structured items: names only
    scored 11/18, names+types scored 18/18, and all seven recovered items were exactly
    that cast. Unlike values (#462), types are metadata, so this costs nothing under LAW 1.
    """
    def _col(c: dict) -> str:
        # A connector-introspected schema always carries a type (structured.py defaults
        # to TEXT), but a hand-built or partial one may not — degrade to the bare name
        # rather than emitting a dangling trailing space.
        return f"{c['name']} {c['type']}" if c.get("type") else c["name"]

    def _table(t: dict) -> str:
        """The signature line, byte-identical to before #486, plus any AUTHORED notes on
        their own lines beneath it.

        Notes go BELOW rather than inline as `-- comment`, deliberately: a model shown
        `--` in its schema copies it into the SQL it writes, and `validate_sql` refuses any
        query containing a comment. That is the #477 failure mode - correct SQL rejected by
        a guard - and it would have been self-inflicted here.

        Names and types alone left the model guessing what a column MEANT: "how many
        parcels made it into the buyer's hands" was answered by counting rows with a
        freight value, because nothing said `order_status` is where that lives. LAW 1
        holds - a description is written ABOUT the schema, never read off a row (see
        `describe_schema`)."""
        line = f"{t['table']}({', '.join(_col(c) for c in t.get('columns', []))})"
        notes = []
        if t.get("comment"):
            notes.append(f"  {t['table']}: {t['comment']}")
        notes += [f"  {t['table']}.{c['name']}: {c['description']}"
                  for c in t.get("columns", []) if c.get("description")]
        return "\n".join([line, *notes])

    schema_text = "\n".join(_table(t) for t in schema)
    # #714: name the target dialect, or the model writes generic SQL — `LIMIT` against a
    # T-SQL engine is a parse error the user sees as "the source did not respond". The
    # per-engine instruction (e.g. "use SELECT TOP n, never LIMIT") rides in the dialect
    # string itself (engine.dialect), so this stays engine-agnostic.
    dialect_line = (f"\nTarget SQL dialect: {dialect}. "
                    "Use ONLY syntax this dialect supports.") if dialect else ""
    return f"Schema:\n{schema_text}{dialect_line}\n\nQuestion: {question}"


_COSMOS_SYSTEM = (
    "You translate an analytics question into ONE Azure Cosmos DB (NoSQL / Core API) query "
    "over the given container. Output ONLY the query - no prose, no code fences, no "
    "explanation, no trailing semicolon.\n"
    "Dialect rules: the container is always aliased `c`; reference ONLY fields present in the "
    "schema, always as `c.<field>`. A SINGLE read-only SELECT. There are NO JOINs across "
    "containers. For a scalar aggregate with no grouping use the VALUE form - e.g. "
    "`SELECT VALUE AVG(c.rating) FROM c`. For a breakdown use "
    "`SELECT c.<field>, COUNT(1) AS count FROM c GROUP BY c.<field>`. Use TOP N to limit. "
    "Prefer an aggregate over returning raw documents whenever the question asks for a total, "
    "an average, a count, a ranking or a breakdown.\n"
    # #230: the same value-grounding trap, the same fix. Cosmos has a case-insensitive comparator.
    "You are given field names and types but NEVER any document values, so you CANNOT know how a "
    "value is cased or spelled. When filtering on a TEXT value taken from the question, always "
    "use the case-insensitive form - STRINGEQUALS(c.field, 'the value', true) - never a bare "
    "c.field = 'the value'. A filter that misses on casing returns zero rows, which is silently "
    "indistinguishable from the data not existing.\n"
    # #228: the naive keyword generator emitted `SELECT TOP 20 * FROM c` for "what is the
    # average rating" - a blind SAMPLE - and the synthesizer averaged the handful of documents
    # it was handed and reported THAT as the population figure (4.2, when the truth over all 84
    # docs was 3.33). An aggregate question MUST become an aggregate query, computed by the
    # database over every document, not by the reader over a sample.
    "NEVER answer an aggregate question by selecting raw documents and leaving the arithmetic "
    "to the reader: a statistic computed from a sample of documents is a WRONG NUMBER stated "
    "as fact.\n"
    "DEFAULT TO ANSWERING, and DO map the question's wording onto the schema's field names - "
    "they will rarely match word-for-word (`product_number` IS the SKU, `reviewed_on` IS the "
    "date it was written). Recognising that two names mean the SAME THING is reading the "
    "schema, not inventing it.\n"
    "The one thing you must NEVER do is answer with a DIFFERENT THING than the one asked for: "
    "do not substitute the closest field for an entity that simply is not here. ONLY when the "
    "thing asked about has no counterpart at all in this container, output exactly "
    "CANNOT_ANSWER and nothing else. A confident wrong answer is far worse than none - another "
    "database in the federation usually holds what was asked for."
)

# Latest models (env knowledge): Haiku 4.5 for cheap chat, Sonnet 4.6 for the proposal draft.
HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"


def cap_chunks_disclosed(context_chunks: list[str], cap: int) -> list[str]:
    """#499: the prompt-boundary chunk cap must DISCLOSE when it cuts, never truncate
    silently. Findings §16 measured it never fires today (ingest chunks at ~1200 < 1500),
    so a firing means an upstream invariant broke — the cut carries an in-prompt note (so
    the model and every recorded prompt see it) and logs a warning. Under-cap chunks pass
    through untouched. The cap lives at the PROMPT boundary only: the index always holds
    complete chunks and retrieval ranks over complete text."""
    import logging

    out = []
    for c in context_chunks:
        if len(c) > cap:
            logging.getLogger("dbsearch.llm").warning(
                "prompt-boundary truncation: %d-char chunk capped to %d (disclosed in prompt)",
                len(c), cap)
            out.append(c[:cap] + " …[TRUNCATED: this passage exceeded the prompt budget; "
                                 "its remainder was not shown to the model]")
        else:
            out.append(c)
    return out


class AnthropicLlm(LlmPort):
    # #911: sized for a full #257 per-DOCUMENT block (top_k * CHUNK_MAX_CHARS ~ 6.1K), not a
    # single ingest chunk - see LlamaLlm._MAX_CHARS_PER_CHUNK. Pinned by selftest_911.
    _MAX_CHARS_PER_CHUNK = 8000
    _MAX_OUTPUT_TOKENS = 1024

    def __init__(self, api_key: str, model: str = HAIKU,
                 base_url: str = "https://api.anthropic.com", *, client=None) -> None:
        self._model = model
        if client is not None:
            self._client = client                       # injected fake (tests) — no network/import
        else:
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key, base_url=base_url)

    # --- the single network seam: everything else composes prompts and calls this ------------
    def _complete(self, system: str, user: str, *, max_tokens: int | None = None,
                  temperature: float = 0.0) -> str:
        if not (user or "").strip():
            return ""                                    # Anthropic 400s on empty user content
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens or self._MAX_OUTPUT_TOKENS,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(getattr(b, "text", "") for b in resp.content).strip()

    @staticmethod
    def _context(context_chunks: list[str]) -> str:
        # #233: number ONLY the evidence passages, so a cited [n] can ONLY ever resolve to a real
        # citation. The synthesizer folds in INSTRUCTION lines - #206's [coverage] sample warning
        # and #227's [query] "here is what produced this evidence" - and if those are numbered too,
        # the model can cite one: observed live, it cited [7] (the [query] line) when only 6
        # evidence passages existed, leaving a dangling footnote in the prose. Instruction lines
        # keep their own [coverage]/[query] label but consume no citable number, so evidence
        # numbering stays 1..N in lockstep with the footnotes built from that same evidence.
        capped = cap_chunks_disclosed(context_chunks, AnthropicLlm._MAX_CHARS_PER_CHUNK)
        out, n = [], 0
        for c in capped:
            if c.startswith("[coverage]") or c.startswith("[query]"):
                out.append(c)
            else:
                n += 1
                out.append(f"[{n}] {c}")
        return "\n\n".join(out)

    # --- LlmPort: answer ---------------------------------------------------------------------
    def answer(self, question: str, context_chunks: list[str]) -> dict:
        if not context_chunks:
            return {"answer": NO_EVIDENCE_ANSWER, "citations": []}
        user = f"Question: {question}\n\nContext:\n{self._context(context_chunks)}"
        return {"answer": self._complete(_ANSWER_SYSTEM, user), "citations": []}

    def answer_stream(self, question: str, context_chunks: list[str]):
        if not context_chunks:
            yield NO_EVIDENCE_ANSWER
            return
        user = f"Question: {question}\n\nContext:\n{self._context(context_chunks)}"
        # Prefer real token streaming; fall back to one chunk if the client can't stream.
        stream = getattr(self._client.messages, "stream", None)
        if stream is None:
            yield self._complete(_ANSWER_SYSTEM, user)
            return
        with stream(model=self._model, max_tokens=self._MAX_OUTPUT_TOKENS, temperature=0,
                    system=_ANSWER_SYSTEM, messages=[{"role": "user", "content": user}]) as s:
            for text in s.text_stream:
                if text:
                    yield text

    def condense_question(self, question: str, history: list[dict]) -> str:
        if not history:
            return question
        convo = "\n".join(f"User: {h['question']}\nAssistant: {h['answer']}" for h in history)
        return self._complete(_CONDENSE_SYSTEM, f"{convo}\n\nLast message: {question}",
                              max_tokens=256) or question

    # --- LlmPort: proposal planning + drafting (real generation, the Sonnet win) --------------
    def plan_subquestions(self, brief: str, sections: list[str]) -> list[str]:
        user = f"Brief: {brief}\n\nSections (in order):\n" + "\n".join(f"- {s}" for s in sections)
        out = self._complete(_PLAN_SYSTEM, user, max_tokens=512)
        lines = [ln.strip(" -•\t") for ln in out.splitlines() if ln.strip()]
        # Always return exactly one sub-question per section; fall back to the heuristic if short.
        if len(lines) < len(sections):
            lines += [f"{s} for: {brief}" for s in sections[len(lines):]]
        return lines[: len(sections)]

    def draft_section(self, title: str, brief: str, context_chunks: list[str]) -> str:
        if not context_chunks:
            return "No authorized source material found for this section."
        user = (f"Brief: {brief}\nSection: {title}\n\nContext passages:\n"
                f"{self._context(context_chunks)}")
        return self._complete(_DRAFT_SYSTEM, user)

    def draft_section_stream(self, title: str, brief: str, context_chunks: list[str]):
        if not context_chunks:
            yield "No authorized source material found for this section."
            return
        user = (f"Brief: {brief}\nSection: {title}\n\nContext passages:\n"
                f"{self._context(context_chunks)}")
        stream = getattr(self._client.messages, "stream", None)
        if stream is None:                               # injected fake / non-streaming client
            yield self._complete(_DRAFT_SYSTEM, user)
            return
        with stream(model=self._model, max_tokens=self._MAX_OUTPUT_TOKENS, temperature=0,
                    system=_DRAFT_SYSTEM, messages=[{"role": "user", "content": user}]) as s:
            for text in s.text_stream:
                if text:
                    yield text

    # --- LlmPort: conversational gather (#57) -------------------------------------------------
    def elicit_requirements(self, history: list[dict]) -> str:
        if not history:
            return ("Tell me about the proposal you'd like to draft — who's the client, "
                    "and what do they need?")
        convo = "\n".join(f"User: {h.get('question','')}\nAssistant: {h.get('answer','')}"
                          for h in history)
        return self._complete(_ELICIT_SYSTEM, convo, max_tokens=256)

    def summarize_requirements(self, history: list[dict]) -> str:
        convo = "\n".join(f"User: {h.get('question','')}" for h in history)
        return self._complete(_SUMMARY_SYSTEM, convo, max_tokens=512)

    # --- conversational setup (#116 C3) --------------------------------------------------------
    def extract_setup_entries(self, text: str) -> str:
        """Raw model text for the setup-entry extraction. JSON parsing, normalisation and the
        follow-up asks stay DETERMINISTIC in `llm_entry_parser` (agents.setup_session) — the
        model only ever proposes entries; it never grants an ACL (LAW 2)."""
        return self._complete(_SETUP_SYSTEM, text, max_tokens=1024)

    # --- federated NL2SQL (#135) ---------------------------------------------------------------
    def decompose_question(self, question: str) -> list:
        """#215: propose the split. The model only PROPOSES — `router.decompose.llm_decomposer`
        validates the shape and falls back to the deterministic split on any bad output, so a
        poor generation can never lose a half of the question."""
        import json

        raw = (self._complete(_DECOMPOSE_SYSTEM, question, max_tokens=400) or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[A-Za-z]*\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw).strip()
        try:
            parts = json.loads(raw)
        except Exception:
            return []                                    # -> caller falls back, never guesses
        return parts if isinstance(parts, list) else []

    def generate_sql(self, question: str, schema: list, dialect: str = "") -> str:
        """Raw model SQL for a schema-grounded analytical query. The model only PROPOSES
        SQL; the read-only guard + keyword fallback live in `llm_sql_generator`
        (router.structured), so a hallucinated table or a write can never reach the
        engine — this just composes the schema+question prompt and returns text.
        `dialect` (#714) names the target engine's SQL dialect: without it the model
        writes generic SQL, and `LIMIT` is a parse error on T-SQL."""
        return self._complete(_SQL_SYSTEM, sql_user_prompt(question, schema, dialect),
                              max_tokens=512)

    def generate_cosmos_query(self, question: str, schema: list) -> str:
        """Schema-grounded NL2query for Cosmos (#229) - the same seam SQL has had since #135.
        The model only PROPOSES the query; the read-only guard, the CANNOT_ANSWER decline and
        the keyword fallback all live in `llm_cosmos_generator` (router.providers.cosmos).

        LAW 1: the schema is field NAMES and TYPES only - Cosmos's pseudo-schema is inferred by
        sampling documents, but only `type(v).__name__` is kept, never a value. No customer data
        goes to the model; only metadata."""
        fields = ", ".join(f"{f['name']} ({f.get('type', '?')})"
                           for f in (schema[0]["fields"] if schema else []))
        container = schema[0].get("container", "c") if schema else "c"
        user = f"Container: {container}\nFields: {fields}\n\nQuestion: {question}"
        return self._complete(_COSMOS_SYSTEM, user, max_tokens=512)
