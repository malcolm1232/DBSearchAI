# How DBSearch.AI Was Built - Presentation Script

> Speaker script for a ~25-30 minute technical talk (19 slides).
> Each section = one slide. **SLIDE** is what to put on the screen, **SAY** is the script.
> Timings are cumulative targets, not rules.
> Line references are to this repo so you can open the code live if someone asks.

---

## Slide 1 - Title (0:00)

**SLIDE**
> **DBSearch.AI**
> Ask one question. Get one cited answer. From every system you already have.
> *And never see a row you weren't already allowed to see.*

**SAY**

"I want to show you a product where the hard part was not the AI.

Everybody in this room can wire an LLM to a database in an afternoon. What almost nobody does correctly is the part underneath: making sure the answer that comes back is one the person asking was already entitled to receive. That is the entire product. The AI is the easy 10%.

So this talk is really about one question - who is allowed to see what, and who gets to decide - and about the twelve or so places we got that wrong before we got it right."

---

## Slide 2 - The problem (2:00)

**SLIDE**
> A consulting firm's knowledge lives in:
> SharePoint · Azure SQL · Postgres · MySQL · Synapse · Cosmos · BigQuery · a folder on someone's laptop
>
> "Have we done this before?"
> "Which products have the most support tickets, and what revenue do they bring?"

**SAY**

"Take a professional services firm. Their institutional memory is scattered across eight systems that were bought by different people in different decades.

The question a partner actually wants to ask is 'have we done this before'. Or a cross-system one: 'which of our products generate the most support tickets, and how much revenue do those same products bring in'. Notice that second one - the tickets live in a support database, the revenue lives in a sales database, and nobody has ever joined them.

Today the answer to both is: ask three people, wait two days, get a guess.

The obvious move is to copy everything into one big index and put a chatbot on it. That move is a data breach with a nice UI. Because the moment you copy data out of a system, you leave that system's permissions behind."

---

## Slide 3 - The two things that must never happen (4:00)

**SLIDE**
> **LAW 1 - Data residency.** Customer content never leaves the customer's tenant.
> **LAW 2 - Permission-faithful.** No query returns a result the user isn't already authorized to see. Mandatory. Default-deny.
>
> *`SKILL.md` - 10 laws, and a gate that runs on every change*

**SAY**

"So before we wrote a line of feature code, we wrote the laws. They live in a file called SKILL.md at the root of the repo, and there are ten of them. Two matter more than the rest.

Law one: customer document content never leaves the customer's cloud. Not to us, not to our logs, not to our error reports, not to our analytics. We run inside their tenant. We architecturally cannot see their data, which means we cannot leak it, and it means their security review is short.

Law two: no query ever returns a result the user was not already authorized to see in the source system. The permission filter is mandatory and default-deny - meaning if we cannot prove you are allowed to see something, you do not see it. Silence is the safe answer.

And there is a gate. Every single change - every feature, every bug fix - runs a checklist against these laws before it merges. If a feature violates a law it gets redesigned. It never gets merged 'for now'. That sounds like process theatre until you see how many times it caught something."

---

## Slide 4 - The shape of the system (6:00)

**SLIDE**
```
        ┌──────── CUSTOMER'S CLOUD (DATA PLANE) ────────────┐
        │  Connectors → parse → embed → index               │
        │  Query API → route → SECURITY-TRIM → LLM → answer │
        │  Data, embeddings, LLM calls: ALL stay in here.    │
        └────────────────────▲──────────────────────────────┘
                             │ metadata + telemetry ONLY
        ┌────────────────────┴──── OUR CLOUD (CONTROL PLANE) ┐
        │  Releases · billing counters · health · onboarding  │
        └─────────────────────────────────────────────────────┘
```
> Air-gapped edition = the uplink turned **off**. A config flag, not a code fork.

**SAY**

"Two planes, one very narrow wire.

The data plane runs in the customer's own Azure subscription. Documents, embeddings, indexes, and every LLM call happen in there. The control plane is ours - it does releases, billing counters, health, onboarding - and it never receives customer content. Ever.

The only things allowed to cross upward are defined in a versioned schema we call the boundary contract: counts, costs, health percentages, a version number, a timestamp. Anything not in that contract is rejected at the wire and logged as a violation.

And here is the part that closes enterprise deals: for a customer who forbids any uplink at all - defence, some banks - the air-gapped edition is that wire turned off. A config flag. Not a fork, not a special build, not a different product we have to maintain. Same artifact."

