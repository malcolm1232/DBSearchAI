"""#336 - the boundary is a TEST, not a convention.

Walks every /router endpoint body in router_api.py and FAILS if it touches a scope
internal directly. Those names are legal only inside scope.py / the scope builder,
so the next endpoint someone adds inherits the demo/live boundary instead of having
to remember it (#340 happened because /catalog and /rerun each hand-picked their
collaborators and picked one from each world).

    PYTHONPATH=src python3 tests/selftest_scope_enforcement.py
"""
from __future__ import annotations

import ast
from pathlib import Path

ROUTER_API = Path(__file__).resolve().parents[1] / "src/dbsearch/server/router_api.py"

# Endpoint bodies (and any helper closure they delegate to, see _endpoint_functions
# below) may not reference these. edition.identity is THE #340 culprit; the
# hand-branching helpers are what this card removes; `_service(`/`_demo_catalog(`
# are the live/demo closures a scope-consuming endpoint must never call directly
# (#336 final review finding 1b - a mispairing living one call frame down).
FORBIDDEN = ("edition.identity", "DEMO_PREFIX", "_is_demo", "_principal(",
             "_read_catalog", "_read_service", "state.catalog", "demo_chat_llm",
             "_service(", "_demo_catalog(")

# These helpers must not exist ANYWHERE in the file once the scope seam owns them.
DELETED = ("_is_demo", "_read_catalog", "_read_service")

_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _sibling_scopes(tree: ast.AST) -> dict:
    """Group every function def (sync or async) by its immediately enclosing
    function - None for module level - the way Python itself resolves a bare name:
    only among functions defined in the SAME enclosing scope. A module-level helper
    (e.g. _compose_manifest(state, ...), which takes its collaborators as
    PARAMETERS and is deliberately shared by the demo and live compose paths) lives
    in a different scope and is never pulled into an endpoint's reachable text by
    this grouping."""
    groups: dict = {}

    def walk(node, enclosing):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNC_TYPES):
                groups.setdefault(enclosing, {})[child.name] = child
                walk(child, child)
            else:
                walk(child, enclosing)

    walk(tree, None)
    return groups


def _call_names(node: ast.AST) -> set:
    return {c.func.id for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}


def _is_endpoint(node) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if (isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "api"):
            return True
    return False


def _endpoint_functions(source: str):
    """Yield (name, combined_source) for every `@api.xxx` endpoint - FunctionDef OR
    AsyncFunctionDef (#336 final review finding 1a: `async def` endpoints were
    invisible to a walker that matched only ast.FunctionDef) - where combined_source
    is the endpoint's own body PLUS the source of every helper closure it reaches by
    direct call, one or more frames down, restricted to closures defined in the same
    enclosing scope (typically build_router_api). A forbidden token living in a
    helper the endpoint delegates to is exactly as visible as one written directly
    in the endpoint body (#336 final review finding 1b)."""
    tree = ast.parse(source)
    for siblings in _sibling_scopes(tree).values():
        for node in siblings.values():
            if not _is_endpoint(node):
                continue
            seen = {node.name}
            frontier = [node]
            texts = [ast.get_source_segment(source, node) or ""]
            while frontier:
                current = frontier.pop()
                for name in _call_names(current) & siblings.keys():
                    if name in seen:
                        continue
                    seen.add(name)
                    callee = siblings[name]
                    texts.append(ast.get_source_segment(source, callee) or "")
                    frontier.append(callee)
            yield node.name, "\n".join(texts)


def test_endpoints_never_touch_scope_internals():
    source = ROUTER_API.read_text()
    endpoints = list(_endpoint_functions(source))
    # Not pinned to today's exact endpoint count (brittle) - just that the walker
    # actually matched something. An empty list here means the AST walk itself is
    # broken (e.g. a decorator/attribute-name change), which would otherwise let
    # every other assertion in this test pass vacuously.
    assert endpoints, "the walker matched no /router endpoints - check the AST walk"
    offenders = [(name, tok) for name, seg in endpoints for tok in FORBIDDEN if tok in seg]
    assert not offenders, (
        "endpoint bodies reference scope internals (use the injected RequestScope): "
        f"{offenders}")
    print(f"  PASS  {len(endpoints)} endpoint bodies are scope-clean")


