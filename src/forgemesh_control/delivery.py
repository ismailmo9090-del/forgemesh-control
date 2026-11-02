"""Verification, artifacts and delivery reports.

A test run is a real subprocess on the central server. Its exit code is the
only thing that decides ``passed``; nothing here upgrades a failure into a
success, and an unrun check is reported as never run.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shlex
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from forgemesh_browser.security.permissions import ensure_private_dir, secure_file
from forgemesh_control.audit import AuditService
from forgemesh_control.config import ControlSettings
from forgemesh_control.events import EventStore
from forgemesh_control.models import (
    ArtifactRow,
    BlueprintRow,
    DecisionRow,
    MeetingRow,
    ProjectRow,
    ReportRow,
    TaskEventRow,
    TaskRow,
    TestRunRow,
    iso,
)
from forgemesh_control.storage import ControlDatabase
from forgemesh_control.workspaces import WorkspaceService

logger = logging.getLogger("forgemesh.control.delivery")

# Verification commands are allowlisted: the dashboard must not become a
# general remote shell. Each entry is a literal argv prefix.
_REPORT_KIND = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

ALLOWED_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("pytest",),
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("npm", "test"),
    ("npm", "run", "test"),
    ("npm", "run", "build"),
    ("npm", "run", "lint"),
    ("npx", "tsc"),
    ("npx", "eslint"),
    ("node", "--test"),
    ("make", "test"),
    ("go", "test"),
    ("cargo", "test"),
    ("ruff", "check"),
    ("git", "status"),
    ("git", "diff"),
)
MAX_OUTPUT_CHARS = 200_000


class DeliveryError(RuntimeError):
    def __init__(self, message: str, *, code: str = "delivery_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def is_allowed_command(argv: list[str]) -> bool:
    for prefix in ALLOWED_COMMANDS:
        if argv[: len(prefix)] == list(prefix):
            return True
    return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DeliveryService:
    def __init__(
        self,
        settings: ControlSettings,
        db: ControlDatabase,
        events: EventStore,
        audit: AuditService,
        workspaces: WorkspaceService,
    ) -> None:
        self.settings = settings
        self.db = db
        self.events = events
        self.audit = audit
        self.workspaces = workspaces

    async def _project(self, session, project_id: str) -> ProjectRow:
        row = await session.get(ProjectRow, project_id)
        if row is None:
            raise DeliveryError(f"Unknown project: {project_id}", code="not_found")
        return row

    def _project_root(self, project: ProjectRow) -> Path:
        if project.workspace_path:
            return Path(project.workspace_path)
        return self.settings.workspaces / project.id

    # ------------------------------------------------------------------ commands
    async def run_command(
        self,
        project_id: str,
        command: str,
        *,
        task_id: str | None = None,
        cwd: str | None = None,
        timeout_seconds: int = 900,
    ) -> dict[str, Any]:
        argv = shlex.split(command)
        if not argv:
            raise DeliveryError("Empty command.")
        if not is_allowed_command(argv):
            raise DeliveryError(
                f"Command {argv[0:3]} is not in the verification allowlist. "
                "Allowed: " + ", ".join(" ".join(prefix) for prefix in ALLOWED_COMMANDS)
            )
        async with self.db.session_scope() as session:
            project = await self._project(session, project_id)
        root = self._project_root(project).resolve()
        target = (Path(cwd).expanduser() if cwd else root).resolve()
        if not target.is_relative_to(root):
            raise DeliveryError(
                f"Working directory {target} is outside the project workspace {root}."
            )
        if not target.is_dir():
            raise DeliveryError(f"Working directory {target} does not exist.")

        run_id = f"run_{uuid.uuid4().hex[:10]}"
        started = datetime.now(UTC)
        await self._record_run(
            run_id, project_id, task_id, command, status="running", output="", duration_ms=0
        )
        await self.events.publish(
            "testrun.started", run_id=run_id, project_id=project_id, command=command
        )
        output, status, duration_ms = await self._execute(argv, cwd=target, command=command)
        finished = datetime.now(UTC)
        payload = {
            "id": run_id,
            "project_id": project_id,
            "task_id": task_id,
            "command": command,
            "status": status,
            "output": output,
            "duration_ms": duration_ms,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "cwd": str(target),
        }
        await self._update_run(run_id, status=status, output=output, duration_ms=duration_ms)
        await self.audit.record("testrun.complete", run=run_id, status=status)
        await self.events.publish(
            "testrun.finished", run_id=run_id, status=status, duration_ms=duration_ms
        )
        return payload

    async def _execute(
        self, argv: list[str], *, cwd: Path, command: str
    ) -> tuple[str, str, int]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return (
                f"$ {command}\nExecutable not found: {argv[0]}",
                "error",
                int((loop.time() - started) * 1000),
            )
        except PermissionError as exc:
            return f"$ {command}\n{exc}", "error", int((loop.time() - started) * 1000)
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.command_timeout_seconds
            )
        except TimeoutError:
            process.kill()
            with_suppress = asyncio.gather(process.wait(), return_exceptions=True)
            await with_suppress
            return (
                f"$ {command}\nTimed out after {self.settings.command_timeout_seconds}s",
                "timeout",
                int((loop.time() - started) * 1000),
            )
        text = stdout.decode(errors="replace")[-MAX_OUTPUT_CHARS:]
        code = process.returncode if process.returncode is not None else -1
        status = "passed" if code == 0 else "failed"
        return f"$ {command}\nexit={code}\n{text}", status, int((loop.time() - started) * 1000)

    async def _record_run(
        self,
        run_id: str,
        project_id: str,
        task_id: str | None,
        command: str,
        *,
        status: str,
        output: str,
        duration_ms: int,
    ) -> None:
        async with self.db.session_scope() as session:
            session.add(
                TestRunRow(
                    id=run_id,
                    project_id=project_id,
                    task_id=task_id,
                    command=command,
                    status=status,
                    output=output,
                    duration_ms=duration_ms,
                )
            )

    async def _update_run(
        self, run_id: str, *, status: str, output: str, duration_ms: int
    ) -> None:
        async with self.db.session_scope() as session:
            row = await session.get(TestRunRow, run_id)
            if row is None:
                return
            row.status = status
            row.output = output
            row.duration_ms = duration_ms

    async def list_runs(self, project_id: str | None = None, limit: int = 50) -> list[dict]:
        async with self.db.session_scope() as session:
            statement = select(TestRunRow).order_by(TestRunRow.created_at.desc()).limit(limit)
            if project_id:
                statement = statement.where(TestRunRow.project_id == project_id)
            rows = (await session.execute(statement)).scalars().all()
        return [
            {
                "id": row.id,
                "project_id": row.project_id,
                "task_id": row.task_id,
                "command": row.command,
                "status": row.status,
                "duration_ms": row.duration_ms,
                "created_at": iso(row.created_at),
                "output_excerpt": (row.output or "")[-4000:],
            }
            for row in rows
        ]

    # ----------------------------------------------------------------- artifacts
    async def collect_artifacts(
        self, project_id: str, *, task_id: str | None = None
    ) -> dict[str, Any]:
        """Hash the files git reports as changed inside the project workspace."""
        async with self.db.session_scope() as session:
            project = await self._project(session, project_id)
        root = self._project_root(project)
        if not root.is_dir():
            raise DeliveryError(f"Workspace {root} does not exist yet.")
        changed: list[str] = []
        detail: str | None = None
        try:
            state = await self.workspaces.status(root, required=False)
            changed = [line[3:].strip() for line in state.get("changes", []) if line.strip()]
            detail = state.get("error")
            if not changed and state.get("head"):
                # The scheduler commits before it collects, so the delivered work
                # is in HEAD rather than the working tree.
                changed = await self.workspaces.last_commit_files(root)
        except Exception as exc:  # noqa: BLE001 - artifact collection stays honest
            detail = str(exc)
        collected = 0
        skipped = 0
        for relative in changed:
            path = (root / relative).resolve()
            if root not in path.parents or not path.is_file():
                skipped += 1
                continue
            if path.stat().st_size > self.settings.max_archive_bytes:
                skipped += 1
                continue
            try:
                digest = sha256_file(path)
            except OSError as exc:
                skipped += 1
                detail = f"{detail or ''} hash failed for {relative}: {exc}"
                continue
            async with self.db.session_scope() as session:
                existing = (
                    await session.execute(
                        select(ArtifactRow).where(
                            ArtifactRow.project_id == project_id, ArtifactRow.path == str(path)
                        )
                    )
                ).scalars().first()
                if existing is not None and existing.sha256 == digest:
                    skipped += 1
                    continue
                if existing is not None:
                    await session.delete(existing)
                session.add(
                    ArtifactRow(
                        id=f"art_{uuid.uuid4().hex[:10]}",
                        project_id=project_id,
                        task_id=task_id,
                        kind="workspace_file",
                        name=relative[:300],
                        path=str(path),
                        sha256=digest,
                        size=path.stat().st_size,
                        verified=True,
                    )
                )
            collected += 1
        await self.events.publish(
            "artifacts.collected",
            project_id=project_id,
            task_id=task_id,
            collected=collected,
            skipped=skipped,
            important=False,
        )
        return {
            "collected": collected,
            "skipped": skipped,
            "changed": len(changed),
            "note": detail,
            "artifacts": await self.list_artifacts(project_id),
        }

    async def list_artifacts(self, project_id: str, *, limit: int = 200) -> list[dict]:
        async with self.db.session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(ArtifactRow)
                        .where(ArtifactRow.project_id == project_id)
                        .order_by(ArtifactRow.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [
            {
                "id": row.id,
                "task_id": row.task_id,
                "kind": row.kind,
                "name": row.name,
                "path": row.path,
                "sha256": row.sha256,
                "size": row.size,
                "verified": row.verified,
                "created_at": iso(row.created_at),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------- reports
    async def write_report(self, project_id: str, kind: str = "summary") -> dict[str, Any]:
        # Defence in depth: the HTTP model validates too, but this service is
        # the component that turns the value into a filename.
        if not _REPORT_KIND.match(kind or ""):
            raise DeliveryError(
                "Report kind must be a short slug (a-z, 0-9, underscore or dash)."
            )
        async with self.db.session_scope() as session:
            project = await self._project(session, project_id)
            blueprint = (
                await session.execute(
                    select(BlueprintRow)
                    .where(BlueprintRow.project_id == project_id)
                    .order_by(BlueprintRow.version.desc())
                    .limit(1)
                )
            ).scalars().first()
            meetings = (
                (
                    await session.execute(
                        select(MeetingRow)
                        .where(MeetingRow.project_id == project_id)
                        .order_by(MeetingRow.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            tasks = (
                (
                    await session.execute(
                        select(TaskRow)
                        .where(TaskRow.project_id == project_id)
                        .order_by(TaskRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
            runs = (
                (
                    await session.execute(
                        select(TestRunRow)
                        .where(TestRunRow.project_id == project_id)
                        .order_by(TestRunRow.created_at.desc())
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )
            artifacts = (
                (
                    await session.execute(
                        select(ArtifactRow).where(ArtifactRow.project_id == project_id)
                    )
                )
                .scalars()
                .all()
            )
            decisions: list[DecisionRow] = []
            for meeting in meetings:
                found = (
                    await session.execute(
                        select(DecisionRow)
                        .where(DecisionRow.meeting_id == meeting.id)
                        .order_by(DecisionRow.id.desc())
                        .limit(1)
                    )
                ).scalars().first()
                if found is not None:
                    decisions.append(found)

        stamp = datetime.now(UTC)
        lines = [
            f"# {project.name} - {kind} report",
            f"Generated {stamp.isoformat()} by ForgeMesh Control.",
            "",
            f"Mode: {project.mode} | Status: {project.status} | Progress: {project.progress}%",
            f"Workspace: {project.workspace_path or 'not created'}",
            "",
            "## Council",
        ]
        if not meetings:
            lines.append("No council meeting has run for this project.")
        for meeting in meetings:
            decision = next((item for item in decisions if item.meeting_id == meeting.id), None)
            lines.append(
                f"- {meeting.id} ({meeting.mode}, {meeting.status}, "
                f"round {meeting.current_round}/{meeting.rounds_total}): {meeting.agenda[:120]}"
            )
            if decision is not None:
                lines.append(f"  Decision: {decision.summary}")
                for objection in (decision.disagreements or [])[:10]:
                    lines.append(
                        f"  Disagreement [{objection.get('severity')}]: "
                        f"{objection.get('counter_claim', '')[:200]}"
                    )
        lines += ["", "## Tasks"]
        if not tasks:
            lines.append("No tasks exist yet.")
        for task in tasks:
            lines.append(
                f"- {task.id} [{task.status}] {task.title} "
                f"(agent={task.agent_kind or 'unassigned'}, attempts={task.attempts})"
            )
            for acceptance in (task.acceptance or [])[:8]:
                lines.append(f"    - accepts: {acceptance[:200]}")
            if task.error:
                lines.append(f"    - error: {task.error[:400]}")
        lines += ["", "## Verification"]
        if not runs:
            lines.append("No verification command has been run.")
        for run in runs:
            lines.append(
                f"- {run.id} [{run.status}] `{run.command}` in {run.duration_ms}ms "
                f"({iso(run.created_at)})"
            )
        lines += ["", "## Artifacts"]
        if not artifacts:
            lines.append("No artifacts collected.")
        for artifact in artifacts:
            lines.append(
                f"- {artifact.name} ({artifact.size} bytes, sha256={artifact.sha256[:16]}…, "
                f"verified={artifact.verified})"
            )
        if blueprint is not None:
            lines += ["", f"## Blueprint v{blueprint.version}"]
            content = blueprint.content or {}
            lines.append(f"Selected: {content.get('selected', {}).get('selected_title') or 'n/a'}")
        body = "\n".join(lines) + "\n"

        directory = self.settings.reports_dir / project_id
        ensure_private_dir(directory)
        path = directory / f"{kind}-{stamp.strftime('%Y%m%dT%H%M%SZ')}.md"
        path.write_text(body, encoding="utf-8")
        secure_file(path)
        async with self.db.session_scope() as session:
            session.add(
                ReportRow(
                    id=f"rep_{uuid.uuid4().hex[:10]}",
                    project_id=project_id,
                    kind=kind,
                    path=str(path),
                )
            )
        await self.audit.record("report.write", project=project_id, kind=kind)
        await self.events.publish(
            "report.written", project_id=project_id, kind=kind, path=str(path)
        )
        return {"path": str(path), "kind": kind, "chars": len(body), "created_at": stamp.isoformat()}

    async def list_reports(self, project_id: str | None = None, limit: int = 50) -> list[dict]:
        async with self.db.session_scope() as session:
            statement = select(ReportRow).order_by(ReportRow.created_at.desc()).limit(limit)
            if project_id:
                statement = statement.where(ReportRow.project_id == project_id)
            rows = (await session.execute(statement)).scalars().all()
        return [
            {
                "id": row.id,
                "project_id": row.project_id,
                "kind": row.kind,
                "path": row.path,
                "created_at": iso(row.created_at),
            }
            for row in rows
        ]

    async def task_log(self, task_id: str, limit: int = 300) -> list[dict[str, Any]]:
        async with self.db.session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(TaskEventRow)
                        .where(TaskEventRow.task_id == task_id)
                        .order_by(TaskEventRow.id.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [
            {
                "id": row.id,
                "kind": row.kind,
                "message": row.message,
                "data": row.data or {},
                "created_at": iso(row.created_at),
            }
            for row in reversed(list(rows))
        ]
