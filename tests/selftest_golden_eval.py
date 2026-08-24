"""HTTP runner for the golden retrieval-efficacy suite (spec 2026-07-31 Task 9).
Hermetic: builds a tiny golden pack in a tmp dir and composes it IN-PROCESS via
`router.load_manifest` (memory backend, HashingEmbedding, an extractive fake LLM) -
never a server, mirroring tests/selftest_router_eval.py's bootstrap exactly. Proves
`pack_manifest` and the pure scoring glue (`score_row`) end-to-end; `run_items`/`main`
(the only HTTP-touching code) are exercised for real in Task 11's live sweep.

    python3 tests/selftest_golden_eval.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.adapters.local import HashingEmbedding, InMemoryIdentity  # noqa: E402
from dbsearch import router  # noqa: E402
from dbsearch.eval.golden import gate, scorecard  # noqa: E402
from dbsearch.eval.golden.pack import GoldenQ, load_pack  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "golden_runner", ROOT / "scripts" / "golden_runner.py")
gr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gr)


class _ExtractiveLlm:
    """Same faithful stand-in as selftest_router_eval.py: echoes the retrieved context
    (so key facts in evidence appear in the answer) and abstains on nothing retrieved."""

    def answer(self, question, context_chunks):
        if not context_chunks:
            return {"answer": "I couldn't find anything you have access to about that.",
                    "citations": []}
        return {"answer": " ".join(context_chunks), "citations": []}


# --------------------------------------------------------------------------------- #
# Tiny golden pack builder (mirrors tests/selftest_validate_golden_pack.py's _mk_pack,
# extended with acl variants: public/restricted docs + a single-row SQL table designed
# so the extractive fake can pass - HashingEmbedding is bag-of-words, so every question
# below lexically overlaps its truth doc/table, and the SQL table has exactly one row
# whose amount value IS the SUM the gold_sql computes).
# --------------------------------------------------------------------------------- #

def _write_pack(root, meta, docs, tables, questions):
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "tables").mkdir(parents=True, exist_ok=True)
    (root / "pack_meta.json").write_text(json.dumps(meta))
    for store_id, items in docs.items():
        store_dir = root / "docs" / store_id
        store_dir.mkdir(parents=True, exist_ok=True)
        for item in items:
            (store_dir / f"{item['external_id']}.json").write_text(json.dumps(item))
    for store_id, table_map in tables.items():
        store_dir = root / "tables" / store_id
        store_dir.mkdir(parents=True, exist_ok=True)
        for name, csv_text in table_map.items():
            (store_dir / f"{name}.csv").write_text(csv_text)
    with open(root / "questions.jsonl", "w") as f:
        for q in questions:
            f.write(json.dumps(q) + "\n")


def _meta():
    return {"version": 1, "provenance": "synthetic", "stores": {
        "hr-wiki": {"title": "HR Wiki", "kind": "docs", "business_unit": "hr",
                    "description": "human resources parental leave policy"},
        "fin-ledger": {"title": "Finance Ledger", "kind": "docs", "business_unit": "finance",
                       "description": "confidential restricted deal memos"},
        "sales-figures": {"title": "Sales", "kind": "sql", "business_unit": "sales",
                          "description": "sales figures total paid search spend by region"},
    }, "alignments": []}


def _docs():
    return {
        "hr-wiki": [
            {"external_id": "leave-policy", "title": "Parental leave",
             "text": "parental leave is sixteen weeks", "acl": "public"},
            {"external_id": "leave-draft", "title": "Old draft",
             "text": "office equipment requests go through the IT reimbursement portal",
             "acl": "public"},
        ],
        "fin-ledger": [
            {"external_id": "project-falcon", "title": "Deal memo",
             "text": "project falcon acquisition budget is 4200000",
             "acl": "restricted"},
        ],
    }


def _tables():
    return {"sales-figures": {
        "spend": "region,channel,amount\nus,paid-search,100000\n",
    }}


def _questions():
    return [
        {"id": "A-001", "capability": "A",
         "question": "How many weeks of parental leave do employees get?",
         "expect_stores": ["hr-wiki"], "doc_qrels": ["leave-policy"],
         "negative_qrels": ["leave-draft"], "key_facts": ["sixteen weeks"]},
        {"id": "B-001", "capability": "B",
         "question": "What was the total amount of US paid search spend?",
         "expect_stores": ["sales-figures"], "gold_table": "spend",
         "gold_sql": "SELECT SUM(amount) FROM spend WHERE region='us'",
         "key_facts": ["100000"]},
    ]


def _mk_pack(root, meta=None, docs=None, tables=None, questions=None):
    _write_pack(root,
               meta if meta is not None else _meta(),
               docs if docs is not None else _docs(),
               tables if tables is not None else _tables(),
               questions if questions is not None else _questions())


def _svc(manifest):
    reg = router.ProviderRegistry()
    reg.register(router.LocalIndexProvider())
    reg.register(router.CsvSqlProvider())
    cat = router.load_manifest(manifest, registry=reg)
    return router.RouterQueryService(cat, InMemoryIdentity({}), HashingEmbedding())


# --------------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------------- #

def test_pack_manifest_shape():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk_pack(root)
        pack = load_pack(root)
        manifest = gr.pack_manifest(pack, "alice", "bob")
        by_id = {s["id"]: s for s in manifest["stores"]}

        hr = by_id["hr-wiki"]
        assert hr["kind"] == "local"
        hr_seeds = {s["external_id"]: s for s in hr["config"]["seed"]}
        assert hr_seeds["leave-policy"]["acl"] == ["alice", "bob"]

        fin = by_id["fin-ledger"]
        fin_seeds = {s["external_id"]: s for s in fin["config"]["seed"]}
        assert fin_seeds["project-falcon"]["acl"] == ["alice"]

        sql = by_id["sales-figures"]
        assert sql["kind"] == "csv"
        table = sql["config"]["tables"]["spend"]
        assert table["columns"] == ["region", "channel", "amount"]
        assert table["rows"] == [["us", "paid-search", 100000.0]]


def test_in_process_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk_pack(root)
        pack = load_pack(root)
        manifest = gr.pack_manifest(pack, "alice", "bob")
        svc = _svc(manifest)
        llm = _ExtractiveLlm()

        rows = []
        for item in pack.questions:
            gold = gr._gold_for(pack, item)
            result = svc.ask("alice", item.question, llm).to_dict()
            rows.append(gr.score_row(item, result, None, gold))

        assert all(r["passed"] for r in rows), rows
        assert all(r["attribution"] == "pass" for r in rows), rows

        key = scorecard.baseline_key("hermetic-lexical", "memory-hashing", "extractive",
                                     pack.content_hash)
        card = scorecard.build_scorecard(rows, key)
        assert not gate.compare(rows, card["items"]).red


def test_distractor_only_corpus_fails_stage1():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk_pack(root)
        pack = load_pack(root)
        manifest = gr.pack_manifest(pack, "alice", "bob")
        hr = next(s for s in manifest["stores"] if s["id"] == "hr-wiki")
        # keep the distractor, drop the truth doc
        hr["config"]["seed"] = [s for s in hr["config"]["seed"]
                                if s["external_id"] != "leave-policy"]
        svc = _svc(manifest)
        item = next(q for q in pack.questions if q.id == "A-001")
        result = svc.ask("alice", item.question, _ExtractiveLlm()).to_dict()
        row = gr.score_row(item, result, None, None)
        assert row["passed"] is False
        assert row["attribution"] == "retrieval-miss", row


def test_restricted_leak_detected():
    item = GoldenQ(id="L-001", capability="LAW2", question="confidential revenue ledger",
                   protection="restricted", forbidden_facts=("4200000",),
                   forbid_stores=("fin-ledger",))
    alice_result = {"answer": "the confidential revenue ledger figure is 4200000",
                    "citations": [{"store_id": "fin-ledger", "doc": "project-falcon"}],
                    "evidence": [], "routing": {"stores": [{"store_id": "fin-ledger"}]}}

    bob_leaky = {"answer": "the ledger shows 4200000", "citations": [], "evidence": [],
                "routing": {"reason": "leaked"}}
    leaked_row = gr.score_row(item, alice_result, bob_leaky, None)
    assert leaked_row["passed"] is False
    assert leaked_row["attribution"] == "leak", leaked_row

    bob_clean = {"answer": "I couldn't find anything you have access to about that.",
                "citations": [], "evidence": [], "routing": {}}
    clean_row = gr.score_row(item, alice_result, bob_clean, None)
    assert clean_row["passed"] is True, clean_row
    assert clean_row["attribution"] == "pass", clean_row

    no_reask_row = gr.score_row(item, alice_result, None, None)
    assert no_reask_row["passed"] is False
    assert no_reask_row["attribution"] == "error", no_reask_row


def test_default_subset_covers_every_capability_hardness_cell():
    """Over the REAL frozen pack: the default (non --full) tier must still keep a
    representative of every (capability, hardness tag) cell the full profile-
    filtered set has, and must be much smaller than the full set (IMPORTANT-2)."""
    pack = load_pack(ROOT / "eval_fixtures" / "golden_pack")
    filtered = [q for q in pack.questions if "hermetic-lexical" in q.profiles]
    full_cells = {(q.capability, tag) for q in filtered for tag in q.hardness}

    subset = gr.select_subset(filtered, full=False)
    subset_cells = {(q.capability, tag) for q in subset for tag in q.hardness}

    assert subset_cells == full_cells, sorted(full_cells - subset_cells)
    assert len(subset) < len(filtered) / 2, (len(subset), len(filtered))


def test_select_subset_full_is_identity():
    pack = load_pack(ROOT / "eval_fixtures" / "golden_pack")
    filtered = [q for q in pack.questions if "hermetic-lexical" in q.profiles]
    assert gr.select_subset(filtered, full=True) == filtered


def test_catalog_visible_pure_shapes():
    """catalog_visible is the pure helper behind the #368 pre-baseline probe
    (IMPORTANT-3): true only when the visible_tree shape actually names a store."""
    populated = {"business_units": [{"sources": [{"stores": [{"store_id": "hr-wiki"}]}]}]}
    empty_sources = {"business_units": [{"sources": []}]}
    empty_stores = {"business_units": [{"sources": [{"stores": []}]}]}
    no_business_units = {"business_units": []}

    assert gr.catalog_visible(populated) is True
    assert gr.catalog_visible(empty_sources) is False
    assert gr.catalog_visible(empty_stores) is False
    assert gr.catalog_visible(no_business_units) is False
    assert gr.catalog_visible({}) is False
    assert gr.catalog_visible(None) is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    print("selftest_golden_eval: all green")