def test_scope_helpers_are_gone_from_router_api():
    source = ROUTER_API.read_text()
    leftovers = [name for name in DELETED if name in source]
    assert not leftovers, f"hand-branching helpers still defined/used: {leftovers}"
    print("  PASS  _is_demo/_read_catalog/_read_service no longer exist in router_api")


def test_async_endpoint_leaking_scope_internals_is_caught():
    """#336 final review finding 1a. `_endpoint_functions` matched only
    `ast.FunctionDef` - `ast.AsyncFunctionDef` is NOT a subclass, so an `async def`
    endpoint was entirely invisible to the walker. Pin the reviewer's exact
    reproduction: appended to a copy of router_api.py, this printed PASS."""
    source = '''
from fastapi import APIRouter, Depends

api = APIRouter()

@api.get("/leak")
async def leak(user=Depends(current_user_demo_ok)):
    return state.catalog.visible_tree(edition.identity.expand_groups(user))
'''
    endpoints = list(_endpoint_functions(source))
    assert endpoints, "an async def endpoint must be visible to the walker"
    offenders = [(name, tok) for name, seg in endpoints for tok in FORBIDDEN if tok in seg]
    assert offenders, ("an async endpoint touching scope internals (state.catalog, "
                       "edition.identity) must be CAUGHT, not invisible to the scan")
    print("  PASS  an async def endpoint touching scope internals is caught (finding 1a)")


def test_endpoint_delegating_to_a_helper_that_touches_live_state_is_caught():
    """#336 final review finding 1b. FORBIDDEN banned `_read_service`/`state.catalog`
    but not the live closures `_service`/`_demo_catalog`, and only the decorated
    endpoint's OWN body was scanned - so a mispairing living one call frame down, in
    a helper the endpoint delegates to, was invisible. `export` here never mentions
    `_service` itself; it delegates to `_leaky_helper`, which does."""
    source = '''
def build_router_api(edition, current_user, current_user_demo_ok=None):
    api = APIRouter()

    def _service():
        return state.service

    def _leaky_helper(user):
        return _service().ask(user, "x")

    @api.post("/export")
    def export(user=Depends(current_user_demo_ok)):
        return _leaky_helper(user)

    return api
'''
    endpoints = list(_endpoint_functions(source))
    exports = [seg for name, seg in endpoints if name == "export"]
    assert exports, "the export endpoint must be visible to the walker"
    offenders = [tok for tok in FORBIDDEN if tok in exports[0]]
    assert offenders, ("export() delegates to _leaky_helper(), which calls the live "
                       "_service() closure directly - one call frame down must be as "
                       "visible as the endpoint body itself")
    print("  PASS  a helper closure one call frame down that touches live state is "
          "caught (finding 1b)")


def test_endpoint_calling_service_directly_is_caught():
    """The direct-in-body form of finding 1b, exactly as the reviewer wrote it."""
    source = '''
def build_router_api(edition, current_user, current_user_demo_ok=None):
    api = APIRouter()

    def _service():
        return state.service

    @api.post("/export")
    def export(user=Depends(current_user_demo_ok)):
        return _service().ask(user, "x")

    return api
'''
    endpoints = list(_endpoint_functions(source))
    exports = [seg for name, seg in endpoints if name == "export"]
    assert exports, "the export endpoint must be visible to the walker"
    offenders = [tok for tok in FORBIDDEN if tok in exports[0]]
    assert offenders, "export() calling _service() directly must be caught"
    print("  PASS  an endpoint calling _service() directly is caught (finding 1b, direct form)")


def main():
    print("Scope boundary enforcement (#336) self-test:")
    test_endpoints_never_touch_scope_internals()
    test_scope_helpers_are_gone_from_router_api()
    test_async_endpoint_leaking_scope_internals_is_caught()
    test_endpoint_delegating_to_a_helper_that_touches_live_state_is_caught()
    test_endpoint_calling_service_directly_is_caught()
    print("\nSCOPE ENFORCEMENT SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
