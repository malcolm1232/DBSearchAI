"""#499 structure-aware chunking: a JSON/tabular document chunks per RECORD, not per
1200 chars — plus the truncation-disclosure guard at the prompt boundary.

The measured failure (findings §14-§16, B-011/B-012): a 400,000-char Graph-API users
dump stored inside `Json.txt` reaches the plain-text path (mime text/plain), becomes ONE
giant segment, and the sliding window slices records at arbitrary 1200-char boundaries —
the fact-chunk is unfindable by embedding (doc-MRR 0.33) and verbatim extraction fails
on the slices that do arrive.
"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local.extract import json_, text as text_mod


def _graph_dump(n_records: int = 60) -> dict:
    """A Graph-API-shaped users dump: wrapper dict, records under 'value'."""
    return {
        "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users",
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$top=999",
        "value": [
            {
                "displayName": f"Person {i}",
                "jobTitle": f"Title {i}",
                "officeLocation": f"Office {i}",
                "mail": f"person{i}@example.com",
                "department": f"Department {i} with some padding text to size the record",
            }
            for i in range(n_records)
        ],
    }


# ---- prong B: json_.segment splits oversized subtrees per record ----

def test_json_wrapper_dict_splits_per_record():
    data = json.dumps(_graph_dump(60)).encode()
    segs = json_.segment(data)
    record_segs = [s for s in segs if s.locator and s.locator.get("path", "").startswith("value[")]
    assert len(record_segs) == 60, f"expected 60 record segments, got {len(record_segs)}"
    # each record is one coherent segment: its own fields together, nobody else's
    s7 = next(s for s in record_segs if s.locator["path"] == "value[7]")
    assert "Person 7" in s7.text and "Title 7" in s7.text and "Office 7" in s7.text
    assert "Person 8" not in s7.text
    # no giant flattened segment remains
    assert all(len(s.text) < 2500 for s in segs), "an oversized segment survived the split"


def test_json_small_dict_unchanged():
    data = b'{"pricing": {"rate": 2000}, "timeline": {"weeks": 12}}'
    segs = json_.segment(data)
    assert {s.locator["path"] for s in segs} == {"pricing", "timeline"}


def test_json_small_array_unchanged():
    data = b'[{"id": 1, "note": "alpha deal"}, {"id": 2, "note": "beta deal"}]'
    segs = json_.segment(data)
    assert len(segs) == 2
    assert segs[0].locator == {"kind": "record", "path": "[0]"}


# ---- prong A: text path sniffs JSON content and chunks per record ----

def test_text_sniffs_json_content():
    data = json.dumps(_graph_dump(60)).encode()
    segs = text_mod.segment(data)
    record_segs = [s for s in segs if s.locator and s.locator.get("path", "").startswith("value[")]
    assert len(record_segs) == 60, "a .txt file holding JSON must chunk per record"


def test_text_plain_prose_unchanged():
    segs = text_mod.segment(b"Just an ordinary memo about the quarterly review.")
    assert len(segs) == 1
    assert segs[0].text == "Just an ordinary memo about the quarterly review."
    assert not segs[0].locator


def test_text_invalid_json_falls_back_to_prose():
    data = b'{ this is not valid json, just prose that happens to open with a brace'
    segs = text_mod.segment(data)
    assert len(segs) == 1
    assert segs[0].text.startswith("{ this is not valid json")


def test_text_truncated_json_still_splits_per_record():
    # The REAL B-011 artifact: build_doc_pack caps text at 400,000 chars, slicing the
    # Graph dump mid-record — invalid JSON. The sniff must salvage the complete records
    # rather than fall back to one giant prose segment (which the window then shreds).
    full = json.dumps(_graph_dump(60))
    data = full[: len(full) - 80].encode()  # cut mid-record, like text[:MAX_CHARS]
    segs = text_mod.segment(data)
    record_segs = [s for s in segs if s.locator and s.locator.get("path", "").startswith("value[")]
    assert len(record_segs) >= 58, f"expected the complete records, got {len(record_segs)}"
    assert all(len(s.text) < 2500 for s in segs)


def test_text_concatenated_dumps_split_per_record():
    # The OTHER real-artifact shape (the actual Json.txt): several API responses pasted
    # into one .txt back to back — a complete dump, then another, the last one truncated.
    d1 = json.dumps(_graph_dump(30))
    d2 = json.dumps(_graph_dump(30))
    data = (d1 + " " + d2[: len(d2) - 80]).encode()
    segs = text_mod.segment(data)
    record_segs = [s for s in segs if s.locator and "value[" in s.locator.get("path", "")]
    assert len(record_segs) >= 58, f"expected records from BOTH dumps, got {len(record_segs)}"
    assert all(len(s.text) < 2500 for s in segs)


def test_text_trivial_json_falls_back_to_prose():
    # "{}" parses but yields no segments — must not return an empty extraction
    segs = text_mod.segment(b"{}")
    assert len(segs) == 1 and segs[0].text == "{}"


# ---- the seam that failed live: needle record survives extract + chunk intact ----

def test_needle_record_survives_chunking_whole():
    from dbsearch.adapters.local.rich_extractor import LocalRichExtractor
    from dbsearch.pipeline.runner import _chunk_text

    dump = _graph_dump(400)  # ~40K chars: big enough that sliding window would shred it
    dump["value"][213] = {
        "displayName": "Loo Say Hoo",
        "jobTitle": "Manager, Key Account",
        "officeLocation": "Shah Alam",
        "mail": "loo.say.hoo@example.com",
        "department": "Sales",
    }
    data = json.dumps(dump).encode()
    segments = LocalRichExtractor().extract_segments(data, "text/plain")
    chunks = [c for seg in segments for c in _chunk_text(seg.text)]
    intact = [c for c in chunks
              if "Loo Say Hoo" in c and "Manager, Key Account" in c and "Shah Alam" in c]
    assert intact, "the needle record must land whole inside a single chunk"
    # and the chunk is PURE — one record per chunk, no neighbors diluting the embedding
    # (a 1200-char sliding window around a ~170-char record always drags in neighbors)
    for c in chunks:
        if "Loo Say Hoo" in c:
            assert "Person " not in c, "needle chunk must not contain neighbor records"


# ---- prong C: prompt-boundary truncation must disclose, never cut silently ----

def test_prompt_cap_truncation_disclosed():
    from dbsearch.adapters.anthropic import cap_chunks_disclosed

    long_chunk = "x" * 2000
    short_chunk = "short and under the cap"
    capped = cap_chunks_disclosed([long_chunk, short_chunk], 1500)
    assert capped[1] == short_chunk, "under-cap chunks must pass through untouched"
    assert len(capped[0]) < 2000
    assert "TRUNCATED" in capped[0], "a cut chunk must carry a visible truncation note"
    assert capped[0].startswith("x" * 100)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("PASS", name)
