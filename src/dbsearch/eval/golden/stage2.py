"""Stage-2 answer scoring, independent sqlite gold engine, and failure attribution
(spec 2026-07-31 sections 4 and I1). Gold truth executes on stdlib sqlite over the
frozen CSVs, never through the product's SQL broker, so a broker bug cannot corrupt
truth and product identically.

Helpers never mutate a caller's dict or list: each returns its own
(metrics_fragment, failures_fragment) pair and `score_stage2` merges them - the same
house pattern Task 4's stage1.py established."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from dbsearch.eval import abstained
from dbsearch.eval.tally import number_present, phrase_present


def _as_number(fact):
    try:
        return float(fact)
    except (TypeError, ValueError):
        return None


def fact_in(text: str, fact) -> bool:
    """Dispatch a key-fact/forbidden-fact check to the right tally primitive: a fact
    that parses as float is checked with `number_present`, everything else with
    `phrase_present`. Public (no leading underscore) because Task 7's pack validator
    reuses this exact dispatch to confirm gold facts are locatable in the source
    corpus."""
    num = _as_number(fact)
    return number_present(text, num) if num is not None else phrase_present(text, fact)


def _load_csv(con: sqlite3.Connection, name: str, path: Path) -> None:
    """Load one frozen CSV into an in-memory sqlite table. A column loads as REAL
    only when every body cell parses as float; otherwise it loads as TEXT, so a
    numeric-looking value in an otherwise-text column stays a string.

    #489: a blank cell loads as NULL - the same call the PRODUCT engine makes (#490).
    This engine loaded blanks as '', so B-004's gold counted the empty string as a 63rd
    product category, and the model's correct 62 was scored wrong for a day. When the
    answer key and the system under test disagree about what absence IS, every
    comparison downstream of a blank is noise."""
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    header, body = rows[0], rows[1:]
    numeric = [all(_as_number(r[i]) is not None for r in body) for i in range(len(header))]
    cols = ", ".join(f"{c} {'REAL' if numeric[i] else 'TEXT'}" for i, c in enumerate(header))
    con.execute(f"CREATE TABLE {name} ({cols})")
    con.executemany(
        f"INSERT INTO {name} VALUES ({','.join('?' * len(header))})",
        [[None if str(v).strip() == "" else (float(v) if numeric[i] else v)
          for i, v in enumerate(r)] for r in body],
    )


def gold_value(tables: dict, gold_sql: str):
    """Run `gold_sql` against a flat, in-memory sqlite engine built fresh from every
    table in `tables` (store_id -> {table_name: csv Path}), and return the first
    cell of the first row. Table names must be unique pack-wide; a collision across
    stores raises ValueError (the pack validator enforces this too, at author time)."""
    con = sqlite3.connect(":memory:")
    seen: set = set()
    for store_tables in tables.values():
        for name, path in store_tables.items():
            if name in seen:
                raise ValueError(f"duplicate table name across stores: {name}")
            seen.add(name)
            _load_csv(con, name, path)
    return con.execute(gold_sql).fetchone()[0]


def _fact_recall_score(answer: str, item) -> tuple:
    metrics: dict = {}
    failures: list = []
    if item.key_facts:
        hits = sum(1 for f in item.key_facts if fact_in(answer, f))
        metrics["fact_recall"] = hits / len(item.key_facts)
        if hits < len(item.key_facts):
            failures.append("key-facts")
    return metrics, failures


def _execution_accuracy_score(answer: str, item, gold) -> tuple:
    metrics: dict = {}
    failures: list = []
    if item.gold_sql and gold is not None:
        metrics["execution_accuracy"] = number_present(answer, float(gold))
        if not metrics["execution_accuracy"]:
            failures.append("execution-accuracy")
    return metrics, failures


def _abstention_score(answer: str, item) -> tuple:
    metrics: dict = {}
    failures: list = []
    if not item.answerable:
        metrics["abstained_ok"] = abstained(answer)
        if not metrics["abstained_ok"]:
            failures.append("abstention")
    return metrics, failures


def _leak_score(result: dict, item) -> tuple:
    """Same blob surface as router_eval._surface_blob: the whole result dict
    (answer, citations, routing.reason, everything), JSON-dumped and lowercased.
    forbidden_facts dispatch through fact_in; forbid_stores always via phrase_present.
    Both hit lists combine into a single leak verdict."""
    blob = json.dumps(result, default=str).lower()
    hits = [f for f in item.forbidden_facts if fact_in(blob, f)]
    hits += [s for s in item.forbid_stores if phrase_present(blob, s)]
    metrics = {"leak": bool(hits)}
    failures = ["leak"] if hits else []
    return metrics, failures


def score_stage2(result: dict, item, gold=None) -> dict:
    """Score one golden item's stage-2 answer dimensions. Unscored dimensions stay
    None and are never added to failures."""
    answer = result.get("answer") or ""
    metrics = {m: None for m in ("fact_recall", "execution_accuracy", "abstained_ok", "leak")}
    failures: list = []
    for frag_metrics, frag_failures in (
        _fact_recall_score(answer, item),
        _execution_accuracy_score(answer, item, gold),
        _abstention_score(answer, item),
        _leak_score(result, item),
    ):
        metrics.update(frag_metrics)
        failures.extend(frag_failures)
    return {"metrics": metrics, "failures": failures}


def attribute(s1: dict, s2: dict) -> str:
    """Roll stage-1 and stage-2 failures into a single root-cause bucket, precedence
    exactly: leak, then routing-miss, then retrieval-miss, then synthesis-miss."""
    if "leak" in s2["failures"] or "forbidden-store" in s1["failures"]:
        return "leak"
    if "routing" in s1["failures"]:
        return "routing-miss"
    if s1["failures"]:
        return "retrieval-miss"
    if s2["failures"]:
        return "synthesis-miss"
    return "pass"
