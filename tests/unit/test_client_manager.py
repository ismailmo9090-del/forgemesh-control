"""Client Manager tests: transport guard, secret storage and artifact safety.

These cover the daemon and the supervised worker child without opening a socket
to a real control server.
"""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import httpx
import pytest

from forgemesh_control import client_manager as cm
from forgemesh_control import client_worker as cw


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> cm.ClientPaths:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # Force the file backend so tests never touch the developer's keyring.
    monkeypatch.setattr(cm, "_secret_tool", lambda: None)
    return cm.ClientPaths.create()


@pytest.fixture
def service(paths: cm.ClientPaths) -> cm.ClientService:
    return cm.ClientService(paths)


# ---------------------------------------------------------------- transport
@pytest.mark.parametrize(
    "server",
    [
        "https://mesh.example.com",
        "https://mesh.example.com:8787/",
        "http://127.0.0.1:8787",
        "http://localhost:8787",
        "http://[::1]:8787",
    ],
)
def test_guard_scheme_accepts_https_and_loopback(server: str) -> None:
    assert cm.guard_scheme(server, insecure_http=False) == server.rstrip("/")


def test_guard_scheme_refuses_plain_http_to_lan() -> None:
    with pytest.raises(cm.ClientError) as excinfo:
        cm.guard_scheme("http://192.168.1.20:8787", insecure_http=False)
    assert excinfo.value.code == "insecure_transport_warning"


def test_guard_scheme_allows_explicit_opt_in() -> None:
    assert (
        cm.guard_scheme("http://192.168.1.20:8787", insecure_http=True)
        == "http://192.168.1.20:8787"
    )


@pytest.mark.parametrize(
    "server",
    ["ftp://mesh.example.com", "mesh.example.com:8787", "https://user:pass@mesh.example.com"],
)
def test_guard_scheme_rejects_malformed_urls(server: str) -> None:
    with pytest.raises(cm.ClientError) as excinfo:
        cm.guard_scheme(server, insecure_http=True)
    assert excinfo.value.code == "invalid_url"


def test_ws_url_maps_schemes() -> None:
    assert cm.ws_url("https://mesh.example.com") == "wss://mesh.example.com"
    assert cm.ws_url("http://127.0.0.1:8787") == "ws://127.0.0.1:8787"


def test_parse_server_detail_reads_nested_and_flat_errors() -> None:
    nested = httpx.Response(
        403,
        json={"detail": {"code": "worker_revoked", "message": "revoked"}},
        request=httpx.Request("GET", "http://x"),
    )
    flat = httpx.Response(
        409,
        json={"code": "capacity", "message": "full"},
        request=httpx.Request("GET", "http://x"),
    )
    assert cm.parse_server_detail(nested) == ("worker_revoked", "revoked")
    assert cm.parse_server_detail(flat) == ("capacity", "full")


# --------------------------------------------------------------- child env
def test_child_env_propagates_insecure_http(service: cm.ClientService) -> None:
    asyncio.run(
        service.config.save(server_url="http://10.1.2.3:8787", insecure_http=True)
    )
    service.paths.token_file.write_text("worker-token-value", encoding="utf-8")
    env = service._child_env()
    assert env["FMW_INSECURE_HTTP"] == "1"
    assert env["FMW_SERVER"] == "http://10.1.2.3:8787"
    assert env["FMW_WORKER_TOKEN"] == "worker-token-value"
    assert env["FMW_DEVICE_KEY"] == service.config.data["device_key"]
    assert env["PYTHONPATH"]


