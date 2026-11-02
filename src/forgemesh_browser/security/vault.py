"""Encrypted credential vault for identity prefill logins.

Design rules:

* No plaintext password ever touches the SQLite database, logs, the task
  records or the ForgeMesh agent API.
* The whole vault is one AES-256-GCM blob. The 32-byte key comes either from a
  master passphrase (``FMB_VAULT_PASSPHRASE``, stretched with scrypt) or from a
  random key file (``config_dir/vault.key``, mode 0600) created on first use.
* The vault directory is 0700 and the file 0600. Reads and writes take a POSIX
  file lock so the CLI and the daemon cannot corrupt each other.
* Losing the key means losing the stored passwords. That is intentional: there
  is no recovery backdoor.

The vault file stores no email addresses in plaintext either: the whole
mapping (email -> password, email/provider -> password) is inside the
ciphertext. Only the KDF parameters and the nonce are visible.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from forgemesh_browser.core.exceptions import CredentialMissing, SecurityError, VaultError
from forgemesh_browser.security.permissions import ensure_private_dir, secure_file
from forgemesh_browser.settings import Settings

VAULT_FORMAT_VERSION = 1
_KEY_SOURCE_PASSPHRASE = "passphrase"
_KEY_SOURCE_KEYFILE = "keyfile"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_KEY_BYTES = 32
_NONCE_BYTES = 12
_AAD = b"forgemesh-vault:v1"
_MAX_PASSWORD_CHARS = 1024


def normalize_email(email: str) -> str:
    return email.strip().lower()


class Vault:
    """AES-GCM credential store shared by the CLI and the daemon."""

    def __init__(self, settings: Settings, passphrase: str | None = None) -> None:
        self.settings = settings
        self._passphrase = passphrase if passphrase is not None else self._env_passphrase()

    # ------------------------------------------------------------- construction
    def _env_passphrase(self) -> str | None:
        value = self.settings.vault_passphrase
        if value is None:
            return None
        raw = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
        return raw or None

    @property
    def path(self) -> Path:
        return self.settings.vault_path

    @property
    def key_path(self) -> Path:
        return self.settings.vault_key_path

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _check_paths(self) -> None:
        if self.path.is_symlink():
            raise SecurityError(f"Vault path is a symlink: {self.path}")
        if self.key_path.is_symlink():
            raise SecurityError(f"Vault key path is a symlink: {self.key_path}")

    # ------------------------------------------------------------------ locking
    @contextlib.contextmanager
    def _lock(self, *, exclusive: bool) -> Iterator[None]:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - not POSIX
            yield
            return
        ensure_private_dir(self.path.parent)
        lock_path = self.path.with_suffix(".lock")
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # ------------------------------------------------------------------ crypto
    def _keyfile_bytes(self, *, create: bool) -> bytes | None:
        if not self.key_path.is_file():
            if not create:
                return None
            ensure_private_dir(self.key_path.parent)
            key = secrets.token_bytes(_KEY_BYTES)
            fd = os.open(
                self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(base64.b64encode(key).decode("ascii") + "\n")
            secure_file(self.key_path)
            return key
        secure_file(self.key_path)
        try:
            key = base64.b64decode(self.key_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError) as exc:
            raise VaultError("The vault key file is unreadable or corrupted.") from exc
        if len(key) != _KEY_BYTES:
            raise VaultError("The vault key file does not contain a 256-bit key.")
        return key

    def _derive_passphrase_key(
        self, salt: bytes, params: dict[str, int] | None = None
    ) -> bytes:
        assert self._passphrase is not None
        import hashlib

        n = (params or {}).get("n", _SCRYPT_N)
        r = (params or {}).get("r", _SCRYPT_R)
        p = (params or {}).get("p", _SCRYPT_P)
        if not (2**10 <= n <= 2**20 and 1 <= r <= 32 and 1 <= p <= 16):
            raise VaultError("The vault's scrypt parameters are out of range.")
        try:
            return hashlib.scrypt(
                self._passphrase.encode("utf-8"),
                salt=salt,
                n=n,
                r=r,
                p=p,
                maxmem=_SCRYPT_MAXMEM,
                dklen=_KEY_BYTES,
            )
        except ValueError as exc:
            raise VaultError("Could not derive the vault key from the passphrase.") from exc

    @staticmethod
    def _scrypt_params(envelope: dict[str, Any]) -> dict[str, int]:
        stored = envelope.get("scrypt")
        if not isinstance(stored, dict):
            return {"n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P}
        params: dict[str, int] = {}
        for name, default in (("n", _SCRYPT_N), ("r", _SCRYPT_R), ("p", _SCRYPT_P)):
            value = stored.get(name, default)
            if not isinstance(value, int) or isinstance(value, bool):
                raise VaultError("The vault's scrypt parameters are corrupted.")
            params[name] = value
        return params

    @staticmethod
    def _salt(envelope: dict[str, Any]) -> bytes:
        try:
            salt = base64.b64decode(envelope["salt"])
        except (KeyError, ValueError) as exc:
            raise VaultError("The vault file is missing its passphrase salt.") from exc
        if len(salt) < 16:
            raise VaultError("The vault's passphrase salt is corrupted.")
        return salt

    def _key_for_write(self, envelope: dict[str, Any]) -> bytes:
        if self._passphrase is not None:
            return self._derive_passphrase_key(
                self._salt(envelope), self._scrypt_params(envelope)
            )
        key = self._keyfile_bytes(create=True)
        assert key is not None
        return key

    def _key_for_read(self, envelope: dict[str, Any]) -> bytes:
        source = envelope.get("key_source")
        if source == _KEY_SOURCE_PASSPHRASE:
            if self._passphrase is None:
                raise VaultError(
                    "This vault is protected by a master passphrase. Set "
                    "FMB_VAULT_PASSPHRASE in the environment (for the CLI and the daemon)."
                )
            return self._derive_passphrase_key(
                self._salt(envelope), self._scrypt_params(envelope)
            )
        if source == _KEY_SOURCE_KEYFILE:
            if self._passphrase is not None:
                raise VaultError(
                    "This vault is protected by a key file, not a passphrase. Unset "
                    "FMB_VAULT_PASSPHRASE, or create a new vault."
                )
            key = self._keyfile_bytes(create=False)
            if key is None:
                raise VaultError(
                    f"The vault key file is missing: {self.key_path}. Without it the "
                    "stored passwords cannot be decrypted."
                )
            return key
        raise VaultError("Unknown vault key source; the vault file may be corrupted.")

    # ------------------------------------------------------------------- state
    def _empty_payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {"defaults": {}, "providers": {}}
        envelope: dict[str, Any] = {
            "version": VAULT_FORMAT_VERSION,
            "cipher": "AES-256-GCM",
            "key_source": _KEY_SOURCE_KEYFILE,
            "scrypt": {
                "n": _SCRYPT_N,
                "r": _SCRYPT_R,
                "p": _SCRYPT_P,
            },
            "data": data,
        }
        if self._passphrase is not None:
            envelope["key_source"] = _KEY_SOURCE_PASSPHRASE
            envelope["salt"] = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        return envelope

    def _read_envelope(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise VaultError("The vault file is unreadable or corrupted.") from exc
        if not isinstance(envelope, dict):
            raise VaultError("The vault file is not a JSON object.")
        if envelope.get("version") != VAULT_FORMAT_VERSION:
            raise VaultError(
                "Unsupported vault format version "
                f"{envelope.get('version')!r}; expected {VAULT_FORMAT_VERSION}."
            )
        return envelope

    def _decrypt(self, envelope: dict[str, Any]) -> dict[str, Any]:
        key = self._key_for_read(envelope)
        try:
            nonce = base64.b64decode(envelope["nonce"])
            ciphertext = base64.b64decode(envelope["ciphertext"])
        except (KeyError, ValueError) as exc:
            raise VaultError("The vault file is missing its nonce or ciphertext.") from exc
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, _AAD)
        except InvalidTag as exc:
            raise VaultError(
                "Could not decrypt the vault: the passphrase or key file is wrong."
            ) from exc
        try:
            data = json.loads(plaintext.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise VaultError("The decrypted vault payload is not valid JSON.") from exc
        if not isinstance(data, dict):
            raise VaultError("The decrypted vault payload is not an object.")
        data.setdefault("defaults", {})
        data.setdefault("providers", {})
        return data

    def _write_envelope(self, envelope: dict[str, Any], data: dict[str, Any]) -> None:
        key = self._key_for_write(envelope)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(key).encrypt(
            nonce, json.dumps(data, sort_keys=True).encode("utf-8"), _AAD
        )
        # Rebuild the envelope so a key-file vault never carries a stale salt.
        stored: dict[str, Any] = {
            "version": VAULT_FORMAT_VERSION,
            "cipher": "AES-256-GCM",
            "key_source": envelope["key_source"],
            "scrypt": envelope["scrypt"],
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        if envelope["key_source"] == _KEY_SOURCE_PASSPHRASE:
            stored["salt"] = envelope["salt"]
        ensure_private_dir(self.path.parent)
        tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(stored, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            secure_file(self.path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()

    def _load(self) -> dict[str, Any]:
        self._check_paths()
        with self._lock(exclusive=False):
            envelope = self._read_envelope()
            if envelope is None:
                return {"defaults": {}, "providers": {}}
            return self._decrypt(envelope)

    def _mutate(self, change) -> None:
        self._check_paths()
        with self._lock(exclusive=True):
            envelope = self._read_envelope() or self._empty_payload()
            data = self._decrypt(envelope) if "ciphertext" in envelope else (
                envelope.pop("data", None) or {"defaults": {}, "providers": {}}
            )
            change(data)
            self._write_envelope(envelope, data)

    # ------------------------------------------------------------------- API
    def emails(self) -> list[str]:
        data = self._load()
        known = set(data["defaults"]) | set(data["providers"])
        return sorted(known)

    def index(self) -> dict[str, dict[str, Any]]:
        """One decrypt for a whole listing: email -> {has_password, providers}."""

        data = self._load()
        result: dict[str, dict[str, Any]] = {}
        for email in data["defaults"]:
            result.setdefault(email, {"has_password": True, "providers": []})
        for email, overrides in data["providers"].items():
            entry = result.setdefault(email, {"has_password": False, "providers": []})
            entry["providers"] = sorted(overrides)
            entry["has_password"] = True
        return result

    def providers_for(self, email: str) -> list[str]:
        data = self._load()
        return sorted(data["providers"].get(normalize_email(email), {}))

    def has_password(self, email: str, provider: str | None = None) -> bool:
        data = self._load()
        email = normalize_email(email)
        if data["defaults"].get(email):
            return True
        overrides = data["providers"].get(email, {})
        if provider:
            return bool(overrides.get(provider))
        return bool(overrides)

    def get_password(self, email: str, provider: str | None = None) -> str | None:
        data = self._load()
        email = normalize_email(email)
        if provider:
            override = data["providers"].get(email, {}).get(provider)
            if override is not None:
                return override
        return data["defaults"].get(email)

    def require_password(self, email: str, provider: str | None = None) -> str:
        password = self.get_password(email, provider)
        if not password:
            hint = (
                f"fmb identity password {email} --provider {provider}"
                if provider
                else f"fmb identity password {email}"
            )
            raise CredentialMissing(
                f"No stored password for {email}"
                + (f" on {provider}" if provider else "")
                + f". Run: {hint}"
            )
        return password

    def set_password(
        self, email: str, password: str, *, provider: str | None = None
    ) -> None:
        email = normalize_email(email)
        if not password:
            raise VaultError("Refusing to store an empty password.")
        if len(password) > _MAX_PASSWORD_CHARS:
            raise VaultError(
                f"Password is longer than {_MAX_PASSWORD_CHARS} characters; refusing to store it."
            )

        def change(data: dict[str, Any]) -> None:
            if provider:
                data["providers"].setdefault(email, {})[provider] = password
            else:
                data["defaults"][email] = password

        self._mutate(change)

    def delete_password(self, email: str, *, provider: str | None = None) -> bool:
        email = normalize_email(email)
        # Nothing exists yet: do not create a key file just to record a no-op.
        if not self.exists:
            return False
        removed = {"found": False}

        def change(data: dict[str, Any]) -> None:
            if provider:
                overrides = data["providers"].get(email)
                if overrides and provider in overrides:
                    overrides.pop(provider)
                    removed["found"] = True
                    if not overrides:
                        data["providers"].pop(email, None)
            else:
                removed["found"] = data["defaults"].pop(email, None) is not None
                if data["providers"].pop(email, None) is not None:
                    removed["found"] = True

        self._mutate(change)
        return removed["found"]

    def status(self) -> dict[str, Any]:
        """Non-secret vault metadata for ``fmb identity status``."""

        exists = self.exists
        return {
            "path": str(self.path),
            "exists": exists,
            "key_source": _KEY_SOURCE_PASSPHRASE
            if self._passphrase is not None
            else _KEY_SOURCE_KEYFILE,
            "key_path": str(self.key_path),
            "emails": len(self.emails()) if exists else 0,
        }
