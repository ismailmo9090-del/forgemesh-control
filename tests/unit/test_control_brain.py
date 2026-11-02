"""Ordered brain: Qoder runs first, OpenCode only when Qoder really failed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from forgemesh_control.agents.base import AgentRunResult
from forgemesh_control.brain import BRAIN_ORDER, BrainService
from forgemesh_control.config import ControlSettings


def _settings(tmp_path: Path, **overrides: Any) -> ControlSettings:
    base: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "config_dir": tmp_path / "config",
        "auto_install_agents": False,
        "brain_timeout_seconds": 20,
    }
    base.update(overrides)
    return ControlSettings(**base)


def _info(kind: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "kind": kind,
        "enabled": True,
        "availability": "available",
        "auth_status": "signed_in",
        "path": f"/usr/bin/{kind}",
        "version": "test",
        "last_error": None,
    }
    base.update(overrides)
    return base


def _run(ok: bool, text: str = "", error: str | None = None, model: str | None = None):
    return AgentRunResult(
        ok=ok,
        text=text,
        exit_code=0 if ok else 1,
        duration_ms=1,
        error=error,
        model=model,
    )


class FakeAdapter:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def submit_task(self, prompt: str, **kwargs: Any) -> Any:
        self.calls.append({"prompt": prompt, **kwargs})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeRegistry:
    def __init__(
        self,
        infos: dict[str, dict[str, Any]],
        adapters: dict[str, FakeAdapter],
    ) -> None:
        self.infos = infos
        self.adapters = adapters

    async def get_agent(self, kind: str) -> dict[str, Any]:
        if kind not in self.infos:
            raise KeyError(f"unknown agent {kind}")
        return self.infos[kind]

    def adapter(self, kind: str, path: str) -> FakeAdapter:
        assert path == self.infos[kind]["path"]
        return self.adapters[kind]


def _service(tmp_path: Path, registry: FakeRegistry, **overrides: Any) -> BrainService:
    return BrainService(_settings(tmp_path, **overrides), registry)


def test_the_order_is_qoder_then_opencode() -> None:
    assert BRAIN_ORDER == ("qoder", "opencode")


async def test_qoder_answers_and_opencode_is_never_started(tmp_path: Path) -> None:
    qoder = FakeAdapter(_run(True, "brief from qoder", model="qoder/model"))
    opencode = FakeAdapter(RuntimeError("must not run"))
    registry = FakeRegistry(
        {"qoder": _info("qoder"), "opencode": _info("opencode")},
        {"qoder": qoder, "opencode": opencode},
    )
    result = await _service(tmp_path, registry).run("write a brief", purpose="test")
    assert result.ok is True
    assert result.kind == "qoder"
    assert result.text == "brief from qoder"
    assert [row.kind for row in result.attempts] == ["qoder"]
    assert opencode.calls == []
    assert qoder.calls[0]["cwd"] == tmp_path / "data" / "brain"


async def test_opencode_takes_over_when_qoder_hits_its_limit(tmp_path: Path) -> None:
    qoder = FakeAdapter(_run(False, "", error="usage limit reached, try again later"))
    opencode = FakeAdapter(_run(True, "polish from opencode", model="opencode/beam"))
    registry = FakeRegistry(
        {"qoder": _info("qoder"), "opencode": _info("opencode")},
        {"qoder": qoder, "opencode": opencode},
    )
    result = await _service(tmp_path, registry).run("polish this", purpose="test")
    assert result.ok is True
    assert result.kind == "opencode"
    assert result.text == "polish from opencode"
    assert [row.kind for row in result.attempts] == ["qoder", "opencode"]
    assert "usage limit" in (result.attempts[0].detail or "")
    assert result.attempts[1].model == "opencode/beam"


async def test_opencode_takes_over_when_qoder_is_unavailable(tmp_path: Path) -> None:
    qoder = FakeAdapter(_run(True, "should not run"))
    opencode = FakeAdapter(_run(True, "from opencode"))
    registry = FakeRegistry(
        {
            "qoder": _info("qoder", availability="signed_out", auth_status="signed_out"),
            "opencode": _info("opencode"),
        },
        {"qoder": qoder, "opencode": opencode},
    )
    result = await _service(tmp_path, registry).run("hello", purpose="test")
    assert result.kind == "opencode"
    assert qoder.calls == []
    assert "signed_out" in (result.attempts[0].detail or "")


async def test_both_fail_reports_both_attempts_without_inventing_text(tmp_path: Path) -> None:
    qoder = FakeAdapter(RuntimeError("qoder crashed"))
    opencode = FakeAdapter(_run(False, "", error="opencode exited 1: no model"))
    registry = FakeRegistry(
        {"qoder": _info("qoder"), "opencode": _info("opencode")},
        {"qoder": qoder, "opencode": opencode},
    )
    result = await _service(tmp_path, registry).run("hello", purpose="test")
    assert result.ok is False
    assert result.text == ""
    assert result.kind is None
    assert [row.kind for row in result.attempts] == ["qoder", "opencode"]
    assert "qoder crashed" in (result.attempts[0].detail or "")
    assert "no model" in (result.attempts[1].detail or "")


async def test_a_disabled_brain_does_not_spawn_anything(tmp_path: Path) -> None:
    qoder = FakeAdapter(_run(True, "should not run"))
    registry = FakeRegistry(
        {"qoder": _info("qoder"), "opencode": _info("opencode")},
        {"qoder": qoder, "opencode": FakeAdapter(_run(True, "x"))},
    )
    result = await _service(tmp_path, registry, brain_enabled=False).run(
        "hello", purpose="test"
    )
    assert result.ok is False
    assert result.error is not None
    assert qoder.calls == []


async def test_an_empty_answer_from_qoder_falls_back_to_opencode(tmp_path: Path) -> None:
    qoder = FakeAdapter(_run(True, "   "))
    opencode = FakeAdapter(_run(True, "real text"))
    registry = FakeRegistry(
        {"qoder": _info("qoder"), "opencode": _info("opencode")},
        {"qoder": qoder, "opencode": opencode},
    )
    result = await _service(tmp_path, registry).run("hello", purpose="test")
    assert result.ok is True
    assert result.kind == "opencode"
    assert result.text == "real text"
    assert "no text" in (result.attempts[0].detail or "")