---

## Slide 5 - A user arrives (8:00)

**SLIDE**
> Three identity modes, chosen automatically:
>
> | mode | when | identity from |
> |---|---|---|
> | dev | no login configured | a header (a switcher) |
> | demo | login configured, not signed in | `demo:alice`, sandboxed |
> | live | signed in | the session cookie |
>
> `api/auth.py:81` - **one** resolver. REST, GraphQL, everything.

**SAY**

"Let's follow an actual user.

They land on the canvas - a board where you drag your data sources on as nodes. Before anything else happens, the server has to answer one question: who is this?

There is exactly one function in the entire codebase that answers it. api/auth.py, line 81, resolve_identity. Every transport calls it - the REST endpoints, the GraphQL layer, all of it.

That is not tidiness, it is a scar. We used to have two resolvers. The REST path got tightened to require a real login. GraphQL kept its old behaviour. So for a window there, an unauthenticated caller could send a header saying 'I am Alice' to the GraphQL endpoint and receive Alice's documents. Perfectly permission-trimmed documents. Trimmed to the wrong person.

The rule we took from that: a security decision belongs *below* every transport, not once per transport. Because the second copy is the one that drifts, and you will not notice."

---

## Slide 6 - Entra sign-in, the concepts (10:00)

**SLIDE**
> **Entra ID** = the org's directory: users, groups, app registrations
> **Tenant** = one organization. **oid** = your immutable id inside it.
>
> **Delegated** permission → the app acts **as you**
> **Application** permission → the app acts **as itself** (admin consent)
>
> We use **both**, for different jobs.

**SAY**

"Now they click sign in. Let me spend a minute on Entra itself, because the distinction on this slide is the hinge of the whole design.

Entra ID is Microsoft's identity directory - it used to be called Azure AD. It holds an organization's users, its groups, and registrations for the apps allowed to talk to it. One organization is a tenant. Your identity inside that tenant is an object id, an 'oid', and it never changes. That oid is the primary key of this entire product.

When you register an app, you ask for permissions, and they come in two flavours.

Delegated permissions mean the app acts as the signed-in user. The token carries their identity. The app can never do more than that person could do themselves.

Application permissions mean the app acts as itself, with no user present. That is tenant-wide power, so it needs an administrator to consent.

We use both, deliberately, for two different jobs. We use application permission to *read the directory* - to find out which groups you belong to. We use delegated permission to *touch data* - to run a query as you.

That is the difference between 'I need to know who you are' and 'I need to act as you'. Getting those backwards is how products end up with a service account that can read everything."

---

## Slide 7 - The authorization code flow (12:00)

**SLIDE**
```
Browser              DBSearch                Microsoft
   │ /auth/login →       │                       │
   │ ← 302 to Microsoft  │                       │
   │ ─────── sign in (password, MFA) ──────────→ │
   │ ← 302 back  ?code=abc                       │
   │ /auth/callback ─→   │                       │
   │                     │ POST /token           │
   │                     │ code + client_secret →│
   │                     │ ← id_token            │
   │                     │   access_token        │
   │                     │   refresh_token       │
   │ ← Set-Cookie        │                       │
```
> The browser **never holds a token.**

**SAY**

"Here is the flow. Watch what the browser is trusted with.

We redirect the user to Microsoft. They authenticate there - password, MFA, whatever conditional access policies the company has. We never see their password, which is the point.

Microsoft redirects them back to us with a code in the URL. That code is a one-time voucher with a short lifetime and it is completely useless on its own.

Then our server - not the browser - posts that code to Microsoft's token endpoint, together with our client secret. Only then do the actual tokens exist, over a direct TLS connection between our server and Microsoft.

So the browser never holds a token. Nothing sensitive lands in a URL bar, in browser history, or in a Referer header leaking to some analytics script.

We get three things back and each has a different job. The id_token says who you are. The access token lets us call one specific resource. The refresh token lets us get new access tokens later without dragging you through a login screen again - and we only get it because we explicitly asked for the offline_access scope."

---

## Slide 8 - Three tokens, three jobs (13:00)

**SLIDE**
> **OAuth** answers "what may this app call?" · **OIDC** answers "who is this person?"
> We ask for both in one round trip.
>
> | token | job | audience | lifetime |
> |---|---|---|---|
> | `id_token` | **who you are** | our app | minutes |
> | `access_token` | **what we may call** | one resource | ~1 hour |
> | `refresh_token` | **acting as you, later** | the token endpoint | days to months |
>
> We keep the third. The first two are used and discarded.

