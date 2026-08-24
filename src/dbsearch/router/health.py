"""ConnectionTest — the Phase G connector health check (card #130).

Upgrades onboarding from "reachable" (provider.probe) to a graded ROUND-TRIP verdict: a
record actually flows back through the real retrieval path. A single orchestrator owns the
invariant sequence (probe -> build -> exercise -> teardown-in-finally -> verdict) and the
LAW-critical teardown guarantee; it delegates only the mode-specific `exercise` to a
per-mode HealthCheckStrategy (ADR 0008 modes). Providers stay unchanged.

Verdict is three-tier and never raises:
  healthy  — every stage green; retrieval genuinely round-tripped.
  degraded — reachable + built, but the round-trip could not complete under the caller's
             identity (grants, empty schema, zero hits, or a failed teardown). Usable, not proven.
  failed   — cannot reach / authenticate (probe or build raised). Mirrors /probe's
             honest-unavailable — never a 500.

The check runs AS THE CALLING ADMIN: retrieve() needs an AccessContext from
store.authorize(user_oid) (no anonymous path, LAW 2), so "your identity couldn't round-trip"
is a first-class degraded outcome, not a failure.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

from dbsearch.router.provider import ProviderRegistry

HEALTHY = "healthy"
DEGRADED = "degraded"
FAILED = "failed"


@dataclass
class StageResult:
    name: str          # probe | exercise | teardown
    ok: bool
    ms: int
    detail: str


@dataclass
class HealthVerdict:
    status: str                       # healthy | degraded | failed
    stages: list[StageResult] = field(default_factory=list)
    summary: str = ""
    remediation: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "stages": [vars(s) for s in self.stages],
            "summary": self.summary,
            "remediation": self.remediation,
        }


class HealthCheckStrategy(Protocol):
    """The mode-specific round-trip. `exercise` proves retrieval; `teardown` undoes any
    seed (no-op / None for read-only canary strategies)."""

    def exercise(self, store, profile, access) -> StageResult: ...

    def teardown(self, store) -> StageResult | None: ...


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _count_from(evidence) -> "int | None":
    """Pull the integer out of a COUNT(*) canary result (Evidence content like 'count=5').

    Read the VALUE, never the first digit in the string. Evidence content is
    `", ".join(f"{c}={v}")`, so the COLUMN NAME is in there too - and an unaliased
    `SELECT COUNT(*)` is named by the warehouse, not by us. BigQuery calls it `f0_`, so the
    old `re.search(r"(-?\\d+)")` matched the 0 in `f0_` and read `f0_=4` as ZERO: a full table
    reported "the table is empty or not visible to your grants", on a store whose credential,
    schema and query were all correct. Whether an alias appears at all depends on the SQL
    generator (the naive one emits `AS count`, an LLM may not), so this was intermittent -
    healthy on one call and degraded on the next, against unchanged data.

    Anchoring to `=` is what makes it a value rather than a coincidence."""
    import re
    if not evidence:
        return None
    m = re.search(r"=\s*(-?\d+)\b", getattr(evidence[0], "content", "") or "")
    return int(m.group(1)) if m else None


class RetrieveCanary:
    """Read-only round-trip: run a probe through the store's REAL retrieve() path and assert
    a record comes back. Never writes — for pushdown the naive generator emits `SELECT * …
    LIMIT k`, for native/index it's a search over the store's own descriptive terms. Read-only
    everywhere means there is nothing to tear down (§12)."""

    def __init__(self, mode: str, empty_hint: str) -> None:
        self.mode = mode
        self.empty_hint = empty_hint

    def _probe(self, profile) -> str:
        if self.mode == "pushdown":
            # COUNT(*) is valid on EVERY SQL dialect; 'SELECT * ... LIMIT k' is NOT (SQL
            # Server rejects LIMIT). 'how many' drives keyword_sql_generator's COUNT branch.
            return "how many records"
        terms = " ".join(t for t in (
            [profile.title, profile.description] + list(getattr(profile, "topics", []) or []))
            if t)
        return terms or "sample"

    def exercise(self, store, profile, access) -> StageResult:
        t = time.perf_counter()
        if self.mode == "pushdown" and not getattr(profile, "schema", None):
            return StageResult("exercise", False, _ms(t),
                               "reachable, but no tables are visible to your grants")
        # #304: a document source's relevance probe is derived from the store's title/description,
        # which for a freshly-connected SharePoint node is just its id — matching NO document text,
        # so retrieve() returns 0 and the source is falsely reported "not indexed yet". When the
        # store offers an EXISTENCE check (has_content), use it: the health question is "can the
        # caller retrieve content from this source?", not "is a title-derived query relevant?".
        if self.mode != "pushdown":
            has_content = getattr(store, "has_content", None)
            if callable(has_content):
                try:
                    ok = has_content(access)
                except Exception as exc:
                    return StageResult("exercise", False, _ms(t), f"retrieve failed: {exc}")
                return StageResult("exercise", bool(ok), _ms(t),
                                   "content is retrievable" if ok else self.empty_hint)
        try:
            evidence = store.retrieve(access, self._probe(profile), top_k=1)
        except Exception as exc:
            return StageResult("exercise", False, _ms(t), f"retrieve failed: {exc}")
        if self.mode == "pushdown":
            # the COUNT(*) probe always returns one row; read the count so an EMPTY table is
            # still honestly 'degraded' rather than a false 'healthy'.
            n = _count_from(evidence)
            if n is None:                       # unexpected shape but a row came back
                return StageResult("exercise", bool(evidence), _ms(t),
                                   "round-tripped" if evidence else self.empty_hint)
            if n > 0:
                return StageResult("exercise", True, _ms(t), f"counted {n} row(s)")
            return StageResult("exercise", False, _ms(t), self.empty_hint)
        if evidence:
            return StageResult("exercise", True, _ms(t),
                               f"retrieved {len(evidence)} record(s)")
        return StageResult("exercise", False, _ms(t), self.empty_hint)

    def teardown(self, store) -> StageResult | None:
        return None


def default_strategies() -> dict[str, RetrieveCanary]:
    return {
        "pushdown": RetrieveCanary(
            "pushdown",
            "reachable, but the canary query returned no rows "
            "(the table is empty or not visible to your grants)"),
        "native": RetrieveCanary(
            "native",
            "reachable, but the search returned no hits "
            "(empty scope or your delegated token lacks access)"),
        "index": RetrieveCanary(
            "index",
            "connected, but no content was retrieved "
            "(the source may be empty or not indexed yet)"),
    }


class ConnectionTest:
    def __init__(self, registry: ProviderRegistry,
                 strategies: dict[str, HealthCheckStrategy]) -> None:
        self._registry = registry
        self._strategies = strategies

    def run(self, entry: dict, user_oid: str,
            introspect_credential: "str | None" = None) -> HealthVerdict:
        """`introspect_credential` (ADR 0022): the CALLER's delegated access token, already
        minted by the server layer, for a store that declares a `delegation:` block. This
        layer stays credential-agnostic - it never mints, never reads the vault, and never
        learns which cloud the token is for."""
        kind = entry.get("kind", "")
        mode = entry.get("mode") or None
        config = {"id": entry.get("id", "store"), **entry.get("config", {})}

        # --- probe (reachability) ---
        t = time.perf_counter()
        try:
            provider = self._registry.get(kind, mode)
            # Same optional-capability idiom as `build_isolated` below: a provider that can
            # introspect as the caller does so; every other provider is untouched.
            prober = getattr(provider, "probe_as", None)
            profile = (prober(config, credential=introspect_credential)
                       if prober is not None and introspect_credential
                       else provider.probe(config))
        except Exception as exc:
            logging.getLogger("dbsearch").warning(
                "health %s (%s) probe failed after %dms: %s",
                config["id"], kind, _ms(t), exc)
            return HealthVerdict(
                status=FAILED,
                stages=[StageResult("probe", False, _ms(t), f"unreachable: {exc}")],
                summary=f"Cannot reach {kind!r}.",
                remediation=f"Check connection config / credentials for {kind!r}: {exc}",
            )
        stages = [StageResult("probe", True, _ms(t), "reachable; schema read")]

        # --- build + resolve the mode strategy ---
        resolved_mode = mode or self._registry._defaults.get(kind)  # noqa: SLF001
        strategy = self._strategies.get(resolved_mode)
        if strategy is None:
            stages.append(StageResult("exercise", False, 0,
                                      f"no health strategy for mode {resolved_mode!r}"))
            return HealthVerdict(
                status=DEGRADED, stages=stages,
                summary="Reachable, but no round-trip check for this mode.",
                remediation=f"mode {resolved_mode!r} has no health strategy registered")
        try:
            # #454: a connector provider's build() mutates its live state (descriptor, index
            # pipe, in-flight crawl), so a health check must ask for an ISOLATED build where
            # one is offered. Providers without the capability are unaffected.
            # ADR 0022: probe and build make SEPARATE engines, and `retrieve` re-reads the
            # schema off the built one - so the credential has to reach both or the check
            # speaks with two identities and fails on the server one mid-round-trip.
            # `build_isolated` still wins where it exists: it protects a connector
            # provider's live crawl state, which is a correctness constraint, not a
            # preference, and no connector provider offers build_as.
            build_as = getattr(provider, "build_as", None)
            if build_as is not None and introspect_credential and not hasattr(
                    provider, "build_isolated"):
                store = build_as(config, credential=introspect_credential)
            else:
                builder = getattr(provider, "build_isolated", provider.build)
                store = builder(config)
        except Exception as exc:
            logging.getLogger("dbsearch").warning(
                "health %s (%s) build failed: %s", config["id"], kind, exc)
            stages.append(StageResult("exercise", False, 0, f"build failed: {exc}"))
            return HealthVerdict(
                status=FAILED, stages=stages,
                summary=f"Reached {kind!r} but could not build a store.",
                remediation=f"build failed: {exc}")

        # --- exercise (round-trip) with GUARANTEED teardown ---
        ex: StageResult
        needs_signin = False
        t = time.perf_counter()
        try:
            access = store.authorize(user_oid)
            # ADR 0022, the round-trip half. The broker registers delegations at COMPOSE
            # time, keyed by store id - and a health check runs on a store that is not
            # composed yet, which is the whole point of Test-connection. So authorize()
            # legitimately comes back with no delegated credential, and the round-trip fell
            # through to the engine's server identity: probe read the schema as the caller
            # and then `exercise` failed on ADC one stage later, which reads as a broken
            # store rather than as an unwired check.
            #
            # Only ever FILLS A HOLE - a credential the authorizer did supply always wins,
            # because that one came from the composed store's own registered delegation and
            # is the identity this store is actually configured to run as.
            # Plain assignment, not dataclasses.replace: `authorizer` is pluggable and is not
            # required to return an AccessContext, and replace() raises TypeError on anything
            # that is not a dataclass - which would turn "no delegation registered" into a
            # round-trip error, i.e. a new failure invented by the code meant to remove one.
            # AccessContext is not frozen, and this object is built fresh per authorize().
            if introspect_credential and getattr(access, "delegated_credential", None) is None:
                access.delegated_credential = introspect_credential
            ex = strategy.exercise(store, profile, access)
        except Exception as exc:
            # "you need to sign in" is the ONE failure the user can fix, so it must lead —
            # not sit under a generic "the round-trip could not complete" headline (#196).
            # Structural check, not an import: NotSignedIn carries .idp (which cloud to
            # connect) and a dead grant carries .expired_grant — server/broker types that
            # this layer must not depend on.
            needs_signin = hasattr(exc, "idp") or getattr(exc, "expired_grant", False)
            ex = StageResult("exercise", False, _ms(t), f"round-trip error: {exc}")
        finally:
            td = strategy.teardown(store)
        stages.append(ex)
        if td is not None:
            stages.append(td)

        # --- verdict ---
        if ex.ok and (td is None or td.ok):
            return HealthVerdict(status=HEALTHY, stages=stages,
                                 summary="Connection healthy — a record round-tripped.")
        remediation = (ex.detail if not ex.ok
                       else (td.detail if td is not None else ""))
        logging.getLogger("dbsearch").info(
            "health %s (%s) degraded: %s", config["id"], kind, remediation)
        if needs_signin:
            # The connection is FINE — the caller just has no credential. Lead with the fix.
            return HealthVerdict(
                status=DEGRADED, stages=stages,
                summary="Sign in to query this source — it runs queries as you.",
                remediation=remediation)
        summary = ("Reachable, but the round-trip could not complete under your identity."
                   if not ex.ok else "Round-trip succeeded but cleanup was incomplete.")
        return HealthVerdict(status=DEGRADED, stages=stages,
                             summary=summary, remediation=remediation)
