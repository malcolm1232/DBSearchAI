"""Who operates THIS deployment (ADR 0011 s3).

One definition, two consumers: `/config` decides which affordances to advertise, and the
compose gate decides which manifest powers a caller may actually exercise. They must agree
- an affordance the UI hides but the API still honors is not a gate, it is a hint - so the
question lives here rather than in either caller.

`DBSEARCH_OPERATOR_OIDS` is read per call, not cached at import: tests mutate the env, and
a restart-free rotation of the list is a feature. The list itself is never serialized into
any response.
"""
from __future__ import annotations

import os

from dbsearch.api.auth import real_login_enabled


def operator_oids() -> frozenset:
    """The configured operator oids, lower-cased. Entra oids are GUIDs, which are
    case-insensitive: the same operator pasted from the portal in one case and from the CLI
    in another must not silently stop being the operator."""
    return frozenset(o.strip().lower() for o in
                     os.environ.get("DBSEARCH_OPERATOR_OIDS", "").split(",") if o.strip())


def is_operator(oid: str) -> bool:
    """True when `oid` may use operator affordances on this deployment.

    A deployment with NO real login configured is somebody's own machine - a dev rig or a
    self-host box where whoever runs the server also sets its environment - so every caller
    there is the operator, and the gates below are no-ops. That is what dissolved the #317
    burn: the local rig legitimately has both a real login and operator-provisioned vars,
    and gating on "has a login" broke it.

    Under a real login, only a listed oid qualifies. An empty list means nobody does, which
    is the safe direction: the operator adds their own oid and gets their affordances back.
    """
    if not real_login_enabled():
        return True
    return bool(oid) and oid.strip().lower() in operator_oids()
