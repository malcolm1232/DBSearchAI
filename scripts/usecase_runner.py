"""#337 - executes one capability in one mode and judges the result.

The judging logic is deliberately pure and separate from the network call, so the
routing assertions can be proven correct in a unit test rather than only observed
against five live engines.
"""
from __future__ import annotations

import sys
from pathlib import Path

from usecase_cases import Capability

try:
    from dbsearch.eval.tally import is_numeric_token, num_str, number_present, phrase_present
except ImportError:  # direct script use without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from dbsearch.eval.tally import is_numeric_token, num_str, number_present, phrase_present

_phrase_present = phrase_present
_number_present = number_present
_num_str = num_str
_is_numeric_token = is_numeric_token


def observed_stores(resp: dict) -> tuple[str, ...]:
    """Which stores the answer actually cited, in first-seen order, deduplicated."""
    seen: list[str] = []
    for citation in resp.get("citations", []) or []:
        store = citation.get("store_id")
        if store and store not in seen:
            seen.append(store)
    return tuple(seen)


def judge_stores(cap: Capability, observed: tuple[str, ...]) -> tuple[bool, str]:
    """Did the question traverse the stores it was supposed to, and none it was not?

    Both halves matter. Checking only that the RIGHT store was reached lets a router
    that sprays every store pass every test.
    """
    missing = [s for s in cap.expect_stores if s not in observed]
    forbidden = [s for s in cap.forbid_stores if s in observed]

    if missing:
        return False, f"expected store(s) not reached: {', '.join(missing)} (observed: {observed})"
    if forbidden:
        return False, f"FORBIDDEN store(s) reached: {', '.join(forbidden)} (observed: {observed})"
    return True, ""


import re

# Denials that CONFIRM a store's absence. Capability 7 fails on these: telling an
# unauthorized asker "there is no Exec-Comp database" reveals a true fact about the
# tenant, while "no accessible data" reveals nothing. Regex rather than fixed
# substrings, because the leak is the CLAIM, not any one phrasing of it.
#
# The distinguishing property: a leak is a claim about the DATABASE/store existing
# or not; a safe denial talks about the ASKER's access or records. The cannot-find
# pattern is therefore anchored to a nearby database/store/source mention so it does
# not false-fail "I cannot find any records you are authorized to see."
_LEAKY_DENIAL_PATTERNS = (
    r"\bthere (?:is|are) (?:no|not any)\b",
    r"\bthere['’]?s (?:no|not any)\b",
    r"\bdo(?:es)?n['’]?t (?:have|exist)\b",
    r"\bdo(?:es)? not exist\b",
    r"\bno such\b",
    r"\bnot configured\b",
    r"\bwe (?:do not|don['’]?t) have\b",
    r"\bisn['’]?t (?:a|any)\b",
    r"\bzero\b.{0,30}\bdatabases?\b",
    r"\bnot present in\b",
    r"\b(?:can['’]?t|cannot|could not|couldn['’]?t) find\b.{0,40}\b(?:database|store|source)\b",
    r"\bno .{0,40}\bdatabases?\b.{0,30}\b(?:exist|configured|present|available)\b",
    r"\bno\b.{0,60}\b(?:database|store|source)\b.{0,30}\bexists?\b",
    r"\bnever (?:existed|been)\b",
    r"\bno record(?:s)? of\b.{0,60}\b(?:database|store|source)\b",
    r"\b(?:database|store|source)\b.{0,20}\b(?:was|were|has been|had been) (?:deleted|removed|decommissioned)\b",
)

# A product identifier in the AdventureWorks fixtures, e.g. Mountain-200, Touring-1000.
_SKU_PATTERN = r"\b[A-Z][A-Za-z]+-\d+\b"


def _missing(field: str, why: str) -> tuple[bool, str]:
    """A checker that cannot tally FAILS. It never returns True by default."""
    return False, f"cannot tally: {field} not supplied ({why})"


def _as_list(value):
    """Normalise a value that a checker expects to iterate as a list.

    A single scalar (str/int/float) becomes a one-element list rather than being
    iterated character-by-character (the bare-string bug) or raising TypeError (the
    bare-int/float bug). Anything else uncoercible (dict, custom object) returns None
    so the caller can fail closed instead of crashing.
    """
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, (str, int, float)):
        return [value]
    return None


