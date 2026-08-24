"""Self-test: the canvas credential panel (#417, ADR 0010 s3) keeps its three promises.

The panel is client JS, so these are structural checks over js/surfaces/canvas.js - the same style as
the other canvas selftests. They pin the invariants a refactor could silently drop:

1. ONE-WAY DOOR - a self-serve credential input is type=password, carries data-secretfield,
   and is NEVER bound to the [data-cfg] keystroke-persistence path; only the /secrets POST's
   returned handle is assigned into node.config.
2. REPLACE, NOT EDIT - the stored-handle row renders a Replace control and a disabled input;
   there is no path that writes a stored secret value back into the DOM.
3. STORAGE SCRUB - saveCanvas persists config through scrubSecrets, which drops any
   secret-typed value that is neither an ${ENV} ref nor a secret:// handle.

    python3 tests/selftest_canvas_credential_panel.py
"""
import re
import sys
from pathlib import Path

CANVAS = (Path(__file__).resolve().parents[1]
          / "src" / "dbsearch" / "server" / "static" / "js" / "surfaces" / "canvas.js")
html = CANVAS.read_text()


def test_credential_input_is_a_one_way_door():
    m = re.search(r'<input type="password" data-secretfield=', html)
    assert m, "self-serve credential input (type=password + data-secretfield) is gone"
    # the credential input must not double as a [data-cfg] input - that binding writes every
    # keystroke into node.config and localStorage
    assert not re.search(r'data-secretfield="[^"]*"[^>]*data-cfg', html) and \
           not re.search(r'data-cfg="[^"]*"[^>]*data-secretfield', html), \
        "credential input is bound to the data-cfg persistence path"
    assert "node.config[field]=d.handle" in html, \
        "the /secrets response handle is no longer what lands in node.config"
    assert 'api("/secrets",{method:"POST"' in html, \
        "credential commit no longer POSTs to /secrets"


def test_stored_handle_renders_replace_not_edit():
    assert re.search(r'data-sechint="[^"]*"', html), "stored-handle hint row is gone"
    assert "data-secreplace" in html, "Replace control is gone"
    # the hint fetch may print existence and a masked hint, never a value: the only /secrets
    # GET consumer must read .exists/.hint fields alone
    seg = html[html.index('p.querySelectorAll("[data-sechint]")'):]
    seg = seg[:seg.index("function commitSecret")]
    assert ".value" not in seg.replace("inp.value", ""), \
        "the handle-describe block touches something other than the masked hint"


def test_savecanvas_scrubs_secret_literals():
    assert "config:scrubSecrets(n.kind,n.config)" in html, \
        "saveCanvas no longer persists config through scrubSecrets"
    m = re.search(r"function scrubSecrets\(kind,config\)\{(.*?)\n  \}", html, re.S)
    assert m, "scrubSecrets is gone"
    body = m.group(1)
    assert "isEnvRef" in body and "isSecretHandle" in body and "delete out[f.k]" in body, \
        "scrubSecrets no longer drops non-ref, non-handle secret values"


def test_prefill_is_operator_gated():
    """ADR 0011 s4: a non-operator's new node must not be seeded with the operator's
    ${ENV} refs - CFG_OPERATOR guards the resolvable check."""
    assert "let CFG_OPERATOR" in html, "CFG_OPERATOR state is gone"
    assert re.search(r"CFG_OPERATOR\s*=\s*c\.operator", html), \
        "/config handler no longer records the operator flag"
    assert re.search(r"envRef\s*&&\s*CFG_OPERATOR\s*&&\s*ENV_PRESENT\.has", html), \
        "addNode prefill is not gated on CFG_OPERATOR"


def test_env_labeling_is_operator_gated():
    """ADR 0011 s4: the 'env secret' hint and the ENV tag follow the same flag. A
    non-operator has no env refs to label, so advertising the affordance only promises
    something the server refuses at compose (#423)."""
    m = re.search(r"function renderPanel\(\)\{(.*?)\n  \}", html, re.S)
    assert m, "renderPanel not found - this test is pinned to it"
    body = m.group(1)
    assert re.search(r"const envTag\s*=\s*f\.secret\s*&&\s*CFG_OPERATOR", body), \
        "the ENV labeling no longer follows CFG_OPERATOR"
    assert '(f.secret?"env secret":"connection")' not in body and \
           "(f.secret?'<span class=\"tag\">ENV</span>':'')" not in body, \
        "renderPanel still labels an env secret / paints the ENV tag unconditionally"


if __name__ == "__main__":
    test_credential_input_is_a_one_way_door()
    test_stored_handle_renders_replace_not_edit()
    test_savecanvas_scrubs_secret_literals()
    test_prefill_is_operator_gated()
    test_env_labeling_is_operator_gated()
    print("PASS")
