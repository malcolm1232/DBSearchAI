"""Golden-pack MANIFEST building - pure, no network, no server imports.

Extracted from `golden_runner` (#478): that module sat exactly at the 400-line budget, so
the LAW 2 ACL fix had nowhere to go. These functions were already marked as their own
section there and share no state with the runner, so the seam was the module's own.

`golden_runner` re-exports `pack_manifest`, so every existing import keeps working.
"""
import csv
from pathlib import Path

# --------------------------------------------------------------------------------- #
# Manifest building (pure)
# --------------------------------------------------------------------------------- #

def _csv_columns_rows(path: Path) -> dict:
    """One frozen CSV into the inline `{"columns", "rows"}` shape CsvSqlProvider expects.
    Numeric cells become floats, so a gold SUM lands on the same type on both sides."""
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    header, data = rows[0], rows[1:]
    typed = [[float(c) if c.replace(".", "", 1).lstrip("-").isdigit() else c for c in row]
             for row in data]
    return {"columns": header, "rows": typed}


def _seed_acl(acl_field: str, alice: str, bob: str) -> list:
    if acl_field == "public":
        return [alice, bob]
    if acl_field == "restricted":
        return [alice]
    raise ValueError(f"unknown doc acl {acl_field!r} (expected 'public' or 'restricted')")


def _doc_seed(doc: dict, alice: str, bob: str) -> dict:
    return {"external_id": doc["external_id"], "title": doc.get("title", doc["external_id"]),
            "uri": doc.get("uri", doc["external_id"]), "text": doc["text"],
            "acl": _seed_acl(doc["acl"], alice, bob)}


def _docs_store(store_id: str, meta: dict, docs: list, alice: str, bob: str) -> dict:
    seeds = [_doc_seed(d, alice, bob) for d in docs]
    return {"id": store_id, "kind": "local", "business_unit": meta.get("business_unit", ""),
            "acl": sorted({a for s in seeds for a in s["acl"]}), "config": {"seed": seeds},
            "title": meta.get("title", store_id), "description": meta.get("description", "")}


def _sql_store(store_id: str, meta: dict, tables: dict, alice: str, bob: str) -> dict:
    inline = {name: _csv_columns_rows(path) for name, path in tables.items()}
    # #478: ACL from the PACK, not hardcoded [alice, bob] - that made every structured
    # store visible to both identities, so LAW 2 was never exercised on this rail. Shares
    # _seed_acl with the doc path. Why it matters: tests/selftest_478_law2_sql_rail.py.
    return {"id": store_id, "kind": "csv", "business_unit": meta.get("business_unit", ""),
            "acl": _seed_acl(meta.get("acl", "public"), alice, bob),
            "title": meta.get("title", store_id),
            "description": meta.get("description", ""),   # #486 descriptions: authored, not
            "config": {"tables": inline,                   # derived - see describe_schema
                       "schema_descriptions": meta.get("schema_descriptions") or {}}}


def pack_manifest(pack, alice: str, bob: str) -> dict:
    """Translate a loaded GoldenPack into a `/router/compose` manifest (spec section 2).
    Docs stores become `kind: "local"` seeds (acl "public" -> [alice, bob], "restricted"
    -> [alice]); SQL stores become `kind: "csv"` stores with the CSVs inlined."""
    stores = []
    for store_id, meta in pack.meta["stores"].items():
        kind = meta["kind"]
        if kind == "docs":
            stores.append(_docs_store(store_id, meta, pack.docs.get(store_id, []), alice, bob))
        elif kind == "sql":
            stores.append(_sql_store(store_id, meta, pack.tables.get(store_id, {}), alice, bob))
        else:
            raise ValueError(f"{store_id}: unknown pack store kind {kind!r} in pack_meta.json")
    return {"tenant": "golden-pack", "stores": stores}