def _sensitive_tokens(resp: dict, ctx: dict) -> list[str]:
    """What an unauthorized identity must not be able to say back to us.

    Derived from the ground truth plus any product identifiers OR numbers the
    authorized answer itself asserted. Harvesting from the authorized answer, not
    only the ground truth, is what keeps this from going vacuous: a capability with
    no declared ground_truth still has a concrete numeric answer, and that number is
    exactly what must not leak. This is what makes the leak gate work on real
    output: two model calls are almost never byte-identical, so string equality
    alone would miss nearly every genuine leak while looking rigorous.
    """
    tokens: list[str] = []
    truth = ctx.get("ground_truth")
    if isinstance(truth, (str, int, float)):
        tokens.append(_num_str(truth))
    elif isinstance(truth, (list, tuple, set)):
        tokens.extend(_num_str(t) for t in truth)
    tokens.extend(re.findall(_SKU_PATTERN, resp.get("answer") or ""))
    ours = (resp.get("answer") or "").replace(",", "")
    tokens.extend(re.findall(r"(?<![\d.])\d+(?:\.\d+)?(?!\d)", ours))
    # A numeric token counts regardless of length (gt=42 must still count); a
    # non-numeric token still needs the length floor to avoid trivial substrings.
    return [t for t in tokens if _is_numeric_token(t) or len(t) >= 3]


def _unauthorized_leaked(resp: dict, ctx: dict) -> tuple[bool, str] | None:
    """Shared LAW 2 gate. Returns a failure tuple if the unauthorized identity saw
    substantively what the authorized one saw, else None.

    Applied by EVERY checker, so a leak fails the capability it leaked through rather
    than only capability 8.
    """
    unauth = ctx.get("unauthorized")
    if unauth is None:
        return _missing("unauthorized response", "LAW 2 cannot be checked")

    theirs = (unauth.get("answer") or "").strip()
    if not theirs and not unauth.get("citations"):
        return None

    ours = (resp.get("answer") or "").strip()
    # Identical answers are only evidence of a leak when the authorized answer actually
    # carried something. Capability 7 is refused BY DESIGN, so both identities correctly
    # receive the same denial - flagging that as a leak would fail the very behaviour the
    # capability exists to prove. Substantive means: cited a source, or asserted a value.
    ours_substantive = bool(resp.get("citations")) or bool(_sensitive_tokens(resp, ctx))
    if ours and ours_substantive and theirs.lower() == ours.lower():
        return False, f"LEAK: unauthorized identity received the same answer: {theirs[:90]!r}"

    # The real test: did any protected value reach them, however they phrased it?
    tokens = _sensitive_tokens(resp, ctx)
    if not tokens:
        # No ground truth, no SKU, no number to check against: the gate cannot
        # verify anything. That must FAIL, not silently pass (fail-closed).
        return _missing("sensitive tokens", "no ground truth or protected value could be "
                         "derived; the leak gate cannot verify anything")
    for token in tokens:
        probe = theirs.replace(",", "") if str(token).replace(".", "").isdigit() else theirs
        if _phrase_present(probe, token):
            return False, (f"LEAK: unauthorized identity's answer contains the protected "
                           f"value {token!r}: {theirs[:90]!r}")
    return None


def catalog_store_ids(tree: dict) -> tuple[str, ...]:
    """Flatten /router/catalog's nested tenant -> business_units -> sources -> stores
    tree (router/catalog.py visible_tree) to store ids, first-seen order. The old
    flat resp["stores"] read matched no real endpoint shape, so capability 0 could
    never pass against the actual server."""
    seen: list[str] = []
    for bu in (tree or {}).get("business_units", []) or []:
        for src in bu.get("sources", []) or []:
            for s in src.get("stores", []) or []:
                sid = s.get("store_id")
                if sid and sid not in seen:
                    seen.append(sid)
    return tuple(seen)


def _ground_truth_tokens(ctx: dict) -> list[str]:
    truth = ctx.get("ground_truth")
    if isinstance(truth, (str, int, float)):
        return [_num_str(truth)]
    if isinstance(truth, (list, tuple, set)):
        return [_num_str(t) for t in truth]
    if isinstance(truth, dict):
        # Mirrors truth_cross_store_skus' own dict handling: a dict ground_truth
        # names its public value(s) under "aligned" (#339/Task 6 review). Without
        # this branch a dict ground_truth (capability 4's real declaration) falls
        # through to [], and _public_parity fails closed with "cannot tally" no
        # matter how correct the answer is - the capability could never pass.
        return [_num_str(t) for t in _as_list(truth.get("aligned")) or []]
    return []


