"""#805 - compose must never leave the LIVE broker without its delegations.

`_compose_manifest` used to call `state.broker.reset()` and then re-register the new
manifest's delegation blocks onto the same live object. Two defects hide in that gap:

  1. THE WINDOW. A concurrent ask between reset and re-register sees a broker with no
     delegations at all: `access_for` silently falls through to the principals-only path,
     which for a delegated store means querying with the wrong identity - the LAW 2 shape,
     open for a moment on every warm recompose.
  2. STRIPPED ON FAILURE. A compose that fails inside register_delegations (one bad
     delegation block -> 400) has already reset the live broker: the workspace keeps
     serving its OLD manifest, but every delegation that manifest registered is gone.

The fix registers into a STAGING broker and adopts it atomically on success, so the live
broker always holds either the previous manifest's delegations or the new ones - never
nothing. Both tests drive the real /router/compose endpoint.

    PYTHONPATH=src python3 tests/selftest_805_compose_broker_window.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
os.environ["DBSEARCH_RATE_LIMIT"] = "0"
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

import dbsearch.router as r  # noqa: E402
from dbsearch.server import app as appmod  # noqa: E402
from dbsearch.server.workspaces import SHARED_KEY  # noqa: E402

client = TestClient(appmod.app)
ALICE = {"X-DBSearch-User": "alice"}


def _live_broker():
    state = appmod._router_api._workspace_pool.get_if_warm(SHARED_KEY)
    assert state is not None, "no warm workspace - compose first"
    return state.broker


def _delegated_manifest():
    demo = client.get("/router/demo", headers=ALICE).json()["manifest"]
    for s in demo["stores"]:
        if s["kind"] == "csv":
            s["delegation"] = {"kind": "static", "tokens": {"alice": "${DEV_STATIC_TOKEN}"}}
    return demo


def test_a_failed_recompose_does_not_strip_the_live_delegations():
    os.environ["DEV_STATIC_TOKEN"] = "dev-tok"
    try:
        good = _delegated_manifest()
        assert client.post("/router/compose", headers=ALICE,
                           json={"manifest": good}).status_code == 200
        broker = _live_broker()
        assert broker._delegations, "fixture failed: the good compose registered nothing"
        before = dict(broker._delegations)

        bad = dict(good)
        bad["stores"] = [dict(good["stores"][0], delegation={"kind": "wat"})]
        r2 = client.post("/router/compose", headers=ALICE, json={"manifest": bad})
        assert r2.status_code == 400, r2.text

        after = _live_broker()._delegations
        assert after == before, (
            f"a FAILED compose stripped the live broker: had {list(before)}, "
            f"now {list(after)} - the workspace still serves the old manifest, "
            "but its delegations are gone")
    finally:
        del os.environ["DEV_STATIC_TOKEN"]
        client.post("/router/compose", headers=ALICE,
                    json={"manifest": client.get("/router/demo",
                                                 headers=ALICE).json()["manifest"]})


def test_the_live_broker_is_never_empty_during_registration():
    """The window, made deterministic: while the new manifest's delegations are being
    registered, a probe plays the concurrent ask and reads the LIVE broker. Pre-fix,
    reset() has already run at that moment and the probe sees nothing."""
    os.environ["DEV_STATIC_TOKEN"] = "dev-tok"
    seen = {}
    real = r.register_delegations

    def probing(spec, broker, provider, on_rotate=None, secrets=None):
        seen["mid_registration"] = dict(_live_broker()._delegations)
        return real(spec, broker, provider, on_rotate=on_rotate, secrets=secrets)

    try:
        assert client.post("/router/compose", headers=ALICE,
                           json={"manifest": _delegated_manifest()}).status_code == 200
        r.register_delegations = probing
        assert client.post("/router/compose", headers=ALICE,
                           json={"manifest": _delegated_manifest()}).status_code == 200
        assert seen.get("mid_registration"), (
            "the concurrent-ask window is open: mid-registration the LIVE broker held "
            f"{seen.get('mid_registration')} - a delegated store asked in this moment "
            "would fall through to the principals-only path")
    finally:
        r.register_delegations = real
        del os.environ["DEV_STATIC_TOKEN"]
        client.post("/router/compose", headers=ALICE,
                    json={"manifest": client.get("/router/demo",
                                                 headers=ALICE).json()["manifest"]})


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
