"""EncryptedFileSecrets - the self-host SecretsPort for user-supplied credentials (#319).

ADR 0010 requires a self-host store that needs nothing external, so `docker compose up`
still works, while a customer's database password rests encrypted rather than in a manifest.

Two deliberate refusals:

1. NO GENERATED KEY. If DBSEARCH_SECRET_KEY is unset this raises. A key generated on first
   boot and persisted next to the ciphertext protects against nothing an attacker with the
   disk cares about, and it would be the DEFAULT path - which is #183's exact failure shape
   (the protection nobody configured is the one running in public).
2. NO HAND-ROLLED CRYPTO. The stdlib has no cipher, so this needs `cryptography` (Fernet:
   AES-128-CBC + HMAC-SHA256, authenticated and versioned). It is an optional extra
   (`pip install dbsearch[secrets]`) and its absence is an actionable error, never a silent
   downgrade to plaintext.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path

from dbsearch.ports.base import SecretsPort


def _fernet(key: str, old_keys=()):
    """#832: a MultiFernet, so a key rotation is a maintenance step, not a data-loss event.
    keys[0] (the PRIMARY, from DBSEARCH_SECRET_KEY) is the only key that ENCRYPTS; the old
    keys (DBSEARCH_SECRET_KEY_OLD, the rotation window) only DECRYPT. MultiFernet-of-one is
    byte-identical to plain Fernet, so a deployment with no old key configured behaves
    exactly as before - including refusing everything under a single wrong key."""
    try:
        from cryptography.fernet import Fernet, MultiFernet
    except ImportError as exc:                 # pragma: no cover - environment-dependent
        raise RuntimeError(
            "user-supplied credentials need the 'cryptography' package: "
            "pip install 'dbsearch[secrets]'. Refusing to store secrets unencrypted."
        ) from exc
    try:
        keys = [key, *old_keys]
        return MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in keys])
    except Exception as exc:
        raise RuntimeError(
            "DBSEARCH_SECRET_KEY (or DBSEARCH_SECRET_KEY_OLD) is not a valid Fernet key. "
            "Generate one with: python3 -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"") from exc


class EncryptedFileSecrets(SecretsPort):
    """Fernet-encrypted JSON file. One ciphertext per handle, so a rewrite of one secret
    cannot corrupt another, and the file is chmod 600 on create.

    C1 (#319 review): FastAPI runs sync routes (like POST /secrets) in a threadpool, so a
    single uvicorn process is enough to run `put_secret`/`delete_secret` concurrently on
    different threads. Both are read-modify-write over the SAME JSON blob, so an unlocked
    pair racing loses one writer's update outright (last save wins, the other vanishes) -
    verified: 30 concurrent writers landed 1 secret on disk, with 9 callers told
    `{"stored": true}` for a credential that was never actually written. Every
    read-modify-write below therefore holds an exclusive `flock` across load+modify+save."""

    def __init__(self, path, key: "str | None" = None,
                 old_keys: "list[str] | None" = None) -> None:
        key = key or os.environ.get("DBSEARCH_SECRET_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "DBSEARCH_SECRET_KEY is not set. Self-serve credentials are refused rather "
                "than stored under a key generated on this box (that is encryption at rest "
                "in name only). Generate one with: python3 -c \"from cryptography.fernet "
                "import Fernet; print(Fernet.generate_key().decode())\" and set it in the "
                "server environment.")
        # #832: the rotation window. A single separate var, deliberately not a comma-list in
        # the primary: a typo'd comma in DBSEARCH_SECRET_KEY must stay impossible to express.
        if old_keys is None:
            old = os.environ.get("DBSEARCH_SECRET_KEY_OLD", "").strip()
            old_keys = [old] if old else []
        self._f = _fernet(key, old_keys)
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # One fixed lock-file path per store, so every writer (any thread, any process on
        # this box) contends on the SAME lock. It is never read for content - only its fd
        # is flock'd - so it is created empty and left alone.
        self._lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        self._lock_path.touch(exist_ok=True)

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            text = self._path.read_text()
            if not text.strip():
                raise ValueError("store file exists but has no usable content (0 bytes or whitespace)")
            return json.loads(text)
        except Exception as exc:
            raise RuntimeError(
                f"secrets store at {self._path} could not be read (corrupt or unreadable). "
                "Refusing to treat it as empty and refusing to write, because the next write "
                "would overwrite it and destroy whatever secrets survive in it. Restore the "
                "file from backup, or move it aside to start a fresh store, then retry."
            ) from exc

    def _save(self, data: dict) -> None:
        # C1: a UNIQUE temp path per write (mkstemp mixes in pid + a random suffix), not one
        # fixed `<file>.tmp` shared by every writer. Two writers racing on a shared fixed
        # path can each open/replace it out from under the other - the loser's `os.chmod`
        # then raises FileNotFoundError on a path a third writer already consumed. A unique
        # name means concurrent writers each own their own temp file; the flock in
        # `_read_modify_write` is what makes the read+modify+save cycle itself atomic.
        fd, tmp_name = tempfile.mkstemp(
            prefix=self._path.name + ".", suffix=".tmp", dir=str(self._path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(data))
            os.chmod(tmp, 0o600)
            tmp.replace(self._path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def _read_modify_write(self, mutate) -> None:
        """C1: the WHOLE load-modify-save cycle under one exclusive flock, so `put_secret`
        and `delete_secret` (both read-modify-write over the same file) can never interleave -
        neither with each other nor with themselves across threads. `mutate(data) -> dict |
        None`; returning None means "nothing changed, skip the write" (e.g. deleting an
        absent key), so a no-op delete does not still pay for + risk a save.

        A fresh fd is opened per call (never reused across calls) because flock locks are
        keyed off the OPEN FILE DESCRIPTION, not the path or the owning thread/process: two
        threads that opened the SAME already-open fd would (on POSIX) share one lock and not
        actually contend, so each call must do its own open()."""
        with open(self._lock_path, "r+") as lockfile:
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX)
            try:
                data = self._load()
                new_data = mutate(data)
                if new_data is not None:
                    self._save(new_data)
            finally:
                fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)

    def get_secret(self, name: str) -> str:
        blob = self._load().get(name)
        if blob is None:
            return ""
        return self._f.decrypt(blob.encode()).decode()

    def put_secret(self, name: str, value: str) -> None:
        def mutate(data: dict) -> dict:
            data[name] = self._f.encrypt(value.encode()).decode()
            return data
        self._read_modify_write(mutate)

    def delete_secret(self, name: str) -> None:
        def mutate(data: dict) -> "dict | None":
            return data if data.pop(name, None) is not None else None
        self._read_modify_write(mutate)

    def describe_secret(self, name: str) -> "dict | None":
        blob = self._load().get(name)
        if blob is None:
            return None
        try:
            value = self._f.decrypt(blob.encode()).decode()
        except Exception:
            # #832: say so. Before this, an undecryptable blob (key rotated away) answered
            # the same {exists: true, hint: ""} as a genuinely short secret, so the operator
            # who rotated the key got "is set" on every surface while every USE failed.
            return {"exists": True, "readable": False, "hint": ""}
        return {"exists": True, "readable": True,
                "hint": value[-4:] if len(value) > 4 else ""}

    def reencrypt_all(self) -> dict:
        """#832: the rotation's exit. Rewrites every blob under the PRIMARY key (MultiFernet
        .rotate decrypts with any configured key, re-encrypts with keys[0]), so the old key
        can be dropped afterwards. A blob NO configured key can read is reported by handle
        and left byte-identical - rewriting or dropping it would destroy the one artifact a
        recovered key could still decrypt. Runs under the store's exclusive flock, one
        atomic write for the whole pass. Returns {"reencrypted": n, "unreadable": [names]}."""
        report = {"reencrypted": 0, "unreadable": []}

        def mutate(data: dict) -> "dict | None":
            changed = False
            for handle in sorted(data):
                try:
                    data[handle] = self._f.rotate(data[handle].encode()).decode()
                    report["reencrypted"] += 1
                    changed = True
                except Exception:
                    report["unreadable"].append(handle)
            return data if changed else None

        self._read_modify_write(mutate)
        return report
