"""#336 - the demo/live scope seam (ADR 0009, hardened after #340).

`RequestScope` bundles EVERYTHING that differs between the demo world and the live
world - principal, identity provider, catalog, service, chat model - resolved at ONE
seam directly above `resolve_identity` (the #184 chokepoint) and injected into the
demo-safe read endpoints. An endpoint that consumes a scope can never pair a demo
catalog with the live identity provider (#340: /router/catalog and /router/rerun did
exactly that, so a demo visitor could query stores they could not enumerate or rerun).
The catalog/service members are lazy callables: building a scope never composes the
demo catalog and never 409s (a live user with nothing composed must still be able to
GET /router/demo).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from dbsearch.adapters.local import InMemoryIdentity
from dbsearch.api.auth import DEMO_PREFIX


@dataclass(frozen=True)
class RequestScope:
    kind: str            # "demo" | "live"
    user: str            # the NAMESPACED identity exactly as resolved (binds rerun tokens)
    principal: str       # the bare principal used for authorization
    identity: object     # IdentityPort for THIS scope
    chat_llm: object
    catalog_fn: Callable[[], object] = field(repr=False)
    service_fn: Callable[[], object] = field(repr=False)

    def catalog(self):
        return self.catalog_fn()

    def service(self):
        return self.service_fn()

    def groups(self) -> list:
        return self.identity.expand_groups(self.principal)


def make_scope_builder(*, edition, demo_catalog, live_catalog, live_service,
                       demo_user_groups: dict) -> Callable[[str], RequestScope]:
    """The ONLY place `DEMO_PREFIX` is inspected and de-namespaced (the #184
    discipline, extended from who-is-calling to which-world-they-live-in).
    `demo_catalog` is a lazy zero-arg callable. `live_catalog`/`live_service` take
    the resolved user (#368 - the live world is per-workspace now)."""
    demo_identity = InMemoryIdentity({k: list(v) for k, v in demo_user_groups.items()})

    def build(user: str) -> RequestScope:
        if user.startswith(DEMO_PREFIX):
            return RequestScope(
                kind="demo", user=user, principal=user[len(DEMO_PREFIX):],
                identity=demo_identity, chat_llm=edition.demo_chat_llm,
                catalog_fn=lambda: demo_catalog().catalog,
                service_fn=lambda: demo_catalog().service)
        return RequestScope(
            kind="live", user=user, principal=user,
            identity=edition.identity,
            chat_llm=edition.chat_models[edition.chat_model_default],
            catalog_fn=lambda: live_catalog(user), service_fn=lambda: live_service(user))

    return build
