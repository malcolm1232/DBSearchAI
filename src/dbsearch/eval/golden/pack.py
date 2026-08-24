"""Golden pack data model (spec 2026-07-31 section 1). Pure: the only I/O is reading
the frozen pack directory. Scorers and the validator consume the loaded GoldenPack;
nothing here imports server or router code."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path

_PROTECTIONS = ("public", "restricted", "refused")


@dataclass(frozen=True)
class GoldenQ:
    id: str
    capability: str
    question: str
    expect_stores: tuple = ()
    forbid_stores: tuple = ()
    doc_qrels: tuple = ()
    chunk_qrels: tuple = ()
    negative_qrels: tuple = ()
    gold_sql: str = ""
    gold_table: str = ""
    key_facts: tuple = ()
    forbidden_facts: tuple = ()
    protection: str = "public"
    variant_of: str = ""
    hardness: tuple = ("plain",)
    profiles: tuple = ("hermetic-lexical", "semantic")
    answerable: bool = True

    def __post_init__(self):
        if self.protection not in _PROTECTIONS:
            raise ValueError(f"{self.id}: protection {self.protection!r} not in {_PROTECTIONS}")

    @classmethod
    def from_json(cls, d: dict) -> "GoldenQ":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown GoldenQ keys: {sorted(unknown)}")
        coerced = {k: tuple(v) if isinstance(v, list) else v for k, v in d.items()}
        return cls(**coerced)


@dataclass(frozen=True)
class GoldenPack:
    meta: dict
    questions: tuple
    docs: dict          # store_id -> [doc dicts: external_id/title/text/acl]
    tables: dict        # store_id -> {table_name: csv Path}
    content_hash: str
    root: Path


def pack_hash(root: Path) -> str:
    """sha256 over every file's relative path + bytes, sorted, so any content or
    layout change invalidates all baselines (spec section 5)."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_docs(root: Path) -> dict:
    docs: dict = {}
    for f in sorted((root / "docs").rglob("*.json")):
        docs.setdefault(f.parent.name, []).append(json.loads(f.read_text()))
    return docs


def _load_tables(root: Path) -> dict:
    tables: dict = {}
    for f in sorted((root / "tables").rglob("*.csv")):
        tables.setdefault(f.parent.name, {})[f.stem] = f
    return tables


def load_pack(root: Path) -> GoldenPack:
    meta = json.loads((root / "pack_meta.json").read_text())
    lines = (root / "questions.jsonl").read_text().splitlines()
    questions = tuple(GoldenQ.from_json(json.loads(l)) for l in lines if l.strip())
    return GoldenPack(meta=meta, questions=questions, docs=_load_docs(root),
                      tables=_load_tables(root), content_hash=pack_hash(root), root=root)