# The ground truths of the RESTRICTED capabilities (#336 review finding 3) - what must
# NEVER appear in a PUBLIC capability's answer, to either identity. Six of nine
# capabilities are protection=public, and _public_parity used to be the ONLY LAW 2 gate
# that ran on their answers - and it checked nothing except "did the public value
# survive trimming". A routing regression that pulls a restricted fin-ledger chunk into
# a public capability's answer sailed straight through: capability 8's own leak check
# never ran on it, capability 0's restricted-store check is inert on a catalog body, and
# _public_parity saw the public token present and returned None. Explicit here rather
# than derived from the capability table at import time, so a reader can see exactly
# what must never leak without cross-referencing usecase_cases.py: "4.2 billion" is the
# Project Falcon valuation (capability 8's declared ground truth), "four point two
# million" is the fin-ledger Q3 revenue figure (the OTHER deal-team-only secret, never
# declared as any capability's ground truth today, but no less protected).
RESTRICTED_TOKENS: tuple[str, ...] = ("4.2 billion", "four point two million")


def _public_parity(resp: dict, ctx: dict):
    """protection=public: the unauthorized identity MUST see the public value too.
    Over-trimming a public document is a real failure mode nothing else catches
    (#339: capability 1 is all-staff, so alice==bob is CORRECT there, not a leak).
    Compares ground-truth tokens only - incidental numbers in the authorized answer
    would false-fail parity between two separately-phrased LLM answers.

    ADDITIONALLY (#336 review finding 3): neither answer may contain a RESTRICTED
    token. A public capability has no business surfacing deal-team-only content to
    ANYONE, authorized asker included - if it does, the router pulled from the wrong
    store and that is a leak this gate must catch, not wave through because the
    public value also happened to be present."""
    unauth = ctx.get("unauthorized")
    if unauth is None:
        return _missing("unauthorized response", "public parity cannot be checked")
    theirs = unauth.get("answer") or ""
    ours = resp.get("answer") or ""
    leaked = [t for t in RESTRICTED_TOKENS if _phrase_present(theirs, t) or _phrase_present(ours, t)]
    if leaked:
        return False, (f"LEAK: a public capability's answer contains restricted "
                       f"value(s) {leaked}: {(theirs or ours)[:90]!r}")
    tokens = _ground_truth_tokens(ctx)
    if not tokens:
        return _missing("ground truth", "public parity has no value to compare")
    for token in tokens:
        probe = theirs.replace(",", "") if _is_numeric_token(str(token)) else theirs
        if not _phrase_present(probe, token):
            return False, (f"OVER-TRIM: public value {token!r} missing from the "
                           f"unauthorized identity's answer: {theirs[:90]!r}")
    return None


def _refused_parity(resp: dict, ctx: dict):
    """protection=refused: NOBODY gets an answer, and neither denial may confirm
    non-existence (#339: capability 7 has no protected value to leak-compare)."""
    unauth = ctx.get("unauthorized")
    if unauth is None:
        return _missing("unauthorized response", "refusal parity cannot be checked")
    if unauth.get("citations"):
        return False, "unauthorized identity received citations on a refused-for-everyone capability"
    answer = (unauth.get("answer") or "").lower()
    for pattern in _LEAKY_DENIAL_PATTERNS:
        if re.search(pattern, answer):
            return False, (f"unauthorized denial confirms non-existence via "
                           f"/{pattern}/: {answer[:110]!r}")
    return None


def law2_gate(cap, resp: dict, ctx: dict):
    """THE LAW 2 dispatcher (#339). Every checker calls this instead of
    _unauthorized_leaked directly, so the assertion inverts per protection bucket
    instead of silently not applying."""
    protection = getattr(cap, "protection", "restricted")
    if protection == "public":
        return _public_parity(resp, ctx)
    if protection == "refused":
        return _refused_parity(resp, ctx)
    return _unauthorized_leaked(resp, ctx)


def truth_denies_without_confirming(cap, resp: dict, ctx: dict):
    leak = law2_gate(cap, resp, ctx)
    if leak:
        return leak
    answer = (resp.get("answer") or "").lower()
    if not answer:
        return False, "no answer returned at all"
    for pattern in _LEAKY_DENIAL_PATTERNS:
        if re.search(pattern, answer):
            return False, f"denial confirms non-existence via /{pattern}/: {answer[:110]!r}"
    return True, ""


