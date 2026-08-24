"""Self-test: /ask/suggestions never offers a question the corpus cannot answer (#392).

The bug this locks down: the Ask surface shipped two hardcoded example chips naming the
DEMO SEED's two documents. On prod the seed is off and the index holds zero rows, so the
likeliest first click a new user made was guaranteed to return nothing - and the generic
"I couldn't find anything you have access to" made an EMPTY INDEX look like a PERMISSIONS
REFUSAL. The operator hit exactly that and reasonably concluded the product was broken.

Both halves are asserted here, because both can regress independently:
  1. examples exist only when the corpus that answers them exists;
  2. the two "nothing to show" states stay DISTINGUISHABLE (empty index vs nothing you may
     see), since collapsing them is what made an empty corpus read as a permissions fault.

    python3 tests/selftest_ask_suggestions.py
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _run(seed: bool) -> dict:
    """Import the app in a FRESH process per seed setting: _seed_demo() runs at build time,
    so the flag cannot be flipped after import (the same reason selftest_demo_seed sets it
    before importing)."""
    code = r"""
import json, os, sys
sys.path.insert(0, %r)
from fastapi.testclient import TestClient
from dbsearch.server.app import app
c = TestClient(app)
out = {"anon": c.get("/ask/suggestions").status_code}
for u in ("alice", "bob"):
    r = c.get("/ask/suggestions", headers={"X-DBSearch-User": u})
    out[u] = {"status": r.status_code, "body": r.json() if r.status_code == 200 else None}
print(json.dumps(out))
""" % str(SRC)
    env = dict(os.environ, SELFHOST_BACKEND="memory", DBSEARCH_DEV_AUTH="1")
    if seed:
        env["DBSEARCH_DEMO_SEED"] = "1"
    else:
        env.pop("DBSEARCH_DEMO_SEED", None)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert p.returncode == 0, f"subprocess failed:\n{p.stderr[-2000:]}"
    import json
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_unauthenticated_is_refused():
    """Not demo-safe and not public: it reports a per-caller count, so it needs a caller."""
    for seed in (False, True):
        assert _run(seed)["anon"] == 401, "/ask/suggestions must require an identity"
    print("  PASS  /ask/suggestions requires an identity (401 anonymous)")


def test_no_seed_means_no_examples():
    """THE REGRESSION. With the seed off there is no corpus that can answer the demo
    prompts, so the surface must offer none - this is the prod configuration."""
    r = _run(seed=False)["alice"]["body"]
    assert r["examples"] == [], \
        f"examples offered with NO corpus to answer them (#392): {r['examples']}"
    assert r["known"] is True, "memory backend can count; 'known' should be True"
    assert r["indexed"] is False, "an unseeded memory index should report indexed=False"
    assert r["authorized_docs"] == 0
    print("  PASS  seed off -> zero examples, indexed=False (the prod shape that broke)")


def test_seed_on_offers_examples_and_they_are_answerable():
    """With the seed on the prompts are true, so they may be offered."""
    r = _run(seed=True)["alice"]["body"]
    assert r["examples"], "the seeded corpus should offer its example prompts"
    assert r["indexed"] is True and r["authorized_docs"] == 2, r
    # The prompts must come from the module that owns the seed, never a UI-side copy.
    sys.path.insert(0, str(SRC))
    from dbsearch.server import edition as edition_mod
    assert list(edition_mod.DEMO_EXAMPLE_PROMPTS) == r["examples"], \
        "the served examples drifted from the seed's own prompt list"
    print("  PASS  seed on -> examples served, sourced from edition.DEMO_EXAMPLE_PROMPTS")


def test_authorized_count_is_permission_faithful():
    """LAW 2: the count is per-caller, not a global total. bob is all-staff, alice is
    deal-team, so a count that ignored the ACL would report the same number for both and
    promise bob a document he can never retrieve."""
    seeded = _run(seed=True)
    alice, bob = seeded["alice"]["body"], seeded["bob"]["body"]
    assert alice["authorized_docs"] == 2, alice
    assert bob["authorized_docs"] == 1, \
        f"LAW 2: bob must not be counted for the deal-team doc: {bob}"
    assert alice["authorized_docs"] != bob["authorized_docs"], \
        "the count is not ACL-trimmed - it reports the same total to every caller"
    print("  PASS  authorized_docs is ACL-trimmed per caller (alice 2, bob 1)")


def test_ui_no_longer_hardcodes_prompts():
    """The root cause was the prompt strings living in the UI, where they outlived the
    corpus. If they come back, this fails no matter how correct the endpoint is."""
    ask_js = (SRC / "dbsearch/server/static/js/surfaces/ask.js").read_text()
    assert "holiday and expenses" not in ask_js, \
        "ask.js hardcodes a demo-seed prompt again (#392) - it must come from /ask/suggestions"
    assert "Project Falcon" not in ask_js, "ask.js hardcodes a demo-seed prompt again (#392)"
    assert "askSuggestions" in ask_js, "ask.js no longer asks the server what it may offer"
    # The two empty states must stay DISTINCT - collapsing them recreates the original bug.
    assert "No documents have been indexed yet" in ask_js, "the empty-index state is gone (#392)"
    assert "No documents you are permitted to see" in ask_js, \
        "the no-authorized-docs state is gone - an empty index and a permissions wall now read alike"
    assert "—" not in ask_js, "ask.js contains an em dash (house style)"
    print("  PASS  ask.js sources prompts from the server and keeps both empty states distinct")


def main():
    print("Ask suggestions / empty-corpus honesty (#392) self-test:")
    test_unauthenticated_is_refused()
    test_no_seed_means_no_examples()
    test_seed_on_offers_examples_and_they_are_answerable()
    test_authorized_count_is_permission_faithful()
    test_ui_no_longer_hardcodes_prompts()
    print("\nASK SUGGESTIONS SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
