"""Central Agent Registry for coding CLIs on the ForgeMesh server.

The registry owns durable state. It never assumes that an install succeeded:
the installer must return a real path/version, and verification is recorded as
three separate facts — executable installed, interface/version verified, and
authentication/model access checked.

Worker-side agents are tracked elsewhere (``agents`` table, populated by the
Mesh Client). This registry is central-only by design.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from forgemesh_control.agents.base import AgentInfo, CodingAgent
from forgemesh_control.agents.installer import (
    OFFICIAL_SOURCES,
    AgentInstaller,
    InstallResult,
    detect_platform,
)
from forgemesh_control.agents.opencode import OpenCodeAgent
from forgemesh_control.agents.qoder import QoderAgent
from forgemesh_control.audit import AuditService
from forgemesh_control.config import ControlSettings
from forgemesh_control.events import EventStore
from forgemesh_control.models import CentralAgentRow, iso
from forgemesh_control.storage import ControlDatabase

AGENT_KINDS = ("opencode", "qoder")
DISPLAY_NAMES = {kind: source.display_name for kind, source in OFFICIAL_SOURCES.items()}
READY_STATUSES = {"ready"}
FAILED_STATUSES = {"failed"}


class AgentRegistryError(RuntimeError):
    def __init__(self, message: str, *, code: str = "agent_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class CentralAgentRegistry:
    def __init__(
        self,
        settings: ControlSettings,
        db: ControlDatabase,
        events: EventStore,
        audit: AuditService,
        *,
        installer: AgentInstaller | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.events = events
        self.audit = audit
        self.installer = installer or AgentInstaller(
            settings, on_stage=self._on_stage
        )
        self._locks = {kind: asyncio.Lock() for kind in AGENT_KINDS}
        self._verify_locks = {kind: asyncio.Lock() for kind in AGENT_KINDS}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _validate_kind(kind: str) -> str:
        if kind not in AGENT_KINDS:
            raise AgentRegistryError(
                f"Unknown central agent {kind!r}; expected one of {', '.join(AGENT_KINDS)}.",
                code="unknown_agent",
            )
        return kind

    def _adapter(self, kind: str, path: str) -> CodingAgent:
        binary = Path(path)
        if kind == "opencode":
            return OpenCodeAgent(binary, timeout_seconds=self.settings.agent_timeout_seconds)
        return QoderAgent(binary, timeout_seconds=self.settings.agent_timeout_seconds)

    @staticmethod
    def _row_dict(row: CentralAgentRow) -> dict[str, Any]:
        metadata = row.metadata_json or {}
        return {
            "kind": row.kind,
            "display_name": row.display_name,
            "installed": row.installed,
            "path": row.path,
            "version": row.version,
            "install_status": row.install_status,
            "install_stage": row.install_stage,
            "install_source": row.install_source,
            "configuration_status": row.configuration_status,
            "auth_status": row.auth_status,
            "availability": row.availability,
            "os_supported": row.os_supported,
            "enabled": row.enabled,
            "update_policy": row.update_policy,
            "auto_update": row.auto_update,
            "last_error": row.last_error,
            "metadata": metadata,
            "log_path": metadata.get("log_path", ""),
            "last_install_at": iso(row.last_install_at),
            "detected_at": iso(row.detected_at),
            "verified_at": iso(row.verified_at),
            "auth_checked_at": iso(row.auth_checked_at),
            "updated_at": iso(row.updated_at),
        }

    async def _get_row(self, kind: str) -> CentralAgentRow:
        async with self.db.session_scope() as session:
            row = await session.get(CentralAgentRow, kind)
            if row is None:
                row = CentralAgentRow(
                    kind=kind,
                    display_name=DISPLAY_NAMES[kind],
                    os_supported=detect_platform(kind).supported,
                )
                session.add(row)
                await session.flush()
            return row

    async def _update_row(self, kind: str, **fields: Any) -> dict[str, Any]:
        metadata = fields.pop("metadata", None)
        async with self.db.session_scope() as session:
            row = await session.get(CentralAgentRow, kind)
            if row is None:
                row = CentralAgentRow(
                    kind=kind, display_name=DISPLAY_NAMES[kind]
                )
                session.add(row)
            for key, value in fields.items():
                setattr(row, key, value)
            if metadata is not None:
                row.metadata_json = {**(row.metadata_json or {}), **metadata}
            await session.flush()
            return self._row_dict(row)

    async def _on_stage(
        self, kind: str, stage: str, message: str, data: dict[str, Any]
    ) -> None:
        fields: dict[str, Any] = {
            "install_stage": stage,
            "metadata": {"last_stage_message": message, "last_stage_at": iso(datetime.now(UTC))},
        }
        if stage == "failed":
            fields["install_status"] = "failed"
            fields["availability"] = "unavailable"
            fields["last_error"] = data.get("error") or message
        elif stage in {"detected", "downloading", "installing", "configuring"}:
            fields["install_status"] = "installing"
            fields["availability"] = "unavailable"
        elif stage == "verifying":
            fields["install_status"] = "verifying"
        if data.get("path"):
            fields["path"] = str(data["path"])
        if data.get("version"):
            fields["version"] = str(data["version"])
        if data.get("log_path"):
            fields["metadata"] = {
                **(fields.get("metadata") or {}),
                "log_path": str(data["log_path"]),
            }
        await self._update_row(kind, **fields)
        await self.events.publish(
            "agent.install.stage",
            kind=kind,
            stage=stage,
            message=message,
            **{key: value for key, value in data.items() if key != "metadata"},
        )

    # --------------------------------------------------------------------- read
    async def _all_rows(self) -> list[dict[str, Any]]:
        async with self.db.session_scope() as session:
            rows = (
                await session.execute(
                    select(CentralAgentRow).order_by(CentralAgentRow.kind)
                )
            ).scalars().all()
        return [self._row_dict(row) for row in rows]

    async def ensure_rows(self) -> list[dict[str, Any]]:
        for kind in AGENT_KINDS:
            await self._get_row(kind)
        return await self._all_rows()

    async def list_agents(self) -> list[dict[str, Any]]:
        for kind in AGENT_KINDS:
            await self._get_row(kind)
        return await self._all_rows()

    async def get_agent(self, kind: str) -> dict[str, Any]:
        self._validate_kind(kind)
        await self._get_row(kind)
        async with self.db.session_scope() as session:
            row = await session.get(CentralAgentRow, kind)
            assert row is not None
            return self._row_dict(row)

    async def agent_info(self, kind: str) -> AgentInfo:
        data = await self.get_agent(kind)
        return AgentInfo(
            kind=data["kind"],
            display_name=data["display_name"],
            installed=data["installed"],
            path=data["path"],
            version=data["version"],
            install_status=data["install_status"],
            configuration_status=data["configuration_status"],
            auth_status=data["auth_status"],
            availability=data["availability"],
            os_supported=data["os_supported"],
            last_error=data["last_error"],
            metadata=data["metadata"],
        )

    # ------------------------------------------------------------------- detect
    async def detect(self, kind: str) -> dict[str, Any]:
        self._validate_kind(kind)
        platform_info = detect_platform(kind)
        detection = await self.installer.detect(kind)
        previous = await self.get_agent(kind)
        now = datetime.now(UTC)
        fields: dict[str, Any] = {
            "os_supported": platform_info.supported,
            "detected_at": now,
            "metadata": {
                "platform": platform_info.to_dict(),
                "detection": detection.to_dict(),
            },
        }
        if detection.installed:
            # Detection proves presence only. Keep a previous real verification
            # when the exact executable and version are unchanged; anything else
            # must go back to "detected" and be verified again.
            same_binary = (
                previous["install_status"] == "ready"
                and previous["path"] == detection.path
                and previous["version"] == detection.version
            )
            disabled = previous["install_status"] == "disabled" or not previous["enabled"]
            if same_binary:
                install_status = "ready"
                availability = previous["availability"]
            elif disabled:
                install_status = "disabled"
                availability = "disabled"
            else:
                install_status = "detected"
                availability = "limited"
            fields.update(
                installed=True,
                path=detection.path,
                version=detection.version,
                install_status=install_status,
                availability=availability,
                last_error=None if same_binary else previous["last_error"],
            )
        else:
            fields.update(
                installed=False,
                path="",
                version="",
                install_status="not_detected" if platform_info.supported else "failed",
                availability="unavailable",
                last_error=detection.error,
            )
        result = await self._update_row(kind, **fields)
        await self.events.publish("agent.detected", **result)
        await self.audit.record(
            "agent.detect",
            kind=kind,
            installed=detection.installed,
            path=detection.path,
            version=detection.version,
        )
        return result

    # ------------------------------------------------------------------- verify
    async def verify(
        self, kind: str, *, functional: bool = True
    ) -> dict[str, Any]:
        """Verify executable, interface, authentication and a real prompt."""
        self._validate_kind(kind)
        async with self._verify_locks[kind]:
            row = await self.get_agent(kind)
            path = row.get("path") or ""
            if not row.get("installed") or not path:
                return await self._update_row(
                    kind,
                    install_status="not_detected",
                    availability="unavailable",
                    last_error="No verified executable path is registered.",
                )
            adapter = self._adapter(kind, path)
            version = await adapter.get_version()
            capabilities = await adapter.get_capabilities()
            if not version:
                return await self._update_row(
                    kind,
                    install_status="failed",
                    availability="unavailable",
                    configuration_status="misconfigured",
                    last_error="Executable exists but `--version` failed.",
                    verified_at=datetime.now(UTC),
                )

            auth_status, auth_detail = await adapter.check_auth()
            models = await adapter.list_models()
            functional_result: dict[str, Any] = {
                "attempted": False,
                "detail": (
                    "Installed and executable; no functional prompt has run for the "
                    "current account yet. Use 'Check version & verify' to test it."
                ),
            }
            if functional:
                check = getattr(adapter, "functional_check", None)
                if callable(check):
                    try:
                        check_result = check(
                            cwd=self.settings.workspaces,
                            timeout_seconds=int(
                                getattr(adapter, "verify_timeout_seconds", 240)
                            ),
                        )
                        if asyncio.iscoroutine(check_result):
                            functional_result = await check_result
                        else:
                            functional_result = check_result
                    except Exception as exc:  # noqa: BLE001 - report, never fabricate
                        functional_result = {
                            "verified": False,
                            "detail": f"Functional check crashed: {exc}",
                        }
                else:  # pragma: no cover - every shipped adapter implements it
                    functional_result = {
                        "verified": False,
                        "detail": "Adapter has no functional check.",
                    }

            interface_ok = bool(capabilities.get("verified"))
            functional_ok = bool(functional_result.get("verified"))
            auth_required = bool(functional_result.get("auth_required"))

            if interface_ok and functional_ok:
                availability = "available"
                install_status = "ready"
                last_error = None
            elif interface_ok and auth_required:
                availability = "limited"
                install_status = "ready"
                last_error = (
                    "Installed and executable, but authentication is required "
                    "before this agent can run coding tasks."
                )
            elif interface_ok:
                availability = "limited"
                install_status = "ready"
                last_error = functional_result.get("detail") or (
                    "Installed, but the functional prompt did not succeed."
                )
            else:
                availability = "unavailable"
                install_status = "failed"
                last_error = capabilities.get("error") or "CLI interface verification failed."

            now = datetime.now(UTC)
            metadata = {
                "capabilities": capabilities,
                "models": models[:50],
                "model_count": len(models),
                "functional_check": functional_result,
                "auth_detail": auth_detail,
                # Whether this CLI needs a credential to run anything at all.
                # A tool that answers with free models while signed out must not
                # be listed as waiting for a sign-in.
                "auth_required_for_use": bool(
                    adapter.auth_support().get("required_for_basic_use", True)
                ),
                "verified_version": version,
            }
            result = await self._update_row(
                kind,
                version=version,
                install_status=install_status,
                configuration_status="configured" if interface_ok else "unconfigured",
                auth_status=auth_status,
                availability=availability,
                last_error=last_error,
                metadata=metadata,
                verified_at=now,
                auth_checked_at=now,
            )
            await self.events.publish("agent.verified", **result)
            await self.audit.record(
                "agent.verify",
                kind=kind,
                version=version,
                interface_ok=interface_ok,
                functional_ok=functional_ok,
                auth_status=auth_status,
                availability=availability,
            )
            return result

    # ------------------------------------------------------------------ install
    async def install(
        self,
        kind: str,
        *,
        force: bool = False,
        verify: bool = True,
    ) -> dict[str, Any]:
        self._validate_kind(kind)
        async with self._locks[kind]:
            await self._update_row(
                kind,
                install_status="installing",
                install_stage="detected",
                availability="unavailable",
                last_error=None,
                detected_at=datetime.now(UTC),
                os_supported=detect_platform(kind).supported,
            )
            await self.events.publish(
                "agent.install.started", kind=kind, force=force
            )
            result: InstallResult = await self.installer.install(kind, force=force)
            if not result.ok:
                await self._update_row(
                    kind,
                    install_status="failed",
                    install_stage="failed",
                    install_source=result.source,
                    install_log=(result.log_tail or result.error or "")[:650],
                    last_error=result.error or "Installation failed.",
                    last_install_at=datetime.now(UTC),
                    availability="unavailable",
                    metadata={
                        "log_path": result.log_path,
                        "install_duration_ms": result.duration_ms,
                    },
                )
                await self.events.publish(
                    "agent.install.failed",
                    kind=kind,
                    error=result.error,
                    log_path=result.log_path,
                )
                await self.audit.record(
                    "agent.install",
                    kind=kind,
                    ok=False,
                    error=result.error,
                    force=force,
                )
                return await self.get_agent(kind)

            await self._update_row(
                kind,
                installed=True,
                path=result.path,
                version=result.version,
                install_status="verifying" if verify else "ready",
                install_stage="ready",
                install_source=result.source,
                install_log=(result.log_tail or "")[:650],
                last_error=None,
                last_install_at=datetime.now(UTC),
                configuration_status="configured" if verify else "pending_verification",
                metadata={
                    "log_path": result.log_path,
                    "install_duration_ms": result.duration_ms,
                    "install_stage": result.stage,
                },
            )
            await self.events.publish(
                "agent.installed",
                kind=kind,
                path=result.path,
                version=result.version,
                source=result.source,
            )
            await self.audit.record(
                "agent.install",
                kind=kind,
                ok=True,
                path=result.path,
                version=result.version,
                source=result.source,
                force=force,
            )
            if verify:
                return await self.verify(kind, functional=True)
            return await self.get_agent(kind)

    async def reinstall(self, kind: str) -> dict[str, Any]:
        self._validate_kind(kind)
        await self.audit.record("agent.reinstall", kind=kind)
        return await self.install(kind, force=True, verify=True)

    async def update(self, kind: str, *, approve: bool) -> dict[str, Any]:
        self._validate_kind(kind)
        row = await self.get_agent(kind)
        if row["update_policy"] != "manual" and not approve:
            raise AgentRegistryError(
                "Automatic updates are not performed without the configured approval.",
                code="approval_required",
            )
        result = await self.installer.update(kind, approve=approve)
        await self.audit.record(
            "agent.update", kind=kind, ok=result.ok, approve=approve, error=result.error
        )
        if not result.ok:
            await self._update_row(
                kind,
                last_error=result.error,
                metadata={"log_path": result.log_path, "update_failed": True},
            )
            return await self.get_agent(kind)
        await self._update_row(
            kind,
            path=result.path or row["path"],
            version=result.version or row["version"],
            metadata={"log_path": result.log_path, "update_ok": True},
        )
        return await self.verify(kind, functional=True)

    # --------------------------------------------------------------- management
    async def set_enabled(self, kind: str, enabled: bool) -> dict[str, Any]:
        self._validate_kind(kind)
        row = await self.get_agent(kind)
        if not enabled:
            result = await self._update_row(
                kind,
                enabled=False,
                install_status="disabled",
                availability="disabled",
            )
        else:
            result = await self._update_row(
                kind,
                enabled=True,
                install_status="detected" if row["installed"] else "not_detected",
                availability="limited" if row["installed"] else "unavailable",
            )
        await self.audit.record("agent.enable", kind=kind, enabled=enabled)
        await self.events.publish("agent.availability", **result)
        return result

    async def set_path(self, kind: str, raw_path: str) -> dict[str, Any]:
        self._validate_kind(kind)
        if not raw_path.strip():
            raise AgentRegistryError("Executable path must not be empty.", code="invalid_path")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise AgentRegistryError(
                "Executable path must be absolute.", code="invalid_path"
            )
        if kind == "qoder" and (path.name == "qoder" or "/opt/qoder" in str(path).lower()):
            raise AgentRegistryError(
                "That is the Qoder desktop application, not the Qoder CLI. "
                "Register `qodercli` instead.",
                code="wrong_binary",
            )
        if not path.is_file():
            raise AgentRegistryError(
                f"File does not exist: {path}", code="invalid_path"
            )
        version, error = await self.installer._probe_binary(path)
        if error:
            raise AgentRegistryError(
                f"Executable check failed: {error}", code="invalid_binary"
            )
        await self._update_row(
            kind,
            path=str(path),
            installed=True,
            version=version,
            install_status="detected",
            configuration_status="configured",
            last_error=None,
            metadata={"configured_path": str(path)},
        )
        await self.audit.record("agent.set_path", kind=kind, path=str(path), version=version)
        await self.events.publish("agent.path", kind=kind, path=str(path), version=version)
        return await self.verify(kind, functional=True)

    async def logs(self, kind: str) -> dict[str, Any]:
        self._validate_kind(kind)
        row = await self.get_agent(kind)
        log_path = row["metadata"].get("log_path") or row.get("log_path") or ""
        text = ""
        if log_path:
            path = Path(log_path)
            with contextlib.suppress(OSError):
                if path.is_file() and path.stat().st_size <= 512_000:
                    text = path.read_text(encoding="utf-8", errors="replace")
        if not text:
            text = row["metadata"].get("last_stage_message", "")
        return {
            "kind": kind,
            "path": log_path,
            "log": text,
            "stored_tail": row["metadata"].get("last_stage_message", ""),
        }

    # ---------------------------------------------------------------- first run
    async def setup_status(self) -> dict[str, Any]:
        agents = await self.list_agents()
        by_kind = {item["kind"]: item for item in agents}
        ready = [k for k, item in by_kind.items() if item["install_status"] in READY_STATUSES]
        failed = [k for k, item in by_kind.items() if item["install_status"] in FAILED_STATUSES]
        pending = [
            k
            for k, item in by_kind.items()
            if item["install_status"] in {"installing", "verifying"}
        ]
        unverified = [
            k
            for k, item in by_kind.items()
            if item["install_status"] in {"detected", "not_detected", "disabled"}
        ]
        def requires_sign_in(item: dict[str, Any]) -> bool:
            if item["auth_status"] != "unauthenticated":
                return False
            required = item["metadata"].get("auth_required_for_use")
            if required is None:
                # Detection alone stores no capability record. Ask the adapter
                # rather than defaulting to "yes": OpenCode runs free models
                # while signed out, and a false blocker is its own bug.
                path = item.get("path") or ""
                if path:
                    try:
                        required = self._adapter(
                            item["kind"], path
                        ).auth_support().get("required_for_basic_use", True)
                    except Exception:  # noqa: BLE001 - verification stays strict
                        required = True
                else:
                    required = True
            return bool(required)

        unsigned = [k for k, item in by_kind.items() if item["auth_status"] == "unauthenticated"]
        auth_needed = [k for k in unsigned if requires_sign_in(by_kind[k])]
        auth_skippable = [k for k in unsigned if not requires_sign_in(by_kind[k])]
        available = [k for k, item in by_kind.items() if item["availability"] == "available"]
        limited = [k for k, item in by_kind.items() if item["availability"] == "limited"]
        if pending:
            state = "setting_up"
        elif not ready and not failed:
            state = "needs_setup"
        elif len(available) == len(AGENT_KINDS):
            state = "ready"
        elif available:
            # At least one coding CLI can really run; the rest are installed
            # but blocked by authentication, models or a failed install.
            state = "degraded"
        elif ready:
            state = "degraded"
        else:
            state = "failed"
        return {
            "state": state,
            "agents": by_kind,
            "ready": ready,
            "failed": failed,
            "pending": pending,
            "unverified": unverified,
            "auth_needed": auth_needed,
            "auth_optional": auth_skippable,
            "available": available,
            "limited": limited,
            "degraded": state == "degraded",
            "complete": state in {"ready", "degraded", "failed"},
            "missing_capabilities": [
                {
                    "kind": kind,
                    "reason": by_kind[kind]["last_error"]
                    or (
                        "Authentication required."
                        if kind in auth_needed
                        else "Not available."
                    ),
                }
                for kind in AGENT_KINDS
                if by_kind[kind]["availability"] != "available"
                or by_kind[kind]["install_status"] != "ready"
            ],
        }

    async def detect_all(self) -> list[dict[str, Any]]:
        await asyncio.gather(*(self.detect(kind) for kind in AGENT_KINDS))
        return await self.list_agents()

    async def first_run_setup(self) -> dict[str, Any]:
        """Install whatever is missing, independently, then verify each agent."""
        await self.ensure_rows()
        results = await asyncio.gather(
            *(self.install(kind, force=False, verify=True) for kind in AGENT_KINDS),
            return_exceptions=True,
        )
        for kind, result in zip(AGENT_KINDS, results, strict=True):
            if isinstance(result, Exception):
                await self._update_row(
                    kind,
                    install_status="failed",
                    availability="unavailable",
                    last_error=f"Setup task crashed: {result}",
                )
                await self.events.publish(
                    "agent.install.failed", kind=kind, error=str(result)
                )
        status = await self.setup_status()
        await self.events.publish("setup.agents.complete", **status)
        return status

    def adapter(self, kind: str, path: str) -> CodingAgent:
        return self._adapter(kind, path)
