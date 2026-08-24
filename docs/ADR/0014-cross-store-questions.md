# ADR 0014 - Cross-store questions: what "traverse two databases" actually requires

Date: 2026-08-03 · Status: ACCEPTED (decision taken 260804) · Builds on ADR 0007 (pushdown federation) · Cards #474, #35, #473

## Context

The real-data measurement (#473) scored capability D - a question whose filter lives in one
store and whose measure lives in another - at **0 of 5**.
That capability is the whole of card #35, "download multiple data and act as databases, then
see if can traverse", and it is the one the product cannot do.

The first diagnosis, written from the run output alone, was "there is no cross-store join."
Reading the code says otherwise, and the correction matters because it changes the fix.

**A cross-store join exists.** The #219 federated semi-join is real, wired, and proven:
`router_service.answer` runs decomposed halves sequentially, `_carried_join_values` takes the
join-key values half A *showed*, and `retrieve_bound` wraps half B's own query as
`SELECT * FROM (<B>) AS _semi WHERE <key> IN (<values>)`.
No customer value reaches an LLM; the values are allowlist-sanitized and quote-safe.

It did not fire for a single one of the five D questions, and there are **three independent
gates**, each of which alone is enough to stop it:

1. **The question must decompose.** `decompose_query` splits on `versus | vs | compared to` or
   one top-level ` and `. "What is the total item revenue from customers located in the state
   of RJ?" contains none of them, so it never becomes two halves. It is one sentence, and the
   product treats one sentence as one store's problem.

2. **Half A must GROUP BY.** `bind_values_from_evidence` recovers the join key from half A's
   `GROUP BY` clause and returns `(None, [])` otherwise. Asked which customers are in RJ, the
   generator writes `SELECT customer_id FROM customers WHERE LOWER(customer_state)='rj'` - a
   plain projection. There is no GROUP BY, so nothing carries.

3. **Half B must GROUP BY too.** `retrieve_bound` binds on `groupby_column(sql)` of half B's
   *own* SQL. Half B here is a scalar `SUM(price)` with no GROUP BY, so there is no column to
   wrap and it falls open, unbound and disclosed.

Verified live, both phrasings, against the real pack:

    "What is the total item revenue from customers located in the state of RJ?"
        -> does not decompose -> each store answers alone -> "I do not have that information"
           (and the baseball store, routed on nothing, answered SELECT SUM(playerID) FROM batting)

    "Which customers are located in the state of RJ and what is the total item revenue
     of their orders?"
        -> decomposes -> half A returns two customer ids -> carry is None (no GROUP BY)
        -> half B runs UNBOUND: SUM(price) ... WHERE order_status = 'delivered'
        -> "I do not have that information"

Gold is 39180.04. Neither phrasing reaches it.

**So the honest statement of the gap.** #219 is a *breakdown-to-breakdown aligner*: it lines up
two grouped result sets that already share a key grain ("tickets per product" against "revenue
per product"). It was never a mechanism for **filter-here, measure-there**, which is the shape
of every ordinary cross-store question a user will ask.

That shape cannot be answered by fan-out. Each store is asked the whole question independently
and can only see its own columns, so the store holding the measure has no way to express the
filter. D-001 shows what that looks like from the inside: unable to see `customer_state`, the
generator went looking for the state code inside a timestamp column
(`WHERE LOWER(order_purchase_timestamp) LIKE '%rj%'`).

## Decision

**B - key-carry planning, scoped to a single tenant, with a disclosed key cap.**
Taken 260804 by the owner.

The framing below presented three live options. Two of them were not live:

- **A is not available.** "Search across all of your data" is the product's premise, and the
  consulting wedge is a customer who has documents in one place and records in another.
  Choosing A does not mean "build nothing"; it means retracting the claim and rewriting the
  canvas and the marketing copy to say the product searches each source separately. That is a
  product retreat wearing the costume of a cheap engineering option.
- **C is not available.** Joining two stores in one engine means materializing rows from both
  in one place. Inside a single tenant that is defensible; across tenants it breaks LAW 1
  outright, and it drops the pushdown property ADR 0007 exists to protect. C buys question
  shapes B cannot answer by spending a LAW. That is the wrong trade for this product, and it
  is the one trade this architecture has consistently refused.

So the real decision was never *whether* - it was B, and the remaining judgement is how B
behaves at its correctness cliff. That is settled here too, consistent with the
accuracy-first objective (#488): **the carried key set is capped, the cap is enforced fail-
closed, and exceeding it produces a disclosed decline - never a partial total.** A wrong
number that looks right is the failure mode this whole measurement effort exists to remove;
a cross-store question that silently sums 200 of 2,000 customers would reintroduce it at the
exact moment the user trusts the feature most.

## Options

### A. Leave it. Answer single-store questions well; decline cross-store ones honestly.

Cheapest, and not obviously wrong: after #476 the product already declines rather than
inventing. The cost is that "search across all of your data" is not what the product does -
it searches each of your data sources, separately. For the consulting wedge (documents in
SharePoint) that may be enough; for the structured connectors it is a visible ceiling.

Requires: nothing. Requires *honesty*: the canvas and the marketing copy must not imply joins
across sources.

### B. Key-carry planning - generalize #219 from breakdowns to filters.

Teach the planner to recognize that a question's filter column and measure column live in
different stores, run the filter store first, carry its key values, and bind the measure
store's query to them. Concretely: decompose on the *schema*, not on the word "and"; let a
plain single-column projection carry (gate 2); let a scalar aggregate accept a bind by pushing
the `IN` list into its WHERE rather than wrapping its output (gate 3).

Real cost: this needs a cross-store schema plan before dispatch, which is a genuine planner,
not a regex. It also has a correctness cliff - the carried key list is capped at `top_k`, so a
filter matching 2,000 customers cannot be carried, and a partial carry silently produces a
wrong total. Any implementation must fail closed and disclose when the key set exceeds the cap.

Keeps LAW 1 (values move between the customer's own stores, never into a prompt) and LAW 2
(each store still trims to the caller's own permissions independently).

### C. Virtual federation - one query engine over all stores.

Register each store as a foreign table in an embedded engine (DuckDB, Trino) and let ONE SQL
query span them. Answers every shape, including the ones B cannot.

This is a different product architecture, and it collides with LAW 1 the moment two stores sit
in different tenants: joining them means materializing rows from both in one place. Inside a
single customer tenant it is defensible; across tenants it is not. It also drops the pushdown
property ADR 0007 exists to protect - the query no longer runs *in* the customer's database.

## Recommendation

**B, scoped to a single tenant, with a disclosed key cap** - it extends machinery that already
exists and already respects both LAWs, and it covers the question shape users actually ask.
C is the more powerful answer and the wrong one to reach for first: it trades a LAW for a
feature. A is the right answer only if structured connectors are not the wedge.

Whichever is chosen, the gap is now measured rather than assumed: `golden_pack_real`
capability D is 0/5 today and is the regression test for any of these.
