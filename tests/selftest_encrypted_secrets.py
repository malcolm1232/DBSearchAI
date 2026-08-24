"""#319 (ADR 0010): the self-host secrets adapter.

Malcolm's ruling 260727: this adapter REQUIRES DBSEARCH_SECRET_KEY and never generates a
fallback. A generated-and-persisted key sits beside the ciphertext, so disk theft yields
plaintext - encryption at rest in name only. #183's lesson applies: a protection that
defaults to off is off on the box nobody configured, which is always the public one.

    PYTHONPATH=src python3 tests/selftest_encrypted_secrets.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.adapters.local.secrets import EncryptedFileSecrets  # noqa: E402

KEY = "3F2n8pQ7yR1s5T9vX2zB4dG6hJ8kL0mN3qS5uW7yA9c="   # test-only Fernet key


def test_missing_key_fails_closed_and_says_how_to_fix_it():
    os.environ.pop("DBSEARCH_SECRET_KEY", None)
    with tempfile.TemporaryDirectory() as d:
        try:
            EncryptedFileSecrets(Path(d) / "s.json")
        except RuntimeError as exc:
            assert "DBSEARCH_SECRET_KEY" in str(exc), f"error must name the var: {exc}"
        else:
            raise AssertionError(
                "adapter started without a key - it must fail closed, never generate one")
    print("  PASS  no key -> hard failure naming DBSEARCH_SECRET_KEY (never a generated key)")


def test_round_trip_and_describe_never_reveals_the_value():
    with tempfile.TemporaryDirectory() as d:
        s = EncryptedFileSecrets(Path(d) / "s.json", key=KEY)
        s.put_secret("acme/alice/db/password", "hunter2-correct-horse")
        assert s.get_secret("acme/alice/db/password") == "hunter2-correct-horse"
        d1 = s.describe_secret("acme/alice/db/password")
        assert d1 and d1["exists"] is True
        assert "hunter2-correct-horse" not in str(d1), f"describe leaked the value: {d1}"
        assert d1["hint"] == "orse", d1
        assert s.describe_secret("acme/alice/db/nope") is None
    print("  PASS  round trip works; describe returns existence + a 4-char hint only")


def test_the_value_is_not_on_disk_in_plaintext():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.json"
        s = EncryptedFileSecrets(p, key=KEY)
        s.put_secret("acme/alice/db/password", "hunter2-correct-horse")
        blob = p.read_bytes()
        assert b"hunter2-correct-horse" not in blob, "SECRET ON DISK IN PLAINTEXT"
    print("  PASS  the ciphertext file does not contain the plaintext")


def test_a_wrong_key_cannot_read_an_existing_store():
    other = "aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gI3kM5oQ7sU9w="
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.json"
        EncryptedFileSecrets(p, key=KEY).put_secret("acme/alice/db/password", "hunter2")
        s2 = EncryptedFileSecrets(p, key=other)
        try:
            got = s2.get_secret("acme/alice/db/password")
        except Exception:
            pass
        else:
            raise AssertionError(f"a wrong key decrypted the store, got {got!r}")
    print("  PASS  a different key cannot decrypt the store")


def test_delete_removes_it():
    with tempfile.TemporaryDirectory() as d:
        s = EncryptedFileSecrets(Path(d) / "s.json", key=KEY)
        s.put_secret("acme/alice/db/password", "hunter2")
        s.delete_secret("acme/alice/db/password")
        assert s.describe_secret("acme/alice/db/password") is None
        s.delete_secret("acme/alice/db/password")      # idempotent, must not raise
    print("  PASS  delete removes the secret and is idempotent")


def test_a_corrupt_store_file_raises_instead_of_reading_as_empty():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.json"
        EncryptedFileSecrets(p, key=KEY).put_secret("acme/alice/db/password", "hunter2")
        p.write_text("{not valid json at all")
        fresh = EncryptedFileSecrets(p, key=KEY)
        try:
            fresh.get_secret("acme/alice/db/password")
        except Exception:
            pass
        else:
            raise AssertionError("a corrupt store file was read as if it were empty")
        try:
            fresh.describe_secret("acme/alice/db/password")
        except Exception:
            pass
        else:
            raise AssertionError("describe_secret read a corrupt store as if it were empty")
    print("  PASS  a corrupt store file raises on read rather than returning empty")


def test_a_corrupt_store_file_blocks_writes_so_surviving_secrets_are_not_destroyed():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.json"
        s = EncryptedFileSecrets(p, key=KEY)
        s.put_secret("acme/alice/db/password", "hunter2")
        s.put_secret("acme/bob/db/password", "swordfish")
        corrupt_bytes = b"{not valid json at all"
        p.write_bytes(corrupt_bytes)
        fresh = EncryptedFileSecrets(p, key=KEY)
        try:
            fresh.put_secret("acme/carol/db/password", "unrelated-new-secret")
        except Exception:
            pass
        else:
            raise AssertionError(
                "put_secret on a corrupt store did not raise - it would have overwritten "
                "the file with only the new key, destroying alice's and bob's secrets")
        on_disk = p.read_bytes()
        assert on_disk == corrupt_bytes, (
            "the corrupt file's bytes changed even though put_secret raised - "
            f"surviving data may have been destroyed: {on_disk!r}")
    print("  PASS  a corrupt store blocks put_secret rather than silently destroying it on write")


def test_a_missing_file_still_yields_a_working_empty_store():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "does" / "not" / "exist-yet" / "s.json"
        s = EncryptedFileSecrets(p, key=KEY)
        assert s.get_secret("acme/alice/db/password") == ""
        assert s.describe_secret("acme/alice/db/password") is None
        s.put_secret("acme/alice/db/password", "hunter2")
        assert s.get_secret("acme/alice/db/password") == "hunter2"
    print("  PASS  a missing file is a genuinely empty store, not an error")


def test_a_zero_byte_store_file_raises_instead_of_reading_as_empty():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.json"
        EncryptedFileSecrets(p, key=KEY).put_secret("acme/alice/db/password", "hunter2")
        p.write_bytes(b"")
        fresh = EncryptedFileSecrets(p, key=KEY)
        try:
            fresh.get_secret("acme/alice/db/password")
        except Exception:
            pass
        else:
            raise AssertionError("a zero-byte store file was read as if it were empty")
    print("  PASS  a zero-byte store file raises on read rather than returning empty")


def test_a_zero_byte_store_file_blocks_writes_so_surviving_secrets_are_not_destroyed():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.json"
        s = EncryptedFileSecrets(p, key=KEY)
        s.put_secret("acme/alice/db/password", "hunter2")
        s.put_secret("acme/bob/db/password", "swordfish")
        p.write_bytes(b"")
        fresh = EncryptedFileSecrets(p, key=KEY)
        try:
            fresh.put_secret("acme/carol/db/password", "unrelated-new-secret")
        except Exception:
            pass
        else:
            raise AssertionError(
                "put_secret on a zero-byte store did not raise - it would have overwritten "
                "the file with only the new key, destroying alice's and bob's secrets")
        on_disk = p.read_bytes()
        assert on_disk == b"", (
            "the zero-byte file's bytes changed even though put_secret raised - "
            f"surviving data may have been destroyed: {on_disk!r}")
    print("  PASS  a zero-byte store blocks put_secret rather than silently destroying it on write")


def test_a_whitespace_only_store_file_behaves_the_same_as_zero_byte():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.json"
        EncryptedFileSecrets(p, key=KEY).put_secret("acme/alice/db/password", "hunter2")
        p.write_text("   \n\t  \n")
        fresh = EncryptedFileSecrets(p, key=KEY)
        try:
            fresh.get_secret("acme/alice/db/password")
        except Exception:
            pass
        else:
            raise AssertionError("a whitespace-only store file was read as if it were empty")
    print("  PASS  a whitespace-only store file raises the same as a zero-byte file")


def test_a_missing_file_still_round_trips_after_the_zero_byte_fix():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "does" / "not" / "exist-yet-either" / "s.json"
        s = EncryptedFileSecrets(p, key=KEY)
        s.put_secret("acme/dave/db/password", "hunter3")
        assert s.get_secret("acme/dave/db/password") == "hunter3"
    print("  PASS  a genuinely absent file still round-trips put/get (first run unaffected)")


def test_concurrent_writers_all_survive():
    """C1 (review finding, 260727): FastAPI runs sync routes in a threadpool, so a single
    uvicorn is enough to run `put_secret` concurrently on many threads. Before the fix this
    was an unlocked read-modify-write over one JSON blob PLUS a single fixed `<file>.tmp`
    shared by every writer - the reviewer's repro of 30 concurrent POST /secrets landed 1
    secret on disk (of 30), 9 callers told `200 {"stored": true}` for a credential that was
    never written, and a bare `FileNotFoundError` from `os.chmod` when another thread had
    already replaced the tmp path out from under it. Every one of the 30 distinct handles
    written here must be readable back afterward - not "most", not "no crash"."""
    N = 30
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.json"
        s = EncryptedFileSecrets(p, key=KEY)
        errors: list = []

        def _write(i: int) -> None:
            try:
                s.put_secret(f"acme/writer-{i}/db/password", f"value-{i}")
            except Exception as exc:                       # noqa: BLE001 - recorded, not swallowed
                errors.append((i, exc))

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"{len(errors)}/{N} writers raised: {errors[:5]}"

        missing = [i for i in range(N)
                   if s.get_secret(f"acme/writer-{i}/db/password") != f"value-{i}"]
        assert not missing, (
            f"{len(missing)}/{N} concurrent writes were silently lost (found "
            f"{N - len(missing)}/{N} on disk): missing indices {missing}")
    print(f"  PASS  {N}/{N} concurrent writers all landed and are readable back")


def main():
    print("Encrypted self-host secrets (#319) self-test:")
    test_missing_key_fails_closed_and_says_how_to_fix_it()
    test_round_trip_and_describe_never_reveals_the_value()
    test_the_value_is_not_on_disk_in_plaintext()
    test_a_wrong_key_cannot_read_an_existing_store()
    test_delete_removes_it()
    test_a_corrupt_store_file_raises_instead_of_reading_as_empty()
    test_a_corrupt_store_file_blocks_writes_so_surviving_secrets_are_not_destroyed()
    test_a_missing_file_still_yields_a_working_empty_store()
    test_a_zero_byte_store_file_raises_instead_of_reading_as_empty()
    test_a_zero_byte_store_file_blocks_writes_so_surviving_secrets_are_not_destroyed()
    test_a_whitespace_only_store_file_behaves_the_same_as_zero_byte()
    test_a_missing_file_still_round_trips_after_the_zero_byte_fix()
    test_concurrent_writers_all_survive()
    print("\nENCRYPTED SECRETS SELF-TEST PASSED.")


if __name__ == "__main__":
    main()
