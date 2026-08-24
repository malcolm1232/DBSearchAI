#!/usr/bin/env python3
"""Build a golden pack for the DOCUMENT rail from a local corpus (#487).

The real pack (#473) is 100% SQL, so the consulting wedge SKILL.md is actually built for -
"have we done this before / draft this proposal from our past work" - has never been
measured on real data. It is also why #460's embedder question is unanswerable: embeddings
barely participate in a SQL answer.

**This pack is PERSONAL and never leaves the machine.** The corpus, the extracted text, the
questions and the answer key all live under a gitignored directory; only aggregate SCORES
are ever committed. That is a stricter rule than the SQL pack needs, and it is deliberate:
a question authored over private documents encodes their content just as surely as the
documents do.

    python3 scripts/build_doc_pack.py --corpus "unstructured documents/F&N" \\
        --out "unstructured documents/doc_pack"

Extraction runs entirely in-process through the product's own LocalRichExtractor, so
nothing is sent anywhere. Files that cannot be parsed are reported, never silently dropped:
a corpus that quietly lost a third of its documents would make every number meaningless.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.adapters.local.rich_extractor import LocalRichExtractor  # noqa: E402
from dbsearch.ports.base import ParseProducedNoText, UnsupportedMedia  # noqa: E402

MIMES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".rtf": "text/plain",
}

#: .csv and .xlsx are deliberately NOT here. They are structured data, and the SQL rail
#: already measures that shape properly - with an executable answer key. Letting 383 CSVs
#: into a DOCUMENT corpus would have made this a fake document eval: the numbers would
#: mostly describe how well the product reads spreadsheets as prose.
_STRUCTURED_NOT_DOCUMENTS = (".csv", ".xlsx", ".xls")

#: Directories that hold software, not documents. An Electron bundle contributes thousands
#: of files and not one of them is a document.
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build",
             "Old Downloads/chromedriver"}
SKIP_SUFFIXES = (".app", ".framework", ".dmg", ".pkg")

STORE_ID = "fnn-docs"   # must equal the docs/<dir>/ name: pack.py derives store id from it

MIN_CHARS = 200          # below this there is nothing to retrieve
MAX_CHARS = 400_000      # a single doc that dwarfs the corpus distorts every ranking


def candidates(corpus: Path) -> list:
    """Every document-shaped file under `corpus`, excluding software bundles."""
    out = []
    for path in sorted(corpus.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in MIMES:
            continue
        if path.name.startswith("~$"):
            continue                          # an Office lock file, never a document
        parts = set(path.relative_to(corpus).parts)
        if parts & SKIP_DIRS or any(
                p.endswith(SKIP_SUFFIXES) for p in path.relative_to(corpus).parts):
            continue
        out.append(path)
    return out


def doc_id(path: Path, corpus: Path) -> str:
    """A stable, readable, UNIQUE id per document.

    The slug alone is not unique: paths are truncated to keep ids readable, and this corpus
    has several that agree for well over 120 characters (three meeting documents under the
    same project folder). The first build wrote 119 files for 120 extracted documents - one
    was silently overwritten, which is precisely the "quietly lost a document" failure this
    script exists to avoid. The digest of the FULL relative path makes collisions
    impossible while keeping the slug legible."""
    rel = str(path.relative_to(corpus))
    slug = re.sub(r"[^a-z0-9]+", "-", rel.lower()).strip("-")[:100]
    return f"{slug}-{hashlib.sha1(rel.encode()).hexdigest()[:8]}"


def extract(paths: list, corpus: Path) -> tuple:
    """(docs, skipped) - docs are {external_id, title, uri, text, acl}."""
    extractor = LocalRichExtractor()
    docs, skipped = [], Counter()
    for path in paths:
        try:
            text = extractor.extract(path.read_bytes(), MIMES[path.suffix.lower()])
        except (UnsupportedMedia, ParseProducedNoText):
            skipped[f"unparseable {path.suffix.lower()}"] += 1
            continue
        except Exception as exc:                      # noqa: BLE001 - report, never crash
            skipped[f"{type(exc).__name__} {path.suffix.lower()}"] += 1
            continue
        text = " ".join(text.split())
        if len(text) < MIN_CHARS:
            skipped[f"too short {path.suffix.lower()}"] += 1
            continue
        docs.append({
            "external_id": doc_id(path, corpus),
            "title": path.stem,
            "uri": str(path.relative_to(corpus)),
            "text": text[:MAX_CHARS],
            "acl": "public",
        })
    return docs, skipped


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default="unstructured documents/F&N")
    parser.add_argument("--out", default="unstructured documents/doc_pack")
    args = parser.parse_args(argv)
    corpus, out = Path(args.corpus), Path(args.out)
    if not corpus.is_dir():
        print(f"no corpus at {corpus}", file=sys.stderr)
        return 2

    paths = candidates(corpus)
    print(f"candidates: {len(paths)} document-shaped files")
    docs, skipped = extract(paths, corpus)
    print(f"extracted:  {len(docs)}")
    if skipped:
        print("skipped:")
        for reason, n in skipped.most_common():
            print(f"    {n:>4}  {reason}")

    (out / "docs" / STORE_ID).mkdir(parents=True, exist_ok=True)
    for doc in docs:
        (out / "docs" / "corpus" / f"{doc['external_id']}.json").write_text(
            json.dumps(doc, indent=1))
    (out / "tables").mkdir(exist_ok=True)
    written = len(list((out / "docs" / STORE_ID).glob("*.json")))
    if written != len(docs):
        print(f"ERROR: extracted {len(docs)} but wrote {written} - ids collided",
              file=sys.stderr)
        return 1
    chars = sum(len(d["text"]) for d in docs)
    print(f"\nwrote {len(docs)} docs -> {out}/docs/corpus  ({chars:,} chars, "
          f"median {sorted(len(d['text']) for d in docs)[len(docs)//2]:,})")
    print("NOTE: this pack is personal - it stays gitignored, and only scores are committed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