def truth_source_visibility(cap, resp: dict, ctx: dict):
    """Capability 0, spec 3.1. A source is correctly connected only when it is
    visible to its owner AND the restricted store is invisible to the unauthorized
    identity - in BOTH modes (the unauthorized catalog arrives via the standard
    re-ask, so the separate other_catalog channel is gone)."""
    leak = law2_gate(cap, resp, ctx)
    if leak:
        return leak
    mine = catalog_store_ids(resp)
    if not mine:
        return False, "catalog is empty; no source is visible to its owner"
    restricted = (ctx.get("ground_truth") or {}).get("restricted_store") \
        if isinstance(ctx.get("ground_truth"), dict) else None
    if not restricted:
        return _missing("ground_truth.restricted_store", "isolation cannot be checked")
    if restricted not in mine:
        return False, f"restricted store {restricted!r} not visible to its OWNER: {mine}"
    unauth = ctx.get("unauthorized")
    if unauth is None:
        return _missing("unauthorized catalog", "isolation cannot be checked")
    theirs = catalog_store_ids(unauth)
    if not theirs:
        return False, ("unauthorized identity's catalog is EMPTY - over-trimmed "
                       "(they are all-staff and must see the public stores)")
    if restricted in theirs:
        return False, f"restricted store {restricted!r} visible to the unauthorized identity"
    return True, ""


def truth_doc_content(cap, resp: dict, ctx: dict):
    """The answer must actually contain what the known document says, not merely cite it."""
    leak = law2_gate(cap, resp, ctx)
    if leak:
        return leak
    expected = ctx.get("ground_truth")
    if expected is None:
        return _missing("ground_truth", "document content cannot be compared")
    if not resp.get("citations"):
        return False, "answer cites no document"
    phrases = _as_list(expected)
    if phrases is None:
        return _missing("ground_truth", f"unexpected type {type(expected).__name__} "
                         "for document content")
    answer = resp.get("answer") or ""
    for phrase in phrases:
        if not _phrase_present(answer, phrase):
            return False, f"answer omits known document text {phrase!r}: {answer[:110]!r}"
    return True, ""


def truth_sql_aggregate(cap, resp: dict, ctx: dict):
    leak = law2_gate(cap, resp, ctx)
    if leak:
        return leak
    expected = ctx.get("ground_truth")
    if expected is None:
        return _missing("ground_truth", "the figure cannot be compared to the source")
    if isinstance(expected, (list, tuple, set)):
        expected = next(iter(expected), None)
    try:
        expected_num = float(expected)
    except (TypeError, ValueError):
        return _missing("ground_truth", f"{expected!r} is not a numeric figure")
    if not _number_present(resp.get("answer") or "", expected_num):
        return False, f"answer does not assert the true figure {expected}"
    return True, ""


def truth_point_lookup(cap, resp: dict, ctx: dict):
    leak = law2_gate(cap, resp, ctx)
    if leak:
        return leak
    expected = ctx.get("ground_truth")
    if expected is None:
        return _missing("ground_truth", "the row set cannot be compared to the source")
    orders = _as_list(expected)
    if orders is None:
        return _missing("ground_truth", f"unexpected type {type(expected).__name__} "
                         "for point lookup")
    answer = resp.get("answer") or ""
    # Route through the shared word-anchored helper rather than a bespoke regex: a
    # bespoke `(?![\w.])` lookahead disqualifies a following period, so a correct
    # answer ending a sentence on the order id would FAIL on punctuation alone.
    missing = [str(o) for o in orders if not _phrase_present(answer, o)]
    if missing:
        return False, f"answer omits order(s) {missing[:5]}"
    return True, ""


