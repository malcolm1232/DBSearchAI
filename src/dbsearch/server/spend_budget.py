"""A daily spend budget for the anonymous public demo (#342).

WHY THIS EXISTS. The hosted demo at dbsearch.ai is anonymous by design, and it drives a
real, paid Groq key. Groq offers no per-key hard spend cap, so the provider-side control
other hosts would rely on does not exist here — the cap has to be ours.

WHAT #332 DOES NOT DO. The rate limiter bounds THROUGHPUT (per-IP 10/min, global 60/min).
Throughput is not spend: 60/min sustained is roughly 86,000 requests a day. A limiter keeps
one visitor from hammering the box; it does not stop a slow, patient, month-long drain. This
module bounds the money instead, and the two are complementary rather than redundant.

TWO LAYERS, ON PURPOSE.

  `meter_client` is the GUARANTEE. Every paid Groq call in this codebase goes through one
  choke point — `self._client.chat.completions.create`, built once in LlamaLlm.__init__ and
  inherited by GroqLlm — so metering there covers answer, answer_stream, condense_question,
  decompose_question, plan_subquestions, draft_section(_stream), elicit_requirements and
  summarize_requirements, plus anything added later, with no per-method wiring anyone can
  forget to add. Out of budget, it raises BEFORE the network call, so exhaustion costs zero.

  `BudgetedLlm` is the UX. On its own, layer 1 turns an exhausted demo into a 500. This
  routes to the free in-tenant Extractive model instead, so a spent budget degrades the
  answer quality rather than the site. Layer 1 stops the spend; layer 2 keeps it usable.

FAIL CLOSED, TWICE. Unreadable or corrupt state reports the budget as fully spent rather
than fresh, and a response carrying no usage figures (streaming chunks do not) is charged
the per-call ceiling rather than nothing. Both choose a cheap wrong answer (degraded demo)
over an expensive one (uncapped spend) — the same reasoning as #334's robots default.

PERSISTENCE. A counter that resets whenever the container is recreated is not a budget, so
state is a small JSON file. In prod it belongs on the `objects:/data` volume, which survives
`docker compose up -d`; the default path reflects that.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

# One day's worth of tokens for the anonymous demo. Sized so a fully-drained day is a rounding
# error rather than a bill: at Groq's llama-3.3-70b rates this is well under a dollar. Raise it
# with DBSEARCH_GROQ_DAILY_TOKENS once the demo is earning its keep.
DEFAULT_DAILY_TOKENS = 1_000_000

# Charged when a response reports no usage (streaming). Matches the per-call max_tokens ceiling
# the adapters pass, so the estimate errs high rather than free.
DEFAULT_CHARGE = 1024

DEFAULT_STATE_PATH = "/data/spend_budget.json"


class BudgetExhausted(RuntimeError):
    """Raised instead of making a paid call once the day's budget is gone."""


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


class SpendBudget:
    """A per-UTC-day token budget, persisted so a restart cannot refill it.

    The window is the calendar day rather than a rolling one because ops has to be able to
    answer "did we hit the cap today?" by reading one file, and because a rolling window
    needs a request log — more state, more to corrupt, for a control whose whole job is to
    be boringly dependable.
    """

    def __init__(self, limit: int = DEFAULT_DAILY_TOKENS,
                 state_path: "str | Path" = DEFAULT_STATE_PATH,
                 day_fn=_utc_day) -> None:
        self._limit = max(0, int(limit))
        self._path = Path(state_path)
        self._day_fn = day_fn
        self._lock = threading.Lock()
        # None means "state unreadable" — distinct from 0 spent, and treated as exhausted.
        self._day, self._spent = self._load()

    # --- state -------------------------------------------------------------------------
    def _load(self) -> "tuple[str, int | None]":
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return str(raw["day"]), int(raw["spent"])
        except FileNotFoundError:
            # A budget that has never been used is genuinely fresh — not a failure.
            return self._day_fn(), 0
        except Exception:
            # Corrupt or unreadable. Do NOT hand out a free day: a malformed file must not
            # be a way to reset the cap.
            return self._day_fn(), None

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps({"day": self._day, "spent": self._spent}),
                           encoding="utf-8")
            tmp.replace(self._path)      # atomic: a crash mid-write cannot truncate the real file
        except Exception:
            # Never let bookkeeping take the site down. The in-memory counter still governs
            # this process; only durability across a restart is lost.
            pass

    def _roll(self) -> None:
        """Refill if the day changed. Caller holds the lock."""
        today = self._day_fn()
        if today != self._day:
            self._day, self._spent = today, 0
            self._save()

    # --- queries -----------------------------------------------------------------------
    @property
    def limit(self) -> int:
        return self._limit

    def remaining(self) -> int:
        with self._lock:
            self._roll()
            if self._spent is None:
                return 0
            return max(0, self._limit - self._spent)

    def exhausted(self) -> bool:
        return self.remaining() <= 0

    # --- accounting --------------------------------------------------------------------
    def spend(self, tokens: int) -> None:
        with self._lock:
            self._roll()
            base = 0 if self._spent is None else self._spent
            self._spent = base + max(0, int(tokens))
            self._save()


