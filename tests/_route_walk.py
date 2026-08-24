"""Walk a FastAPI app's REAL route table, on any FastAPI this project supports (#696).

Import this from any test that needs to ask "what routes exist?" or "what does this route
depend on?". Do not re-implement the walk locally - that is what caused #696.

WHY THIS EXISTS. FastAPI made `include_router()` lazy at or below 0.139.2: `app.routes` stops
holding the child router's `APIRoute` objects and holds a `fastapi.routing._IncludedRouter`
wrapper instead, which resolves them on demand. A flat walk over `app.routes` reading
`route.path` therefore MISSES every route registered via `include_router()` - on 0.140.13 that
is all 14 `/router/*` routes, `/secrets/*`, the `/c/{token}` anonymous-link family, and
`/graphql`.

The routes themselves are fine on those versions (TestClient returns 200 for `/router/kinds`,
`/router/manifest` and `/graphql`). What breaks is INTROSPECTION - which matters because the
default-deny sweep in `selftest_demo_scope_boundary.py` is introspection: a sweep that cannot
see a route cannot report that the route is unauthenticated. It does not go red for the thing
it stopped checking; it simply checks less.

An unwrap for this already existed - inside that one sweep - and the two other route-walking
tests never got it, so they went red instead. A fix that lives in one caller cannot protect the
next caller who needs it, which is the whole reason this is a module and not a local helper.

INHERITED DEPENDENCIES ARE PART OF THE ANSWER. `include_router(..., dependencies=[...])`
attaches its dependencies to the WRAPPER, not to the child routes, so unwrapping alone loses
them. `/graphql` is included with `dependencies=[Depends(current_user)]` exactly that way:
recover the child routes without their include-level dependencies and the sweep concludes
`/graphql` has no auth - a false DEFAULT-DENY VIOLATION against correct code. So the walk
carries them down and `route_dependencies` counts them.

On FastAPI versions without lazy includes no route has an `original_router` attribute, so this
degrades exactly to the old flat walk with an empty prefix and no inherited dependencies -
identical behaviour there.
"""


def iter_routes(routes, prefix="", inherited=()):
    """Yield `(route, full_path, inherited_dependencies)` for every REAL route.

    `inherited_dependencies` is the tuple of `Depends(...)` markers attached by any
    enclosing `include_router()` call, outermost first.
    """
    for route in routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            ctx = getattr(route, "include_context", None)
            inc_prefix = getattr(ctx, "prefix", "") or ""
            inc_deps = tuple(getattr(ctx, "dependencies", None) or ())
            yield from iter_routes(original_router.routes, prefix + inc_prefix,
                                   tuple(inherited) + inc_deps)
        else:
            path = getattr(route, "path", None)
            yield route, (prefix + path if path is not None else path), tuple(inherited)


def route_paths(routes):
    """Every real route path, as a set. The flat `{r.path for r in app.routes}` replacement."""
    return {full for _r, full, _d in iter_routes(routes) if full is not None}


def route_dependencies(route, inherited=()):
    """Every dependency callable that guards `route`, including inherited ones.

    Three sources, because any one alone under-reports: the endpoint signature's own
    `Depends(...)` defaults, the resolved `route.dependant` tree (router-level plus
    signature-level, nested), and the include-level dependencies handed down by
    `iter_routes`.
    """
    import inspect

    import fastapi

    deps = []
    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        try:
            sig = inspect.signature(endpoint)
        except (TypeError, ValueError):
            sig = None
        if sig is not None:
            deps += [p.default.dependency for p in sig.parameters.values()
                     if isinstance(p.default, fastapi.params.Depends)]

    stack = list(getattr(getattr(route, "dependant", None), "dependencies", []) or [])
    while stack:
        d = stack.pop()
        if getattr(d, "call", None) is not None:
            deps.append(d.call)
        stack.extend(getattr(d, "dependencies", []) or [])

    for d in inherited or ():
        call = getattr(d, "dependency", None)
        if call is not None:
            deps.append(call)
    return deps