def truth_cross_store_skus(cap, resp: dict, ctx: dict):
    """Both halves must be about the SAME products.

    Reaching two stores is necessary but not sufficient: the 260705 bug produced an
    answer whose halves described different products, and store count alone passes it.

    ground_truth may be EITHER:
      - a plain list of aligned SKUs: require at least one to be named, and apply no
        further gate. Naming a legitimate qualifier ("Mountain-200 Black-38") or a
        runner-up product is not a defect and must not be false-failed.
      - a dict {"aligned": [...], "not_aligned": [...]}: additionally FAIL if any
        not_aligned SKU is named, which is the precise 260705 regression (the halves
        diverged onto a specific product known NOT to be in both stores).
    """
    leak = law2_gate(cap, resp, ctx)
    if leak:
        return leak
    stores = observed_stores(resp)
    if len(stores) < 2:
        return False, f"compound question reached only {stores}; it was not decomposed"

    raw = ctx.get("ground_truth")
    if raw is None:
        return _missing("ground_truth", "SKU alignment across halves cannot be checked")

    if isinstance(raw, dict):
        aligned = _as_list(raw.get("aligned")) or []
        not_aligned = _as_list(raw.get("not_aligned")) or []
    else:
        aligned = _as_list(raw)
        not_aligned = []
    if aligned is None:
        return _missing("ground_truth", f"unexpected type {type(raw).__name__} "
                         "for SKU alignment")
    if not aligned:
        return _missing("ground_truth", "no aligned SKUs supplied to check against")

    answer = resp.get("answer") or ""
    named = [s for s in aligned if _phrase_present(answer, s)]
    if not named:
        return False, (f"answer names none of the SKUs that appear in BOTH stores "
                       f"({list(aligned)[:4]}); the halves may describe different products")

    stray = sorted({s for s in not_aligned if _phrase_present(answer, s)})
    if stray:
        return False, (f"answer names product(s) {stray} that do NOT appear in both stores; "
                       f"the halves describe different products (aligned: {list(aligned)[:4]})")
    return True, ""


def truth_best_seller_reviews(cap, resp: dict, ctx: dict):
    leak = law2_gate(cap, resp, ctx)
    if leak:
        return leak
    expected = ctx.get("ground_truth")
    if expected is None:
        return _missing("ground_truth", "the best seller cannot be confirmed")
    if isinstance(expected, (list, tuple, set)):
        seq = list(expected)
        if not seq:
            return _missing("ground_truth", "empty best-seller list")
        expected = seq[0]
    if not _phrase_present(resp.get("answer") or "", expected):
        return False, f"answer does not name the true best seller {expected!r}"
    return True, ""


def truth_routing_split(cap, resp: dict, ctx: dict):
    """Capability 6's assertion IS the traversal, so it requires proof the traversal
    check ran. Returning True unconditionally would leave the router untested, which is
    the opposite of this capability's purpose."""
    leak = law2_gate(cap, resp, ctx)
    if leak:
        return leak
    if not ctx.get("traversal_ok"):
        return _missing("traversal_ok", "judge_stores verdict was not recorded")
    return True, ""


def truth_negative_identity(cap, resp: dict, ctx: dict):
    """Capability 8 is LAW 2 stated directly: the unauthorized identity must not have
    received the data.

    That is only provable if the AUTHORIZED call actually got something back. A
    server that returns 200 with an empty body to every caller - authorized and
    unauthorized alike - enforces nothing and reveals nothing, and would otherwise
    make this checker report passed=True on an empty detail: the absence of a leak
    proves nothing if there was nothing to leak in the first place."""
    if not (resp.get("answer") or "").strip() and not resp.get("citations"):
        return _missing("authorized response",
                        "authorized call returned nothing; LAW 2 cannot be verified "
                        "against an empty response")
    leak = law2_gate(cap, resp, ctx)
    if leak:
        return leak
    return True, ""


TRUTH = {
    "truth_source_visibility": truth_source_visibility,
    "truth_doc_content": truth_doc_content,
    "truth_sql_aggregate": truth_sql_aggregate,
    "truth_point_lookup": truth_point_lookup,
    "truth_cross_store_skus": truth_cross_store_skus,
    "truth_best_seller_reviews": truth_best_seller_reviews,
    "truth_routing_split": truth_routing_split,
    "truth_denies_without_confirming": truth_denies_without_confirming,
    "truth_negative_identity": truth_negative_identity,
}


from usecase_cases import CapabilityResult

try:
    from dbsearch.eval.http_probe import call as _call
except ImportError:  # direct script use without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from dbsearch.eval.http_probe import call as _call

ANON = None
DEMO_IDENTITY = "alice"

# The demo scope is reached as a namespaced demo principal, not anonymously: LAW 2's
# first invariant denies an identity-less request outright (401), so an "anonymous"
# demo run exercises nothing at all. These are BARE names - resolve_identity prefixes
# `demo:` itself and accepts only the fixed allowlist ("alice", "bob").
DEMO_PRINCIPAL = "alice"

