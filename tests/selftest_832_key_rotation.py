"""#832 - rotating DBSEARCH_SECRET_KEY must be a maintenance step, not a data-loss event.

Before this card the store held one Fernet key. Swapping it did not rotate anything: it
orphaned every ciphertext - the vaulted AWS keys, the refresh-token vault - while
describe_secret kept answering {exists: true}, so the operator got no signal that every USE
of those credentials now fails. This file pins the three properties that make a rotation
safe:

  1. HONESTY. Under the wrong key alone, get_secret still refuses (InvalidToken) - and
     describe_secret now says so: readable=False, distinct from a genuinely short secret.
  2. THE OLD-KEY WINDOW. With key=NEW and old_keys=[OLD] (env: DBSEARCH_SECRET_KEY_OLD),
     everything written under OLD still reads, and NEW writes encrypt under NEW alone.
  3. THE EXIT. reencrypt_all() rewrites every blob under NEW, so OLD can then be dropped and
     the store still reads. A blob no key can read is REPORTED and left untouched - never
     silently dropped, never silently rewritten.

The existing guards this must not weaken: selftest_encrypted_secrets (a single wrong key
with no old-key list still refuses everything) and selftest_token_vault_durable (a rotation
degrades to sign-in-again, never a crash).

    PYTHONPATH=src python3 tests/selftest_832_key_rotation.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cryptography.fernet import Fernet, InvalidToken  # noqa: E402

from dbsearch.adapters.local import EncryptedFileSecrets  # noqa: E402

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


def _store(d, **kw):
    return EncryptedFileSecrets(Path(d) / "s.json", **kw)


def test_wrong_key_alone_refuses_and_describe_says_unreadable():
    with tempfile.TemporaryDirectory() as d:
        _store(d, key=KEY_A).put_secret("t/o/pg/password", "hunter2-swordfish")
        s = _store(d, key=KEY_B)
        try:
            s.get_secret("t/o/pg/password")
            raise AssertionError("a wrong key must never decrypt")
        except InvalidToken:
            pass
        got = s.describe_secret("t/o/pg/password")
        assert got is not None and got.get("exists") is True, got
        assert got.get("readable") is False, (
            f"an undecryptable blob must say readable=False, got {got}")


def test_a_readable_secret_says_readable_true():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d, key=KEY_A)
        s.put_secret("t/o/pg/password", "hunter2-swordfish")
        got = s.describe_secret("t/o/pg/password")
        assert got == {"exists": True, "readable": True, "hint": "fish"}, got


def test_old_key_list_keeps_old_blobs_readable():
    with tempfile.TemporaryDirectory() as d:
        _store(d, key=KEY_A).put_secret("t/o/pg/password", "written-under-A")
        s = _store(d, key=KEY_B, old_keys=[KEY_A])
        assert s.get_secret("t/o/pg/password") == "written-under-A"
        assert s.describe_secret("t/o/pg/password")["readable"] is True


def test_new_writes_encrypt_under_the_primary_alone():
    """keys[0] must be the NEW key: a value written during the old-key window must be
    readable AFTER the old key is dropped, with no re-encrypt needed."""
    with tempfile.TemporaryDirectory() as d:
        _store(d, key=KEY_B, old_keys=[KEY_A]).put_secret("t/o/pg/fresh", "written-under-B")
        assert _store(d, key=KEY_B).get_secret("t/o/pg/fresh") == "written-under-B"


def test_reencrypt_all_lets_the_old_key_drop():
    with tempfile.TemporaryDirectory() as d:
        old = _store(d, key=KEY_A)
        old.put_secret("t/o/pg/password", "v1")
        old.put_secret("authrt/entra/oid-owner", "refresh-token-blob")

        dual = _store(d, key=KEY_B, old_keys=[KEY_A])
        report = dual.reencrypt_all()
        assert report["reencrypted"] == 2, report
        assert report["unreadable"] == [], report

        solo = _store(d, key=KEY_B)
        assert solo.get_secret("t/o/pg/password") == "v1"
        assert solo.get_secret("authrt/entra/oid-owner") == "refresh-token-blob"

        again = _store(d, key=KEY_B).reencrypt_all()
        assert again["reencrypted"] == 2 and again["unreadable"] == [], (
            f"idempotence: a second pass must succeed harmlessly, got {again}")


def test_reencrypt_reports_an_unreadable_blob_and_leaves_it_alone():
    """A blob NO configured key can read (e.g. two rotations ago) must be named in the
    report and left byte-identical - rewriting or dropping it destroys the one artifact a
    recovered key could still decrypt."""
    with tempfile.TemporaryDirectory() as d:
        _store(d, key=KEY_A).put_secret("t/o/pg/ancient", "from-a-lost-era")
        blob_before = (Path(d) / "s.json").read_text()

        dual = _store(d, key=KEY_B, old_keys=[Fernet.generate_key().decode()])
        report = dual.reencrypt_all()
        assert report["reencrypted"] == 0, report
        assert report["unreadable"] == ["t/o/pg/ancient"], report
        assert (Path(d) / "s.json").read_text() == blob_before, (
            "an unreadable blob must be left byte-identical")


def test_canvas_renders_unreadable_distinct_from_set_and_from_missing():
    """Structural check in the house style of selftest_canvas_credential_panel: the
    data-sechint block must branch on readable===false BEFORE the masked-hint branch, so a
    rotated-away credential renders as needing attention rather than as 'is set'."""
    canvas = (ROOT / "src" / "dbsearch" / "server" / "static"
              / "js" / "surfaces" / "canvas.js").read_text()
    seg = canvas[canvas.index('p.querySelectorAll("[data-sechint]")'):]
    seg = seg[:seg.index("function commitSecret")]
    assert "d.readable===false" in seg, "the unreadable branch is gone"
    assert "unreadable" in seg, "the unreadable message no longer says what is wrong"
    assert seg.index("d.readable===false") < seg.index("d.exists && d.hint"), (
        "the readable check must come FIRST: after the hint branch it can never fire, "
        "because an unreadable blob still has exists=true")


def test_env_var_wires_the_old_key():
    import os
    with tempfile.TemporaryDirectory() as d:
        _store(d, key=KEY_A).put_secret("t/o/pg/password", "via-env")
        os.environ["DBSEARCH_SECRET_KEY"] = KEY_B
        os.environ["DBSEARCH_SECRET_KEY_OLD"] = KEY_A
        try:
            s = EncryptedFileSecrets(Path(d) / "s.json")
            assert s.get_secret("t/o/pg/password") == "via-env"
        finally:
            del os.environ["DBSEARCH_SECRET_KEY"], os.environ["DBSEARCH_SECRET_KEY_OLD"]


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
