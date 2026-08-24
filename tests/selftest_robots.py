"""robots.txt policy self-test (#334): the app must SERVE a robots policy, and it must
default to disallow.

Why this exists. Before #334 the FastAPI app had no /robots.txt route at all, so the
deployed box answered 404. A 404 is not neutral - crawlers read a missing robots.txt as
"crawl everything", so the site would have been indexed the moment the basic-auth gate
came off. Indexing is the one step in going public that is not cleanly reversible: pages
stay cached and surfaced long after a later disallow.

So the default is DISALLOW, chosen at the default rather than at the deploy: a fresh box
that nobody remembered to configure must fail closed, not fail indexed. Opening up is an
explicit env flip (DBSEARCH_ROBOTS=allow), which is a prod.env line plus a restart, not a
code change and a rebuild.

    python3 tests/selftest_robots.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)


def _get(policy=None):
    """Fetch /robots.txt under a given DBSEARCH_ROBOTS value (None = unset)."""
    prev = os.environ.pop("DBSEARCH_ROBOTS", None)
    if policy is not None:
        os.environ["DBSEARCH_ROBOTS"] = policy
    try:
        return client.get("/robots.txt")
    finally:
        os.environ.pop("DBSEARCH_ROBOTS", None)
        if prev is not None:
            os.environ["DBSEARCH_ROBOTS"] = prev


def main():
    print("robots.txt policy self-test (#334):")

    # --- the route exists at all: a 404 here IS the bug this card is about ---
    r = _get()
    assert r.status_code == 200, f"GET /robots.txt -> {r.status_code} (404 means crawl-everything)"
    assert "text/plain" in r.headers["content-type"], r.headers["content-type"]
    print("  PASS  /robots.txt is served (not 404)")

    # --- unset env must fail CLOSED ---
    body = r.text
    assert "User-agent: *" in body, body
    assert "Disallow: /" in body, f"default policy is not disallow:\n{body}"
    assert "Allow: /" not in body, f"default policy leaks an allow rule:\n{body}"
    print("  PASS  default (env unset) disallows every crawler")

    # --- explicit disallow is the same ---
    body = _get("disallow").text
    assert "Disallow: /" in body and "Allow: /" not in body, body
    print("  PASS  DBSEARCH_ROBOTS=disallow disallows")

    # --- opening up is an explicit flip, and it really opens ---
    r = _get("allow")
    assert r.status_code == 200, r.status_code
    body = r.text
    assert "User-agent: *" in body, body
    assert "Allow: /" in body, body
    assert "Disallow: /" not in body, f"allow policy still blocks the site:\n{body}"
    print("  PASS  DBSEARCH_ROBOTS=allow opens the site to crawlers")

    # --- an unrecognized value must NOT silently mean "allow" ---
    body = _get("yes-please").text
    assert "Disallow: /" in body, f"unknown policy did not fail closed:\n{body}"
    print("  PASS  an unknown DBSEARCH_ROBOTS value fails closed to disallow")

    # --- the policy must be flippable without a stale cached copy pinning it ---
    cache = _get().headers.get("cache-control", "")
    assert "no-cache" in cache or "max-age=0" in cache, f"cache-control: {cache!r}"
    print("  PASS  robots.txt is revalidated, so a policy flip is not pinned by a cache")

    # --- robots.txt must never be throttled: it is cheap, and a 429 reads as "no rules" ---
    from dbsearch.server.rate_limit import COSTLY_PREFIXES
    assert not any("/robots.txt".startswith(p) for p in COSTLY_PREFIXES), COSTLY_PREFIXES
    print("  PASS  /robots.txt is not a rate-limited path")

    print("\nAll robots.txt policy checks passed.")


if __name__ == "__main__":
    main()
