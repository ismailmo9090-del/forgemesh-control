"""Control API integration tests (loopback + token, no real installs)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from forgemesh_control.api.server import create_app
from forgemesh_control.config import ControlSettings


@pytest.fixture
def app_client(tmp_path: Path):
    settings = ControlSettings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        auto_install_agents=False,
        frontend_dist=tmp_path / "missing-dist",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        token = app.state.runtime.token
        yield client, app, {"X-API-Token": token}


def test_health_is_public_but_status_requires_token(app_client) -> None:
    client, _app, headers = app_client
    assert client.get("/health").status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-API-Token": "wrong"}).status_code == 401
    response = client.get("/api/status", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "forgemesh-control"
    assert {"metrics", "agents", "setup", "browser", "counts"} <= set(data)
    assert isinstance(data["metrics"]["cpu_percent"], (int, float))


def test_session_cookie_authentication(app_client) -> None:
    client, app, _headers = app_client
    response = client.get(
        "/auth/session", params={"token": app.state.runtime.token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "fmc_token" in response.cookies or "fmc_token" in client.cookies
    assert client.get("/api/status").status_code == 200


def test_agents_and_setup_endpoints(app_client) -> None:
    client, _app, headers = app_client
    listed = client.get("/api/agents", headers=headers)
    assert listed.status_code == 200
    agents = listed.json()["agents"]
    assert {item["kind"] for item in agents} == {"opencode", "qoder"}
    for item in agents:
        assert "install_status" in item
        assert "auth_status" in item
        assert "availability" in item

    setup = client.get("/api/setup/status", headers=headers)
    assert setup.status_code == 200
    assert setup.json()["state"] in {
        "needs_setup",
        "setting_up",
        "ready",
        "degraded",
        "failed",
    }
    requirements = client.get("/api/setup/requirements", headers=headers)
    assert requirements.status_code == 200
    assert "bash" in requirements.json()["checks"]


def test_agent_install_route_uses_registry(app_client, monkeypatch) -> None:
    client, app, headers = app_client
    registry = app.state.runtime.agents

    async def fake_install(kind: str, *, force: bool = False, verify: bool = True):
        return {
            "kind": kind,
            "install_status": "ready",
            "availability": "available",
            "force": force,
            "verify": verify,
        }

    monkeypatch.setattr(registry, "install", fake_install)
    response = client.post(
        "/api/agents/opencode/install", headers=headers, json={"force": True}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["install_status"] == "ready"
    assert data["force"] is True


def test_agent_update_requires_approval_flag(app_client, monkeypatch) -> None:
    client, app, headers = app_client

    async def fake_update(kind: str, *, approve: bool):
        if not approve:
            from forgemesh_control.agents import AgentRegistryError

            raise AgentRegistryError("approval required", code="approval_required")
        return {"kind": kind, "updated": True}

    monkeypatch.setattr(app.state.runtime.agents, "update", fake_update)
    denied = client.post("/api/agents/qoder/update", headers=headers, json={"approve": False})
    assert denied.status_code == 400
    allowed = client.post("/api/agents/qoder/update", headers=headers, json={"approve": True})
    assert allowed.status_code == 200
    assert allowed.json()["updated"] is True


def test_qoder_gui_path_is_rejected(app_client) -> None:
    client, _app, headers = app_client
    response = client.post(
        "/api/agents/qoder/path", headers=headers, json={"path": "/opt/Qoder/qoder"}
    )
    assert response.status_code == 400
    assert "desktop" in response.json()["detail"].lower()


def test_project_approval_and_human_action_flow(app_client) -> None:
    client, _app, headers = app_client
    created = client.post(
        "/api/projects",
        headers=headers,
        json={"name": "Hackathon project", "mode": "hackathon"},
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]

    blueprint = client.post(
        f"/api/projects/{project_id}/blueprint",
        headers=headers,
        json={"content": {"vision": "real"}},
    )
    assert blueprint.status_code == 200
    assert blueprint.json()["version"] == 1

    approval = client.post(
        f"/api/projects/{project_id}/approvals",
        headers=headers,
        json={"stage": "blueprint", "summary": "Review"},
    )
    assert approval.status_code == 200
    approval_id = approval.json()["id"]
    decided = client.post(
        f"/api/projects/{project_id}/approvals/{approval_id}/decide",
        headers=headers,
        json={"approved": True, "note": "ok"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"

    action = client.post(
        f"/api/projects/{project_id}/human-actions",
        headers=headers,
        json={"title": "Create Google OAuth credentials", "blocking": True},
    )
    assert action.status_code == 200
    resolved = client.post(
        f"/api/projects/{project_id}/human-actions/{action.json()['id']}/resolve",
        headers=headers,
        json={"status": "done"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "done"

    detail = client.get(f"/api/projects/{project_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["human_actions"][0]["status"] == "done"


def test_document_upload_and_extraction(app_client) -> None:
    client, _app, headers = app_client
    content = (
        b"Problem Statement: Build a dashboard\n"
        b"Requirements\n- Show real metrics\n- Notify users\n"
    )
    response = client.post(
        "/api/documents",
        headers=headers,
        files={"file": ("problem.txt", content, "text/plain")},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "extracted"
    assert data["extracted"]["chars"] > 0
    assert data["extracted"]["fields"]

    detail = client.get(f"/api/documents/{data['id']}", headers=headers)
    assert detail.status_code == 200
    assert "Build a dashboard" in detail.json()["text"]


def test_websocket_requires_token_and_sends_snapshot(app_client) -> None:
    client, _app, _headers = app_client
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=wrong") as socket:
            socket.receive_json()

    token = _app.state.runtime.token
    with client.websocket_connect(f"/ws?token={token}") as socket:
        message = socket.receive_json()
    assert message["type"] == "snapshot"
    assert message["data"]["service"] == "forgemesh-control"
