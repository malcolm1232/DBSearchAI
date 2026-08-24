"""#368 final review (CRITICAL 2 / IMPORTANT 1): the canvas <-> stored-manifest round trip
must be LOSSLESS, because since #368 it writes back to the system of record.

`loadLiveUser` restores the canvas from GET /router/manifest and then calls `composeUp()`,
which POSTs what it rebuilt to /router/compose - and `_persisting_compose` OVERWRITES the
stored Postgres row with it. So any field the restore mapper drops is destroyed durably after
ONE page reload: it survives restarts, and clearing localStorage does not undo it.

Two ways that bit:
  * `delegation:` - entryOf DELETES config.require_signin and emits a sibling delegation
    block. The restore mapper read only id/kind/business_unit/acl/config/description, so
    "queries run as the signed-in user" (OBO, #156) silently reverted to the service
    credential: the store's row-level security stopped applying and it returned every row the
    service principal can see, while the panel rendered require_signin blank so the UI agreed
    with the wrong state.
  * `kind` - an unknown kind (`folder`, the setup agent's main output, and `databricks`; both
    real registered providers with no KINDS row) was rewritten to `local`, which composes as
    an empty local index: a green node that answers nothing (#200).

There is no JS test harness in this repo, so this drives the REAL functions out of the shipped
asset with node. Without node it degrades to source assertions rather than silently passing.

    PYTHONPATH=src python3 tests/selftest_canvas_manifest_roundtrip.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import _domgate  # the shared node/jsdom gate (#792)

ROOT = Path(__file__).resolve().parents[1]
CANVAS = (ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js").read_text()
# Whole-line `//` comments stripped. The comments here QUOTE the defective idiom in order to
# explain why it is banned, and a grep that trips over its own rationale is the comment-
# sensitivity trap this repo has been bitten by before (see selftest_canvas_fragility).
CODE = "\n".join("" if ln.strip().startswith("//") else ln for ln in CANVAS.splitlines())

# The canvas node shape carries no delegation-shaped defaults, so a store entry authored by
# something OTHER than the canvas (the setup agent, a hand-written manifest) is the harder
# case: its delegation block may be a kind delegationFor() never emits.
CUSTOM_OBO = {"kind": "entra_obo", "tenant_id": "${AUTH_TENANT_ID}",
              "client_id": "${AUTH_CLIENT_ID}",
              "client_secret": "secret://acme/oid-a/warehouse/client_secret",
              "resource": "https://custom.database.windows.net"}


def _extract(name: str) -> str:
    """One top-level `function name(...){...}` from the canvas surface, by brace matching."""
    needle = f"function {name}("
    i = CANVAS.index(needle)
    j = CANVAS.index("{", i)
    depth = 0
    for k in range(j, len(CANVAS)):
        if CANVAS[k] == "{":
            depth += 1
        elif CANVAS[k] == "}":
            depth -= 1
            if depth == 0:
                return CANVAS[i:k + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _extract_const(name: str) -> str:
    """One `const NAME = ...;` declaration (single statement, brace/paren matched)."""
    i = CANVAS.index(f"const {name}")
    depth = 0
    for k in range(i, len(CANVAS)):
        c = CANVAS[k]
        if c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
        elif c == ";" and depth == 0:
            return CANVAS[i:k + 1]
    raise AssertionError(f"could not delimit const {name}")


def _harness(script: str) -> str:
    parts = [
        "let seq=0; let state=[];",
        _extract_const("_DELEG_RESOURCE"),
        _extract_const("_GCP_KINDS"),
        _extract_const("_AWS_KINDS"),      # #666: delegationFor's aws_keys branch reads it
        _extract_const("_ALWAYS_DELEGATED"),   # #673: entryOf reads it for s3
        _extract("delegationFor"),
        _extract("mk"),
        _extract("entryOf"),
        _extract("nodeFromEntry"),
        _extract("liveManifest"),
        _extract("layoutOf"),          # #818: liveManifest carries layout beside stores
        script,
    ]
    return "\n".join(parts)


def _run_js(script: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(_harness(script))
        path = fh.name
    try:
        out = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert out.returncode == 0, f"node failed: {out.stderr or out.stdout}"
    return json.loads(out.stdout)


def _have_node() -> bool:
    """True: drive the canvas JS. False: a skip `tests/_domgate.py` has counted. Raises without
    the opt-out (#792) - these four guards are the ONLY thing that runs the real canvas code
    here, so silently not running them leaves the whole file asserting nothing."""
    return _domgate.gate("the canvas manifest round-trip")


# --- the manifest a signed-in user's workspace holds, in entryOf's own on-the-wire shape ----
STORED = {"tenant": "acme", "stores": [
    # OBO on: `require_signin` is GONE from config and lives as a sibling delegation block
    {"id": "azure-deals", "kind": "azure_sql", "business_unit": "sales", "acl": ["oid-a"],
     "title": "azure-deals", "description": "closed deals",
     "config": {"description": "closed deals", "server": "${AZURE_SQL_SERVER}",
                "database": "${AZURE_SQL_DATABASE}", "tables": ["SalesLT.Product"]},
     "delegation": {"kind": "entra_refresh", "tenant_id": "${AUTH_TENANT_ID}",
                    "client_id": "${AUTH_CLIENT_ID}",
                    "client_secret": "${AUTH_CLIENT_SECRET}"}},
    # a GCP store delegates through Google, never Entra (#193)
    {"id": "bq-sales", "kind": "bigquery", "business_unit": "sales", "acl": ["oid-a"],
     "title": "bq-sales", "description": "sales dataset",
     "config": {"description": "sales dataset", "project": "${GCP_PROJECT}"},
     "delegation": {"kind": "google_refresh", "client_id": "${GOOGLE_CLIENT_ID}",
                    "client_secret": "${GOOGLE_CLIENT_SECRET}", "resource": "bigquery"}},
    # a delegation the canvas itself would never author - must survive VERBATIM
    {"id": "warehouse", "kind": "synapse", "business_unit": "sales", "acl": ["oid-a"],
     "title": "warehouse", "description": "warehouse facts",
     "config": {"description": "warehouse facts", "server": "${SYNAPSE_SERVER}"},
     "delegation": CUSTOM_OBO},
    # `folder`: a REAL registered provider with no KINDS row (the setup agent's main output)
    {"id": "shared-drive", "kind": "folder", "business_unit": "ops", "acl": ["oid-a"],
     "title": "shared-drive", "description": "team documents",
     "config": {"description": "team documents", "path": "/srv/docs"}, "mode": "index"},
    # no delegation at all: require_signin must NOT be conjured up
    {"id": "pg-tickets", "kind": "postgres", "business_unit": "support", "acl": ["oid-a"],
     "title": "pg-tickets", "description": "support tickets",
     "config": {"description": "support tickets", "host": "${AZURE_PG_HOST}"}},
]}


def test_manifest_survives_restore_then_recompose():
    """The whole finding in one assertion: restore the stored manifest the way loadLiveUser
    does, then build the manifest composeUp() would POST back. They must be identical - that
    POST is what overwrites the durable row."""
    if not _have_node():
        return          # permitted skip (DBSEARCH_ALLOW_DOM_SKIP), already counted
    res = _run_js("""
      const stored = %s;
      state = stored.stores.map((s,i)=>nodeFromEntry(s, 100*i, 200*i));
      state.tenant = stored.tenant;
      console.log(JSON.stringify({
        recomposed: liveManifest(),
        signinFlags: state.map(n=>n.config.require_signin || ""),
        kinds: state.map(n=>n.kind),
      }));
    """ % json.dumps(STORED))

    # #818: liveManifest now ADDS `layout` beside `stores` (a move is a mutation too). The
    # invariant this file pins is that nothing STORED is lost - so compare everything
    # except layout for identity, then hold layout to its own claim: one entry per store,
    # at the exact positions the restore placed the nodes.
    recomposed = dict(res["recomposed"])
    layout = recomposed.pop("layout", None)
    assert recomposed == STORED, (
        "the manifest composeUp() would write back is NOT what was stored:\n"
        f"  stored:     {json.dumps(STORED, sort_keys=True)}\n"
        f"  recomposed: {json.dumps(recomposed, sort_keys=True)}")
    print("  PASS  restore -> composeUp round trip is lossless for the whole manifest "
          "(delegation, kind, mode, config, acl)")

    want_layout = {s["id"]: [100 * i, 200 * i] for i, s in enumerate(STORED["stores"])}
    assert layout == want_layout, (
        f"layout must carry every node at its restored position: {layout} != {want_layout}")
    print("  PASS  layout rides the recomposed manifest, one entry per store (#818)")

    # the same state, viewed as the panel sees it: the UI must AGREE with the wiring
    assert res["signinFlags"] == ["yes", "yes", "yes", "", ""], res["signinFlags"]
    print("  PASS  a restored delegated store renders require_signin = yes, so the panel "
          "agrees with the OBO wiring instead of showing it blank")

    assert res["kinds"] == ["azure_sql", "bigquery", "synapse", "folder", "postgres"], \
        res["kinds"]
    print("  PASS  `folder` (a real provider with no KINDS row) keeps its kind instead of "
          "being rewritten to `local`")


def test_delegation_block_is_kept_verbatim_not_re_derived():
    """delegationFor(kind) would answer entra_refresh for a synapse store. A stored block may
    legitimately be entra_obo, name an explicit `resource`, or hold a secret:// handle - so the
    round trip must carry the block itself, not re-derive one from the kind."""
    if not _have_node():
        print("  SKIP  node not on PATH")
        return
    res = _run_js("""
      const entry = %s;
      const n = nodeFromEntry(entry, 0, 0);
      console.log(JSON.stringify({back: entryOf(n).delegation,
                                  canvasDefault: delegationFor("synapse")}));
    """ % json.dumps(STORED["stores"][2]))
    assert res["back"] == CUSTOM_OBO, res["back"]
    assert res["canvasDefault"]["kind"] == "entra_refresh", res["canvasDefault"]
    assert res["back"] != res["canvasDefault"], \
        "the custom delegation was replaced by the canvas default"
    print("  PASS  a custom delegation block (entra_obo + resource + secret:// handle) is "
          "preserved verbatim, not re-derived from the kind")


def test_turning_signin_off_drops_the_delegation():
    """The inverse must still work: clearing require_signin has to REMOVE the block, or a user
    could never turn OBO back off."""
    if not _have_node():
        print("  SKIP  node not on PATH")
        return
    res = _run_js("""
      const n = nodeFromEntry(%s, 0, 0);
      n.config.require_signin = "no";
      const off = entryOf(n);
      n.config.require_signin = "yes";
      const on = entryOf(n);
      console.log(JSON.stringify({off: off.delegation||null, on: on.delegation||null}));
    """ % json.dumps(STORED["stores"][0]))
    assert res["off"] is None, f"require_signin=no must drop the delegation: {res['off']}"
    assert res["on"] is not None, "and turning it back on must restore it"
    print("  PASS  require_signin=no drops the delegation, yes restores it")


def test_localstorage_cache_is_lossless_too():
    """`saveCanvas`/`restoreCanvas` is the fallback path (loadLiveUserFromLocal) and feeds the
    same composeUp(), so it must carry the delegation and mode as well."""
    save = CANVAS[CANVAS.index("function saveCanvas"):CANVAS.index("function restoreCanvas")]
    assert "delegation:n.delegation" in save.replace(" ", ""), \
        "saveCanvas drops the delegation block - the localStorage fallback would lose OBO"
    assert "mode:n.mode" in save.replace(" ", ""), "saveCanvas drops mode"
    restore = CANVAS[CANVAS.index("function restoreCanvas"):]
    restore = restore[:restore.index("\n  }")]
    assert "delegation" in restore and "mode" in restore, \
        "restoreCanvas ignores the saved delegation/mode"
    # ...and it must still hold no credential: LAW 1 (guarded by selftest_canvas_fragility too)
    assert "refresh_token" not in save and "client_secret" not in save, save
    print("  PASS  the localStorage display cache carries delegation + mode, and still no "
          "credential")


def test_no_load_path_downgrades_an_unknown_kind():
    """Source-level guard on the idiom itself: `KINDS[x]?x:"local"` must not come back on any
    path that BUILDS a node, on any of the four loaders."""
    assert 'KINDS[s.kind]?s.kind:"local"' not in CODE.replace(" ", ""), (
        'the KINDS[...]?:"local" downgrade is back on a node-building path - since #368 it '
        "rewrites the kind in the durable system of record")
    assert "function kindDef(" in CANVAS, "kindDef (the honest unknown-kind card) is gone"
    # every renderer must go through it, or an unknown kind is a TypeError on def.mono
    for anchor in ("buildNode", "renderPanel"):
        body = _extract(anchor)
        assert "kindDef(" in body, f"{anchor} still indexes KINDS directly"
        assert "KINDS[node.kind]" not in body, f"{anchor} still indexes KINDS directly"
    print("  PASS  no load path downgrades an unknown kind, and both renderers use kindDef")


if __name__ == "__main__":
    if not _have_node():
        print("  NOTE  node is not installed; the JS round-trip tests will SKIP", file=sys.stderr)
    test_manifest_survives_restore_then_recompose()
    test_delegation_block_is_kept_verbatim_not_re_derived()
    test_turning_signin_off_drops_the_delegation()
    test_localstorage_cache_is_lossless_too()
    test_no_load_path_downgrades_an_unknown_kind()
    print("OK selftest_canvas_manifest_roundtrip")
