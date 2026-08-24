"""#319 (ADR 0010 s5): handles are tenant- and owner-scoped, resolution is default-deny.

The property that matters: a handle leaked into ANOTHER tenant's manifest must resolve to
nothing rather than to a credential. That keeps LAW 5 (isolation) at the credential layer,
not only at the retrieval layer - so a stolen manifest is inert.

    PYTHONPATH=src python3 tests/selftest_secret_handles.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.router.secret_handles import (  # noqa: E402
    SECRET_PREFIX, ScopedSecretResolver, format_handle, parse_handle,
)


class FakeSecrets:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.reads = []

    def get_secret(self, name):
        self.reads.append(name)
        return self.data.get(name, "")


ALICE, BOB = "oid-alice", "oid-bob"
H = format_handle("acme", ALICE, "sales-db", "password")


def test_handle_round_trips():
    assert H == "secret://acme/" + ALICE + "/sales-db/password", H
    p = parse_handle(H)
    assert p == {"tenant": "acme", "owner": ALICE, "store": "sales-db", "field": "password"}, p
    print("  PASS  handle formats and parses per ADR 0010 s5")


def test_malformed_handles_parse_to_none_never_to_a_partial():
    for bad in ("secret://acme/only-three/parts", "secret://", "secret:///a/b/c",
                "not-a-handle", "secret://a/b/c/d/e", "${ENV_NAME}", ""):
        assert parse_handle(bad) is None, f"{bad!r} should not parse"
    print("  PASS  malformed handles parse to None, never to a partial scope")


def test_adversarial_shapes_that_could_smuggle_a_partial_or_shifted_scope():
    """Beyond the brief's malformed set: shapes that could plausibly slip through a
    less-careful splitter and produce a WRONG scope rather than a clean refusal - a
    field that swallows extra segments, an owner that silently becomes empty, or a
    non-string value that crashes instead of failing closed."""
    for bad in (
        # double slash -> an empty owner segment, not "no owner"
        "secret://acme//sales-db/password",
        # trailing slash -> a phantom empty 5th part
        "secret://acme/" + ALICE + "/sales-db/password/",
        # a slash smuggled into what looks like a single field via encoding is out of
        # scope for the parser (it never decodes), but a raw extra "/" before the
        # prefix must not be swallowed into the tenant
        "/secret://acme/" + ALICE + "/sales-db/password",
        # repeated prefix must not collapse to a valid 4-part parse
        "secret://secret://acme/" + ALICE + "/sales-db/password",
        # case must not be normalized away (fail closed, not a case-insensitive match)
        "SECRET://acme/" + ALICE + "/sales-db/password",
        # empty store segment sandwiched between real ones
        "secret://acme/" + ALICE + "//password",
        # empty field at the end (distinct from a missing 4th part entirely)
        "secret://acme/" + ALICE + "/sales-db/",
    ):
        assert parse_handle(bad) is None, f"{bad!r} should not parse"
    # non-string inputs must fail closed, not raise from inside the parser
    for bad in (None, 123, b"secret://acme/" + ALICE.encode() + b"/sales-db/password", [], {}):
        assert parse_handle(bad) is None, f"{bad!r} should not parse"
    print("  PASS  adversarial near-miss shapes still parse to None, not a partial scope")


def test_the_owner_resolves_their_own_secret():
    s = FakeSecrets({"acme/" + ALICE + "/sales-db/password": "hunter2"})
    assert ScopedSecretResolver(s, "acme", ALICE).resolve(H) == "hunter2"
    print("  PASS  the owning identity in the owning tenant resolves the value")


def test_another_owner_in_the_same_tenant_is_refused_without_touching_the_store():
    s = FakeSecrets({"acme/" + ALICE + "/sales-db/password": "hunter2"})
    try:
        ScopedSecretResolver(s, "acme", BOB).resolve(H)
    except PermissionError:
        pass
    else:
        raise AssertionError("bob resolved alice's handle")
    assert s.reads == [], f"refusal must happen BEFORE any read: {s.reads}"
    print("  PASS  a different owner is refused, and the secret store is never even read")


def test_another_tenant_is_refused():
    s = FakeSecrets({"acme/" + ALICE + "/sales-db/password": "hunter2"})
    try:
        ScopedSecretResolver(s, "evilcorp", ALICE).resolve(H)
    except PermissionError:
        pass
    else:
        raise AssertionError("a foreign tenant resolved the handle (LAW 5 breach)")
    assert s.reads == []
    print("  PASS  a foreign tenant is refused (LAW 5 holds at the credential layer)")


def test_unicode_confusable_separator_parses_to_none_even_after_nfkc_normalization():
    """U+FF0F (fullwidth solidus) is not ASCII '/', so it is accepted as literal segment
    content today - but unicodedata.normalize("NFKC", ...) turns it into a real '/'. This
    module must not depend on "no caller ever normalizes": a web framework, JSON/YAML
    loader, or logging pipeline could. So the raw form must be rejected (it is not in the
    safe charset), AND normalizing it must not produce a DIFFERENTLY-SCOPED handle that
    parses cleanly - it must still parse to None (too many parts, or an empty part)."""
    import unicodedata
    confusable = "secret://acme" + "／" + "evil/" + ALICE + "/sales-db/password"
    assert parse_handle(confusable) is None, "confusable separator must not parse"
    normalized = unicodedata.normalize("NFKC", confusable)
    assert normalized != confusable, "the fixture must actually contain a normalizable char"
    assert parse_handle(normalized) is None, (
        "NFKC-normalized confusable must still parse to None, not to a clean, "
        "differently-scoped 4-part handle")
    print("  PASS  a confusable separator is rejected both as sent and after NFKC normalization")


def test_control_chars_whitespace_and_percent_encoding_in_segments_parse_to_none():
    for bad_segment in ("evil\n", "evil\x00", " evil", "evil ", "evil%2Fother"):
        handle = f"secret://acme/{ALICE}/sales-db/{bad_segment}"
        assert parse_handle(handle) is None, f"segment {bad_segment!r} should not parse"
    print("  PASS  control chars, whitespace, and %2F escapes in a segment parse to None")


def test_overlength_segment_parses_to_none():
    handle = f"secret://acme/{ALICE}/sales-db/{'f' * 200}"
    assert parse_handle(handle) is None, "a 200-char segment should not parse"
    print("  PASS  an over-length segment parses to None")


def test_format_handle_raises_for_the_same_unsafe_shapes():
    unsafe_fields = (
        "evil\n", "evil\x00", " evil", "evil ", "evil%2Fother", "f" * 200,
        "acme" + "／" + "evil",
    )
    for bad in unsafe_fields:
        try:
            format_handle("acme", ALICE, "sales-db", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"format_handle accepted unsafe field {bad!r}")
    print("  PASS  format_handle raises ValueError for every unsafe segment shape")


def test_real_values_still_round_trip_after_the_charset_restriction():
    """Regression guard: the fix must not reject values this system actually uses."""
    real = format_handle(
        "acme-demo", "82d85111-cacc-46fa-b02d-465b437aa224", "support-tickets", "password")
    assert real == (
        "secret://acme-demo/82d85111-cacc-46fa-b02d-465b437aa224/"
        "support-tickets/password"), real
    p = parse_handle(real)
    assert p == {
        "tenant": "acme-demo",
        "owner": "82d85111-cacc-46fa-b02d-465b437aa224",
        "store": "support-tickets",
        "field": "password",
    }, p
    print("  PASS  real tenant/owner/store/field values still format and parse cleanly")


def test_a_missing_secret_raises_rather_than_returning_empty():
    """An empty password silently becomes a connection attempt with no credential, which
    fails somewhere far away with a confusing error. Fail where the cause is."""
    try:
        ScopedSecretResolver(FakeSecrets(), "acme", ALICE).resolve(H)
    except KeyError:
        pass
    else:
        raise AssertionError("a missing secret must raise, not resolve to an empty string")
    print("  PASS  a missing secret raises at the resolution site")


def main():
    print("Scoped secret handles (#319 / ADR 0010 s5) self-test:")
    test_handle_round_trips()
    test_malformed_handles_parse_to_none_never_to_a_partial()
    test_adversarial_shapes_that_could_smuggle_a_partial_or_shifted_scope()
    test_unicode_confusable_separator_parses_to_none_even_after_nfkc_normalization()
    test_control_chars_whitespace_and_percent_encoding_in_segments_parse_to_none()
    test_overlength_segment_parses_to_none()
    test_format_handle_raises_for_the_same_unsafe_shapes()
    test_real_values_still_round_trip_after_the_charset_restriction()
    test_the_owner_resolves_their_own_secret()
    test_another_owner_in_the_same_tenant_is_refused_without_touching_the_store()
    test_another_tenant_is_refused()
    test_a_missing_secret_raises_rather_than_returning_empty()
    print("\nSECRET HANDLE SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