**SAY**

"Quick vocabulary, because these three get conflated constantly and the difference is the product.

OAuth 2.0 answers 'what may this application call' and hands you an access token. OIDC is a thin layer on top that answers 'who is this person' and hands you an id_token. We ask for both in one round trip. That is why the scope string has two halves: openid, profile, email is the OIDC part; offline_access and the database scope is the OAuth part.

The **id_token** is proof of identity, for us. Addressed to our app, lives for minutes, and says: this is Alice, here is her object id, here is her tenant.

The **access token** is addressed to exactly one resource. A token for Microsoft Graph cannot be presented to Azure SQL. That scoping is not a formality, it is what stops one leaked token from being a master key.

The **refresh token** is the interesting one. Addressed to the token endpoint, lasts days to months, and it exists only because we explicitly asked for the offline_access scope. If you do not ask, you do not get one.

We keep that third one, in a server-side vault. The first two we use and throw away. And that vaulted refresh token is what makes the entire product possible, because it is what lets us later open a connection to a customer's database as the user rather than as ourselves. Hold that thought, I come back to it in about ten minutes."

---

## Slide 9 - The decision people ask about (14:00)

**SLIDE**
> We read `oid` straight from the `id_token` payload.
> **No JWKS signature verification.** `user_auth.py:188`
>
> Why that is correct here - and would be a critical bug elsewhere.

**SAY**

"Here is a decision that gets challenged in every code review, so let me pre-empt it.

We read the user's oid straight out of the id_token's payload. We do not verify the JWT signature against Microsoft's public keys.

Why do you normally verify a JWT signature? Because you received the token from somewhere untrusted - a browser handed it to you, a client sent it in a header - and you need cryptographic proof that Microsoft actually issued it and nobody tampered with it.

That situation does not exist here. This token arrived as the response body of a direct TLS request that *our server* made to login.microsoftonline.com, authenticated with our client secret. There is no attacker position in the middle. TLS already proved the origin. The signature check would be re-proving the same fact.

But - and this is the part to say out loud - flip any of those conditions and it becomes a critical vulnerability. An implicit flow, where the browser hands you the token: you must verify. An API accepting a bearer token from a client: you must verify. The reasoning is entirely about the channel, not the token.

The lesson I would actually take from this slide: write the *why* next to the decision. That rationale is in the module docstring. Without it, the next engineer either rips it out and adds latency, or copies the pattern somewhere it is genuinely unsafe."

---

## Slide 10 - Four things happen in the callback (16:00)

**SLIDE**
> | | artifact | purpose | lives |
> |---|---|---|---|
> | a | `oid`, `tid`, `name` | **who you are** | read once |
> | b | transitive group oids | **what you may see** | identity adapter |
> | c | refresh token | **acting as you** later | server-side vault |
> | d | signed cookie | **staying signed in** | browser, 8h |

**SAY**

"Four things happen in the callback, and they are four genuinely different purposes. People collapse them and that is where bugs live.

A - identity. Who you are. Read once from the token and largely discarded.

B - authorization. We call Microsoft Graph and ask: what groups is this person in? Note the word *transitive*. Groups nest. Alice is in Deal Team, which sits inside Sales, which sits inside All Staff. A document shared with All Staff must reach her. If you query direct membership only, you deny an entitled user, and you will never get a bug report that says 'transitive' - you will get 'search is broken'.

C - the refresh token goes into a server-side vault. Never in the cookie, never logged, never in a response body. This is the material that later lets us run a query as this person. I will come back to it, because it is the centre of the product.

D - a signed session cookie, httpOnly, eight hours. It contains an oid, a name, an email, an expiry. That is all. No tokens, no permissions, no groups. Nothing in that cookie is a capability."

---

## Slide 11 - The bug that taught us the most (18:00)

**SLIDE**
> `fetch_member_groups()` returns:
> **`None`** = the lookup failed
> **`[]`** = genuinely in no groups
>
> These used to be the same value.
>
> → a 2-second Graph blip became a **silent denial lasting until restart**
> → "I couldn't find anything you have access to" - about their own document

**SAY**

"This is my favourite bug in the codebase and it takes thirty seconds to explain.

The group lookup returned a list. Empty list meant 'this user is in no groups'. And when the call to Microsoft failed - a timeout, a throttle, a two-second blip - it also returned an empty list, because what else do you return.

