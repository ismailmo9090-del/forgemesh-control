"""Composition root for the ForgeMesh Control Center.

The runtime wires the browser connector, durable events, the central agent
registry and the setup wizard. It deliberately does not embed the browser
runtime: the existing ForgeMesh Browser remains a separate HTTP service.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from forgemesh_browser.security.permissions import secure_file
from forgemesh_control import __version__
from forgemesh_control.accounts import AiAccountService
from forgemesh_control.agent_auth import AgentAuthService
from forgemesh_control.agents import CentralAgentRegistry
from forgemesh_control.audit import AuditService
from forgemesh_control.brain import BrainService
from forgemesh_control.browser import BrowserConnector
from forgemesh_control.config import ControlSettings
from forgemesh_control.council import CouncilService
from forgemesh_control.delivery import DeliveryService
from forgemesh_control.documents import DocumentExtractor
from forgemesh_control.events import EventStore
from forgemesh_control.mesh import MeshService
from forgemesh_control.metrics import MetricsService
from forgemesh_control.models import SettingRow
from forgemesh_control.projects import ProjectService
from forgemesh_control.scheduler import TaskScheduler
from forgemesh_control.security import control_token
from forgemesh_control.storage import ControlDatabase
from forgemesh_control.tunnel import TunnelService
from forgemesh_control.workspaces import WorkspaceService

logger = logging.getLogger("forgemesh.control.runtime")


class ControlRuntime:
    def __init__(self, settings: ControlSettings) -> None:
        self.settings = settings
        self.db = ControlDatabase(settings.db_path)
        self.events = EventStore(self.db)
        self.audit = AuditService(self.db, settings.logs_dir)
        self.metrics = MetricsService(settings)
        self.browser = BrowserConnector(settings)
        self.documents = DocumentExtractor(settings)
        self.projects = ProjectService(settings, self.db, self.events, self.audit)
        self.agents = CentralAgentRegistry(
            settings, self.db, self.events, self.audit
        )
        self.mesh = MeshService(settings, self.db, self.events, self.audit)
        self.tunnel = TunnelService(settings, self.events, self.audit)
        self.workspaces = WorkspaceService(settings)
        self.delivery = DeliveryService(
            settings, self.db, self.events, self.audit, self.workspaces
        )
        self.accounts = AiAccountService(
            settings, self.db, self.events, self.audit, self.browser
        )
        self.agent_auth = AgentAuthService(
            settings, self.db, self.events, self.audit, self.agents
        )
        self.brain = BrainService(
            settings, self.agents, account_lookup=self.agent_auth.active_account
        )
        self.council = CouncilService(
            settings,
            self.db,
            self.events,
            self.audit,
            self.browser,
            self.projects,
            self.accounts,
            brain=self.brain,
        )
        self.scheduler = TaskScheduler(
            settings,
            self.db,
            self.events,
            self.audit,
            self.agents,
            self.projects,
            self.workspaces,
            self.council,
            self.delivery,
            agent_accounts=self.agent_auth,
        )
        self.token = ""
        self.started_at: datetime | None = None
        self._setup_task: asyncio.Task[Any] | None = None
        self._stopping = False

    # ------------------------------------------------------------------- startup
    async def start(self) -> None:
        self.settings.ensure_dirs()
        self.token = control_token(self.settings.token_path)
        await self.db.init()
        # A crash leaves statuses saying "running" with no task behind them.
        # Reconcile both planes before serving so the dashboard is truthful.
        await self.council.reconcile()
        await self.scheduler.reconcile()
        self.mesh.start()
        await self.tunnel.reconcile()
        await self.agents.ensure_rows()
        self.started_at = datetime.now(UTC)
        await self.agents.detect_all()
        self._write_server_info()
        await self.events.publish(
            "server.started",
            important=False,
            port=self.settings.port,
            pid=os.getpid(),
        )
        if self.settings.auto_install_agents:
            await self.maybe_start_first_run()
        if self.settings.auto_queue_tasks:
            await self.scheduler.start_queue()

    def _write_server_info(self) -> None:
        payload = {
            "pid": os.getpid(),
            "host": self.settings.host,
            "port": self.settings.port,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "token_path": str(self.settings.token_path),
            "data_dir": str(self.settings.data_dir),
        }
        try:
            self.settings.server_info_path.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
            secure_file(self.settings.server_info_path)
        except OSError:
            logger.warning("Could not write server info to %s", self.settings.server_info_path)

    async def stop(self) -> None:
        self._stopping = True
        if self._setup_task is not None and not self._setup_task.done():
            # Installation is intentionally not killed mid-download; give it a
            # short grace period, then cancel and record the interrupted state.
            try:
                await asyncio.wait_for(asyncio.shield(self._setup_task), timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._setup_task.cancel()
        with contextlib.suppress(Exception):
            await self.scheduler.shutdown()
        with contextlib.suppress(Exception):
            await self.agent_auth.shutdown()
        with contextlib.suppress(Exception):
            await self.council.shutdown()
        with contextlib.suppress(Exception):
            await self.tunnel.stop()
        with contextlib.suppress(Exception):
            await self.mesh.stop()
        await self.browser.close()
        with contextlib.suppress(Exception):
            await self.events.publish("server.stopped", important=True)
        await self.db.close()
        try:
            current = json.loads(self.settings.server_info_path.read_text(encoding="utf-8"))
            if current.get("pid") == os.getpid():
                self.settings.server_info_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ settings
    async def get_setting(self, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self.db.session_scope() as session:
            row = await session.get(SettingRow, key)
            return dict(row.value) if row is not None else dict(default or {})

    async def set_setting(self, key: str, value: dict[str, Any]) -> None:
        async with self.db.session_scope() as session:
            row = await session.get(SettingRow, key)
            if row is None:
                session.add(SettingRow(key=key, value=value))
            else:
                row.value = value

    # --------------------------------------------------------------------- setup
    @property
    def setup_running(self) -> bool:
        return self._setup_task is not None and not self._setup_task.done()

    async def setup_status(self) -> dict[str, Any]:
        status = await self.agents.setup_status()
        setup_setting = await self.get_setting("setup")
        status["first_run_complete"] = bool(setup_setting.get("first_run_complete"))
        status["auto_install"] = self.settings.auto_install_agents
        status["running"] = self.setup_running
        status["started_at"] = iso_from_string(setup_setting.get("started_at"))
        status["finished_at"] = iso_from_string(setup_setting.get("finished_at"))
        status["step"] = setup_setting.get("step")
        return status

    async def maybe_start_first_run(self) -> bool:
        setup = await self.get_setting("setup")
        if setup.get("first_run_complete"):
            return False
        status = await self.agents.setup_status()
        if status["state"] == "ready":
            await self.set_setting(
                "setup",
                {
                    "first_run_complete": True,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "step": "ready",
                },
            )
            return False
        return self.start_agent_setup()

    def start_agent_setup(self) -> bool:
        if self.setup_running:
            return False
        self._setup_task = asyncio.create_task(self._run_agent_setup())
        return True

    async def _run_agent_setup(self) -> None:
        await self.set_setting(
            "setup",
            {
                "started_at": datetime.now(UTC).isoformat(),
                "step": "installing_coding_agents",
                "first_run_complete": False,
            },
        )
        try:
            status = await self.agents.first_run_setup()
        except Exception as exc:  # noqa: BLE001 - surface the real error
            logger.exception("Agent setup crashed")
            await self.events.publish(
                "setup.agents.failed", error=str(exc), stage="setup"
            )
            status = await self.agents.setup_status()
            status["error"] = str(exc)
        await self.set_setting(
            "setup",
            {
                **await self.get_setting("setup"),
                "first_run_complete": True,
                "finished_at": datetime.now(UTC).isoformat(),
                "step": status.get("state", "unknown"),
            },
        )
        final = await self.setup_status()
        final["state"] = status.get("state", final["state"])
        await self.events.publish("setup.complete", **final)

    async def wait_for_setup(self) -> dict[str, Any]:
        if self._setup_task is not None:
            with contextlib.suppress(Exception):
                await self._setup_task
        return await self.setup_status()

    # ------------------------------------------------------------------ snapshot
    async def status_snapshot(self) -> dict[str, Any]:
        metrics = self.metrics.snapshot()
        agents = await self.agents.list_agents()
        setup = await self.setup_status()
        browser = await self.browser.health_check()
        counts = await self._counts()
        return {
            "service": "forgemesh-control",
            "version": __version__,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "host": metrics["host"],
            "metrics": metrics,
            "agents": agents,
            "coding_accounts": await self._coding_accounts(),
            "setup": setup,
            "browser": browser,
            "counts": counts,
            "workers": await self._workers(),
            "council": await self.council.summary(),
            "queue": await self.scheduler.snapshot(),
        }

    async def _counts(self) -> dict[str, int]:
        from sqlalchemy import func

        from forgemesh_control.models import (
            HumanActionRow,
            ProjectRow,
            TaskRow,
            WorkerRow,
        )

        async with self.db.session_scope() as session:
            projects = await session.scalar(select(func.count()).select_from(ProjectRow))
            tasks = await session.scalar(select(func.count()).select_from(TaskRow))
            running = await session.scalar(
                select(func.count()).select_from(TaskRow).where(TaskRow.status == "running")
            )
            workers = await session.scalar(select(func.count()).select_from(WorkerRow))
            online = await session.scalar(
                select(func.count()).select_from(WorkerRow).where(WorkerRow.status == "online")
            )
            actions = await session.scalar(
                select(func.count())
                .select_from(HumanActionRow)
                .where(HumanActionRow.status == "pending")
            )
        return {
            "projects": int(projects or 0),
            "tasks": int(tasks or 0),
            "tasks_running": int(running or 0),
            "workers": int(workers or 0),
            "workers_online": int(online or 0),
            "human_actions_pending": int(actions or 0),
        }

    async def _workers(self) -> list[dict[str, Any]]:
        snapshot = await self.mesh.snapshot()
        return list(snapshot["workers"])

    async def _coding_accounts(self) -> list[dict[str, Any]]:
        """Stored CLI account state; nothing here spawns a process.

        A snapshot must stay fast, so a real re-read happens only when the
        dashboard explicitly asks for a verification.
        """
        summaries: list[dict[str, Any]] = []
        for kind in ("opencode", "qoder"):
            try:
                state = await self.agent_auth.account_status(kind)
            except Exception as exc:  # noqa: BLE001 - the snapshot must still build
                logger.warning("Coding account read failed for %s: %s", kind, exc)
                summaries.append(
                    {"kind": kind, "accounts": [], "signed_in": 0, "error": str(exc)}
                )
                continue
            summaries.append(
                {
                    "kind": kind,
                    "accounts": state["accounts"],
                    "signed_in": state["signed_in"],
                    "active_account": state["active_account"],
                    "capability": state["capability"],
                    "session_running": state["running_session"] is not None,
                }
            )
        return summaries

    async def log_tail(self, name: str = "server") -> str:
        path = self.settings.logs_dir / f"{name}.log"
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")[-50_000:]
        except OSError:
            return ""


def iso_from_string(value: Any) -> str | None:
    if not value:
        return None
    return str(value)
