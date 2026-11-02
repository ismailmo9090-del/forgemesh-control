"""FastAPI service: token auth, localhost policy and profile CRUD."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from forgemesh_browser.api.server import create_app
from forgemesh_browser.settings import Settings

pytestmark = pytest.mark.integration


@contextmanager
def api(settings: Settings) -> Iterator[tuple[TestClient, dict[str, str]]]:
    app = create_app(settings)
    with TestClient(app) as client:
        token = settings.token_path.read_text(encoding="utf-8").strip()
        yield client, {"X-API-Token": token}


def test_health_and_root_are_public(settings: Settings) -> None:
    with api(settings) as (client, _headers):
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        root = client.get("/")
        assert root.status_code == 200
        assert root.json()["service"] == "forgemesh-browser"


def test_protected_endpoints_require_token(settings: Settings) -> None:
    with api(settings) as (client, headers):
        assert client.get("/status").status_code == 401
        assert client.get("/status", headers={"X-API-Token": "wrong"}).status_code == 401
        assert client.get("/profiles").status_code == 401

        assert client.get("/status", headers=headers).status_code == 200
        bearer = {"Authorization": f"Bearer {headers['X-API-Token']}"}
        assert client.get("/status", headers=bearer).status_code == 200


def test_settings_never_exposes_secrets(settings: Settings) -> None:
    with api(settings) as (client, headers):
        body = client.get("/settings", headers=headers).json()
        assert body["api"]["remote_enabled"] is False
        assert body["openai_configured"] is False
        text = str(body).lower()
        assert "openai_api_key" not in text and "api_token" not in text


def test_status_and_provider_discovery(settings: Settings) -> None:
    with api(settings) as (client, headers):
        status = client.get("/status", headers=headers).json()
        assert status["daemon"]["running"] is True
        assert status["selected_profile"] == "primary"

        providers = client.get("/providers", headers=headers).json()["providers"]
        assert {item["id"] for item in providers} == {
            "chatgpt",
            "claude",
            "kimi",
            "deepseek",
            "grok",
            "perplexity",
            "gemini",
            "sarvam",
        }

        chatgpt = client.get("/providers/chatgpt", headers=headers)
        assert chatgpt.status_code == 200
        assert chatgpt.json()["url"].startswith("https://")

        unknown = client.get("/providers/nope", headers=headers)
        assert unknown.status_code == 502
        assert unknown.json()["error"]["code"] == "adapter_error"

        ai = client.get("/ai/providers", headers=headers).json()
        assert ai["default"] == "ollama"
        assert [item["id"] for item in ai["providers"]] == ["ollama"]


def test_profile_crud(settings: Settings) -> None:
    with api(settings) as (client, headers):
        created = client.post(
            "/profiles", json={"id": "api-test", "name": "API Test"}, headers=headers
        )
        assert created.status_code == 200
        assert created.json()["id"] == "api-test"

        duplicate = client.post("/profiles", json={"id": "api-test"}, headers=headers)
        assert duplicate.status_code == 400
        assert duplicate.json()["error"]["code"] == "profile_error"

        invalid = client.post("/profiles", json={"id": "Bad/ID"}, headers=headers)
        assert invalid.status_code == 400

        listed = client.get("/profiles", headers=headers).json()
        assert {item["id"] for item in listed} == {"primary", "api-test"}

        inspected = client.get("/profiles/api-test", headers=headers).json()
        assert inspected["isolation"]["within_root"] is True
        assert inspected["isolation"]["private"] is True
        assert inspected["running"] is False

        activated = client.post("/profiles/api-test/activate", headers=headers)
        assert activated.status_code == 200
        assert activated.json()["selected"] is True

        renamed = client.patch(
            "/profiles/api-test", json={"name": "Renamed"}, headers=headers
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Renamed"

        refused = client.delete("/profiles/api-test", headers=headers)
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "profile_error"

        deleted = client.delete(
            "/profiles/api-test", params={"confirm": "true"}, headers=headers
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] == "api-test"
        assert client.get("/profiles/api-test", headers=headers).status_code == 404


def test_ai_rejects_empty_request(settings: Settings) -> None:
    with api(settings) as (client, headers):
        response = client.post("/ai", json={"prompt": ""}, headers=headers)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "ai_provider_error"


def test_unknown_profile_actions_fail(settings: Settings) -> None:
    with api(settings) as (client, headers):
        assert client.get("/profiles/ghost", headers=headers).status_code == 404
        started = client.post(
            "/browser/start", json={"profile": "ghost"}, headers=headers
        )
        assert started.status_code == 404
        assert started.json()["error"]["code"] == "profile_not_found"


def test_local_host_policy() -> None:
    from forgemesh_browser.api.deps import is_local_host

    assert is_local_host("127.0.0.1")
    assert is_local_host("::1")
    assert is_local_host("localhost")
    assert is_local_host("testclient")
    assert not is_local_host("203.0.113.10")
    assert not is_local_host("10.0.0.5")


def _png(path, color) -> None:
    from PIL import Image

    Image.new("RGB", (40, 30), color).save(path)


def test_compare_endpoint_and_diff_image(settings: Settings, tmp_path) -> None:
    from PIL import Image

    first, second = tmp_path / "a.png", tmp_path / "b.png"
    _png(first, (10, 20, 30))
    Image.open(first).copy().save(second)

    with api(settings) as (client, headers):
        identical = client.post(
            "/compare",
            json={"first": str(first), "second": str(second)},
            headers=headers,
        )
        assert identical.status_code == 200
        body = identical.json()
        assert body["same_size"] is True and body["identical"] is True
        assert body["diff_ratio"] == 0.0

        _png(second, (10, 20, 40))
        diff_path = tmp_path / "diff.png"
        changed = client.post(
            "/compare",
            json={
                "first": str(first),
                "second": str(second),
                "output": str(diff_path),
            },
            headers=headers,
        ).json()
        assert changed["identical"] is False
        assert changed["diff_ratio"] == pytest.approx(1 / 3, abs=0.01)
        assert changed["bbox"] and diff_path.is_file()

        rejected = client.post(
            "/compare",
            json={"first": str(first), "second": str(tmp_path / "ghost.png")},
            headers=headers,
        )
        assert rejected.status_code == 403
        assert rejected.json()["error"]["code"] == "security_error"
        assert "File does not exist" in rejected.json()["error"]["message"]


def test_compare_rejects_non_image(settings: Settings, tmp_path) -> None:
    note = tmp_path / "notes.txt"
    note.write_text("not an image", encoding="utf-8")
    with api(settings) as (client, headers):
        response = client.post(
            "/compare",
            json={"first": str(note), "second": str(note)},
            headers=headers,
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "media_error"


def test_identity_lifecycle_and_vault_metadata(settings: Settings) -> None:
    with api(settings) as (client, headers):
        created = client.post(
            "/identities",
            headers=headers,
            json={"email": "A@Example.com", "label": "work"},
        )
        assert created.status_code == 200
        body = created.json()
        assert body["email"] == "a@example.com"
        assert body["profile_id"] == "a"
        assert body["has_password"] is False

        duplicate = client.post(
            "/identities", headers=headers, json={"email": "a@example.com"}
        )
        assert duplicate.status_code == 400
        assert duplicate.json()["error"]["code"] == "identity_error"

        listed = client.get("/identities", headers=headers).json()["identities"]
        assert [item["email"] for item in listed] == ["a@example.com"]

        vault = client.get("/identities/vault", headers=headers).json()
        assert vault["exists"] is False
        assert vault["key_source"] == "keyfile"

        stored = client.post(
            "/identities/a@example.com/password",
            headers=headers,
            json={"password": "not-returned", "provider": "gemini"},
        )
        assert stored.status_code == 200
        assert stored.json()["has_password"] is True
        assert stored.json()["password_providers"] == ["gemini"]
        assert "not-returned" not in stored.text

        cleared = client.delete(
            "/identities/a@example.com/password",
            headers=headers,
            params={"provider": "gemini"},
        )
        assert cleared.json()["removed"] is True

        removed = client.delete(
            "/identities/a@example.com",
            headers=headers,
            params={"confirm": "true"},
        )
        assert removed.status_code == 200
        assert removed.json()["profile_deleted"] is False
        assert client.get("/identities", headers=headers).json()["identities"] == []
