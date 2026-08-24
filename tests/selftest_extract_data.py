import io
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local.extract import tabular, json_


def test_csv_segments_per_row():
    data = b"name,role,rate\nAlice,Partner,3000\nBob,Analyst,900\n"
    segs = tabular.segment_csv(data)
    assert len(segs) == 2
    assert segs[0].locator == {"kind": "row", "n": 2}      # header is row 1
    assert "name: Alice" in segs[0].text and "rate: 3000" in segs[0].text
    assert segs[1].locator["n"] == 3


def test_xlsx_segments_per_row():
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["name", "city"]); ws.append(["Alice", "London"]); ws.append(["Bob", "Paris"])
    buf = io.BytesIO(); wb.save(buf)
    segs = tabular.segment_xlsx(buf.getvalue())
    assert len(segs) == 2
    assert segs[0].locator["kind"] == "row" and segs[0].locator["n"] == 2
    assert "city: London" in segs[0].text


def test_json_array_segments_per_record():
    data = b'[{"id": 1, "note": "alpha deal"}, {"id": 2, "note": "beta deal"}]'
    segs = json_.segment(data)
    assert len(segs) == 2
    assert segs[0].locator == {"kind": "record", "path": "[0]"}
    assert "note: alpha deal" in segs[0].text


def test_json_object_segments_per_key():
    data = b'{"pricing": {"rate": 2000}, "timeline": {"weeks": 12}}'
    segs = json_.segment(data)
    paths = {s.locator["path"] for s in segs}
    assert paths == {"pricing", "timeline"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("PASS", name)
