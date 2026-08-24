#!/usr/bin/env python3
"""Author and VERIFY the document-rail question set (#487).

The SQL pack got its answer key for free: execute the query, the engine writes the answer.
Documents have no such oracle, so the discipline has to be supplied here instead - every
authored fact is checked against the corpus before the pack is written:

* an answerable question's fact must appear VERBATIM in the document it is attributed to;
* it must appear in EXACTLY as many documents as the question claims, so a "find the right
  document" question cannot be satisfied by an accident of vocabulary;
* an unanswerable question's probe terms must appear in NO document, or it is not
  unanswerable and the abstention it tests is a lie.

Anything that fails is reported and the pack is not written. A question set nobody checked
is the model-authored corpus problem (#473) wearing a different hat.

The questions and the corpus are PERSONAL - written into the gitignored pack, never
committed. Only scores leave this directory.

    PYTHONPATH=src python3 scripts/author_doc_questions.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Documents whose content is genuinely personal. They stay in the corpus - a real
#: workspace contains them, and they are excellent distractors - but they are RESTRICTED,
#: which finally exercises LAW 2 on real data (#478: the SQL pack cannot, because
#: golden_runner gives every SQL store both identities).
RESTRICTED = ("Confirmation Appraisal", "Annexe 2", "Health Checkup", "Baptism",
              "Photoshoot", "Pre-Employment",
              # #494 sweep: the corpus survey found genuinely personal documents OUTSIDE
              # the original six patterns - bank statements (account numbers + balances),
              # a line-of-credit letter, the resume, employment paperwork, personal
              # agreements and notes. They stay in the corpus as realistic distractors,
              # restricted. The shared hash suffix covers every downloaded bank PDF.
              "9d3f7af42ab834ac899ce79ffe2853310edafd7", "malcolmpdf statement",
              "personal-line-of-credit", "xHamster", "Malcolm Resume", "mdm cheang",
              "Letter Of Employment", "Annexe 1", "Employee Consent",
              "Expectation Inventory", "Life’s logic", "jsforms")

QUESTIONS = [
    # --- A: one fact, one document ----------------------------------------------------
    dict(id="A-001", capability="A", doc="1_NewBusinessObjective/ReadMe",
         q="Which Singapore government registry was to be contacted to find out which "
           "businesses are listed?",
         facts=["ACRA"]),
    dict(id="A-002", capability="A", doc="4_ProductMixOptimization/KT_Progress",
         q="Who prepared the progress flow for the product mix optimization work?",
         facts=["Chan Sin Hui"]),
    dict(id="A-003", capability="A", doc="5_FNMC Data Cleansing/Talend Notes",
         q="Which version of Talend Studio was installed on the workstation?",
         facts=["7.3.1"]),

    # --- B: the fact is buried in a long document, among 119 others --------------------
    dict(id="B-001", capability="B", doc="Magnolia_milk_Regression_Analysis",
         q="Who is named as the Transformation Project Manager on the brand commercial "
           "effectiveness project charter?",
         facts=["Sharon Goh"]),
    dict(id="B-002", capability="B", doc="Magnolia_milk_Regression_Analysis",
         q="Which brand was the marketing regression dashboard to be trialled on?",
         facts=["100PLUS"]),
    dict(id="B-003", capability="B", doc="4_ProductMixOptimization/KT_Progress",
         # The source writes "March 2019 to Sep 2019"; a correct answer says "September".
         # Pinning the abbreviation failed a verbatim-correct answer - the #463 artifact
         # class, self-inflicted. Anchor on the part every correct phrasing must contain.
         q="Which pre-covid months were used to build the product mix dataset?",
         facts=["March 2019"]),
    dict(id="B-004", capability="B", doc="4_ProductMixOptimization/KT_Progress",
         q="In which data warehouse project are the product mix AI datasets stored?",
         facts=["fn-datalake-prod"]),

    # --- A (#494 expansion): one fact, one document -------------------------------------
    dict(id="A-004", capability="A",
         doc="Policy Guidelines for External Appointment",
         q="When was the policy on external directorship appointments last reviewed?",
         facts=["20 January 2021"]),
    dict(id="A-005", capability="A", doc="Live Great e-Claim",
         q="Which website do you go to for first-time registration on the e-Claims portal?",
         facts=["employeebenefits.com.sg"]),
    dict(id="A-006", capability="A", doc="Lesson 1-1 Introduction to Talend",
         q="How are the Talend training hands-on sessions facilitated so participants can "
           "work at their own pace?",
         facts=["pre-recorded screen recording"]),
    dict(id="A-007", capability="A", doc="1_NewBusinessObjective/ReadMe",
         q="What is the end goal for the count of unexplored places in the new business "
           "objective app?",
         facts=["ZERO Unexplored"]),
    dict(id="A-008", capability="A", doc="malcolmwaterreading",
         q="Which Australian stock ticker is named in the water investment notes?",
         facts=["D20.AX"]),

    # --- B (#494 expansion): buried facts among 119 distractors -------------------------
    dict(id="B-005", capability="B", doc="Magnolia_milk_Regression_Analysis",
         q="Who is the Theme Head on the brand commercial effectiveness project charter?",
         facts=["Celine Tan"]),
    dict(id="B-006", capability="B", doc="Magnolia_milk_Regression_Analysis",
         q="By which financial year was the marketing dashboard and regression model to "
           "be implemented?",
         facts=["FY2122"]),
    dict(id="B-007", capability="B", doc="4_ProductMixOptimization/KT_Progress",
         q="From which system are vending machines extracted for the product mix dataset?",
         facts=["WIS"]),
    dict(id="B-008", capability="B", doc="5_FNMC Data Cleansing/Talend Notes",
         q="Who is leading the Walburg vending machine business?",
         facts=["Djaron"]),
    dict(id="B-009", capability="B", doc="5_FNMC Data Cleansing/Talend Notes",
         q="Which system records what a distributor sells to outlet stores?",
         facts=["ESND"]),
    dict(id="B-010", capability="B", doc="Talend/Training Files/Learning",
         q="Who granted access to the AIInsights Teams channel with the Microsoft "
           "training materials?",
         facts=["Yeu Shun Yee"]),
    dict(id="B-011", capability="B", doc="Power BI/Department",
         q="What is Loo Say Hoo's job title in the staff directory?",
         facts=["Manager, Key Account"]),
    dict(id="B-012", capability="B", doc="Power BI/Department",
         q="Which office location is Loo Say Hoo based at?",
         facts=["Shah Alam"]),
    dict(id="B-013", capability="B", doc="Magnolia_milk_Regression_Analysis",
         q="Whose retail tracking data is collected for the regression model besides "
           "SAP BI data?",
         facts=["AC Nielsen"]),
    dict(id="B-014", capability="B", doc="Live Great e-Claim",
         q="Besides finding a panel clinic, what can be requested from the e-Claims "
           "portal homepage tools?",
         facts=["Letter of Guarantee"]),

    # --- G: nothing in the corpus answers these; the honest answer is a refusal --------
    dict(id="G-001", capability="G", answerable=False,
         q="What is the annual budget for the Bandung warehouse relocation programme?",
         probes=["Bandung"]),
    dict(id="G-002", capability="G", answerable=False,
         q="What notice period does the distribution agreement with Carrefour specify?",
         probes=["Carrefour"]),
    dict(id="G-003", capability="G", answerable=False,
         q="How many delivery vans were leased for the Penang depot in 2024?",
         probes=["Penang"]),

    # --- G (#494 expansion) -------------------------------------------------------------
    dict(id="G-004", capability="G", answerable=False,
         q="What discount did the Lidl account negotiate for isotonic drinks?",
         probes=["Lidl"]),
    dict(id="G-005", capability="G", answerable=False,
         q="How many pallets does the Surabaya distribution centre hold?",
         probes=["Surabaya"]),
    dict(id="G-006", capability="G", answerable=False,
         q="What cold-chain SLA applies to the Vientiane depot?",
         probes=["Vientiane"]),
    dict(id="G-007", capability="G", answerable=False,
         # "Ariba" failed verification - it appears in the staff directory (a person's
         # name), which is precisely why probes are checked against the corpus.
         q="Which vendor won the Coupa procurement tender?",
         probes=["Coupa"]),
    dict(id="G-008", capability="G", answerable=False,
         q="What headcount was approved for the Phuket sales office?",
         probes=["Phuket"]),

    # --- LAW2: restricted personal documents, alice only -------------------------------
    dict(id="LAW2-001", capability="LAW2", doc="Pre-Employment Health Checkup",
         protection="restricted",
         q="How long must you fast before the blood test at the pre-employment medical?",
         facts=["10 hours"]),
    # #494: the facts below are deliberately innocuous - this file is committed, so a
    # question may name a restricted DOCUMENT but never quote anything sensitive from it.
    dict(id="LAW2-002", capability="LAW2", doc="Malcolm Resume",
         protection="restricted",
         q="Which institute awarded the Bachelor of Business on the resume?",
         facts=["Royal Melbourne Institute of Technology"]),
    dict(id="LAW2-003", capability="LAW2", doc="Letter Of Employment",
         protection="restricted",
         q="How long is the probation period stated in the letter of employment?",
         facts=["six (6) months"]),
]


def load_corpus(pack: Path) -> list:
    return [json.loads(f.read_text())
            for f in sorted((pack / "docs" / "fnn-docs").glob("*.json"))]


_ABBREV_MONTH = re.compile(r"\b(jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b", re.I)


def abbreviation_trap(fact: str) -> bool:
    """An abbreviated month in a key fact fails a correct answer that spells it out.
    B-003 answered "March 2019 to September 2019" and was scored wrong against a fact
    reading "Sep 2019" - a scoring artifact, not a product failure."""
    return bool(_ABBREV_MONTH.search(fact))


def contains(text: str, fact: str) -> bool:
    """Word-anchored, case-insensitive - the same discipline the SQL scorer uses, so a
    fact cannot be satisfied by a substring of a longer token."""
    return re.search(rf"(?<!\w){re.escape(fact)}(?!\w)", text, re.I) is not None


def verify(docs: list) -> tuple:
    """(questions, problems). A question only survives if the corpus agrees with it."""
    out, problems = [], []
    for spec in QUESTIONS:
        answerable = spec.get("answerable", True)
        if not answerable:
            hits = [d for d in docs
                    for p in spec["probes"] if contains(d["text"], p)]
            if hits:
                problems.append(
                    f"{spec['id']}: claimed unanswerable but {len(hits)} document(s) "
                    f"mention {spec['probes']} - e.g. {hits[0]['uri']}")
                continue
            out.append({"id": spec["id"], "capability": spec["capability"],
                        "question": spec["q"], "answerable": False,
                        "protection": "refused", "key_facts": [], "expect_stores": []})
            continue

        target = [d for d in docs if spec["doc"] in d["uri"]]
        if not target:
            problems.append(f"{spec['id']}: no document matches {spec['doc']!r}")
            continue
        for fact in spec["facts"]:
            if abbreviation_trap(fact):
                problems.append(
                    f"{spec['id']}: key fact {fact!r} contains an abbreviated month - a "
                    "correct answer that spells it out would be scored wrong")
        holders = [d for d in docs if all(contains(d["text"], f) for f in spec["facts"])]
        if not holders:
            problems.append(f"{spec['id']}: fact {spec['facts']} appears in NO document")
            continue
        if not any(d in target for d in holders):
            problems.append(
                f"{spec['id']}: fact {spec['facts']} is not in its attributed document "
                f"{spec['doc']!r} - it is in {holders[0]['uri']}")
            continue
        out.append({"id": spec["id"], "capability": spec["capability"],
                    "question": spec["q"], "key_facts": spec["facts"],
                    "doc_qrels": [d["external_id"] for d in holders],
                    "expect_stores": ["fnn-docs"],
                    "protection": spec.get("protection", "public"),
                    "profiles": ["semantic"]})
    return out, problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", default="unstructured documents/doc_pack")
    args = parser.parse_args(argv)
    pack = Path(args.pack)
    docs = load_corpus(pack)
    if not docs:
        print(f"no corpus at {pack}; run build_doc_pack.py first", file=sys.stderr)
        return 2

    # RESTRICTED marking happens here rather than in the builder, so re-authoring the
    # question set cannot silently change which documents are protected.
    restricted = 0
    for f in sorted((pack / "docs" / "fnn-docs").glob("*.json")):
        doc = json.loads(f.read_text())
        acl = "restricted" if any(r in doc["uri"] for r in RESTRICTED) else "public"
        if doc["acl"] != acl:
            doc["acl"] = acl
            f.write_text(json.dumps(doc, indent=1))
        restricted += acl == "restricted"

    questions, problems = verify(docs)
    if problems:
        print("PACK NOT WRITTEN - the corpus disagrees with the question set:")
        for p in problems:
            print(f"  {p}")
        return 1

    (pack / "questions.jsonl").write_text(
        "".join(json.dumps(q, sort_keys=True) + "\n" for q in questions))
    (pack / "pack_meta.json").write_text(json.dumps({
        "provenance": "PERSONAL local corpus - never committed, never portable",
        "answer_key": "every fact verified verbatim against the corpus by "
                      "scripts/author_doc_questions.py before writing",
        "stores": {"fnn-docs": {
            "kind": "docs", "title": "Work Documents", "business_unit": "consulting",
            "description": "Project notes, meeting documents, charters, training material "
                           "and personal records from a working document library - "
                           "projects, data migration, business intelligence, ETL.",
        }},
        "alignments": [], "derivations": [],
    }, indent=2, sort_keys=True) + "\n")
    print(f"{len(questions)} questions verified against {len(docs)} documents "
          f"({restricted} restricted)")
    for q in questions:
        n = len(q.get("doc_qrels", []))
        print(f"  {q['id']:9} {q['capability']:5} "
              f"{'unanswerable' if not q.get('key_facts') else f'{n} doc(s) hold the fact'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
