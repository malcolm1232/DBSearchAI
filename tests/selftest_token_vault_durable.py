"""#435: the TokenVault must survive a restart.

The vault held (oid, idp) -> refresh_token in a plain in-process dict, while the session cookie
is an 8h HMAC-signed token that survives anything. So every `docker compose up -d api` left every
user still apparently SIGNED IN but unable to query - their delegated credential was gone. Seen
twice during the #429 verification alone: "Stranger Test session expired - sign in again to
query" immediately after each deploy. On a box with more than one user that reads as the product
logging people out at random.

Durability goes through the SAME Fernet-encrypted store as user credentials (#319/#417): a
refresh token IS a user credential, it must rest encrypted, and reusing that store means no new
infrastructure and no second at-rest story to get wrong.

Non-negotiable: if the store is absent or its key has been rotated, sign-in must still WORK
(memory-only, i.e. today's behaviour). Durability is an improvement, never a new hard dependency
on the critical path - a deployment without DBSEARCH_SECRET_KEY must not lose the ability to log
in, and a rotated key must degrade to "sign in again", never a 500.

    PYTHONPATH=src python3 tests/selftest_token_vault_durable.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SELFHOST_BACKEND", "memory")

from dbsearch.server import user_auth
from dbsearch.server.user_auth import NotSignedIn, TokenVault

OID = "d0c2a725-9545-455d-967f-c172f3d0c4eb"


def _store(tmp: Path, key: str = ""):
    """A real EncryptedFileSecrets, so this tests the actual at-rest path, not a stub."""
    from cryptography.fernet import Fernet

    from dbsearch.adapters.local.secrets import EncryptedFileSecrets
    return EncryptedFileSecrets(tmp / "secrets.json", key=key or Fernet.generate_key().decode())


def test_a_vaulted_token_survives_a_process_restart():
    """THE BUG. A brand-new TokenVault over the same store must still find the token."""
    with tempfile.TemporaryDirectory() as d:
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        tmp = Path(d)

        v1 = TokenVault(store=_store(tmp, key))
        v1.put(OID, "rt-live-1")

        v2 = TokenVault(store=_store(tmp, key))          # "restart"
        assert v2.get(OID) == "rt-live-1", "the refresh token did not survive the restart"
        assert v2.linked(OID) == ["entra"], v2.linked(OID)


def test_the_token_is_encrypted_at_rest_and_never_appears_in_the_file():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        TokenVault(store=_store(tmp)).put(OID, "rt-super-secret-value")
        raw = (tmp / "secrets.json").read_text()
        assert "rt-super-secret-value" not in raw, "the refresh token is on disk in PLAINTEXT"


def test_logout_deletes_the_durable_copy_too():
    """A logout that only clears memory leaves a redeemable credential on disk, and the next
    restart would silently resurrect the 'signed out' session."""
    with tempfile.TemporaryDirectory() as d:
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        tmp = Path(d)

        v1 = TokenVault(store=_store(tmp, key))
        v1.put(OID, "rt-1")
        v1.put(OID, "rt-g", idp="google")
        v1.drop(OID, idp="google")
        assert v1.linked(OID) == ["entra"]

        v1.drop(OID)                                      # full logout
        v2 = TokenVault(store=_store(tmp, key))
        assert v2.linked(OID) == []
        try:
            v2.get(OID)
            raise AssertionError("a dropped credential is still redeemable after restart")
        except NotSignedIn:
            pass


def test_no_store_still_works_in_memory():
    """A deployment with no DBSEARCH_SECRET_KEY must keep working exactly as before. Durability
    is an improvement, not a new hard dependency on the sign-in path."""
    v = TokenVault(store=None)
    v.put(OID, "rt-mem")
    assert v.get(OID) == "rt-mem"
    assert v.linked(OID) == ["entra"]
    v.drop(OID)
    assert v.linked(OID) == []


def test_a_rotated_key_degrades_to_sign_in_again_not_a_crash():
    """Ciphertext written under the old key is unreadable, which is CORRECT. The user must be
    told to sign in again; the server must not 500, and must not report them as linked."""
    with tempfile.TemporaryDirectory() as d:
        from cryptography.fernet import Fernet
        tmp = Path(d)
        TokenVault(store=_store(tmp, Fernet.generate_key().decode())).put(OID, "rt-old")

        rotated = TokenVault(store=_store(tmp, Fernet.generate_key().decode()))
        try:
            rotated.get(OID)
            raise AssertionError("a token encrypted under a rotated key was returned")
        except NotSignedIn:
            pass
        assert rotated.linked(OID) == [], \
            "an unreadable credential must not be reported as linked"


def test_the_vault_namespace_cannot_be_forged_through_the_secrets_api():
    """SECURITY. /secrets writes names of the form tenant/owner/store/field - always FOUR
    slash-separated segments, each restricted to [A-Za-z0-9._-]. The vault deliberately uses a
    different segment count, so no API caller can address a vault entry: not to inject a
    refresh token for a victim, and not to read its last-4 hint back out of describe_secret."""
    from dbsearch.router.secret_handles import parse_handle

    name = user_auth._vault_name(OID, "entra")
    assert name.count("/") != 3, \
        f"vault name {name!r} has the same shape as a user secret handle - forgeable"
    assert parse_handle("secret://" + name) is None, \
        "the vault name parses as a user handle, so /secrets could address it"


if __name__ == "__main__":
    test_a_vaulted_token_survives_a_process_restart()
    test_the_token_is_encrypted_at_rest_and_never_appears_in_the_file()
    test_logout_deletes_the_durable_copy_too()
    test_no_store_still_works_in_memory()
    test_a_rotated_key_degrades_to_sign_in_again_not_a_crash()
    test_the_vault_namespace_cannot_be_forged_through_the_secrets_api()
    print("OK selftest_token_vault_durable")
