"""Identity registry: one email, one profile, one vault entry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from forgemesh_browser.accounts.identities import IdentityManager, profile_slug
from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import IdentityError, ProfileError
from forgemesh_browser.profiles.manager import ProfileManager
from forgemesh_browser.providers.registry import ProviderRegistry
from forgemesh_browser.security.audit import AuditLogger
from forgemesh_browser.security.vault import Vault
from forgemesh_browser.settings import Settings
from forgemesh_browser.storage.database import Database
from forgemesh_browser.storage.repositories import accounts as account_repo


@dataclass
class IdentityHarness:
    identities: IdentityManager
    profiles: ProfileManager
    vault: Vault
    db: Database


@pytest.fixture
async def harness(settings: Settings) -> AsyncIterator[IdentityHarness]:
    db = Database(settings.database_path)
    await db.init()
    events = EventBus()
    audit = AuditLogger(db, settings.logs_dir)
    providers = ProviderRegistry(settings)
    profiles = ProfileManager(settings, db, events, audit)
    vault = Vault(settings)
    identities = IdentityManager(
        settings, db, profiles, providers, vault, events, audit
    )
    try:
        yield IdentityHarness(identities, profiles, vault, db)
    finally:
        await db.close()


def test_profile_slug() -> None:
    assert profile_slug("Alex.Doe8080@example.com") == "alex-doe8080"
    assert profile_slug("a+b@example.com") == "a-b"
    assert profile_slug("@example.com") == "identity"


async def test_create_identity_creates_profile(harness: IdentityHarness) -> None:
    info = await harness.identities.create("Alex@Example.com", label="Personal")
    assert info.email == "alex@example.com"
    assert info.profile_id == "alex"
    assert info.has_password is False
    profile = await harness.profiles.get("alex")
    assert profile.id == "alex"

    listed = await harness.identities.list()
    assert [item.email for item in listed] == ["alex@example.com"]


async def test_duplicate_identity_is_rejected(harness: IdentityHarness) -> None:
    await harness.identities.create("alex@example.com")
    with pytest.raises(IdentityError) as excinfo:
        await harness.identities.create("ALEX@example.com")
    assert "already exists" in excinfo.value.message


async def test_same_local_part_needs_explicit_profile(
    harness: IdentityHarness,
) -> None:
    await harness.identities.create("alex@gmail.com")
    with pytest.raises(IdentityError) as excinfo:
        await harness.identities.create("alex@outlook.com")
    assert "--profile-id" in excinfo.value.message

    info = await harness.identities.create(
        "alex@outlook.com", profile_id="alex-outlook"
    )
    assert info.profile_id == "alex-outlook"


async def test_invalid_email_is_rejected(harness: IdentityHarness) -> None:
    with pytest.raises(IdentityError):
        await harness.identities.create("not-an-email")


async def test_password_lifecycle(harness: IdentityHarness) -> None:
    await harness.identities.create("alex@example.com")
    info = await harness.identities.set_password("alex@example.com", "pw-1")
    assert info.has_password is True

    info = await harness.identities.set_password(
        "alex@example.com", "pw-gemini", provider_id="gemini"
    )
    assert info.password_providers == ["gemini"]
    assert harness.vault.get_password("alex@example.com", "gemini") == "pw-gemini"

    removed = await harness.identities.clear_password("alex@example.com")
    assert removed["removed"] is True
    assert harness.vault.emails() == []


async def test_remove_identity_clears_password_and_keeps_profile(
    harness: IdentityHarness,
) -> None:
    await harness.identities.create("alex@example.com")
    await harness.identities.set_password("alex@example.com", "pw")

    with pytest.raises(IdentityError):
        await harness.identities.remove("alex@example.com")

    result = await harness.identities.remove("alex@example.com", confirm=True)
    assert result == {"password_removed": True, "profile_deleted": False}
    assert harness.vault.emails() == []
    # The profile (and its cookies) survives unless asked to delete.
    await harness.profiles.get("alex")
    assert await harness.identities.list() == []


async def test_remove_identity_can_delete_profile(harness: IdentityHarness) -> None:
    await harness.identities.create("alex@example.com")
    result = await harness.identities.remove(
        "alex@example.com", confirm=True, delete_profile=True
    )
    assert result["profile_deleted"] is True
    with pytest.raises(ProfileError):
        await harness.profiles.get("alex")


async def test_profile_delete_is_blocked_by_identity(harness: IdentityHarness) -> None:
    await harness.identities.create("alex@example.com")
    with pytest.raises(ProfileError) as excinfo:
        await harness.profiles.delete("alex", confirm=True)
    assert "identity" in excinfo.value.message
    assert "fmb identity remove alex@example.com" in excinfo.value.message


async def test_identity_lists_account_statuses(harness: IdentityHarness) -> None:
    await harness.identities.create("alex@example.com")
    async with harness.db.session_scope() as session:
        row = await account_repo.upsert(session, "alex", "gemini")
        await account_repo.update_status(session, row.id, "available", ok=True)

    info = await harness.identities.get("alex@example.com")
    assert [account.provider_id for account in info.accounts] == ["gemini"]
    assert info.accounts[0].status == "available"
    assert info.accounts[0].provider_name == "Gemini"
