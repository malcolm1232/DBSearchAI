"""The anonymous doorway: `/c/{token}` (#605, ADR 0021).

This is the first and only place in the product where a caller with NO IDENTITY is answered
from a customer's documents. ADR 0021 is the record of that decision - possession of an
unguessable 128-bit token IS the authorization, as a deliberate, bounded exception to LAW 2 -
and its four invariants are the properties this file exists to hold:

  1. Scope is the thread's cited documents, snapshotted at share time. Never the workspace,
     never a store, never anything the conversation did not already cite.
  2. Read only, and never the bytes. There is no download route here and there must never be
     one; citations expose filenames and cited passages, nothing more - which is what
     `visitor_citations` below enforces, and which was NOT true until it existed (#612 review,
     Finding 1): the source uri of every cited document travelled to the stranger verbatim.
  3. Bounded in time. Enforced by `find_by_token`, which returns live rows only.
  4. Killable. Revoke drops the share row, and every fork dies with it because every route
     below resolves the row FIRST.

IT LIVES IN ITS OWN MODULE, not in app.py, and that is the point rather than tidiness. The
one property the whole design rests on is that NO ROUTE HERE DEPENDS ON `current_user` AND
NOTHING HERE MINTS A SESSION.

Every route that ANSWERS FROM CUSTOMER DOCUMENTS depends on `current_user`, which 401s when
there is nothing to resolve - so `/search`, the "Your data" listing, segment previews,
`/download`, `/ask/suggestions` and `/chat` in an unrelated conversation all refuse an
anonymous visitor without one line of code written to defend any of them individually (ADR
0021, "What is NOT granted").

Note the qualifier, which an earlier draft of this paragraph dropped (#605 review, Finding 2):
it is NOT true that every other route in the product depends on `current_user`. A set of
public-infra routes and a set of demo-safe router reads do not - health, the build id, the
login hops, the HTML shells, the demo catalog - and none of them has a customer document
behind it. THE SETS ARE NAMED HERE AND DELIBERATELY NOT COUNTED: `PUBLIC_INFRA_PATHS`,
`DEMO_SAFE_PATHS` and `ANONYMOUS_LINK_PATHS` in tests/selftest_demo_scope_boundary.py, whose
sweep walks the live route table and fails on any route in none of the three. The round-1 fix
put a NUMBER in this sentence and the number was wrong, which is how the finding it was fixing
happened in the first place: a count in prose is a fact with nothing checking it, it goes stale
the next time somebody adds a route, and nothing fails when it does. The counts that matter are
asserted in that file instead, by `test_the_unauthenticated_route_allowlists_are_the_expected_
size`, so growing any of the three is a visible, reviewed edit rather than a silent one.

Keeping this route family in a file of its own makes the property auditable in one place: if a
future change gives a visitor a bearer cookie `current_user` would accept, every one of those
data surfaces silently reopens, and NOTHING in the synthetic partition or the `link:` principal
would catch it - both only govern what happens after a caller has passed `current_user`, and a
visitor with a real session would have passed.

THE THREE VALUES, and getting any two of them confused is the whole risk surface:

  `link:<share_id>`              THE PRINCIPAL. The share's synthetic grantee - what the
                                 conv-scoped document grants actually name, and therefore what
                                 `GrantRegistry.live_principals_for` must expand for retrieval
                                 to be authorized at all. A NAME, never a credential.
  `link:<share_id>:<visitor_id>` THE FORK KEY. Conversation-store key only, never an
                                 authorization value. It is what makes the owner's transcript
                                 STRUCTURALLY unwritable by a stranger rather than merely
                                 un-written by convention, and what keeps two visitors from
                                 reading each other.
  `linkvisitor:<share_id>`       THE PARTITION. A value that equals no real partition
                                 anywhere - see `visitor_scope` for the #601 reason it must.

Swap the first two and either every visitor is unauthorized (a fork key names no grant) or the
visitor's cookie becomes part of an authorization value. They are separate functions with
separate names for that reason and must stay that way.

WHAT THE COOKIE IS NOT. `dbsearch_visitor` is a fork key, not a credential: holding it grants
NOTHING without the token in the URL, because every route resolves the share from the token
first and the cookie is only ever read afterwards to decide which private thread to append to.
Nobody should later "harden" it into something load-bearing - signing it, binding it to a
session, or checking it before the token would each turn a scratchpad key into a bearer
credential and would put this file back on the wrong side of the line ADR 0021 drew.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from dbsearch.ports.base import ReadScope
from dbsearch.server.conversation_shares import AUDIENCE_LINK, LINK_GRANTEE_PREFIX
from dbsearch.server.conversation_store import ConversationStoreUnavailable
from dbsearch.server.rate_limit import FixedWindowLimiter

#: The visitor's fork-key cookie. Deliberately NOT `user_auth.COOKIE` and deliberately not
#: signed: `resolve_identity` never looks at this name, so no value a visitor can put here can
#: ever become an identity, which is the property the six-surface probe depends on.
VISITOR_COOKIE = "dbsearch_visitor"

#: 16 bytes of CSPRNG hex. Validated on the way IN as well as minted here, so a hand-edited
#: cookie cannot smuggle a path separator, a colon or arbitrary length into a conversation
#: store key - a fork key is concatenated into `link:<share_id>:<vid>`, and a value containing
#: a colon could otherwise be crafted to collide with another visitor's fork. Anything that
#: does not match is replaced rather than refused: a malformed cookie is far more likely to be
#: an old build or a browser extension than an attack, and the correct answer to both is a
#: fresh fork, not an error page.
_VISITOR_RE = re.compile(r"v_[0-9a-f]{32}\Z")
_VISITOR_MAX_AGE = 90 * 86400

#: Scoped to the doorway's own path prefix. The cookie has no business riding on requests to
#: any other surface - it authorizes nothing there, and a cookie that is never sent is a cookie
#: that can never be mistaken for a session by a later reader of some unrelated route.
_VISITOR_COOKIE_PATH = "/c"

#: The synthetic partition prefix (ADR 0021, "Mechanism"). See `visitor_scope`.
LINK_PARTITION_PREFIX = "linkvisitor:"

#: THE VISITOR'S OWN DOCUMENT, and deliberately NOT `index.html` (#605 task 12).
#:
#: This route used to serve the app shell, and the shell was the wrong answer twice over.
#: First it did not work at all: `/c` is not one of app.py's SHELL_PATHS, and login.js decides
#: landing-versus-app from the path, so a visitor was shown the LANDING (marketing) view while
#: the app node was built inside a container that was never displayed. A real visitor got a
#: marketing page, and ADR 0021's disclosure - which lived in a module the shell never loaded
#: on this path - was rendered to nobody while the owner could already read what strangers
#: asked. Second, even had it rendered, the shell is a workspace: a navigation rail, a model
#: picker, Connectors, Admin, Developer, "New conversation", "Your data". A visitor has no
#: account and no workspace, and every one of those is a claim that is not true here.
#:
#: `/c` was therefore NOT added to SHELL_PATHS. That list means "paths which serve the app
#: shell"; a doorway with its own document does not belong in it, and adding it would have
#: registered a bare `GET /c` serving the shell to anybody.
VISITOR_PAGE = "visitor.html"

#: One page for revoked, expired and never-existed - see `_gone` below.
GONE_PAGE = "link_gone.html"

_log = logging.getLogger("dbsearch")

#: ADR 0021 consequences: "A per-link question rate cap bounds the cost of a forwarded link."
#: A named grantee's cost is bounded by how many people the owner is willing to name; a link
#: has no such ceiling by construction - it is forwardable to anyone, and every question a
#: visitor asks is a real LLM call the OWNER pays for. This is the substitute ceiling.
#:
#: REUSES `FixedWindowLimiter` FROM THE PUBLIC-DEMO LIMITER (`rate_limit.py`), keyed on
#: `share.share_id` instead of an IP address - a fixed-window counter is a fixed-window
#: counter regardless of what the key names. `per_ip` IS A NAME MISMATCH INHERITED FROM THAT
#: CLASS, not renamed here: `rate_limit.install` also constructs a `FixedWindowLimiter` and
#: depends on that constructor keeping the parameter named `per_ip`. Read `per_ip` as "per
#: share" everywhere in THIS file - it is never an IP address here.
#:
#: `global_limit=10_000` is deliberately NOT the rule doing the work day to day - it is a
#: backstop against many DIFFERENT owners' links being hammered at once on the same box,
#: sized well above any single link's `per_ip` (per-share) budget so the per-link cap below
#: is what actually fires in normal use. Removing it would leave one busy link free to spend
#: the whole box's LLM budget on questions counted under OTHER shares' keys.
#:
#: THE ENV VAR IS READ ONCE, HERE, AT MODULE IMPORT TIME. A process that later mutates
#: `os.environ["DBSEARCH_LINK_QUESTIONS_PER_HOUR"]` does not change this limiter's cap - a
#: test that needs a different cap must `importlib.reload` this module (see
#: `tests/selftest_605_link_rate_cap.py`), which re-runs this line against the new value.
_link_limiter = FixedWindowLimiter(
    per_ip=int(os.environ.get("DBSEARCH_LINK_QUESTIONS_PER_HOUR", "30")),
    global_limit=10_000,
    window=3600.0,
)


def _check_link_rate(share) -> None:
    """Refuse a question past THIS SHARE's hourly cap - ADR 0021 consequences.

    Keyed on `share.share_id`, never on the visitor and never globally:

      per-visitor  would be trivially defeated by clearing (or never sending) the fork-key
                   cookie - a visitor picks their own key.
      global       would let one busy link silence every OTHER owner's link on the same box
                   (see `_link_limiter` above for the actual global backstop and why it is
                   sized not to be the effective cap).

    Called explicitly at the top of BOTH `link_chat` and `link_chat_stream`, rather than
    folded into the shared `_ask_setup` both already call, so that removing the check from
    one route is a change to ONE call site and not the other - a rate-limit regression on
    either question route has to be caught by a test that actually exercises that route.

    Deliberately NOT called from `link_page` or `link_transcript`: reading costs the owner
    nothing (no retrieval, no LLM call), so capping a read would refuse a visitor re-reading
    a page they already paid for, or an owner checking her own share, for no matching saving
    on the bill this cap exists to bound. Only the two question routes drive a model call."""
    retry = _link_limiter.check(share.share_id)
    if retry is not None:
        raise HTTPException(status_code=429,
                            detail="too many questions on this link, try again shortly",
                            headers={"Retry-After": str(retry)})


#: THE KEYS A CITATION LOSES ON ITS WAY TO A STRANGER. Kept as a named set rather than spelled
#: out at each call site because both question routes must drop exactly the same ones - two
#: endpoints onto one capability, and a key dropped by only one of them is a key a caller picks
#: its way around by choosing the other, which is the same reasoning `_ask_setup` records.
_VISITOR_CITATION_DROPPED = ("uri", "locator")


def visitor_citations(citations) -> list[dict]:
    """A citation as a STRANGER may receive it: the document id, the title, and nothing else.

    ADR 0021 invariant 2 says, in full: "Read only, and never the bytes. `download` is 404 for
    a link visitor, always. Citations expose filenames and the cited passages, nothing more."
    Until this function existed the code did not do that. `QueryService._context_and_citations`
    builds `{doc, title, uri, locator}` and the link routes returned the dict verbatim, so a
    visitor was handed the SOURCE URI of every document the answer drew on - and the frontend
    (`static/js/ui/components.js`, `sourceCard`/`safeHref`) renders any `https?://` uri as a
    clickable link AND prints the URL text underneath it.

    THE URI IS NOT A FILENAME, and that is the whole of the defect. On a SharePoint or Drive
    backed deployment it is an internal LOCATION:

        https://acme.sharepoint.com/sites/Finance/Shared%20Documents/HR/Q3-layoffs.docx

    - the tenant, the site, the library, the folder path and the filename, which together
    describe how the owner's organization is arranged and what else is filed beside the
    document. None of that is the cited passage, none of it is what the owner chose to share,
    and a stranger holding a forwarded URL has no business receiving any of it.

    WHY THE VISITOR PATH AND ONLY THE VISITOR PATH. A signed-in caller's citation keeps its uri
    and must: she has an account in the deployment, the uri is how she opens the source she is
    already entitled to open, and #165's provenance argument rests on it. The link visitor is
    different in the one way that matters here - the audience is UNBOUNDED. A link is
    forwardable by construction (that is the feature), so "who receives this" is not a set the
    owner chose and cannot be reasoned about at all. A field that is merely inconvenient to
    over-share with a colleague is a disclosure when the reader could be anyone.

    DELIBERATELY NOT DONE BY CHANGING THE SHARED CITATION BUILDER. `_context_and_citations` is
    the one place both `/search` and `/chat` get their citations from, and narrowing it would
    take the uri away from every signed-in surface to fix an anonymous one. This is a property
    of the AUDIENCE, so it belongs where the audience's response is assembled - here.

    `locator` goes with it for the same reason and a smaller one of its own: it is an internal
    offset/page pointer into the owner's copy of the document, it renders nowhere on the
    visitor page, and a value nobody displays but everybody serializes is exactly how the uri
    itself travelled unnoticed for a whole branch.

    Rebuilds each dict rather than popping keys out of the retrieval result's own objects: the
    same citation list is handed to `record_query` for the OWNER's audit trail, where the uri
    is wanted, and mutating in place would quietly strip it there too."""
    return [{k: v for k, v in (c or {}).items() if k not in _VISITOR_CITATION_DROPPED}
            for c in (citations or [])]


def link_principal(share) -> str:
    """THE PRINCIPAL: the share's synthetic grantee, `link:<share_id>`.

    Read off the RECORD rather than rebuilt from a request value, so this is always the exact
    string `share_conversation` granted every cited document to. Note it is `share.grantee_oid`
    by construction (`create_link` mints the row with this grantee), and is spelled out from
    the id here so a caller reading this file sees where the value comes from."""
    return f"{LINK_GRANTEE_PREFIX}{share.share_id}"


def fork_key(share, visitor_id: str) -> str:
    """THE FORK KEY: this visitor's private conversation-store key under the shared thread.

    ADR 0021 invariant, and the reason the owner's transcript cannot be written by a stranger:
    conversation history is keyed by (conv_id, user_oid), so a visitor asking inside
    `share.conv_id` under THIS key appends to a thread only they and the owner's own read paths
    can name. Nothing in this file ever appends under `share.grantor_oid`, which is what makes
    "the owner's thread is unwritable" a structural fact rather than a convention that the next
    person to add a route has to remember.

    Never an authorization value. It names no grant, expands to no principal, and is not passed
    to retrieval anywhere - see `link_principal` for the value that is."""
    return f"{LINK_GRANTEE_PREFIX}{share.share_id}:{visitor_id}"


def visitor_scope(edition, share) -> ReadScope:
    """WHERE a link visitor may look: a partition that matches nothing, plus one doorway pair
    per document this share actually granted.

    THE PARTITION IS SYNTHETIC AND THAT IS THE WHOLE DESIGN. `ReadScope.allows` is
    `tenant_id == self.partition or (tenant_id, doc_external_id) in self.doorway` - the
    partition arm SHORT-CIRCUITS TRUE before the doorway is ever consulted. That short-circuit
    is defect #601, which shipped on the previous branch: conversation scoping was expressed in
    partition routing, and grantor and grantee sharing a partition is not an edge case, it is
    every self-host box and every single-organization Entra tenant - so on that shape the
    doorway was never reached and the rule was never applied at all. Reproduced end to end:
    /search with no conversation, the "Your data" row, segment previews, and the raw file bytes.

    A link visitor has no real partition to begin with (no session, so nothing for
    `resolve_tenant` to canonicalize). Handing them a REAL one - the deployment's home
    partition, or the grantor's - would rebuild #601 exactly: `allows` would return True for
    every document in the grantor's workspace BEFORE THE DOORWAY RAN, and every scoping rule
    written above it would be dead code. `linkvisitor:<share_id>` cannot equal any tenant's
    real partition, so the partition arm can never fire for a visitor and every document a
    visitor reaches must come through the doorway AND then through the ACL overlap - two
    independent refusals, the same two a signed-in grantee's access already depends on.

    THIS LAYER IS DEFENCE IN DEPTH AND IS KEPT AS THAT, stated precisely because the ADR's
    first draft overstated it (#605 review, Finding 1). The ACL overlap still runs whatever
    this partition says, so a real partition here does NOT, today, hand an excluded document to
    a visitor over HTTP - reproduced during review. What actually holds that line at HTTP level
    is narrower than it looks: a visitor's principals are the sentinel plus this share's
    conv-scoped grants, and no other document's ACL names any of them. This partition is the
    layer that holds when THAT stops being true - an org-audience principal reaching a visitor,
    a #601-shaped regression on the ACL side, an identity adapter that resolves an unknown oid
    to something non-empty. It is not the layer doing the work in the common case, and pinning
    it needs a test that probes routing on its own (selftest_605's
    `test_the_visitor_partitions_routing_arm_can_never_fire`), because an end-to-end test
    passes on the ACL alone.

    This is why `_request_scope` is NOT called here and must never be: it derives a REAL
    partition from the request (`resolve_tenant`), which is precisely the value this scope may
    not have. The two functions look similar and mean opposite things.

    THE DOORWAY comes from grant RECORDS, never from anything a client sent. It is narrowed the
    same two ways the transcript's `granted` set is: to this share's own conversation, and to
    grants THIS share's grantor made - conv_id is client-chosen, so somebody else's grant under
    a colliding id is not part of this share and must not open a door here.

    `active_conv_id` rides along because it is the OTHER half of "where this read may look":
    `GrantRegistry.live_principals_for` expands a conv-scoped grant's principal only while its
    conversation is active and drops it by default (ADR 0020). That is the ACL-side guarantee;
    the doorway below is the routing-side backstop. Either one alone would have to fail before
    a visitor saw anything this share does not cover.

    NOTE what is deliberately NOT applied: `_is_foreign_partition`. That rule (ADR 0019 D1) is
    about the partition a signed-in CALLER reads FROM, and refuses to open a doorway across
    into a foreign tenant on the strength of a grant. A link visitor reads from no tenant at
    all - the grantor deliberately published a doorway out of her own partition, whichever one
    that is, and filtering on it here would make a link minted by a foreign-tenant customer
    open nothing while reporting success."""
    principal = link_principal(share)
    pairs = {(g.tenant_id, g.doc_external_id)
             for g in edition.grant_registry.live_grants_for(principal)
             if g.conv_id == share.conv_id and g.granted_by == share.grantor_oid}
    return ReadScope(partition=f"{LINK_PARTITION_PREFIX}{share.share_id}",
                     doorway=frozenset(pairs),
                     active_conv_id=share.conv_id)


def granted_docs(edition, share) -> "set[str]":
    """The documents this share grants RIGHT NOW - the transcript's re-check set.

    Same records, same two narrowings and the same per-read evaluation as `visitor_scope`'s
    doorway, because if the two could disagree a visitor would end up either reading a turn
    about a document they cannot retrieve, or retrieving a document whose turn is withheld.
    Both directions are the grant-channel-versus-content-channel split this feature has
    produced three separate defects from."""
    principal = link_principal(share)
    return {g.doc_external_id for g in edition.grant_registry.live_grants_for(principal)
            if g.conv_id == share.conv_id and g.granted_by == share.grantor_oid}


def build_link_access_api(edition, *, html, corpus_for_scope, readable_prefix,
                          cookie_secure) -> APIRouter:
    """The `/c` router. A FACTORY taking its seams, the same shape `build_router_api` and
    `build_secrets_api` already use - and here it is also what keeps the dependency one-way:
    app.py imports this module, this module imports nothing from app.py, so there is no import
    cycle to break later by moving a helper.

    The seams are deliberately four small callables rather than "app.py":
      `html`              - the rendered shell, with its build id embedded (#265).
      `corpus_for_scope`  - the honest denominator (#393), computed from an already-built scope.
      `readable_prefix`   - WHICH grantor turns a share hands over. THE SAME function
                            `conversation_transcript` calls, so the signed-in grantee and the
                            anonymous visitor cannot drift on the boundary (see its docstring).
      `cookie_secure`     - `_cookie_secure()`, never a hardcoded True: a `Secure` cookie is
                            silently DISCARDED over http, so hardcoding it would make the
                            visitor cookie undeliverable on every local rig and every self-host
                            box on plain http - a worse failure than the one it would fix, and
                            the same reasoning every session cookie in app.py already follows.
    """
    api = APIRouter(prefix="/c")

    def _live_share(token: str):
        """Resolve a token to the live share it opens, or 404.

        THE GATE IS THAT A LIVE ROW MATCHED THE TOKEN HASH. `find_by_token` compares SHA-256
        digests in constant time and returns live rows only, so "revoked", "expired" and "never
        existed" are one answer here by construction - and they must stay one answer, because
        the caller is anonymous and a distinguishable refusal would confirm to anyone sweeping
        the token space that a particular value was once real.

        The `audience` refusal beside it is DEFENCE, not the gate, and the distinction matters:
        `audience` is an unvalidated client-supplied string on the create path and must never
        be an authorization fact on its own. What actually authorizes is the hash match on a
        live row. What this line adds is that a row which is not a link - a people share that
        somehow carries a token hash, through a migration, a future code path, or a bug - can
        never be opened anonymously, since its recipient was chosen on the understanding that
        she would sign in. Fail-closed on a field whose fail-closed value is "people"."""
        share = edition.conversation_shares.find_by_token(token)
        if share is None or share.audience != AUDIENCE_LINK:
            raise HTTPException(status_code=404, detail="not found")
        return share

    def _gone() -> Response:
        """The dead-link page: 404, and BYTE-IDENTICAL for revoked, expired and never-existed.

        Same one answer `_live_share` already gives the JSON routes, rendered for a human
        instead of raised as a `detail` string, because the page route is the one a person
        lands on by clicking. What matters is that it is one page and not three: the caller is
        anonymous by construction, so a page that distinguished "revoked" from "no such token"
        would confirm to anyone sweeping the token space that a value was once real. It is the
        same file every time, it carries no token, no owner and no date, and it runs no
        JavaScript - so there is no boot sequence that could fetch a fact and render a
        difference into it.

        NO FORK-KEY COOKIE IS STAMPED HERE, and that is not an oversight: a dead link has no
        thread to key, and a Set-Cookie header present on one dead token's response and absent
        on another's would be exactly the distinguishable refusal this page exists not to be.

        `no-store` rather than the shell's revalidation: a share that dies must not leave a
        cached "gone" page that outlives a REPLACEMENT link minted at a new token, and there is
        nothing here worth a round trip to keep."""
        page = html(GONE_PAGE)
        return Response(content=page.body, status_code=404, media_type="text/html",
                        headers={"Cache-Control": "no-store"})

    def _visitor_id(request: Request) -> "tuple[str, bool]":
        """This visitor's fork key id, and whether it is newly minted.

        Returns the pair rather than setting a cookie itself because two of the four routes
        below return a `Response` object directly (the shell, and the SSE stream), and FastAPI
        does NOT merge an injected `Response`'s headers into a response a handler returns
        whole - a cookie set that way would silently never reach the browser on exactly the
        page that has to mint it. Every route stamps the concrete response it returns, through
        `_stamp` below, so all four behave identically.

        `secrets.token_hex`, never `random`: two visitors colliding would merge their private
        threads, which is the one thing forks exist to prevent."""
        vid = request.cookies.get(VISITOR_COOKIE, "") or ""
        if _VISITOR_RE.match(vid):
            return vid, False
        return "v_" + secrets.token_hex(16), True

    def _stamp(response: Response, visitor_id: str, fresh: bool) -> Response:
        """Attach the fork-key cookie to the response actually being returned.

        `httponly` because no script has any business reading it; `samesite=lax` so a link
        followed from mail or chat still carries the fork the visitor already has, while a
        cross-site POST does not. `secure` follows the deployment, see `cookie_secure` above.

        Set only when freshly minted: re-stamping an existing value on every request would
        keep sliding its expiry, which is a tracking behaviour nobody asked for."""
        if fresh:
            response.set_cookie(VISITOR_COOKIE, visitor_id, httponly=True, samesite="lax",
                                secure=cookie_secure(), max_age=_VISITOR_MAX_AGE,
                                path=_VISITOR_COOKIE_PATH)
        return response

    def _question(body: dict) -> str:
        q = str((body or {}).get("question") or "").strip()
        if not q or len(q) > 4000:
            raise HTTPException(status_code=400, detail="question must be 1-4000 characters")
        return q

    def _visitor_turns(share, visitor_id: str) -> list[dict]:
        """THIS visitor's own half of the thread. Read under the fork key, so it can contain
        nobody else's questions - `two visitors never see each other` is this one line."""
        own = edition.conversation_service.history(fork_key(share, visitor_id), share.conv_id)
        return [{"seq": i, "question": t.question, "answer": t.answer, "own": True}
                for i, t in enumerate(own)]

    @api.get("/{token}")
    def link_page(token: str, request: Request) -> Response:
        """The share page - THE VISITOR'S OWN DOCUMENT (`VISITOR_PAGE`), not the app shell.

        Page shell only for everything that is a FACT: the transcript and the answers arrive
        through the JSON routes below, which resolve the token again, so a page served from a
        browser cache can never be the thing that discloses anything.

        THE ONE EXCEPTION, AND IT IS DELIBERATE: ADR 0021's disclosure sentence is static
        markup in that file, so it is in the bytes handed to a cookie-less visitor with no
        fetch, no boot sequence and no route decision standing between "the page loaded" and
        "the visitor has been told". It cost this feature a whole task to learn that a
        disclosure which depends on a later fetch is a disclosure that can be missing while
        every test is green. See `VISITOR_PAGE` for why this is not `index.html`.

        A dead token renders `_gone()` here, rather than raising the JSON 404 the routes below
        raise: this is the route a person lands on by clicking, and `{"detail":"not found"}` is
        not a page. It is still a 404, and still one answer for revoked, expired and
        never-existed. THE GATE IS UNCHANGED AND STILL LIVES IN ONE PLACE - `_live_share` - so
        the page cannot drift from the JSON routes about what "live" means; only the shape of
        the refusal differs.

        `record_open` counts the visit (ADR 0021: how often a link was opened is the only
        signal an owner gets that it travelled further than she meant). It NEVER RAISES, which
        is why it is safe on a page load - see its docstring for why a counter is the one thing
        here allowed to fail silently.

        IT IS THE PRODUCT'S ONLY UNAUTHENTICATED WRITE THAT A PLAIN PAGE READ CAN DRIVE, which
        is why its durable half is coalesced rather than per-request (#612 review, Finding 2 -
        `OPEN_PERSIST_INTERVAL_SECONDS`).

        Not "the only unauthenticated write", which is what this said first and is false (#612
        re-review, Finding B): `link_chat` and `link_chat_stream` below write a
        `conversation_turns` row and an audit row for an anonymous caller too. The difference
        is what bounds them - both go through `_check_link_rate` and cost an LLM call each, so
        they are ceilinged twice; a page load was ceilinged by nothing at all.

        The in-memory count is still exact and immediate; the
        Postgres write behind it happens at most once per share per interval, so a Slack unfurl
        bot, a crawler or a loop on one forwarded link can no longer drive one connect-and-auth
        per request through this sync route. The READ is not capped and must not be - see
        `_check_link_rate` for why - so the ceiling belongs on the write, which is the only
        part of a page load that costs anything."""
        try:
            share = _live_share(token)
        except HTTPException:
            return _gone()
        vid, fresh = _visitor_id(request)
        edition.conversation_shares.record_open(share.share_id)
        return _stamp(html(VISITOR_PAGE), vid, fresh)

    @api.get("/{token}/transcript")
    def link_transcript(token: str, request: Request) -> Response:
        """What this visitor may read: the grantor's shared prefix, then their own fork.

        THE PREFIX IS NOT STORED, IT IS RE-DERIVED ON EVERY READ, through the SAME
        `_readable_prefix` the signed-in grantee's transcript uses: `turn_cutoff` bounds what
        was handed over, and each turn is re-checked against the grants that are LIVE RIGHT NOW.
        A share whose documents have gone - deleted, narrowed, revoked - stops serving the
        turns that drew on them, including their answer TEXT. Three defects on the previous
        branch closed the grant channel and left that content channel open beside it.

        `own: false` at the top level says this reader is reading through a share, and each
        grantor turn carries `own: false` of its own so the surface renders that half read-only.
        `disclosure: true` is the flag ADR 0021's accepted consequences make mandatory: the
        owner can see the questions strangers ask here, and a visitor typing into this page has
        to be told so at the point of asking.

        404 when nothing is readable, which is the same rule (and the same silence) the
        signed-in transcript applies: when the last grant behind a share goes, the share opens
        nothing at all rather than a thread of bare questions."""
        share = _live_share(token)
        vid, fresh = _visitor_id(request)
        granted = granted_docs(edition, share)
        try:
            theirs = (edition.conversation_service.history(share.grantor_oid, share.conv_id)
                      if granted else [])
            turns = readable_prefix(share, theirs, granted)
            turns += _visitor_turns(share, vid)
        except ConversationStoreUnavailable:
            raise HTTPException(status_code=503,
                                detail="cannot read the conversation right now, try again "
                                       "shortly")
        if not turns:
            raise HTTPException(status_code=404, detail="not found")
        # A FLOOR, not a fix for anything currently broken: `readable_prefix` and
        # `_visitor_turns` both build {seq, question, answer, own} and neither carries a
        # citation today, so this strips nothing right now. It is here because the day a turn
        # DOES carry its citations - the obvious next thing to want on this page - the uri
        # would ride along unnoticed, which is exactly how it rode along on the ask routes for
        # a whole branch. The rule belongs on the route family, not on the two places that
        # happen to serve a citation this week. See `visitor_citations`.
        turns = [({**t, "citations": visitor_citations(t["citations"])} if "citations" in t
                  else t) for t in turns]
        return _stamp(JSONResponse({"turns": turns, "own": False, "disclosure": True}),
                      vid, fresh)

    def _ask_setup(token: str, request: Request, body: dict):
        """Everything the two ask routes must do IDENTICALLY, in one place.

        They are two endpoints onto one capability, so a rule held by only one of them is a
        rule a caller picks its way around by choosing the other - which is why the scope, the
        principal, the fork key and the model choice are all decided here rather than twice.

        THE MODEL IS THE DEPLOYMENT'S DEFAULT AND IS NOT NEGOTIABLE BY A VISITOR. The signed-in
        `/chat` honours `req.model` because the person choosing is the person whose deployment
        pays for it. A link is forwardable by construction (ADR 0021's whole cost argument), so
        letting an anonymous caller select the most expensive configured model would hand a
        stranger a knob on the owner's bill that no rate cap counting QUESTIONS can bound."""
        share = _live_share(token)
        vid, fresh = _visitor_id(request)
        question = _question(body)
        scope = visitor_scope(edition, share)
        return (share, vid, fresh, question, scope,
                edition.resolve_chat_llm(None), fork_key(share, vid),
                link_principal(share))

    @api.post("/{token}/chat")
    def link_chat(token: str, body: dict, request: Request) -> Response:
        """A visitor asks their own question, answered from exactly this share's documents.

        The two identities go to the two places they belong: the FORK KEY keys the conversation
        store (so this turn lands on a thread only this visitor reads, and never on the
        owner's), and the PRINCIPAL drives retrieval (so the ACL overlap expands the share's
        conv-scoped grants, which is the only reason anything is authorized at all). The
        retrieval itself is the unchanged, permission-trimmed `ConversationService` core the
        signed-in `/chat` runs - there is one retrieval path in this product and this is not a
        second one.

        The corpus denominator is computed for the PRINCIPAL and the SAME scope the retrieval
        used, never re-derived: a footer that counted a different identity's documents from the
        one that answered is the #393 mismatch, and here it would also be the honest number a
        visitor uses to judge how much of the workspace they are looking at."""
        share, vid, fresh, question, scope, gen_llm, fork, principal = _ask_setup(
            token, request, body)
        _check_link_rate(share)
        r = edition.conversation_service.ask(fork, share.conv_id, question, llm=gen_llm,
                                             tenant_id=scope, retrieval_oid=principal)
        # Audited under the FORK KEY: the audit trail records who asked what, and for a visitor
        # "who" is the fork - anonymous, but distinguishable, which is exactly what the owner's
        # question log needs and is a strict improvement on recording nothing.
        edition.record_query(fork, question, r, surface="link")
        _touch(r.retrieved_owners)
        # `visitor_citations`, never `r.citations`: the uri is an internal source LOCATION and
        # the audience of a forwardable link is unbounded. See that function for the full
        # account; the audit call above deliberately runs FIRST, on the unstripped result, so
        # the owner's own trail keeps what the stranger does not get.
        return _stamp(JSONResponse({"answer": r.answer,
                                    "citations": visitor_citations(r.citations),
                                    # #736: the stream twin forwards its done event by
                                    # SUBTRACTION (it drops `retrieved_owners` and keeps the
                                    # rest), so it has carried `referenced` since #724 while
                                    # this route, which hand-picks its keys, silently did not.
                                    # Latent rather than harmful - `visitor.js` derives the
                                    # pointed-at set client-side today - but it is exactly the
                                    # asymmetry `_ask_setup` says must not exist, and the day a
                                    # visitor surface reads it, choosing the JSON route would
                                    # quietly disable the #724 suppression.
                                    "referenced": r.referenced,
                                    "retrieved_docs": r.retrieved_docs,
                                    "authorized_docs": r.retrieved_docs,   # deprecated (#393)
                                    "corpus": corpus_for_scope(principal, scope),
                                    "conv_id": share.conv_id}),
                      vid, fresh)

    @api.post("/{token}/chat/stream")
    def link_chat_stream(token: str, body: dict, request: Request) -> Response:
        """SSE twin of `link_chat` (#50), and the same rules by the same calls.

        The scope and the denominator are measured BEFORE the generator runs, because the
        streaming body is produced after the request scope closes and `request` is not safe to
        touch from inside it (#393)."""
        from dbsearch.query.service import QueryResult

        share, vid, fresh, question, scope, gen_llm, fork, principal = _ask_setup(
            token, request, body)
        _check_link_rate(share)
        corpus = corpus_for_scope(principal, scope)

        def sse():
            final = None
            for ev in edition.conversation_service.ask_stream(
                    fork, share.conv_id, question, llm=gen_llm, tenant_id=scope,
                    retrieval_oid=principal):
                if ev.get("type") == "done":
                    ev = {**ev, "corpus": corpus}
                    final = ev
                    # #576 review round 2, Finding B: `retrieved_owners` is an ACCOUNT ID and
                    # must never reach the wire. It matters more here than on /chat: the
                    # recipient is a stranger, and the oid would name the owner of a document
                    # to somebody who was given the document's contents and nothing else.
                    #
                    # The citations are narrowed by the SAME rule and in the same breath: the
                    # source uri is an internal location, and this route is the other half of
                    # one capability, so a key `link_chat` drops and this one keeps is a key a
                    # caller picks up by choosing the stream. See `visitor_citations`.
                    #
                    # `final` is bound to the UNSTRIPPED event on purpose - `record_query`
                    # below builds the owner's audit trail from it, and the owner is entitled
                    # to the uri her own documents were cited by.
                    wire_ev = {k: v for k, v in ev.items() if k != "retrieved_owners"}
                    wire_ev["citations"] = visitor_citations(wire_ev.get("citations"))
                else:
                    wire_ev = ev
                yield f"data: {json.dumps(wire_ev)}\n\n"
            if final:
                edition.record_query(
                    fork, question,
                    QueryResult(final["answer"], final["citations"], final["retrieved_docs"],
                                final.get("retrieved_owners", [])),
                    surface="link")
                _touch(final.get("retrieved_owners", []))

        return _stamp(StreamingResponse(sse(), media_type="text/event-stream"), vid, fresh)

    return api


def _touch(owner_oids) -> None:
    """#576 Finding 2, "any access counts": a link visitor retrieving a document keeps that
    document's OWNER's workspace alive, exactly as a colleague reading it does. A retention
    sweep that deleted the documents behind a live link because the owner had not signed in
    lately would be the same defect the retrieval-time touch exists to fix, arriving through a
    door that did not exist when it was written.

    Best-effort and wrapped whole, the same stance app.py's `_touch_retrieved_owners` takes:
    retention bookkeeping must never turn a successful answer into a 500."""
    if not owner_oids:
        return
    try:
        from dbsearch.server import retention
        for owner in owner_oids:
            retention.touch(owner)
    except Exception:
        _log.exception("retention touch (link visitor retrieval) failed")
