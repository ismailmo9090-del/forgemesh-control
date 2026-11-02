"""Encrypted vault: roundtrip, at-rest secrecy and key-source rules."""

from __future__ import annotations

import json
import os
import stat

import pytest

from forgemesh_browser.core.exceptions import CredentialMissing, VaultError
from forgemesh_browser.security.vault import Vault
from forgemesh_browser.settings import Settings


def _vault(settings: Settings) -> Vault:
    return Vault(settings)


def test_vault_roundtrip_and_provider_override(settings: Settings) -> None:
    vault = _vault(settings)
    assert vault.exists is False
    assert vault.get_password("user@example.com") is None

    vault.set_password("User@Example.com", "default-secret")
    assert vault.get_password("user@example.com") == "default-secret"
    assert vault.get_password("USER@example.com") == "default-secret"

    vault.set_password("user@example.com", "gemini-secret", provider="gemini")
    assert vault.get_password("user@example.com", "gemini") == "gemini-secret"
    assert vault.get_password("user@example.com", "deepseek") == "default-secret"
    assert vault.providers_for("user@example.com") == ["gemini"]
    assert vault.emails() == ["user@example.com"]

    assert vault.delete_password("user@example.com", provider="gemini") is True
    assert vault.get_password("user@example.com", "gemini") == "default-secret"

    assert vault.delete_password("user@example.com") is True
    assert vault.emails() == []


def test_vault_never_stores_plaintext(settings: Settings) -> None:
    vault = _vault(settings)
    email = "alex@example.com"
    password = "super-secret-password-42"
    vault.set_password(email, password)

    raw = vault.path.read_text(encoding="utf-8")
    assert password not in raw
    assert email not in raw
    assert "ciphertext" in raw

    mode = stat.S_IMODE(os.stat(vault.path).st_mode)
    assert mode == 0o600
    dir_mode = stat.S_IMODE(os.stat(settings.secrets_dir).st_mode)
    assert dir_mode == 0o700
    key_mode = stat.S_IMODE(os.stat(vault.key_path).st_mode)
    assert key_mode == 0o600


def test_vault_require_password_reports_missing(settings: Settings) -> None:
    vault = _vault(settings)
    with pytest.raises(CredentialMissing) as excinfo:
        vault.require_password("nobody@example.com", "gemini")
    assert "fmb identity password" in excinfo.value.message
    assert "--provider gemini" in excinfo.value.message


def test_vault_rejects_empty_password(settings: Settings) -> None:
    with pytest.raises(VaultError):
        _vault(settings).set_password("user@example.com", "")


def test_vault_keyfile_is_shared_between_instances(settings: Settings) -> None:
    _vault(settings).set_password("user@example.com", "pw")
    # A second instance (the daemon) reads the same key file.
    assert _vault(settings).get_password("user@example.com") == "pw"


def test_vault_passphrase_mode(settings: Settings) -> None:
    settings.vault_passphrase = "correct horse battery staple"
    vault = _vault(settings)
    vault.set_password("user@example.com", "pw-1")
    assert vault.get_password("user@example.com") == "pw-1"
    assert not settings.vault_key_path.exists()

    raw = vault.path.read_text(encoding="utf-8")
    assert '"key_source": "passphrase"' in raw

    # Wrong passphrase must not decrypt.
    wrong = Vault(settings, passphrase="wrong passphrase")
    with pytest.raises(VaultError) as excinfo:
        wrong.get_password("user@example.com")
    assert "passphrase" in excinfo.value.message.lower()

    # A key-file claim on a passphrase vault is refused instead of guessing.
    settings.vault_passphrase = None
    with pytest.raises(VaultError):
        Vault(settings).get_password("user@example.com")


def test_vault_refuses_passphrase_on_keyfile_vault(settings: Settings) -> None:
    _vault(settings).set_password("user@example.com", "pw")
    settings.vault_passphrase = "a passphrase"
    with pytest.raises(VaultError) as excinfo:
        Vault(settings).get_password("user@example.com")
    assert "key file" in excinfo.value.message


def test_vault_missing_keyfile(settings: Settings) -> None:
    vault = _vault(settings)
    vault.set_password("user@example.com", "pw")
    settings.vault_key_path.unlink()
    with pytest.raises(VaultError) as excinfo:
        Vault(settings).get_password("user@example.com")
    assert "key file is missing" in excinfo.value.message


def test_vault_index_summarises_without_values(settings: Settings) -> None:
    vault = _vault(settings)
    vault.set_password("default@example.com", "pw")
    vault.set_password("override@example.com", "pw", provider="gemini")
    vault.set_password("override@example.com", "pw", provider="deepseek")
    index = vault.index()
    assert index["default@example.com"] == {"has_password": True, "providers": []}
    assert index["override@example.com"] == {
        "has_password": True,
        "providers": ["deepseek", "gemini"],
    }
    assert "pw" not in str(index)


def test_vault_status_has_no_secrets(settings: Settings) -> None:
    vault = _vault(settings)
    vault.set_password("user@example.com", "pw")
    status = vault.status()
    assert status["exists"] is True
    assert status["emails"] == 1
    assert status["key_source"] == "keyfile"
    assert "pw" not in str(status)


def test_delete_on_missing_vault_creates_nothing(settings: Settings) -> None:
    vault = _vault(settings)
    assert vault.delete_password("nobody@example.com") is False
    assert not vault.path.exists()
    assert not settings.vault_key_path.exists()


def test_vault_reads_stored_scrypt_params(settings: Settings) -> None:
    settings.vault_passphrase = "correct horse battery staple"
    vault = _vault(settings)
    vault.set_password("user@example.com", "pw")
    envelope = json.loads(vault.path.read_text(encoding="utf-8"))

    # A tampered work factor must change the key, so decryption fails instead
    # of silently using the module defaults.
    envelope["scrypt"]["n"] = 2**15
    vault.path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(VaultError):
        Vault(settings).get_password("user@example.com")

    envelope["scrypt"]["n"] = 1
    vault.path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(VaultError) as excinfo:
        Vault(settings).get_password("user@example.com")
    assert "out of range" in excinfo.value.message
