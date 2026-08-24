"""#337 - the nine capability definitions, as data.

Derived from the canonical scenario table in
the phase-E federated-multistore-router design spec, section 7 (not in the public tree, #685),
instantiated on the bundled demo fixtures (src/dbsearch/router/demo/fixtures/*/ and the
doc-store seeds in DEMO_MANIFEST, router_api.py) - the same six store ids, questions, and
static ground truth run in BOTH modes: demo scope serves them natively, and Task 6's
user-mode compose builds the same fixtures under real oids. That makes this a hermetic,
no-Azure-fleet regression gate (#338). The Azure-fleet instantiation for live-tenant E2E
lives in scripts/e2e_dbsearch.py (--live-entra etc.), not here.

This module holds NO I/O on purpose. It is the artifact a human reads to answer
"what does DBSearch actually do", and it must be testable without a server.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    num: int
    name: str
    scenario: str                    # "A".."G", "setup", or "LAW 2"
    question: str
    expect_stores: tuple[str, ...]   # store ids that MUST be reached
    forbid_stores: tuple[str, ...]   # store ids that must NOT be reached
    truth: str                       # name of the ground-truth checker in usecase_runner
    modes: tuple[str, ...]           # ("demo", "user")
    protection: str = "restricted"   # "restricted" | "public" | "refused" (#339)
    ground_truth: object = None      # static per-capability truth (#338: fixtures are deterministic)


@dataclass
class CapabilityResult:
    num: int
    mode: str
    passed: bool
    detail: str
    observed_stores: tuple[str, ...] = ()


BOTH = ("demo", "user")

CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        num=0, name="Connect a source", scenario="setup",
        question="",
        expect_stores=(), forbid_stores=(),
        truth="truth_source_visibility", modes=BOTH,
        protection="restricted",
        ground_truth={"restricted_store": "fin-ledger"}),
    Capability(
        num=1, name="Semantic over documents", scenario="A",
        question="What's our parental-leave policy?",
        expect_stores=("hr-wiki",), forbid_stores=(),
        truth="truth_doc_content", modes=BOTH,
        protection="public", ground_truth=("sixteen weeks",)),
    Capability(
        num=2, name="Analytical / NL2SQL", scenario="B",
        question="What is our total closed deal amount in the apac region?",
        expect_stores=("azure-deals",), forbid_stores=("storefront", "warehouse"),
        truth="truth_sql_aggregate", modes=BOTH,
        protection="public", ground_truth=205000),
    Capability(
        num=3, name="Exact lookup", scenario="F",
        question="How many units of SKU-C did we ship in the emea region?",
        expect_stores=("warehouse",), forbid_stores=(),
        truth="truth_point_lookup", modes=BOTH,
        protection="public", ground_truth=("60",)),
    Capability(
        num=4, name="Compound cross-database", scenario="C",
        question=("Which region resolves high-priority support tickets fastest, "
                  "and what is our total closed deal amount in that region?"),
        expect_stores=("support-tickets", "azure-deals"), forbid_stores=(),
        truth="truth_cross_store_skus", modes=BOTH,
        protection="public", ground_truth={"aligned": ["emea"], "not_aligned": []}),
    Capability(
        num=5, name="Mixed text and SQL", scenario="D",
        question=("Which product category has the highest storefront order total, "
                  "and what is our parental-leave policy?"),
        expect_stores=("storefront", "hr-wiki"), forbid_stores=(),
        truth="truth_best_seller_reviews", modes=BOTH,
        protection="public", ground_truth=("Hardware",)),
    Capability(
        num=6, name="Routing under ambiguity", scenario="E",
        question="What is the total order amount by region in our storefront?",
        expect_stores=("storefront",), forbid_stores=("azure-deals",),
        truth="truth_routing_split", modes=BOTH,
        protection="public", ground_truth=("900",)),
    Capability(
        num=7, name="Existence probe", scenario="G",
        question="What's in the Exec-Comp database?",
        expect_stores=(), forbid_stores=("exec-comp",),
        truth="truth_denies_without_confirming", modes=BOTH,
        protection="refused"),
    Capability(
        num=8, name="Permission contrast", scenario="LAW 2",
        question="What does the Project Falcon plan say?",
        expect_stores=("fin-ledger",), forbid_stores=(),
        truth="truth_negative_identity", modes=BOTH,
        protection="restricted", ground_truth=("4.2 billion",)),
)


def by_num(nums: list[int]) -> tuple[Capability, ...]:
    """Select capabilities in the ORDER REQUESTED, so --only 6,2 runs 6 first."""
    index = {c.num: c for c in CAPABILITIES}
    return tuple(index[n] for n in nums if n in index)