Then we added caching. Perfectly reasonable optimisation. And now a two-second network blip gets cached as a permanent fact: this user belongs to no groups. Which, under a default-deny system, means this user can see nothing. Until someone restarts the process.

And what did the user see? 'I couldn't find anything you have access to about that.' About a document they wrote.

So now the function returns None for failure and empty-list for genuinely-no-groups, and they are handled completely differently. A failed lookup is never cached - we retry on the next request. And there is a second piece: on any request where the process has no groups for a valid session, we re-resolve them. Because a restart, or scaling out to a fresh worker, used to leave you with a perfectly valid login and silently vanished permissions.

The general lesson: **'I don't know' and 'nothing' are different answers, and if your type system cannot tell them apart, something eventually will - at the worst moment.**"

---

## Slide 12 - The attack the state parameter stops (20:00)

**SLIDE**
> A signed *expiry* alone binds nothing.
> Login endpoints are unauthenticated → an attacker fetches a valid `state` from the `Location` header.
>
> Victim clicks a link carrying the **attacker's** `code`
> → we vault the **attacker's** refresh token under the **victim's** oid
> → every later query runs as the attacker.
>
> Fix: random nonce + httpOnly cookie + HMAC + **single-use**. `sp_connect.py:83`

**SAY**

"One more security slide, because this one is genuinely subtle.

OAuth has a `state` parameter that round-trips through the identity provider. Textbook answer: it is CSRF protection. Our first version signed a timestamp - tamper-proof, expires, looks fine.

It binds nothing. Any browser's state validates in any other browser's callback. And our login endpoint is unauthenticated by definition, so an attacker just requests it and reads a perfectly valid state out of the redirect header.

Now, we support account linking - one session can hold a Microsoft credential and a Google credential. So: attacker starts a login, gets *their own* code, and lures a signed-in victim to click that callback URL. It is a top-level navigation, so SameSite=Lax sends the victim's session cookie. We accept it. And we vault the **attacker's** refresh token under the **victim's** identity.

The victim notices nothing. They keep using the product. And every query they run from that moment executes as the attacker.

That is not a session hijack, it is a credential swap, and it is far quieter. The fix is three parts: the state is a random nonce, mirrored into an httpOnly cookie the attacker cannot read or forge; it is HMAC'd so a tampered one dies before comparison; and it is single-use, cleared on the callback whatever happens. A state minted in one browser cannot validate in another."

---

## Slide 13 - Connecting a source (22:00)

**SLIDE**
> Drag a node → fill in fields → set the **ACL** → Compose up
>
> ```yaml
> - id: azure-deals
>   kind: azure_sql
>   acl: ["82d85111-…"]          # who may KNOW IT EXISTS
>   config: { server: ${AZURE_SQL_SERVER}, tables: [SalesLT.Orders] }
>   delegation: { kind: entra_refresh, … }   # ← "queries run as the signed-in user"
> ```
> **An empty ACL refuses to compose.**

**SAY**

"Connecting a source is a drag-and-drop on the canvas. You fill in the connection fields - and note the values are environment references, resolved server-side, so a secret never travels to the browser. You set an ACL: the list of people who may know this source exists. And you flip one toggle: 'queries run as the signed-in user'.

That toggle is the whole product, and I will unpack it on the next slide.

But look at the last line. A store with an empty ACL refuses to compose, and tells you why.

Here is what happened before that check existed. Default-deny means an empty ACL is visible to nobody. So you would connect a database, click Test Connection, and get a green node that says 'connection healthy, a record round-tripped'. It goes live. And then every single question returns 'I couldn't find anything you have access to.'

Every affirmative signal in the UI said WORKING while the store was unreachable by construction. That is worse than an error, because an error sends you to the right place. So: refuse, and say why.

**A component that cannot possibly work must never be allowed to look healthy.**"

---

## Slide 14 - The core bet: query as the user (24:00)

**SLIDE**
> We could store "user → which rows they may see".
> **Every bug in that mapping is a silent data breach.**
>
> Instead: exchange the vaulted refresh token for a **source-scoped** credential and let the source enforce.
>
> Azure → Entra + SQL row-level security
> GCP → Google sign-in + BigQuery row-access policies
> AWS → STS + Lake Formation
>
> **A DBSearch bug can return no results. It cannot return unauthorized rows.**

**SAY**

"This is the slide. If you remember one thing, this.

