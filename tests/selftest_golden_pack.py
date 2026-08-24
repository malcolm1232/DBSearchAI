"""Golden pack model + loader (spec 2026-07-31 section 1). Hermetic: builds a tiny
pack in a tmp dir, never touches eval_fixtures/.

    python3 tests/selftest_golden_pack.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.eval.golden.pack import GoldenQ, load_pack, pack_hash  # noqa: E402


def _mk_pack(root: Path):
    (root / "docs" / "hr-wiki").mkdir(parents=True)
    (root / "tables" / "sales-figures").mkdir(parents=True)
    (root / "pack_meta.json").write_text(json.dumps(
        {"version": 1, "provenance": "synthetic", "stores": {
            "hr-wiki": {"title": "HR Wiki", "kind": "docs",
                        "business_unit": "hr", "description": "hr policies"},
            "sales-figures": {"title": "Sales", "kind": "sql",
                              "business_unit": "sales", "description": "sales rows"}},
         "alignments": []}))
    (root / "docs" / "hr-wiki" / "leave-policy.json").write_text(json.dumps(
        {"external_id": "leave-policy", "title": "Parental leave",
         "text": "parental leave is sixteen weeks", "acl": "public"}))
    (root / "tables" / "sales-figures" / "spend.csv").write_text(
        "region,channel,amount\nus,paid-search,812000\nemea,paid-search,150000\n")
    (root / "questions.jsonl").write_text(json.dumps(
        {"id": "A-001", "capability": "A",
         "question": "What is our parental-leave policy?",
         "expect_stores": ["hr-wiki"], "doc_qrels": ["leave-policy"],
         "key_facts": ["sixteen weeks"]}) + "\n")


def test_load_pack_round_trip():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk_pack(root)
        pack = load_pack(root)
        assert pack.questions[0].id == "A-001"
        assert pack.questions[0].expect_stores == ("hr-wiki",)
        assert pack.questions[0].profiles == ("hermetic-lexical", "semantic")
        assert pack.docs["hr-wiki"][0]["external_id"] == "leave-policy"
        assert "spend" in pack.tables["sales-figures"]
        assert len(pack.content_hash) == 64


def test_hash_changes_when_content_changes():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk_pack(root)
        h1 = pack_hash(root)
        (root / "docs" / "hr-wiki" / "leave-policy.json").write_text(
            json.dumps({"external_id": "leave-policy", "title": "Parental leave",
                        "text": "parental leave is TWELVE weeks", "acl": "public"}))
        assert pack_hash(root) != h1


def test_unknown_question_key_rejected():
    try:
        GoldenQ.from_json({"id": "x", "capability": "A", "question": "q", "typo_field": 1})
        raise AssertionError("unknown key accepted")
    except ValueError:
        pass


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    print("selftest_golden_pack: all green")