# The identity that must NOT see the RESTRICTED data in demo mode. bob is this repo's
# canonical negative: all-staff but NOT deal-team, so he legitimately sees the HR wiki and
# must never see fin-ledger or Project Falcon. A principal with NO groups (demo:default)
# would be a weaker test - it sees nothing at all, so it cannot distinguish real permission
# trimming from a store that simply failed to compose.
UNAUTHORIZED_DEMO_PRINCIPAL = "bob"

# `_call` is the http_probe.call transport (GET when payload is None, POST otherwise;
# `session` -> dbs_session cookie, `identity` -> X-DBSearch-Demo-User header), imported
# above and aliased so every call site below is unchanged (#337 -> Task 9 extraction).


def run_capability(cap, mode: str, base: str, session: str | None,
                   unauth_session: str | None = None) -> CapabilityResult:
    """Ask one capability's question in one mode, judge traversal, then tally.

    Fails CLOSED on any transport error. An exception here would abort the matrix and
    hide the remaining capabilities, which is the opposite of what a suite is for.

    In user mode, the LAW 2 re-ask MUST run as a second real signed-in identity
    (`unauth_session`, bob) - an anonymous re-ask 401s under a real login, which
    would make the suite unable to ever pass in that mode. Demo mode is unaffected:
    it re-asks via the `identity` header, exactly as before.
    """
    identity = None if session else DEMO_PRINCIPAL
    unauth_identity = None if session else UNAUTHORIZED_DEMO_PRINCIPAL

    try:
        if cap.num == 0:
            status, resp = _call(base, "/router/catalog", None, session,
                                 identity=identity)                          # GET
        else:
            status, resp = _call(base, "/router/ask",
                                 {"question": cap.question}, session,
                                 identity=identity)                          # POST
    except Exception as exc:                       # noqa: BLE001 - deliberate catch-all
        return CapabilityResult(cap.num, mode, False, f"transport error: {exc}")

    if status >= 400:
        return CapabilityResult(cap.num, mode, False, f"server returned {status}")

    seen = observed_stores(resp)

    traversal_ok, detail = judge_stores(cap, seen)
    if not traversal_ok:
        return CapabilityResult(cap.num, mode, False, detail, seen)

    if mode == "user" and unauth_session is None:
        return CapabilityResult(
            cap.num, mode, False,
            "cannot verify LAW 2: user mode needs a second signed-in session (bob)", seen)

    # LAW 2: re-ask the SAME question as an identity that must not see the data. Every
    # checker gates on this, so a leak fails the capability it leaked through rather
    # than only capability 8. This re-ask is UNCONDITIONAL for every capability except
    # 0 (which has no question at all - it uses the catalog GET instead of /router/ask).
    # A capability that is not 0 and somehow has no question cannot have LAW 2 verified,
    # and that must be a FAILURE, never a silent pass (this was the capability-8 bug:
    # question="" made the re-ask never fire, and an unexercised `unauthorized = {}`
    # read as "no leak"). Failing to fetch the re-ask, or the re-ask itself erroring
    # (status >= 400, exactly like the primary call), is likewise a FAILURE, never a
    # skip - the checkers refuse to pass without a genuinely verified re-ask.
    try:
        if cap.num == 0:
            unauth_status, unauthorized = _call(
                base, "/router/catalog", None, unauth_session,
                identity=unauth_identity)
        elif cap.question:
            unauth_status, unauthorized = _call(
                base, "/router/ask", {"question": cap.question}, unauth_session,
                identity=unauth_identity)
        else:
            return CapabilityResult(
                cap.num, mode, False,
                "cannot verify LAW 2: capability has no question", seen)
    except Exception as exc:                       # noqa: BLE001
        return CapabilityResult(cap.num, mode, False,
                                f"could not verify LAW 2 (unauthorized re-ask failed): {exc}",
                                seen)

    if unauth_status >= 400:
        return CapabilityResult(
            cap.num, mode, False,
            f"could not verify LAW 2 (unauthorized re-ask returned {unauth_status})", seen)

    checker = TRUTH[cap.truth]
    ctx = {
        "base": base,
        "mode": mode,
        "ground_truth": cap.ground_truth,
        "unauthorized": unauthorized,
        "traversal_ok": traversal_ok,
    }
    ok, detail = checker(cap, resp, ctx)
    return CapabilityResult(cap.num, mode, ok, detail, seen)