def test_child_env_drops_stale_opt_in(service: cm.ClientService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FMW_INSECURE_HTTP", "1")
    asyncio.run(
        service.config.save(server_url="https://mesh.example.com", insecure_http=False)
    )
    assert "FMW_INSECURE_HTTP" not in service._child_env()


def test_worker_reads_opt_in_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FMW_INSECURE_HTTP", raising=False)
    assert cw.insecure_from_env() is False
    monkeypatch.setenv("FMW_INSECURE_HTTP", "1")
    assert cw.insecure_from_env() is True
    monkeypatch.setenv("FMW_INSECURE_HTTP", "true")
    assert cw.insecure_from_env() is False


# ------------------------------------------------------------ config store
def test_config_is_private_and_never_contains_token(service: cm.ClientService) -> None:
    backend = asyncio.run(service.config.set_token("secret-token-123"))
    assert backend == "file"
    token_file = service.paths.token_file
    assert token_file.read_text(encoding="utf-8") == "secret-token-123"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(service.paths.config_file.stat().st_mode) == 0o600
    persisted = json.loads(service.paths.config_file.read_text(encoding="utf-8"))
    assert "secret-token-123" not in json.dumps(persisted)
    assert service.config.token() == "secret-token-123"
    assert service.config.has_token() is True


def test_clear_token_removes_both_copies(service: cm.ClientService) -> None:
    asyncio.run(service.config.set_token("secret-token-123"))
    service.config.clear_token()
    assert service.config.token() == ""
    assert not service.paths.token_file.exists()


def test_operator_token_is_private(service: cm.ClientService) -> None:
    service.config.set_operator_token("operator-token", "manual")
    assert stat.S_IMODE(service.paths.control_token_file.stat().st_mode) == 0o600
    assert service.config.operator_token() == "operator-token"
    assert "operator-token" in service.config.secrets_for_redaction()


def test_events_redact_tokens(service: cm.ClientService) -> None:
    asyncio.run(service.config.set_token("secret-token-123"))
    service.log_worker("worker connected with secret-token-123")
    assert "secret-token-123" not in service.worker_logs[-1]["text"]
    assert "[redacted]" in service.worker_logs[-1]["text"]


def test_operator_request_requires_token(service: cm.ClientService) -> None:
    asyncio.run(service.config.save(server_url="https://mesh.example.com"))
    with pytest.raises(cm.ClientError) as excinfo:
        asyncio.run(service.operator_request("GET", "/api/status"))
    assert excinfo.value.code == "operator_token_required"


# --------------------------------------------------------------- artifacts
class _Body(cm.ArtifactActionBody):
    pass


def _fakes(service: cm.ClientService, workspace: Path, artifact: dict) -> None:
    async def fake_request(method, path, *, json_body=None, params=None, timeout=60):
        assert method == "GET"
        assert path == "/api/delivery/artifacts"
        return {"artifacts": [artifact]}

    async def fake_project(project_id):
        return {"id": project_id, "workspace_path": str(workspace)}

    service.operator_request = fake_request  # type: ignore[method-assign]
    service.project_detail = fake_project  # type: ignore[method-assign]


def _artifact(path: Path) -> dict:
    import hashlib

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"id": "art_1", "name": "report name.md", "path": str(path), "sha256": digest}


def test_artifact_rejects_relative_path(service: cm.ClientService, tmp_path: Path) -> None:
    _fakes(service, tmp_path / "ws", {"id": "art_1", "name": "x", "path": "relative.txt", "sha256": ""})
    with pytest.raises(cm.ClientError) as excinfo:
        asyncio.run(cm._artifact_local(service, _Body(project_id="p1", artifact_id="art_1"), copy=False))
    assert excinfo.value.code == "unsafe_path"


def test_artifact_rejects_path_escape(service: cm.ClientService, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    _fakes(service, workspace, _artifact(outside))
    with pytest.raises(cm.ClientError) as excinfo:
        asyncio.run(cm._artifact_local(service, _Body(project_id="p1", artifact_id="art_1"), copy=True))
    assert excinfo.value.code == "unsafe_path"


def test_artifact_missing_file_is_not_local(service: cm.ClientService, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    missing = workspace / "gone.txt"
    _fakes(
        service,
        workspace,
        {"id": "art_1", "name": "gone.txt", "path": str(missing), "sha256": ""},
    )
    with pytest.raises(cm.ClientError) as excinfo:
        asyncio.run(cm._artifact_local(service, _Body(project_id="p1", artifact_id="art_1"), copy=False))
    assert excinfo.value.code == "not_local"
    assert excinfo.value.status == 409


def test_artifact_checksum_mismatch_blocks_copy(service: cm.ClientService, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "report.md"
    target.write_text("real content", encoding="utf-8")
    artifact = _artifact(target)
    artifact["sha256"] = "0" * 64
    _fakes(service, workspace, artifact)

    result = asyncio.run(
        cm._artifact_local(service, _Body(project_id="p1", artifact_id="art_1", confirm=True), copy=True)
    )
    assert result["matches"] is False
    assert "copied_to" not in result
    assert not (service.paths.data / "artifacts" / "report_name.md").exists()
    assert any(event["kind"] == "artifact.checksum_mismatch" for event in service.events)


def test_artifact_verify_and_copy_hardens_destination(service: cm.ClientService, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "report.md"
    target.write_text("real content", encoding="utf-8")
    _fakes(service, workspace, _artifact(target))
    body = _Body(project_id="p1", artifact_id="art_1", confirm=True)

    verified = asyncio.run(cm._artifact_local(service, body, copy=False))
    assert verified["matches"] is True

    copied = asyncio.run(cm._artifact_local(service, body, copy=True))
    destination = Path(copied["copied_to"])
    assert destination.parent == service.paths.data / "artifacts"
    assert destination.name == "report_name.md"
    assert destination.read_text(encoding="utf-8") == "real content"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