There are two ways to build permission-aware federated search.

Option one, the one everybody builds: connect with a service account that can read everything, and filter the results yourself according to a mapping you maintain of who may see what. It is easy, it is fast, and every bug in that mapping is a silent data breach. Silent, because nothing errors - somebody just receives rows they should not have, and nobody finds out for eighteen months.

Option two, the one we took: **query as the user**. We take that refresh token from the vault, exchange it for a credential scoped to that specific source, and open the connection as *them*. Azure SQL then applies its own row-level security. BigQuery applies its own row-access policies. Lake Formation applies its own grants.

We are not in the identity-to-policy business. The enforcement point is the one the customer already audits, already trusts, and already has a compliance story for.

And here is the property that follows, which is the sentence I would put on a billboard: **a bug in our code can produce no results. It cannot produce unauthorized rows.** We designed our failure mode. The worst thing a DBSearch bug does is annoy you.

The honest cost: it needs a consent flow per source at onboarding, and one token exchange per user per source. We cache with a safety margin. That is a real setup cost, paid once, and it is the correct trade."

---

## Slide 15 - Fail closed, always (26:00)

**SLIDE**
> Azure SQL, no delegated connection path wired:
> → **refuse the query.** Never fall back to the service account.
>
> A Google store, no Google credential linked:
> → **refuse.** Never substitute the Microsoft one.
>
> Binding a credential to a cloud: match by **parameter name**, never by shape.
> *"A security binding must never GUESS which cloud a credential belongs to."*

**SAY**

"Once you make that bet, you have to hold it everywhere, and 'fail closed' stops being a slogan and starts being annoying in a productive way.

If a store is configured for delegation but the delegated connection path is not available - a driver is missing, say - we refuse the query. We do not quietly fall back to the service account. Falling back would work. It would return correct-looking data. And it would break the promise silently, which is the worst possible outcome.

If a question routes to a Google-backed source and you have only linked Microsoft, we refuse and tell you to connect Google. We never substitute another cloud's credential.

And my favourite one, which reads like paranoia until you think it through. When we bind a credential provider to a cloud, we match on parameter *name*, never on function shape. Because if we mis-attribute one, we would take a Microsoft refresh token and post it to Google's token endpoint. That is not a failed query - that is transmitting one cloud's credential to another cloud.

The comment in the code says: a security binding must never guess which cloud a credential belongs to. Unrecognised shape, raise an error. Never guess."

---

## Slide 16 - Answering: three gates (28:00)

**SLIDE**
```
question
  ├─ GATE 1  catalog visibility     - can you even know this source exists?
  ├─ GATE 2  authorize per store    - query runs as you; source enforces
  └─ GATE 3  synthesis              - merge only post-trim evidence
```
> Gate 1 is **hereditary**: a store under a business unit you cannot see is invisible,
> even if the store's own ACL would admit you.

**SAY**

"Now the question itself. Three gates.

Gate one is catalog visibility, and it is stricter than it sounds. It does not just filter results - it filters what the router is even allowed to *consider*, and what it is allowed to *name in an explanation*. Because 'I checked the Executive Compensation database and found nothing' leaks the existence of that database, and existence is information.

And it is hereditary: if you cannot see a business unit, you cannot see anything under it, even if the individual store's ACL would admit you. That closes what is called an existence probe - using a child to learn about a parent.

There is a corollary we hold carefully: pinning a source that does not exist and pinning a source you cannot see return the *identical* response. An invisible source must be indistinguishable from a nonexistent one.

Gate two is the per-source authorization we just covered - the delegated credential.

Gate three: everything arriving at synthesis has already been trimmed, so merging is only ever subtractive. We can drop things and reorder them. We can never add. There is no path by which the final step reintroduces something a gate removed."

---

## Slide 17 - The routing (29:30)

**SLIDE**
> classify → decompose → score visible sources → select → **fan out in parallel**
>
> - a compound question splits and each half routes **independently**
> - a slow or broken source is **dropped and disclosed**, never fatal
> - a dispatch budget caps cost - and **says so**
> - "which products have the most tickets, and what revenue do they bring" → **federated semi-join**

**SAY**

"Briefly, the routing, because this is the visible cleverness.

We classify the question, and if it is compound we split it - and each half routes independently over your visible sources. So the tickets half finds the support database, the revenue half finds the sales database. Neither of those systems has ever heard of the other.

