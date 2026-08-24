"""Golden pack freeze gate (spec 2026-07-31 section 5, review C4). The validator is
the single most trust-critical piece of the suite: a pack may only be baselined once
it exits 0. This selftest builds a clean pack that satisfies every check, then mutates
exactly one thing per test to prove each authored-lie class is actually caught.

    python3 tests/selftest_validate_golden_pack.py
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.eval.golden.pack import load_pack  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "validate_golden_pack", ROOT / "scripts" / "validate_golden_pack.py")
vgp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vgp)


# ---------------------------------------------------------------------------
# Base pack: designed once so it passes every check_* untouched. Each bad-pack
# test starts from these builders and mutates exactly one thing before writing.
# Builders return fresh literals on every call so no test can leak mutable
# state into another (re-testability constraint).
# ---------------------------------------------------------------------------

def _base_meta():
    return {
        "version": 1,
        "provenance": "synthetic",
        "stores": {
            "hr-wiki": {"title": "HR Wiki", "kind": "docs",
                        "business_unit": "hr", "description": "hr policies"},
            "sales-figures": {"title": "Sales", "kind": "sql",
                              "business_unit": "sales", "description": "sales rows"},
        },
        "alignments": [
            {"sql": "SELECT SUM(amount) FROM spend WHERE region='us'",
             "must_appear_in_doc": "spend-summary"},
        ],
    }


def _base_docs():
    return {
        "hr-wiki": [
            {"external_id": "leave-policy", "title": "Parental leave",
             "text": "parental leave is sixteen weeks", "acl": "public"},
            {"external_id": "vacation-policy", "title": "Vacation",
             "text": "vacation days are twenty five", "acl": "public"},
            {"external_id": "spend-summary", "title": "Spend summary",
             "text": "us paid-search spend was 812000 dollars total", "acl": "public"},
        ],
    }


def _base_tables():
    return {
        "sales-figures": {
            "spend": "region,channel,amount\nus,paid-search,812000\nemea,paid-search,150000\n",
        },
    }


def _base_questions():
    return [
        {"id": "A-001", "capability": "A",
         "question": "What is our parental-leave policy?",
         "expect_stores": ["hr-wiki"], "doc_qrels": ["leave-policy"],
         "negative_qrels": ["vacation-policy"],
         "key_facts": ["sixteen weeks"], "forbidden_facts": ["severance package"]},
        {"id": "B-001", "capability": "B",
         "question": "What was total US paid-search spend?",
         "expect_stores": ["sales-figures"], "gold_table": "spend",
         "gold_sql": "SELECT SUM(amount) FROM spend WHERE region='us'",
         "key_facts": ["812000"]},
    ]


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


def _mk_pack(root, meta=None, docs=None, tables=None, questions=None):
    _write_pack(root,
                meta if meta is not None else _base_meta(),
                docs if docs is not None else _base_docs(),
                tables if tables is not None else _base_tables(),
                questions if questions is not None else _base_questions())


def _validate_in(root):
    return vgp.validate(load_pack(root))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_clean_pack_validates():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk_pack(root)
        assert _validate_in(root) == []


def test_fact_in_question_caught():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        questions = _base_questions()
        questions[0]["question"] = "Is parental leave sixteen weeks?"
        _mk_pack(root, questions=questions)
        problems = _validate_in(root)
        assert any("A-001" in p and "sixteen weeks" in p for p in problems), problems


def test_distractor_containing_truth_caught():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        docs = _base_docs()
        for d in docs["hr-wiki"]:
            if d["external_id"] == "vacation-policy":
                d["text"] = "vacation days are twenty five, also parental leave is sixteen weeks"
        _mk_pack(root, docs=docs)
        problems = _validate_in(root)
        assert any("A-001" in p and "vacation-policy" in p for p in problems), problems


def test_dangling_qrel_caught():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        questions = _base_questions()
        questions[0]["doc_qrels"] = ["nope"]
        _mk_pack(root, questions=questions)
        problems = _validate_in(root)
        assert any("A-001" in p and "nope" in p for p in problems), problems


def test_gold_sql_mismatch_caught():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        questions = _base_questions()
        questions[1]["key_facts"] = ["999"]
        _mk_pack(root, questions=questions)
        problems = _validate_in(root)
        assert any("B-001" in p and "999" in p for p in problems), problems


def test_misaligned_cross_store_caught():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        docs = _base_docs()
        for d in docs["hr-wiki"]:
            if d["external_id"] == "spend-summary":
                d["text"] = "us paid-search spend was substantial this quarter"
        _mk_pack(root, docs=docs)
        problems = _validate_in(root)
        assert any("spend-summary" in p for p in problems), problems


def test_haystack_item_gating_hermetic_caught():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        questions = _base_questions()
        questions[0]["hardness"] = ["haystack"]
        _mk_pack(root, questions=questions)
        problems = _validate_in(root)
        assert any("A-001" in p and "hermetic-lexical" in p for p in problems), problems


def test_pii_caught():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        docs = _base_docs()
        for d in docs["hr-wiki"]:
            if d["external_id"] == "leave-policy":
                d["text"] = "parental leave is sixteen weeks, contact bob@real.com for details"
        _mk_pack(root, docs=docs)
        problems = _validate_in(root)
        assert any("leave-policy" in p for p in problems), problems


def test_zero_row_table_caught():
    """Authorized extension (Task 5 deferral): a header-only CSV must be rejected,
    not treated as vacuously REAL by gold_value's column sniffing."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        tables = _base_tables()
        tables["sales-figures"]["spend"] = "region,channel,amount\n"
        _mk_pack(root, tables=tables)
        problems = _validate_in(root)
        assert any("spend" in p and "zero data rows" in p for p in problems), problems


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    print("selftest_validate_golden_pack: all green")
