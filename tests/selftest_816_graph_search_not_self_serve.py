"""#816 - graph_search is not self-serve yet, and every surface must stop pretending.

Found in the #780 prod audit as a copy leak ("dev spike; E5 OBO replaces this" on a
customer verdict), and found worse while fixing: the signed-in palette offered the kind
with a `token` secret field that NOTHING consumes - GraphSearchProvider._make never reads
config.token, env_token_provider reads only the deployment env - so a customer could
store a real bearer into a field wired to nowhere. Three pins:

  1. The Files & Links palette rail does not offer graph_search. The KINDS entry stays,
     so a yaml-authored node still renders; the rail is the self-serve door, and a kind
     with no self-serve path must not be behind it.
  2. The KINDS entry carries no secret token field - offering a credential input that is
     never read is worse than offering nothing.
  3. The no-credential message is for its actual audience: it names GRAPH_TOKEN as
     OPERATOR configuration and points the user at a SharePoint source - and carries no
     internal vocabulary ("dev spike", "E5", "OBO", "spike").

    PYTHONPATH=src python3 tests/selftest_816_graph_search_not_self_serve.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CANVAS = (ROOT / "src" / "dbsearch" / "server" / "static" / "js" / "surfaces" / "canvas.js")
html = CANVAS.read_text()


def _rail_kinds(label: str) -> list[str]:
    # re.S: #920 wrapped the Files & Links entry onto two lines, and without it this
    # regex reported the rail "gone" while it sat right there.
    m = re.search(r'label:"%s".*?kinds:\[([^\]]*)\]' % re.escape(label), html, re.S)
    assert m, f"the {label} rail is gone from the palette config"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_the_palette_rail_does_not_offer_graph_search():
    kinds = _rail_kinds("Files & Links")
    assert "graph_search" not in kinds, (
        f"the self-serve rail offers a kind with no self-serve path: {kinds}")
    assert "upload" in kinds and "folder" in kinds, f"rail lost real kinds: {kinds}"


def test_the_kinds_entry_survives_but_without_the_unwired_token_field():
    m = re.search(r"graph_search:\{label[^}]*fields:\[(.*?)\]\}", html)
    assert m, "the graph_search KINDS entry is gone - a yaml-authored node cannot render"
    fields = m.group(1)
    assert '"token"' not in fields and "k:\"token\"" not in fields, (
        f"the token field is offered but nothing reads config.token: {fields}")


def test_the_no_credential_message_speaks_to_its_audience():
    from dbsearch.router.native_search import env_token_provider
    import os
    saved = os.environ.pop("GRAPH_TOKEN", None)
    try:
        env_token_provider()("oid-x")
        raise AssertionError("no token must still raise")
    except RuntimeError as exc:
        msg = str(exc)
        for internal in ("dev spike", "spike", "E5", "OBO"):
            assert internal not in msg, f"internal vocabulary on a user surface: {msg!r}"
        assert "GRAPH_TOKEN" in msg, f"the operator remedy must name the env var: {msg!r}"
        assert "operator" in msg.lower() or "deployment" in msg.lower(), (
            f"the message must say WHOSE problem this is: {msg!r}")
        assert "SharePoint" in msg, (
            f"the user needs the thing they CAN do instead: {msg!r}")
    finally:
        if saved is not None:
            os.environ["GRAPH_TOKEN"] = saved


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