Then we execute in parallel with a deadline. If one source hangs or errors, it is dropped and the answer still comes back - with a line saying which source was omitted and why. There is a cost ceiling on how many sources one question may hit, and when it bites, it is disclosed.

And the piece I am proudest of: the two halves get *aligned*. The first half tells us which products actually top the ticket count, and those keys constrain the second half's query - a semi-join, across two databases in two clouds that have never been joined before. The values are allowlist-sanitised and quote-safe, and critically they never enter an LLM prompt. It is a mechanical join, not a hallucinated one."

---

## Slide 18 - Honesty as a feature (31:00)

**SLIDE**
> `DECLINED` ≠ `EMPTY`
> "I looked and found nothing" vs "this question is not about anything I hold"
>
> Asked for support tickets, a sales database once counted **order lines** and called them tickets.
>
> Every answer carries: citations · a re-runnable proof · what was dropped and why

**SAY**

"Last technical slide, and it is the one that makes this a product rather than a demo.

There is a distinction in the code between EMPTY and DECLINED. Empty means I ran the query and no rows matched. Declined means this question is not about anything I hold, so I am not going to answer it.

We learned that distinction the hard way. Somebody asked how many support tickets there were. The question routed to a sales database, which had no tickets - so it counted order lines and called them support tickets. Confident number. Real citation. Completely fabricated meaning.

So now: a source with no relevant tables declines, out loud, and that decline appears in the answer. There are guards on the generated SQL - it may only touch the tables the question actually retrieved, and only read.

And every answer carries a citation with a *proof*: the exact query we ran, signed with a token bound to you. Click 'verify' and it re-executes under your own permissions, live, in front of you. You do not have to trust us. You can check.

The distinction that runs through all of it: **not knowing is an acceptable answer. Guessing is not.**"

---

## Slide 19 - What it cost, what it bought (33:00)

**SLIDE**
> **The laws came before the features.** Ten laws, a gate on every change.
>
> - one identity resolver, below every transport
> - "I don't know" ≠ "nothing"
> - a broken thing must never look healthy
> - never guess which cloud a credential belongs to
> - design the failure mode: **no results, never wrong results**

**SAY**

"To close.

We wrote the laws before we wrote the features. That felt slow for about two weeks and has paid for itself many times over, because the gate catches things at design time, when they cost an afternoon, instead of at three in the morning in front of a customer.

Five things I would carry to any system that decides who sees what.

Put security decisions below every transport, not once per transport - the second copy is the one that drifts.

'I don't know' and 'nothing' are different answers. Make your types say so.

Something that cannot possibly work must never be allowed to look healthy. A green light on a dead component is worse than a red one.

Never let a security binding guess. Unrecognised means refuse.

And the big one: choose your failure mode on purpose. We pushed enforcement down into the systems that already own it, so the worst thing our bugs can do is return nothing. Every product makes this choice. Most make it by accident.

Thank you."

---

## Appendix - questions you will get, and the answers

**"Isn't querying as the user slow?"**
One token exchange per user per source, cached with a safety margin against expiry. Amortised to roughly nothing after the first query. The alternative - a permission mapping we maintain - is faster and is a breach waiting to happen.

**"What if the customer's source has no row-level security?"**
Then delegation buys you the source's table and object grants, which is still the source enforcing. Where there is no delegation path at all, there is a fallback: a row-policy predicate we compose. It is explicitly a fallback, it is reviewed as security-critical code, and it is never the default - because it is exactly the identity-to-policy mapping we set out not to own.

**"Why not just verify the JWT signature anyway? It's cheap."**
It is cheap, and adding it would not be wrong. The point of the slide is that the *reason* is written down, so the next person understands which property makes it safe here and does not copy the pattern into an implicit flow where it is a critical bug.

**"How do you stop the LLM from hallucinating?"**
Three ways, none of which is prompt engineering. Generated SQL may only reference tables the retrieval step actually returned, and only read. A source with nothing relevant declines rather than answering. And every claim carries a proof you can re-execute yourself under your own permissions.

**"What stops us from being locked in?"**
Every cloud capability sits behind an internal port with a per-cloud adapter, and cloud SDKs are imported lazily inside connection factories. The AWS build swaps the queue, the object store, the index, the identity provider - and the core logic does not change.

**"Where does the answer generation happen?"**
Inside the customer's tenant, on their model deployment. Law one covers embeddings and inference too, not just documents. A customer who wants an in-tenant open model instead of a hosted one changes configuration, not code.