def meter_client(client, budget: SpendBudget, default_charge: int = DEFAULT_CHARGE):
    """Wrap an OpenAI-compatible client so every chat completion is checked and charged.

    Patches the bound `create` in place rather than substituting a proxy object: the adapters
    reach for `self._client.chat.completions.create` directly, and an object that merely looks
    similar would break the moment one of them touched another attribute of the real client.
    """
    completions = client.chat.completions
    inner = completions.create

    def create(*args, **kwargs):
        if budget.exhausted():
            # Raise BEFORE the network call. An exhausted budget must cost nothing.
            logging.getLogger("dbsearch").warning(
                "#342 daily Groq budget exhausted — refusing the paid call. The demo is now "
                "answering from the local Extractive model. Raise DBSEARCH_GROQ_DAILY_TOKENS "
                "(currently %s) if this is legitimate traffic.", budget.limit)
            raise BudgetExhausted("daily demo LLM budget is spent")
        resp = inner(*args, **kwargs)
        usage = getattr(resp, "usage", None)
        total = getattr(usage, "total_tokens", None) if usage is not None else None
        budget.spend(default_charge if total is None else total)
        return resp

    completions.create = create
    return client


class BudgetedLlm:
    """An LlmPort that serves from `primary` while funded and `fallback` once it is not.

    Delegation is dynamic (`__getattr__`) rather than one wrapper method per port method.
    That is the safer shape here: LlmPort has nine methods and the Groq adapter adds more,
    and a hand-written wrapper silently keeps sending a NEWLY added method to the paid model
    forever. Resolving the target per call means new methods are covered the day they land.
    """

    def __init__(self, primary, fallback, budget: SpendBudget) -> None:
        self._primary = primary
        self._fallback = fallback
        self._budget = budget

    @property
    def primary(self):
        """The paid model this wraps. Public so callers and tests can assert WHICH model the
        demo is configured to use without reaching through delegation, which would silently
        answer about the fallback once the budget is spent."""
        return self._primary

    @property
    def fallback(self):
        """The free model served once the budget is spent."""
        return self._fallback

    @property
    def budget(self) -> SpendBudget:
        return self._budget

    def __getattr__(self, name: str):
        # Only reached for attributes this class does not define, i.e. every port method.
        target = self._fallback if self._budget.exhausted() else self._primary
        return getattr(target, name)


def meter_llm(llm, budget: SpendBudget, default_charge: int = DEFAULT_CHARGE):
    """Meter a Groq/Llama adapter's underlying HTTP client against `budget`.

    Reaching for `llm._client` is deliberate, and confined to this one function so there is
    a single documented place where it happens. That attribute is the choke point: LlamaLlm
    builds it once in __init__ and every paid method in it and in GroqLlm calls through it,
    so metering here needs no cooperation from the adapters and cannot be sidestepped by a
    method someone adds later. An adapter with no such client (the local Extractive model)
    is left alone rather than treated as an error — nothing to meter is not a failure.
    """
    client = getattr(llm, "_client", None)
    if client is not None:
        meter_client(client, budget, default_charge)
    return llm


def budget_from_env(state_path: "str | None" = None) -> SpendBudget:
    """The demo budget as configured in prod.env. Tunable without a rebuild (#342)."""
    return SpendBudget(
        limit=int(os.environ.get("DBSEARCH_GROQ_DAILY_TOKENS", DEFAULT_DAILY_TOKENS)),
        state_path=state_path or os.environ.get("DBSEARCH_SPEND_STATE", DEFAULT_STATE_PATH),
    )
