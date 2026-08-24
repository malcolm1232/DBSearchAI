"""#337 - the capability definitions are data, and their shape is a contract.

These tests need no server. They exist so a malformed definition fails in one
second rather than after a sixty-second Azure wake.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from usecase_cases import CAPABILITIES, Capability, by_num  # noqa: E402
from usecase_runner import (  # noqa: E402
    judge_stores, observed_stores, TRUTH, law2_gate, catalog_store_ids,
)


def test_observed_stores_reads_citations():
    resp = {"citations": [{"store_id": "azure-deals"}, {"store_id": "warehouse"}]}
    assert observed_stores(resp) == ("azure-deals", "warehouse"), observed_stores(resp)


def test_observed_stores_deduplicates_preserving_order():
    resp = {"citations": [{"store_id": "a"}, {"store_id": "b"}, {"store_id": "a"}]}
    assert observed_stores(resp) == ("a", "b"), observed_stores(resp)


def test_observed_stores_handles_no_citations():
    assert observed_stores({}) == ()


def test_judge_passes_when_expected_store_reached():
    cap = by_num([3])[0]
    ok, detail = judge_stores(cap, ("warehouse",))
    assert ok, detail


def test_judge_fails_when_expected_store_missing():
    cap = by_num([3])[0]
    ok, detail = judge_stores(cap, ("azure-deals",))
    assert not ok
    assert "warehouse" in detail, detail


def test_judge_fails_when_forbidden_store_reached():
    """The routing regression: reaching the right store is not enough if the
    router ALSO hit the wrong one."""
    cap = by_num([6])[0]
    ok, detail = judge_stores(cap, ("storefront", "azure-deals"))
    assert not ok, "reaching a forbidden store must fail"
    assert "azure-deals" in detail, detail


def test_judge_passes_existence_probe_when_nothing_reached():
    cap = by_num([7])[0]
    ok, detail = judge_stores(cap, ())
    assert ok, detail


def test_there_are_nine_capabilities():
    assert len(CAPABILITIES) == 9, f"expected 9, got {len(CAPABILITIES)}"


def test_numbers_are_zero_through_eight_and_unique():
    nums = sorted(c.num for c in CAPABILITIES)
    assert nums == list(range(9)), nums


def test_every_capability_declares_at_least_one_mode():
    for c in CAPABILITIES:
        assert c.modes, f"capability {c.num} declares no modes"
        for m in c.modes:
            assert m in ("demo", "user"), f"capability {c.num} has bad mode {m!r}"


def test_capability_zero_is_the_asymmetric_one():
    """Spec 3.1: an anonymous visitor has no database to connect, so capability 0
    means something different per mode. It must still run in both."""
    zero = by_num([0])[0]
    assert zero.modes == ("demo", "user"), zero.modes


def test_routing_capability_forbids_the_wrong_store():
    """Capability 6 is the one that proves the router. A definition that only says
    which store SHOULD answer cannot fail when the router also hits the wrong one."""
    six = by_num([6])[0]
    assert six.forbid_stores, "capability 6 must name the store it must NOT reach"


def test_existence_probe_expects_no_stores():
    """Capability 7 passes by reaching nothing and admitting nothing."""
    seven = by_num([7])[0]
    assert seven.expect_stores == (), seven.expect_stores


def test_every_capability_names_a_truth_checker():
    for c in CAPABILITIES:
        assert c.truth, f"capability {c.num} has no ground-truth checker"


def test_by_num_preserves_requested_order():
    got = [c.num for c in by_num([6, 2])]
    assert got == [6, 2], got


def test_capabilities_are_immutable():
    """A run must not be able to mutate the matrix under a later run."""
    import dataclasses
    assert isinstance(CAPABILITIES, tuple)
    c = CAPABILITIES[0]
    try:
        c.question = "mutated"
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("Capability is not frozen")


from usecase_runner import TRUTH  # noqa: E402


def _cap(n):
    return by_num([n])[0]


def test_every_declared_truth_checker_exists():
    """A capability naming a checker that does not exist would silently skip its tally."""
    for c in CAPABILITIES:
        assert c.truth in TRUTH, f"capability {c.num} names unknown checker {c.truth!r}"


def test_no_checker_passes_on_an_empty_context():
    """THE RULE OF THIS TASK. Every checker, given a plausible-looking answer but no
    ground truth and no unauthorized response, must FAIL. A checker that returns True
    here is a tally hole: it will report green forever without ever comparing anything."""
    resp = {"answer": "Revenue was 4200 for SKU-A.",
            "citations": [{"store_id": "azure-deals"}, {"store_id": "support-tickets"}]}
    for c in CAPABILITIES:
        ok, detail = TRUTH[c.truth](c, resp, {"mode": "user"})
        assert not ok, (f"capability {c.num} checker {c.truth} PASSED with no ground truth "
                        f"and no unauthorized response; it tallies nothing")


def test_negative_identity_fails_when_unauthorized_saw_the_same_content():
    """LAW 2. If the unauthorized identity got the same substantive answer, that is a leak."""
    cap = _cap(8)
    resp = {"answer": "The Falcon valuation is 4.2 billion.", "citations": [{"store_id": "fin-ledger"}]}
    leaked = {"answer": "The Falcon valuation is 4.2 billion.", "citations": [{"store_id": "fin-ledger"}]}
    ok, detail = TRUTH[cap.truth](cap, resp, {"mode": "user", "unauthorized": leaked})
    assert not ok, "identical answer for an unauthorized identity must FAIL"
    assert "leak" in detail.lower(), detail


def test_negative_identity_passes_when_unauthorized_was_denied():
    cap = _cap(8)
    resp = {"answer": "The Falcon valuation is 4.2 billion.", "citations": [{"store_id": "fin-ledger"}]}
    denied = {"answer": "I have no accessible data for that.", "citations": []}
    ok, detail = TRUTH[cap.truth](cap, resp, {"mode": "user", "unauthorized": denied})
    assert ok, detail


def test_doc_content_fails_when_the_expected_text_is_absent():
    """A fabricated answer citing an unrelated document must not pass. Capability 1
    is protection=public (#339: hr-wiki is all-staff), so the unauthorized identity
    must ALSO see the public value - it is given "25 days" here so the failure below
    is driven by the doc-content check on the authorized answer, not by public parity."""
    cap = _cap(1)
    resp = {"answer": "Our policy is generous.", "citations": [{"store_id": "hr-wiki"}]}
    ctx = {"mode": "demo", "ground_truth": "25 days",
           "unauthorized": {"answer": "Staff receive 25 days of leave.",
                            "citations": [{"store_id": "hr-wiki"}]}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "answer omitting the known document text must FAIL"


def test_doc_content_passes_when_the_expected_text_is_present():
    cap = _cap(1)
    resp = {"answer": "Staff receive 25 days of leave.", "citations": [{"store_id": "hr-wiki"}]}
    ctx = {"mode": "demo", "ground_truth": "25 days",
           "unauthorized": {"answer": "Staff receive 25 days of leave.",
                            "citations": [{"store_id": "hr-wiki"}]}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert ok, detail


def test_cross_store_fails_when_no_aligned_sku_is_named():
    """The 260705 under-detection bug: halves that describe different products.
    Capability 4 is protection=public, so the unauthorized identity is given both
    aligned tokens too - the failure below must come from the alignment check on the
    authorized answer, not from public-parity over-trimming."""
    cap = _cap(4)
    resp = {"answer": "Tickets are up and revenue is down.",
            "citations": [{"store_id": "support-tickets"}, {"store_id": "azure-deals"}]}
    ctx = {"mode": "demo", "ground_truth": ["Mountain-200", "Touring-1000"],
           "unauthorized": {"answer": "Mountain-200 and Touring-1000 are both public.",
                            "citations": []}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "an answer naming no aligned SKU must FAIL"


def test_cross_store_passes_when_an_aligned_sku_is_named():
    cap = _cap(4)
    resp = {"answer": "Mountain-200 drives the most tickets and 41000 in revenue.",
            "citations": [{"store_id": "support-tickets"}, {"store_id": "azure-deals"}]}
    ctx = {"mode": "demo", "ground_truth": ["Mountain-200", "Touring-1000"],
           "unauthorized": {"answer": "Mountain-200 and Touring-1000 are both public.",
                            "citations": []}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert ok, detail


def test_sql_aggregate_rejects_a_decoy_number():
    """Scanning for ANY number in tolerance lets an unrelated figure pass. The true
    figure is 205000 (apac closed deals); the answer contains 2024 and 3 but not
    205000. Capability 2 is protection=public, so the unauthorized identity is given
    the true figure too - the failure must come from the aggregate check itself."""
    cap = _cap(2)
    resp = {"answer": "In 2024 we closed 3 deals.", "citations": [{"store_id": "azure-deals"}]}
    ctx = {"mode": "demo", "ground_truth": 205000,
           "unauthorized": {"answer": "The apac total was 205000.", "citations": []}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "a decoy number must not satisfy the tally"


def test_sql_aggregate_accepts_the_true_figure_with_separators():
    cap = _cap(2)
    resp = {"answer": "We closed 205,000 in the apac region.",
            "citations": [{"store_id": "azure-deals"}]}
    ctx = {"mode": "demo", "ground_truth": 205000,
           "unauthorized": {"answer": "The apac total was 205000.", "citations": []}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert ok, detail


def test_sql_aggregate_rejects_a_substring_match():
    """205000 must not be satisfied by 2050001 appearing in the prose."""
    cap = _cap(2)
    resp = {"answer": "Reference 2050001 was logged.", "citations": [{"store_id": "azure-deals"}]}
    ctx = {"mode": "demo", "ground_truth": 205000,
           "unauthorized": {"answer": "The apac total was 205000.", "citations": []}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "a longer number containing the expected digits must not pass"


def test_denies_without_confirming_rejects_a_leaky_denial():
    """'no such database' is a LEAK: it confirms the store does not exist. 'No accessible
    data' does not."""
    cap = _cap(7)
    ctx = {"mode": "demo", "unauthorized": {"answer": ""}}
    for leaky in ("There is no Exec-Comp database in this tenant.",
                  "We don't have an Exec-Comp database.",
                  "That database does not exist.",
                  "No such database is configured."):
        ok, detail = TRUTH[cap.truth](cap, {"answer": leaky}, ctx)
        assert not ok, f"leaky denial passed: {leaky!r}"


def test_denies_without_confirming_accepts_a_safe_denial():
    cap = _cap(7)
    ctx = {"mode": "demo", "unauthorized": {"answer": ""}}
    ok, detail = TRUTH[cap.truth](cap, {"answer": "I have no accessible data for that."}, ctx)
    assert ok, detail


def test_source_visibility_user_mode_fails_when_another_identity_sees_the_source():
    """Connecting a source is only correct if the restricted store is invisible to
    the unauthorized identity. Catalog shape is the real nested tree (router/catalog.py
    visible_tree), not the flat resp["stores"] no endpoint ever returns."""
    cap = _cap(0)
    resp = {"tenant": "acme-demo", "business_units": [
        {"id": "hr", "sources": [{"id": "s", "stores": [{"store_id": "my-db"}]}]}]}
    ctx = {"mode": "user", "ground_truth": {"restricted_store": "my-db"},
           "unauthorized": {"tenant": "acme-demo", "business_units": [
               {"id": "hr", "sources": [{"id": "s", "stores": [{"store_id": "my-db"}]}]}]}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "a source visible to another identity must FAIL"


def test_source_visibility_user_mode_passes_when_isolated():
    cap = _cap(0)
    resp = {"tenant": "acme-demo", "business_units": [
        {"id": "hr", "sources": [{"id": "s", "stores": [{"store_id": "my-db"}]}]}]}
    ctx = {"mode": "user", "ground_truth": {"restricted_store": "my-db"},
           "unauthorized": {"tenant": "acme-demo", "business_units": [
               {"id": "fin", "sources": [{"id": "s2",
                "stores": [{"store_id": "someone-elses"}]}]}]}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert ok, detail


def test_cross_store_fails_when_the_halves_name_different_products():
    """THE 260705 BUG, precisely. One aligned SKU appears, so a "does any aligned SKU
    show up" check passes. But Road-750 is a declared not_aligned product, so the
    halves are about different products and the answer must FAIL.

    Uses the dict {"aligned": [...], "not_aligned": [...]} ground_truth form (FIX 5):
    a plain list only asserts "at least one aligned SKU is named" and does not gate
    every other SKU-shaped token, so this precise regression needs not_aligned named
    explicitly.

    Capability 4's real instantiation is protection=public (#338/#339). Task 6
    fixed the dict-ground-truth gap in law2_gate's public-parity path
    (usecase_runner._ground_truth_tokens now extracts "aligned" tokens from a
    dict, mirroring what truth_cross_store_skus already does internally), so
    this test now runs against the REAL declaration instead of working around
    the gap with dataclasses.replace. The unauthorized identity is given the
    same aligned SKUs so public parity passes cleanly, and the failure below is
    driven entirely by truth_cross_store_skus' own not_aligned gate - the
    regression this test exists to prove."""
    cap = _cap(4)
    resp = {"answer": ("Touring-1000 drives the most tickets, but the highest revenue "
                       "product is actually Road-750 at 9000."),
            "citations": [{"store_id": "support-tickets"}, {"store_id": "azure-deals"}]}
    ctx = {"mode": "demo",
           "ground_truth": {"aligned": ["Mountain-200", "Touring-1000"],
                            "not_aligned": ["Road-750"]},
           "unauthorized": {"answer": "Mountain-200 and Touring-1000 are both public.",
                            "citations": []}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "halves naming different products must FAIL"
    assert "Road-750" in detail, detail


def test_number_present_accepts_a_figure_followed_by_a_period():
    """A correct answer must not fail on punctuation. This guards a FALSE NEGATIVE,
    which erodes trust in the gate just as badly as a false pass."""
    cap = _cap(2)
    resp = {"answer": "We closed 205000.", "citations": [{"store_id": "azure-deals"}]}
    ctx = {"mode": "demo", "ground_truth": 205000,
           "unauthorized": {"answer": "The apac total was 205000.", "citations": []}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert ok, detail


def test_doc_content_rejects_a_substring_decoy():
    """'25 days' must not be satisfied by '125 days'."""
    cap = _cap(1)
    resp = {"answer": "Employees get 125 days of something else.",
            "citations": [{"store_id": "hr-wiki"}]}
    ctx = {"mode": "demo", "ground_truth": "25 days",
           "unauthorized": {"answer": "Staff receive 25 days of leave.",
                            "citations": [{"store_id": "hr-wiki"}]}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "a substring decoy must not satisfy the document tally"


def test_leak_detected_when_unauthorized_paraphrases_the_protected_value():
    """Two model calls are never byte-identical, so exact-match leak detection would
    miss nearly every real leak. The protected VALUE reaching them is the leak."""
    cap = _cap(8)
    resp = {"answer": "The Falcon valuation is 4.2 billion.", "citations": [{"store_id": "fin-ledger"}]}
    paraphrased = {"answer": "Sure - the figure you want is 4.2 billion for that deal.",
                   "citations": []}
    ctx = {"mode": "user", "ground_truth": "4.2 billion", "unauthorized": paraphrased}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "a paraphrased leak of the protected figure must FAIL"
    assert "leak" in detail.lower(), detail


def test_leak_detected_when_unauthorized_names_a_protected_product():
    cap = _cap(8)
    resp = {"answer": "Mountain-200 leads on revenue.", "citations": [{"store_id": "fin-ledger"}]}
    leaked = {"answer": "I believe Mountain-200 is the one.", "citations": []}
    ctx = {"mode": "user", "unauthorized": leaked}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "an unauthorized answer naming the protected product must FAIL"


def test_more_leaky_denial_phrasings_are_caught():
    """A finite phrase list is trivially evaded. These four all confirm non-existence."""
    cap = _cap(7)
    ctx = {"mode": "demo", "unauthorized": {"answer": ""}}
    for leaky in ("Sorry, we have zero Exec-Comp databases in this tenant.",
                  "The Exec-Comp database is not present in this tenant.",
                  "I can't find any Exec-Comp database.",
                  "There are not any Exec-Comp databases here."):
        ok, detail = TRUTH[cap.truth](cap, {"answer": leaky}, ctx)
        assert not ok, f"leaky denial passed: {leaky!r}"


def test_routing_split_requires_the_traversal_evidence():
    """truth_routing_split may only pass when judge_stores actually ran and recorded
    its verdict. A bare True would make capability 6 untested. Capability 6 is
    protection=public, so the unauthorized identity is given the public "900" figure
    too - the pass/fail split below must turn on traversal_ok, not on public parity."""
    cap = _cap(6)
    resp = {"answer": "Storefront order totals by region: amer 800, emea 900, apac 800.",
            "citations": [{"store_id": "storefront"}]}
    unauthorized = {"answer": "The storefront region totals include 900 for emea.",
                    "citations": []}
    ctx = {"mode": "demo", "ground_truth": cap.ground_truth, "unauthorized": unauthorized}
    ok, _ = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "routing check must fail without recorded traversal evidence"
    ok, detail = TRUTH[cap.truth](cap, resp, {**ctx, "traversal_ok": True})
    assert ok, detail


# --- round-3 review fixes -----------------------------------------------


def test_point_lookup_accepts_an_order_id_followed_by_a_period():
    """FIX 1. truth_point_lookup used its own lookahead that disqualified a
    following period, so a correct answer ending a sentence on the order id
    FAILED. It must route through the shared _phrase_present helper instead.
    The checker's own token-matching mechanism is generic (any list of tokens), so
    this keeps the order-id example rather than switching to capability 3's real
    unit-count token; the store id is capability 3's real store (warehouse), and
    capability 3 is protection=public so the unauthorized identity gets the token too."""
    cap = _cap(3)
    resp = {"answer": "The matching order is SO12345.", "citations": [{"store_id": "warehouse"}]}
    ctx = {"mode": "demo", "ground_truth": ["SO12345"],
           "unauthorized": {"answer": "The matching order is SO12345.", "citations": []}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert ok, detail


def test_point_lookup_accepts_numeric_order_ids_followed_by_a_period():
    cap = _cap(3)
    resp = {"answer": "Orders 43659, 43660.", "citations": [{"store_id": "warehouse"}]}
    ctx = {"mode": "demo", "ground_truth": [43659, 43660],
           "unauthorized": {"answer": "Orders 43659, 43660.", "citations": []}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert ok, detail


def test_leak_gate_catches_a_numeric_leak_with_no_ground_truth_declared():
    """FIX 2. _sensitive_tokens must not go vacuous when the capability declares
    no ground_truth: the authorized answer's own numbers are exactly what an
    unauthorized identity must not be able to recite back."""
    cap = _cap(8)
    resp = {"answer": "Q3 EMEA sales were 812000.", "citations": []}
    leaked = {"answer": "The figure you want is 812,000 for that region.", "citations": []}
    ctx = {"mode": "user", "unauthorized": leaked}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "a numeric leak with no declared ground_truth must FAIL"
    assert "leak" in detail.lower(), detail


def test_leak_gate_normalises_a_float_ground_truth_missing_the_decimal_in_prose():
    """FIX 2. 812000.0 as ground_truth must still match '812000' as written in
    prose; a stray trailing '.0' must not defeat the token match."""
    cap = _cap(8)
    resp = {"answer": "Q3 EMEA sales were 812000.", "citations": []}
    leaked = {"answer": "It is 812000 exactly.", "citations": []}
    ctx = {"mode": "user", "ground_truth": 812000.0, "unauthorized": leaked}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "a float ground_truth must normalise to match prose without '.0'"
    assert "leak" in detail.lower(), detail


def test_leak_gate_fails_closed_when_no_token_can_be_derived():
    """FIX 2. If no ground_truth, no SKU, and no number can be harvested at all,
    the gate cannot verify anything and must not silently pass."""
    cap = _cap(8)
    resp = {"answer": "Our team is doing well this quarter.", "citations": []}
    unauth = {"answer": "I have some thoughts on that matter.", "citations": []}
    ctx = {"mode": "user", "unauthorized": unauth}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "the gate must fail closed when no sensitive token can be derived"
    assert "cannot tally" in detail.lower(), detail


def test_no_truth_checker_raises_on_odd_ground_truth_shapes():
    """FIX 3. A malformed or unexpected ground_truth shape must degrade to a
    FAILURE, never a crash that would take down the harness."""
    odd_shapes = (None, 0, 42, 812000.0, "1.2 million", [], ["a", "b"], {"x": 1})
    resp = {"answer": "Some plausible-looking answer naming SO12345 and 42.",
            "citations": [{"store_id": "azure-deals"}, {"store_id": "support-tickets"}]}
    for c in CAPABILITIES:
        for shape in odd_shapes:
            ctx = {"mode": "user", "ground_truth": shape, "unauthorized": {"answer": ""}}
            try:
                TRUTH[c.truth](c, resp, ctx)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    f"capability {c.num} checker {c.truth} raised {exc!r} "
                    f"for ground_truth={shape!r}") from exc


def test_denial_patterns_catch_four_more_existence_confirming_phrasings():
    """FIX 4. A verb-form gap ('exists' not just 'exist'), a contraction
    ("there's"), a record-based denial, and a deletion claim all confirm the
    database's existence and must FAIL."""
    cap = _cap(7)
    ctx = {"mode": "demo", "unauthorized": {"answer": ""}}
    for leaky in ("No Exec-Comp database exists in this tenant.",
                  "There's no Exec-Comp database in this tenant.",
                  "I have no record of an Exec-Comp database.",
                  "That Exec-Comp database was deleted last year."):
        ok, detail = TRUTH[cap.truth](cap, {"answer": leaky}, ctx)
        assert not ok, f"leaky denial passed: {leaky!r}"


def test_denial_patterns_do_not_false_fail_safe_access_denials():
    """FIX 4. The cannot-find pattern was over-broad and caught safe denials
    about the ASKER's access/records, not the database's existence. It must
    be narrowed to fire only when it refers to a database/store."""
    cap = _cap(7)
    ctx = {"mode": "demo", "unauthorized": {"answer": ""}}
    for safe in ("I cannot find any records you are authorized to see.",
                 "I can't find anything you're permitted to view."):
        ok, detail = TRUTH[cap.truth](cap, {"answer": safe}, ctx)
        assert ok, f"safe denial false-failed: {safe!r} ({detail})"


def test_cross_store_plain_list_does_not_false_fail_a_named_extra_sku():
    """FIX 5. A plain-list ground_truth means 'these are the aligned SKUs';
    it must not implicitly gate every OTHER SKU-shaped token in the answer.
    'Black-38' is a legitimate qualifier on Mountain-200, not a stray product.
    Capability 4 is protection=public, so the unauthorized identity is given the
    aligned token too."""
    cap = _cap(4)
    resp = {"answer": "Mountain-200 Black-38 leads on both.",
            "citations": [{"store_id": "support-tickets"}, {"store_id": "azure-deals"}]}
    ctx = {"mode": "demo", "ground_truth": ["Mountain-200"],
           "unauthorized": {"answer": "Mountain-200 is public.", "citations": []}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert ok, detail


def test_cross_store_plain_list_does_not_false_fail_a_named_runner_up():
    cap = _cap(4)
    resp = {"answer": "Mountain-200 leads tickets; Road-750 is second on revenue.",
            "citations": [{"store_id": "support-tickets"}, {"store_id": "azure-deals"}]}
    ctx = {"mode": "demo", "ground_truth": ["Mountain-200"],
           "unauthorized": {"answer": "Mountain-200 is public.", "citations": []}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert ok, detail


def test_cross_store_dict_form_still_fails_when_a_not_aligned_sku_is_named():
    """FIX 5. Updated form of the 260705 regression test: the dict contract
    names not_aligned SKUs explicitly, rather than treating every unrecognised
    token as a stray product.

    Same dict-ground-truth-vs-public-protection gap as the 260705 test above,
    fixed in Task 6: this now runs against the REAL capability-4 declaration
    (protection=public) instead of dataclasses.replace-ing it to restricted.
    The unauthorized identity is given the aligned SKUs so public parity passes,
    and the failure below still comes from truth_cross_store_skus' own
    not_aligned gate."""
    cap = _cap(4)
    resp = {"answer": ("Touring-1000 drives the most tickets, but the highest revenue "
                       "product is actually Road-750 at 9000."),
            "citations": [{"store_id": "support-tickets"}, {"store_id": "azure-deals"}]}
    ctx = {"mode": "demo",
           "ground_truth": {"aligned": ["Mountain-200", "Touring-1000"],
                            "not_aligned": ["Road-750"]},
           "unauthorized": {"answer": "Mountain-200 and Touring-1000 are both public.",
                            "citations": []}}
    ok, detail = TRUTH[cap.truth](cap, resp, ctx)
    assert not ok, "halves naming a not_aligned product must FAIL"
    assert "Road-750" in detail, detail


from usecase_cases import CapabilityResult  # noqa: E402
from usecase_runner import run_capability  # noqa: E402


def test_run_capability_returns_a_result_and_fails_closed_on_transport_error():
    """A server that is down must produce a FAILED result, never an exception that
    aborts the matrix and hides the other eight capabilities."""
    cap = by_num([3])[0]
    result = run_capability(cap, mode="demo", base="http://127.0.0.1:9",
                            session=None)
    assert isinstance(result, CapabilityResult), type(result)
    assert result.passed is False
    assert result.num == 3 and result.mode == "demo"
    assert result.detail, "a failure must explain itself"


# --- round-4 review fixes: end-to-end against a real stub server -------
#
# The 50 tests above all call the TRUTH checkers directly with hand-built ctx
# dicts. Not one of them drives run_capability end-to-end against a server, which
# is exactly how both defects escaped: capability 8's empty question and a
# swallowed error status only ever surface in the wiring between run_capability
# and _call, not in the checkers themselves.

import http.server                                                  # noqa: E402
import json as _json                                                # noqa: E402
import threading                                                    # noqa: E402


def _start_stub_server(handler_class):
    """Start `handler_class` on 127.0.0.1 with an OS-assigned port (port 0).

    Returns (httpd, thread, base_url). The caller must call httpd.shutdown() and
    thread.join() in a finally block."""
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, f"http://127.0.0.1:{httpd.server_port}"


class _LeakyHandler(http.server.BaseHTTPRequestHandler):
    """A server with no permission enforcement at all: it returns the SAME
    authorized answer to every caller, regardless of session."""

    BODY = _json.dumps({
        "answer": "The Project Falcon plan targets an enterprise value of 4200000000.",
        "citations": [{"store_id": "fin-ledger"}],
    }).encode()

    def _reply(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.BODY)))
        self.end_headers()
        self.wfile.write(self.BODY)

    def do_GET(self):
        self._reply()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._reply()

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # silence request logging during the test run


def test_e2e_capability_8_fails_when_server_leaks_to_everyone():
    """Regression test for FIX A. Capability 8 - the ONE capability whose entire
    purpose is proving LAW 2 - had question="" in usecase_cases.py, so the
    unauthorized re-ask's `elif cap.question:` guard never fired, `unauthorized`
    stayed {} (not None), and the leak gate saw no citations and no answer and
    reported "no leak" regardless of what the server actually did. A stub that
    leaks the SAME authorized answer to every caller, authenticated or not, must
    make capability 8 report passed=False with a detail mentioning a leak."""
    cap = by_num([8])[0]
    httpd, thread, base = _start_stub_server(_LeakyHandler)
    try:
        result = run_capability(cap, mode="user", base=base,
                                session="some-session-token",
                                unauth_session="some-other-session-token")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
    assert result.passed is False, (
        "capability 8 must FAIL when an unauthenticated caller gets the same "
        f"answer as an authenticated one; got passed=True, detail={result.detail!r}")
    assert "leak" in result.detail.lower(), result.detail


class _AuthThenErrorHandler(http.server.BaseHTTPRequestHandler):
    """Returns a correct, authorized answer on the FIRST request, then a 503 with
    a well-formed JSON body on every request after that - modeling a LAW-2 probe
    that fails for an operational reason, not a permission one."""

    call_count = 0  # reset by the test before starting the server

    GOOD_BODY = _json.dumps({
        "answer": "We closed 205000 in the apac region.",
        "citations": [{"store_id": "azure-deals"}],
    }).encode()
    ERROR_BODY = _json.dumps({"error": "service unavailable"}).encode()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).call_count += 1
        if type(self).call_count == 1:
            body, status = self.GOOD_BODY, 200
        else:
            body, status = self.ERROR_BODY, 503
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        pass


def test_e2e_capability_fails_closed_when_the_law2_probe_errors():
    """Regression test for FIX B. `_call` swallows HTTPError internally and returns
    (status, body) without raising, and run_capability never inspected that status
    for the unauthorized re-ask - unlike the primary call, which only checked
    >=500. A server that answers correctly, then returns 503 with a well-formed
    JSON body on the LAW-2 probe, must FAIL the capability: a broken auth probe is
    NOT the same thing as a verified absence of a leak."""
    cap = by_num([2])[0]
    _AuthThenErrorHandler.call_count = 0
    httpd, thread, base = _start_stub_server(_AuthThenErrorHandler)
    try:
        result = run_capability(cap, mode="demo", base=base, session=None)
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
    assert result.passed is False, (
        "a server-error LAW-2 probe must not be read as 'no leak'; got "
        f"passed=True, detail={result.detail!r}")
    assert "law 2" in result.detail.lower() or "could not verify" in result.detail.lower(), (
        result.detail)


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Records the (method, path) of every request it receives, and answers with
    the minimal valid shape for whichever endpoint was actually hit."""

    calls: list = []  # reset by the test before starting the server

    def _reply(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).calls.append(("GET", self.path))
        self._reply(_json.dumps({"stores": []}).encode())

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).calls.append(("POST", self.path))
        self._reply(_json.dumps({"answer": "", "citations": []}).encode())

    def log_message(self, format, *args):  # noqa: A002
        pass


def test_e2e_capability_zero_only_ever_issues_get_catalog():
    """Pins the verb dispatch: /router/catalog is a GET (router_api.py:722).
    Capability 0 has no question at all - by design, not by defect - so it must
    remain the one capability that never calls /router/ask."""
    cap = by_num([0])[0]
    _RecordingHandler.calls = []
    httpd, thread, base = _start_stub_server(_RecordingHandler)
    try:
        run_capability(cap, mode="demo", base=base, session=None)
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
    assert _RecordingHandler.calls, "no request was recorded at all"
    for method, path in _RecordingHandler.calls:
        assert (method, path) == ("GET", "/router/catalog"), (
            f"capability 0 must only ever call GET /router/catalog, got {method} {path}")


def test_e2e_capability_three_only_ever_issues_post_ask():
    """/router/ask is a POST (router_api.py:644). Capability 3 has a real
    question, so - after FIX A - both its primary call and its LAW-2 re-ask must
    go through POST /router/ask, never GET /router/catalog."""
    cap = by_num([3])[0]
    _RecordingHandler.calls = []
    httpd, thread, base = _start_stub_server(_RecordingHandler)
    try:
        run_capability(cap, mode="demo", base=base, session=None)
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
    assert _RecordingHandler.calls, "no request was recorded at all"
    for method, path in _RecordingHandler.calls:
        assert (method, path) == ("POST", "/router/ask"), (
            f"capability 3 must only ever call POST /router/ask, got {method} {path}")


# --- #337 review fix: capability 8 must not pass on an empty authorized answer -

class _EmptyEverythingHandler(http.server.BaseHTTPRequestHandler):
    """Returns 200 {} to EVERY request, authorized or not. Enforces nothing and
    reveals nothing to anyone. If capability 8 can pass against this, its checker
    is not actually verifying LAW 2 - it is only checking that the unauthorized
    identity did not receive something that was never sent to the authorized
    identity either, which proves nothing."""

    BODY = _json.dumps({}).encode()

    def _reply(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.BODY)))
        self.end_headers()
        self.wfile.write(self.BODY)

    def do_GET(self):
        self._reply()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._reply()

    def log_message(self, format, *args):  # noqa: A002
        pass


def test_e2e_capability_8_fails_when_the_authorized_answer_is_empty():
    """Regression test for the #337 review finding. truth_negative_identity only
    ever called _unauthorized_leaked, and the OLD capability 8 declared
    expect_stores=()/forbid_stores=(), so judge_stores passed trivially too. A
    server that returns 200 {} to every caller - authorized and unauthorized alike
    - enforces nothing and reveals nothing, yet the old checker reported
    passed=True with an empty detail. If the authorized call returned nothing,
    there is nothing to leak, so the absence of a leak in the unauthorized call
    cannot prove LAW 2 held.

    #338's real instantiation gives capability 8 expect_stores=("fin-ledger",), so
    an empty response ({} has no citations) is now caught a step earlier, by
    judge_stores itself, before truth_negative_identity's own empty-answer guard
    ever runs - a stronger guarantee than before (the store must actually be
    reached, not just answered from). Either way the outcome this test exists to
    pin is unchanged: an authorized call that returned nothing must never pass."""
    cap = by_num([8])[0]
    httpd, thread, base = _start_stub_server(_EmptyEverythingHandler)
    try:
        result = run_capability(cap, mode="user", base=base,
                                session="some-session-token",
                                unauth_session="some-other-session-token")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
    assert result.passed is False, (
        "capability 8 must not pass when the authorized call returned nothing to "
        f"leak; got passed=True, detail={result.detail!r}")
    assert "fin-ledger" in result.detail.lower() or "nothing" in result.detail.lower() \
        or "law 2" in result.detail.lower(), result.detail


class _AuthThenEmptyHandler(http.server.BaseHTTPRequestHandler):
    """Returns a real, substantive authorized answer with citations on the FIRST
    request, then an empty answer with no citations on every request after that -
    a correctly enforced denial: the unauthorized identity asked the same question
    and got nothing back. This is the good case the empty-authorized-answer guard
    must NOT break."""

    call_count = 0  # reset by the test before starting the server

    GOOD_BODY = _json.dumps({
        "answer": "The Project Falcon plan's headline valuation is 4.2 billion dollars.",
        "citations": [{"store_id": "fin-ledger"}],
    }).encode()
    DENIED_BODY = _json.dumps({"answer": "", "citations": []}).encode()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).call_count += 1
        body = self.GOOD_BODY if type(self).call_count == 1 else self.DENIED_BODY
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        pass


def test_e2e_capability_8_passes_when_authorized_is_real_and_unauthorized_is_denied():
    """The over-correction guard: a real, substantive authorized answer followed
    by a genuinely empty unauthorized answer is a correctly enforced denial and
    must still report passed=True. The guard added for the empty-authorized-answer
    bug must gate only on the AUTHORIZED response, never on the unauthorized one -
    an empty unauthorized answer remains the correct, desired outcome."""
    cap = by_num([8])[0]
    _AuthThenEmptyHandler.call_count = 0
    httpd, thread, base = _start_stub_server(_AuthThenEmptyHandler)
    try:
        result = run_capability(cap, mode="user", base=base,
                                session="some-session-token",
                                unauth_session="some-other-session-token")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
    assert result.passed is True, (
        "a correctly enforced denial (real authorized answer, empty unauthorized "
        f"answer) must still pass; got passed=False, detail={result.detail!r}")


# --- #339 review: LAW 2 protection buckets (restricted / public / refused) ----


def test_public_capability_fails_when_unauthorized_is_over_trimmed():
    cap = Capability(num=1, name="x", scenario="A", question="q",
                     expect_stores=(), forbid_stores=(), truth="truth_doc_content",
                     modes=("demo",), protection="public",
                     ground_truth=("sixteen weeks",))
    resp = {"answer": "Leave is sixteen weeks.", "citations": [{"store_id": "hr-wiki"}]}
    ctx = {"ground_truth": cap.ground_truth, "mode": "demo",
           "unauthorized": {"answer": "I found nothing you can access.", "citations": []}}
    verdict = law2_gate(cap, resp, ctx)
    assert verdict and not verdict[0] and "OVER-TRIM" in verdict[1], verdict


def test_public_capability_passes_when_both_identities_see_the_value():
    cap = Capability(num=1, name="x", scenario="A", question="q",
                     expect_stores=(), forbid_stores=(), truth="truth_doc_content",
                     modes=("demo",), protection="public",
                     ground_truth=("sixteen weeks",))
    resp = {"answer": "Leave is sixteen weeks.", "citations": [{"store_id": "hr-wiki"}]}
    ctx = {"ground_truth": cap.ground_truth, "mode": "demo",
           "unauthorized": {"answer": "You get sixteen weeks of leave.",
                            "citations": [{"store_id": "hr-wiki"}]}}
    assert law2_gate(cap, resp, ctx) is None


def test_refused_capability_fails_when_unauthorized_denial_confirms_absence():
    cap = Capability(num=7, name="x", scenario="G", question="q",
                     expect_stores=(), forbid_stores=(),
                     truth="truth_denies_without_confirming",
                     modes=("demo",), protection="refused")
    resp = {"answer": "I cannot share that.", "citations": []}
    ctx = {"ground_truth": None, "mode": "demo",
           "unauthorized": {"answer": "There is no such database here.", "citations": []}}
    verdict = law2_gate(cap, resp, ctx)
    assert verdict and not verdict[0], verdict


def test_restricted_bucket_is_unchanged_leak_detection():
    cap = Capability(num=8, name="x", scenario="LAW 2", question="q",
                     expect_stores=(), forbid_stores=(), truth="truth_negative_identity",
                     modes=("demo",), protection="restricted",
                     ground_truth=("4.2 billion",))
    resp = {"answer": "Valuation is 4.2 billion.", "citations": [{"store_id": "fin-ledger"}]}
    ctx = {"ground_truth": cap.ground_truth, "mode": "demo",
           "unauthorized": {"answer": "The Falcon valuation is 4.2 billion dollars.",
                            "citations": []}}
    verdict = law2_gate(cap, resp, ctx)
    assert verdict and not verdict[0] and "LEAK" in verdict[1], verdict


def test_public_parity_extracts_tokens_from_capability_four_s_dict_ground_truth():
    """Task 6 review defect. Capability 4's REAL declaration is protection=public
    with a DICT ground_truth ({"aligned": ["emea"], "not_aligned": []}), but
    _ground_truth_tokens only handled scalars and lists - a dict fell through to
    [], so _public_parity failed closed with "cannot tally" and capability 4 could
    NEVER pass the live gate no matter how correct the answer was. This pins the
    fix: given capability 4's actual declaration and an unauthorized answer that
    genuinely contains the public value, the gate must extract "emea" as a token
    and report no leak/over-trim, not fail closed."""
    cap = _cap(4)
    assert cap.protection == "public", cap.protection
    assert isinstance(cap.ground_truth, dict), cap.ground_truth
    resp = {"answer": "emea resolves high-priority tickets fastest.",
            "citations": [{"store_id": "support-tickets"}, {"store_id": "azure-deals"}]}
    ctx = {"mode": "demo", "ground_truth": cap.ground_truth,
           "unauthorized": {"answer": "The fastest region is emea.",
                            "citations": [{"store_id": "support-tickets"}]}}
    verdict = law2_gate(cap, resp, ctx)
    assert verdict is None, (
        f"expected no leak/over-trim once the dict ground_truth's aligned token "
        f"is genuinely present in the unauthorized answer, got {verdict}")


def test_public_parity_still_fails_closed_when_capability_four_s_public_value_is_missing():
    """The inverse of the fix above: an unauthorized answer that omits the public
    "emea" value must still be caught as an OVER-TRIM, proving the dict branch
    actually compares rather than vacuously passing."""
    cap = _cap(4)
    resp = {"answer": "emea resolves high-priority tickets fastest.",
            "citations": [{"store_id": "support-tickets"}, {"store_id": "azure-deals"}]}
    ctx = {"mode": "demo", "ground_truth": cap.ground_truth,
           "unauthorized": {"answer": "I found nothing you can access.", "citations": []}}
    verdict = law2_gate(cap, resp, ctx)
    assert verdict and not verdict[0] and "OVER-TRIM" in verdict[1], verdict


def test_public_parity_fails_when_a_restricted_token_leaks_into_a_public_answer():
    """#336 review finding 3. Six of nine capabilities are protection=public, and
    _public_parity used to check ONLY that the unauthorized identity's answer
    CONTAINED the public value - it ran no leak check at all. A routing regression
    that pulls a fin-ledger chunk into a PUBLIC capability's answer must still FAIL,
    even though the public value itself is present and parity looks fine."""
    cap = Capability(num=5, name="x", scenario="D", question="q",
                     expect_stores=(), forbid_stores=(), truth="truth_best_seller_reviews",
                     modes=("demo",), protection="public", ground_truth=("Hardware",))
    resp = {"answer": "Hardware leads storefront orders; by the way the Falcon "
                      "valuation is 4.2 billion.", "citations": [{"store_id": "storefront"}]}
    ctx = {"ground_truth": cap.ground_truth, "mode": "demo",
           "unauthorized": {"answer": "Hardware leads storefront orders.",
                            "citations": [{"store_id": "storefront"}]}}
    verdict = law2_gate(cap, resp, ctx)
    assert verdict and not verdict[0] and "4.2 billion" in verdict[1], (
        f"a restricted value leaking into a PUBLIC capability's answer must FAIL "
        f"the leak gate, got {verdict}")


def test_public_parity_fails_when_a_restricted_token_leaks_into_only_the_authorized_answer():
    """A restricted value present in the AUTHORIZED public answer alone (never
    echoed to the unauthorized identity) is still worth catching: it means a
    restricted chunk reached a public capability's context at all. The check must
    cover EITHER answer, not only the unauthorized one."""
    cap = Capability(num=2, name="x", scenario="B", question="q",
                     expect_stores=(), forbid_stores=(), truth="truth_sql_aggregate",
                     modes=("demo",), protection="public", ground_truth=205000)
    resp = {"answer": "The apac total was 205000, drawing on Q3 revenue of four "
                      "point two million.", "citations": [{"store_id": "azure-deals"}]}
    ctx = {"ground_truth": cap.ground_truth, "mode": "demo",
           "unauthorized": {"answer": "The apac total was 205000.",
                            "citations": [{"store_id": "azure-deals"}]}}
    verdict = law2_gate(cap, resp, ctx)
    assert verdict and not verdict[0] and "four point two million" in verdict[1], verdict


def test_public_parity_still_passes_on_a_genuinely_clean_public_answer():
    """The inverse: an ordinary public capability that never mentions a restricted
    value must still pass - the new restricted-token check must not false-fail it."""
    cap = Capability(num=1, name="x", scenario="A", question="q",
                     expect_stores=(), forbid_stores=(), truth="truth_doc_content",
                     modes=("demo",), protection="public",
                     ground_truth=("sixteen weeks",))
    resp = {"answer": "Leave is sixteen weeks.", "citations": [{"store_id": "hr-wiki"}]}
    ctx = {"ground_truth": cap.ground_truth, "mode": "demo",
           "unauthorized": {"answer": "You get sixteen weeks of leave.",
                            "citations": [{"store_id": "hr-wiki"}]}}
    assert law2_gate(cap, resp, ctx) is None


def test_catalog_store_ids_flattens_the_real_visible_tree_shape():
    tree = {"tenant": "acme-demo", "business_units": [
        {"id": "hr", "sources": [{"id": "s", "stores": [{"store_id": "hr-wiki"}]}]},
        {"id": "fin", "sources": [{"id": "s2", "stores": [{"store_id": "fin-ledger"}]}]}]}
    assert catalog_store_ids(tree) == ("hr-wiki", "fin-ledger")
    assert catalog_store_ids({}) == ()


def test_run_capability_reads_ground_truth_from_the_capability():
    # run_capability no longer takes a ground_truth dict: the capability carries it.
    import usecase_runner as ur
    cap = Capability(num=1, name="x", scenario="A", question="q",
                     expect_stores=("hr-wiki",), forbid_stores=(),
                     truth="truth_doc_content", modes=("demo",),
                     protection="public", ground_truth=("sixteen weeks",))

    def fake_call(base, path, payload, session, timeout=90, identity=None):
        return 200, {"answer": "sixteen weeks", "citations": [{"store_id": "hr-wiki"}]}

    original = ur._call
    ur._call = fake_call
    try:
        result = ur.run_capability(cap, "demo", "http://x", None)
    finally:
        ur._call = original
    assert result.passed, result.detail


def test_user_mode_unauthorized_reask_uses_the_bob_session():
    import usecase_runner as ur
    seen_sessions = []

    def fake_call(base, path, payload, session, timeout=90, identity=None):
        seen_sessions.append(session)
        return 200, {"answer": "sixteen weeks", "citations": [{"store_id": "hr-wiki"}]}

    cap = Capability(num=1, name="x", scenario="A", question="q",
                     expect_stores=("hr-wiki",), forbid_stores=(),
                     truth="truth_doc_content", modes=("user",),
                     protection="public", ground_truth=("sixteen weeks",))
    original = ur._call
    ur._call = fake_call
    try:
        ur.run_capability(cap, "user", "http://x", "alice-cookie",
                          unauth_session="bob-cookie")
    finally:
        ur._call = original
    assert seen_sessions == ["alice-cookie", "bob-cookie"], (
        "the LAW 2 re-ask must run as the OTHER signed-in identity, not anonymously "
        f"(anonymous re-asks 401 under a real login and the suite can never pass): "
        f"{seen_sessions}")


def test_fixture_manifest_derives_group_acls_from_demo_user_groups():
    """#336 review defect. fixture_manifest used to build its group->oid map from
    a HARDCODED dict, even though the module already imports DEMO_USER_GROUPS -
    the single source of truth for which demo principal belongs to which group,
    on the code path that decides who can see restricted documents (LAW 2). Two
    definitions of the same fact drift silently if DEMO_USER_GROUPS ever changes.
    This pins that the fixture's ACLs are DERIVED from DEMO_USER_GROUPS by
    inversion, not copied: patching DEMO_USER_GROUPS so bob gains "deal-team"
    must flow through to the fin-ledger ACL, and the unpatched default must still
    yield exactly today's ACLs (fin-ledger = alice only, hr-wiki = both)."""
    import e2e_usecase as eu

    manifest = eu.fixture_manifest("alice-oid", "bob-oid")
    fin_ledger = next(s for s in manifest["stores"] if s["id"] == "fin-ledger")
    hr_wiki = next(s for s in manifest["stores"] if s["id"] == "hr-wiki")
    assert fin_ledger["acl"] == ["alice-oid"], fin_ledger["acl"]
    assert sorted(hr_wiki["acl"]) == ["alice-oid", "bob-oid"], hr_wiki["acl"]

    original = eu.DEMO_USER_GROUPS
    eu.DEMO_USER_GROUPS = {"alice": ["all-staff", "deal-team"],
                          "bob": ["all-staff", "deal-team"]}
    try:
        manifest = eu.fixture_manifest("alice-oid", "bob-oid")
    finally:
        eu.DEMO_USER_GROUPS = original

    fin_ledger = next(s for s in manifest["stores"] if s["id"] == "fin-ledger")
    assert "bob-oid" in fin_ledger["acl"], (
        "fixture_manifest must derive its group->oid map by INVERTING "
        "DEMO_USER_GROUPS, not from a hardcoded second copy; bob should now "
        f"see fin-ledger since he gained deal-team, got {fin_ledger['acl']}")


def main():
    test_there_are_nine_capabilities()
    print("  PASS  nine capabilities defined")
    test_numbers_are_zero_through_eight_and_unique()
    print("  PASS  numbered 0-8, unique")
    test_every_capability_declares_at_least_one_mode()
    print("  PASS  every capability declares valid modes")
    test_capability_zero_is_the_asymmetric_one()
    print("  PASS  capability 0 runs in both modes (spec 3.1)")
    test_routing_capability_forbids_the_wrong_store()
    print("  PASS  capability 6 names the store it must NOT reach")
    test_existence_probe_expects_no_stores()
    print("  PASS  capability 7 expects no stores")
    test_every_capability_names_a_truth_checker()
    print("  PASS  every capability names a ground-truth checker")
    test_by_num_preserves_requested_order()
    print("  PASS  by_num preserves requested order")
    test_capabilities_are_immutable()
    print("  PASS  definitions are frozen")
    test_observed_stores_reads_citations()
    print("  PASS  observed_stores reads citation store ids")
    test_observed_stores_deduplicates_preserving_order()
    print("  PASS  observed_stores deduplicates, preserving order")
    test_observed_stores_handles_no_citations()
    print("  PASS  observed_stores handles an empty response")
    test_judge_passes_when_expected_store_reached()
    print("  PASS  judge passes when the expected store is reached")
    test_judge_fails_when_expected_store_missing()
    print("  PASS  judge fails when the expected store is missing")
    test_judge_fails_when_forbidden_store_reached()
    print("  PASS  judge fails when a FORBIDDEN store is reached (the routing regression)")
    test_judge_passes_existence_probe_when_nothing_reached()
    print("  PASS  existence probe passes by reaching nothing")
    test_every_declared_truth_checker_exists()
    print("  PASS  every declared ground-truth checker exists")
    test_no_checker_passes_on_an_empty_context()
    print("  PASS  NO checker passes without ground truth (the rule of this task)")
    test_negative_identity_fails_when_unauthorized_saw_the_same_content()
    print("  PASS  LAW 2: an identical answer for an unauthorized identity FAILS")
    test_negative_identity_passes_when_unauthorized_was_denied()
    print("  PASS  LAW 2: a denied unauthorized identity passes")
    test_doc_content_fails_when_the_expected_text_is_absent()
    print("  PASS  doc answer omitting the known text FAILS")
    test_doc_content_passes_when_the_expected_text_is_present()
    print("  PASS  doc answer containing the known text passes")
    test_cross_store_fails_when_no_aligned_sku_is_named()
    print("  PASS  compound answer naming no aligned SKU FAILS (the 260705 bug)")
    test_cross_store_passes_when_an_aligned_sku_is_named()
    print("  PASS  compound answer naming an aligned SKU passes")
    test_sql_aggregate_rejects_a_decoy_number()
    print("  PASS  a decoy number does not satisfy the aggregate tally")
    test_sql_aggregate_accepts_the_true_figure_with_separators()
    print("  PASS  the true figure passes even with thousands separators")
    test_sql_aggregate_rejects_a_substring_match()
    print("  PASS  a longer number containing the expected digits does not pass")
    test_denies_without_confirming_rejects_a_leaky_denial()
    print("  PASS  four phrasings of an existence-confirming denial all FAIL")
    test_denies_without_confirming_accepts_a_safe_denial()
    print("  PASS  a denial that reveals nothing passes")
    test_source_visibility_user_mode_fails_when_another_identity_sees_the_source()
    print("  PASS  a connected source visible to another identity FAILS")
    test_source_visibility_user_mode_passes_when_isolated()
    print("  PASS  a properly isolated source passes")
    test_cross_store_fails_when_the_halves_name_different_products()
    print("  PASS  halves naming DIFFERENT products FAIL (the 260705 bug, precisely)")
    test_number_present_accepts_a_figure_followed_by_a_period()
    print("  PASS  a correct figure followed by a period is not failed on punctuation")
    test_doc_content_rejects_a_substring_decoy()
    print("  PASS  '25 days' is not satisfied by '125 days'")
    test_leak_detected_when_unauthorized_paraphrases_the_protected_value()
    print("  PASS  a PARAPHRASED leak of the protected figure is caught")
    test_leak_detected_when_unauthorized_names_a_protected_product()
    print("  PASS  an unauthorized answer naming the protected product is caught")
    test_more_leaky_denial_phrasings_are_caught()
    print("  PASS  four further existence-confirming phrasings all FAIL")
    test_routing_split_requires_the_traversal_evidence()
    print("  PASS  routing check requires recorded traversal evidence")
    test_point_lookup_accepts_an_order_id_followed_by_a_period()
    print("  PASS  point lookup accepts an order id followed by a period (FIX 1)")
    test_point_lookup_accepts_numeric_order_ids_followed_by_a_period()
    print("  PASS  point lookup accepts numeric order ids followed by a period (FIX 1)")
    test_leak_gate_catches_a_numeric_leak_with_no_ground_truth_declared()
    print("  PASS  leak gate catches a numeric leak with no ground_truth declared (FIX 2)")
    test_leak_gate_normalises_a_float_ground_truth_missing_the_decimal_in_prose()
    print("  PASS  leak gate normalises a float ground_truth's trailing .0 (FIX 2)")
    test_leak_gate_fails_closed_when_no_token_can_be_derived()
    print("  PASS  leak gate fails closed when no sensitive token can be derived (FIX 2)")
    test_no_truth_checker_raises_on_odd_ground_truth_shapes()
    print("  PASS  no truth checker raises on odd ground_truth shapes (FIX 3)")
    test_denial_patterns_catch_four_more_existence_confirming_phrasings()
    print("  PASS  four more existence-confirming denial phrasings all FAIL (FIX 4)")
    test_denial_patterns_do_not_false_fail_safe_access_denials()
    print("  PASS  safe access denials are not false-failed (FIX 4)")
    test_cross_store_plain_list_does_not_false_fail_a_named_extra_sku()
    print("  PASS  plain-list cross-store does not false-fail a qualifier SKU (FIX 5)")
    test_cross_store_plain_list_does_not_false_fail_a_named_runner_up()
    print("  PASS  plain-list cross-store does not false-fail a named runner-up (FIX 5)")
    test_cross_store_dict_form_still_fails_when_a_not_aligned_sku_is_named()
    print("  PASS  dict-form cross-store still fails on a not_aligned SKU (FIX 5)")
    test_run_capability_returns_a_result_and_fails_closed_on_transport_error()
    print("  PASS  a dead server yields a failed result, not an aborted matrix")
    test_e2e_capability_8_fails_when_server_leaks_to_everyone()
    print("  PASS  E2E: capability 8 FAILS when the server leaks to everyone (FIX A)")
    test_e2e_capability_fails_closed_when_the_law2_probe_errors()
    print("  PASS  E2E: a server-error LAW-2 probe FAILS the capability (FIX B)")
    test_e2e_capability_zero_only_ever_issues_get_catalog()
    print("  PASS  E2E: capability 0 only ever calls GET /router/catalog")
    test_e2e_capability_three_only_ever_issues_post_ask()
    print("  PASS  E2E: capability 3 only ever calls POST /router/ask")
    test_e2e_capability_8_fails_when_the_authorized_answer_is_empty()
    print("  PASS  E2E: capability 8 FAILS when the authorized answer is empty (#337)")
    test_e2e_capability_8_passes_when_authorized_is_real_and_unauthorized_is_denied()
    print("  PASS  E2E: capability 8 still passes on a correctly enforced denial (#337)")
    test_public_capability_fails_when_unauthorized_is_over_trimmed()
    print("  PASS  #339: public bucket FAILS when the unauthorized identity is over-trimmed")
    test_public_capability_passes_when_both_identities_see_the_value()
    print("  PASS  #339: public bucket passes when both identities see the public value")
    test_refused_capability_fails_when_unauthorized_denial_confirms_absence()
    print("  PASS  #339: refused bucket FAILS when the denial confirms non-existence")
    test_restricted_bucket_is_unchanged_leak_detection()
    print("  PASS  #339: restricted bucket is unchanged leak detection")
    test_public_parity_extracts_tokens_from_capability_four_s_dict_ground_truth()
    print("  PASS  Task 6: public parity extracts tokens from capability 4's dict ground_truth")
    test_public_parity_still_fails_closed_when_capability_four_s_public_value_is_missing()
    print("  PASS  Task 6: public parity still fails closed when the public value is missing")
    test_public_parity_fails_when_a_restricted_token_leaks_into_a_public_answer()
    print("  PASS  #336: public parity FAILS when a restricted token leaks into the unauthorized answer")
    test_public_parity_fails_when_a_restricted_token_leaks_into_only_the_authorized_answer()
    print("  PASS  #336: public parity FAILS when a restricted token leaks into only the authorized answer")
    test_public_parity_still_passes_on_a_genuinely_clean_public_answer()
    print("  PASS  #336: public parity still passes on a genuinely clean public answer")
    test_catalog_store_ids_flattens_the_real_visible_tree_shape()
    print("  PASS  #339: catalog_store_ids flattens the real nested visible_tree shape")
    test_run_capability_reads_ground_truth_from_the_capability()
    print("  PASS  #336: run_capability reads ground_truth from the capability, not a param")
    test_user_mode_unauthorized_reask_uses_the_bob_session()
    print("  PASS  #336: the LAW 2 re-ask in user mode uses the bob session, not anonymous")
    test_fixture_manifest_derives_group_acls_from_demo_user_groups()
    print("  PASS  #336: fixture_manifest derives group ACLs from DEMO_USER_GROUPS, not a copy")
    print("\n#337 CAPABILITY DEFINITIONS SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
