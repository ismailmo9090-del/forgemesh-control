"""ngrok tunnel tests: official commands only, secrets never echoed."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgemesh_control import tunnel as tunnel_module
from forgemesh_control.api.server import create_app
from forgemesh_control.config import ControlSettings
from forgemesh_control.tunnel import TunnelError, TunnelService

TOKEN = "2" + "A" * 48


@pytest.fixture
def control_settings(tmp_path: Path) -> ControlSettings:
    return ControlSettings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        auto_install_agents=False,
        frontend_dist=tmp_path / "missing-dist",
    )


def fake_ngrok(path: Path, *, start_behaviour: str = "fail") -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        "args = sys.argv[1:]\n"
        "if args and args[0] == '--version':\n"
        "    print('ngrok version 3.39.13')\n"
        "    sys.exit(0)\n"
        "if args[:2] == ['config', 'add-authtoken']:\n"
        "    token = args[2]\n"
        "    cfg = pathlib.Path(args[args.index('--config') + 1])\n"
        "    cfg.parent.mkdir(parents=True, exist_ok=True)\n"
        "    cfg.write_text('version: \"3\"\\nagent:\\n    authtoken: ' + token + '\\n')\n"
        "    sys.exit(0)\n"
        "if args[:2] == ['config', 'check']:\n"
        "    sys.exit(0)\n"
        "if args and args[0] == 'http':\n"
        + (
            "    print('{\"lvl\":\"eror\",\"msg\":\"authentication failed\"}', flush=True)\n"
            "    sys.exit(1)\n"
            if start_behaviour == "fail"
            else "    sys.exit(0)\n"
        )
        + "print('unexpected args', args)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


async def test_a_missing_binary_is_reported_as_not_installed(
    control_settings: ControlSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = TunnelService(control_settings)
    monkeypatch.setattr(service, "resolve_binary", lambda: "")

    status = await service.status()

    assert status["installed"] is False
    assert status["state"] == "not_installed"
    assert status["public_url"] is None
    assert status["join_steps"] == []
    assert status["worker_install_command"].startswith("curl -fsSL ")
    assert "--worker" in status["worker_install_command"]


async def test_authtoken_is_saved_by_the_official_command_and_never_returned(
    control_settings: ControlSettings, tmp_path: Path
) -> None:
    binary = fake_ngrok(tmp_path / "ngrok")
    control_settings.ngrok_bin = str(binary)
    service = TunnelService(control_settings)

    status = await service.configure_authtoken(TOKEN)

    assert status["saved"] is True
    assert status["authtoken_saved"] is True
    assert status["authtoken_source"] == "forgemesh"
    assert TOKEN not in json.dumps(status)
    config = control_settings.data_dir / "tunnel" / "ngrok.yml"
    assert config.is_file()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert TOKEN in config.read_text(encoding="utf-8")


async def test_an_invalid_authtoken_is_rejected_before_running_ngrok(
    control_settings: ControlSettings,
) -> None:
    service = TunnelService(control_settings)
    with pytest.raises(TunnelError) as caught:
        await service.configure_authtoken("short")
    assert caught.value.code == "invalid"
    assert "short" not in caught.value.message


async def test_start_without_a_token_tells_the_operator_what_is_missing(
    control_settings: ControlSettings, tmp_path: Path
) -> None:
    control_settings.ngrok_bin = str(fake_ngrok(tmp_path / "ngrok"))
    service = TunnelService(control_settings)

    with pytest.raises(TunnelError) as caught:
        await service.start()

    assert caught.value.code == "needs_authtoken"
    assert service._process_alive() is False


async def test_a_failed_start_surfaces_ngroks_own_output(
    control_settings: ControlSettings, tmp_path: Path
) -> None:
    config_dir = control_settings.data_dir / "tunnel"
    config_dir.mkdir(parents=True)
    (config_dir / "ngrok.yml").write_text(
        f'version: "3"\nagent:\n    authtoken: {TOKEN}\n', encoding="utf-8"
    )
    control_settings.ngrok_bin = str(fake_ngrok(tmp_path / "ngrok", start_behaviour="fail"))
    service = TunnelService(control_settings)

    with pytest.raises(TunnelError) as caught:
        await service.start()

    assert caught.value.code == "start_failed"
    assert "authentication failed" in caught.value.message
    assert TOKEN not in caught.value.message
    assert service._process_alive() is False
    assert not (config_dir / "state.json").exists()
    status = await service.status()
    assert status["running"] is False
    assert status["state"] == "error"


async def test_install_rejects_a_non_zip_download(
    control_settings: ControlSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeResponse:
        status_code = 200
        content = b"<html>not a zip</html>"

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, _url):
            return FakeResponse()

    service = TunnelService(control_settings)
    monkeypatch.setattr(service, "resolve_binary", lambda: "")
    monkeypatch.setattr(tunnel_module.httpx, "AsyncClient", FakeClient)

    with pytest.raises(TunnelError) as caught:
        await service.install()

    assert caught.value.code == "install_failed"
    assert "zip" in caught.value.message.lower()


def test_the_public_url_prefers_https() -> None:
    payload = {
        "tunnels": [
            {"public_url": "http://plain.example", "proto": "http"},
            {"public_url": "https://secure.example", "proto": "https"},
        ]
    }
    assert TunnelService._public_url_from(payload) == "https://secure.example"
    assert TunnelService._public_url_from({"tunnels": []}) is None
    assert TunnelService._public_url_from({}) is None


def test_the_tunnel_routes_require_the_control_token(control_settings: ControlSettings) -> None:
    app = create_app(control_settings)
    with TestClient(app) as client:
        headers = {"X-API-Token": app.state.runtime.token}
        assert client.get("/api/tunnel").status_code == 401
        listed = client.get("/api/tunnel", headers=headers)
        assert listed.status_code == 200
        assert set(listed.json()) >= {"installed", "state", "public_url", "authtoken_saved"}

        rejected = client.post(
            "/api/tunnel/authtoken", json={"token": "not-a-real-token"}, headers=headers
        )
        assert rejected.status_code == 400
        assert rejected.json()["error"]["code"] == "invalid"
        assert "not-a-real-token" not in rejected.text


async def test_boot_does_not_publish_with_a_shared_ngrok_token(
    control_settings: ControlSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    shared = home / ".config" / "ngrok" / "ngrok.yml"
    shared.parent.mkdir(parents=True)
    shared.write_text(f'version: "3"\nagent:\n    authtoken: {TOKEN}\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    control_settings.ngrok_bin = str(fake_ngrok(tmp_path / "ngrok"))
    service = TunnelService(control_settings)

    async def never() -> dict:
        raise AssertionError("a shared ngrok token must not expose this server")

    monkeypatch.setattr(service, "start", never)

    assert await service.maybe_start_on_boot() is None


async def test_boot_republishes_a_tunnel_configured_in_forgemesh(
    control_settings: ControlSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = control_settings.data_dir / "tunnel"
    config_dir.mkdir(parents=True)
    (config_dir / "ngrok.yml").write_text(
        f'version: "3"\nagent:\n    authtoken: {TOKEN}\n', encoding="utf-8"
    )
    control_settings.ngrok_bin = str(fake_ngrok(tmp_path / "ngrok"))
    service = TunnelService(control_settings)
    started: list[bool] = []

    async def fake_start() -> dict:
        started.append(True)
        return {"public_url": "https://live.ngrok.app"}

    monkeypatch.setattr(service, "start", fake_start)

    result = await service.maybe_start_on_boot()

    assert started == [True]
    assert result == {"public_url": "https://live.ngrok.app"}
