"""Phase E E5 — federated identity broker (gate #2, card #102).
OBO token exchange (RFC 8693 shape), per-source credential isolation + caching, and the
broker's ADR-0006 precedence: delegation > row-policy fallback > principals-only.
Run: python3 tests/selftest_router_e5.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local import InMemoryIdentity  # noqa: E402
from dbsearch.router.identity_broker import (  # noqa: E402
    IdentityBroker, OboTokenExchange, StaticTokenExchange,
)


class FakePost:
    def __init__(self, expires_in=3600):
        self.calls = []
        self.expires_in = expires_in
        self.counter = 0

    def __call__(self, url, form):
        self.counter += 1
        self.calls.append({"url": url, "form": dict(form)})
        return {"access_token": f"tok-{self.counter}", "expires_in": self.expires_in}


def _obo(post, **kw):
    return OboTokenExchange(
        token_url="https://login.example/oauth2/v2.0/token",
        client_id="app-123", client_secret="s3cret",
        subject_token_provider=lambda user: "assertion-for-" + user,
        post=post, **kw)


def test_obo_posts_rfc8693_shape():
    post = FakePost()
    tok = _obo(post).exchange("alice", "https://bq.example/project")
    assert tok == "tok-1", tok
    form = post.calls[0]["form"]
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange", form
    assert form["subject_token"] == "assertion-for-alice", form
    assert form["client_id"] == "app-123" and form["client_secret"] == "s3cret", form
    assert form["resource"] == "https://bq.example/project", form
    assert form["subject_token_type"] == "urn:ietf:params:oauth:token-type:access_token", form


def test_obo_caches_until_expiry():
    post = FakePost(expires_in=3600)
    x = _obo(post)
    a1 = x.exchange("alice", "res")
    a2 = x.exchange("alice", "res")
    assert a1 == a2 and post.counter == 1, (a1, a2, post.counter)
    b = x.exchange("bob", "res")                      # different user -> new exchange
    assert b != a1 and post.counter == 2, (b, post.counter)


def test_obo_expired_token_refreshes():
    post = FakePost(expires_in=0)                     # instantly stale
    x = _obo(post, skew_s=0.0)
    x.exchange("alice", "res")
    x.exchange("alice", "res")
    assert post.counter == 2, post.counter


def test_per_store_isolation_distinct_exchanges():
    p1, p2 = FakePost(), FakePost()
    b = _broker()
    b.register_delegation("bq-sales", _obo(p1), resource="res-sales")
    b.register_delegation("bq-hr", _obo(p2), resource="res-hr")
    b.access_for("alice", "bq-sales")
    assert p1.counter == 1 and p2.counter == 0, "credential isolation per store"
    assert p1.calls[0]["form"]["resource"] == "res-sales", p1.calls


def _broker():
    return IdentityBroker(InMemoryIdentity({"alice": ["deal-team"], "bob": ["all-staff"]}))


def test_broker_delegation_precedence():
    b = _broker()
    b.register_delegation("bq", StaticTokenExchange("fixed-tok"), resource="r")
    b.register_row_policy("bq", lambda user, principals: f"owner = '{user}'")
    a = b.access_for("alice", "bq")
    assert a.delegated_credential == "fixed-tok", a
    assert a.row_policy is None, "delegation wins over row policy (ADR 0006)"
    assert "deal-team" in a.principals, a.principals   # oid itself is included by identity


def test_broker_row_policy_fallback():
    b = _broker()
    b.register_row_policy("legacy-db", lambda user, principals: f"owner = '{user}'")
    a = b.access_for("bob", "legacy-db")
    assert a.delegated_credential is None, a
    assert a.row_policy == "owner = 'bob'", a.row_policy


def test_broker_principals_only_default():
    a = _broker().access_for("bob", "plain-index")
    assert a.delegated_credential is None and a.row_policy is None, a
    assert "all-staff" in a.principals, a.principals


def main():
    print("Phase E E5 identity-broker self-test:")
    test_obo_posts_rfc8693_shape()
    test_obo_caches_until_expiry()
    test_obo_expired_token_refreshes()
    test_per_store_isolation_distinct_exchanges()
    test_broker_delegation_precedence()
    test_broker_row_policy_fallback()
    test_broker_principals_only_default()
    print("  PASS  rfc8693 shape / cache / expiry refresh / per-store isolation / "
          "precedence / row-policy fallback / principals default")
    print("\nE5 IDENTITY-BROKER SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
