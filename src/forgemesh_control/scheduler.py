"""Task planning, approval gating and execution on the central server.

Two rules shape this module:

* Nothing runs until the implementation gate is approved. The gate is the same
  one the dashboard uses (``ProjectService.assert_ready_for_implementation``),
  so there is no side door around it.
* A council decision can only become tasks if it is scoreable and its open
  objections are accounted for. Missing pieces are returned as blockers, never
  silently skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from forgemesh_control.agents.registry import CentralAgentRegistry
from forgemesh_control.audit import AuditService
from forgemesh_control.config import ControlSettings
from forgemesh_control.council import CouncilService, decision_contract, proposal_details
from forgemesh_control.delivery import DeliveryService
from forgemesh_control.events import EventStore
from forgemesh_control.models import (
    ProjectRow,
    TaskEventRow,
    TaskRow,
    WorkerRow,
    WorkspaceRow,
    iso,
    utcnow,
)
from forgemesh_control.projects import ProjectError, ProjectService
from forgemesh_control.storage import ControlDatabase
from forgemesh_control.workspaces import WorkspaceService

logger = logging.getLogger("forgemesh.control.scheduler")

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
RUNNABLE_STATUSES = {"queued", "failed", "draft"}
MAX_OUTPUT_STORED = 100_000


class SchedulerError(RuntimeError):
    def __init__(self, message: str, *, code: str = "scheduler_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def build_task_prompt(
    *,
    project: dict[str, Any],
    task: dict[str, Any],
    decision: dict[str, Any] | None,
    workspace: str,
) -> str:
    parts = [
        f"You are working inside the ForgeMesh project '{project['name']}' ({project['mode']}).",
        f"Working directory (already your cwd): {workspace}",
        f"TASK: {task['title']}",
    ]
    if task.get("description"):
        parts.append(f"DETAIL:\n{task['description']}")
    if decision:
        parts.append(
            "COUNCIL DECISION THIS COMES FROM:\n"
            f"{decision.get('summary', '')}\n"
            f"Claim: {decision.get('claim', '')}"
        )
        objections = [
            item
            for item in decision.get("disagreements", [])
            if item.get("proposal") == decision.get("id")
        ]
        if objections:
            parts.append(
                "OPEN OBJECTIONS YOU MUST ACCOUNT FOR:\n"
                + "\n".join(
                    f"- [{item.get('severity')}] {item.get('counter_claim', '')[:400]}"
                    for item in objections[:6]
                )
            )
    acceptance = task.get("acceptance") or []
    if acceptance:
        parts.append(
            "ACCEPTANCE CRITERIA (the work is done only when these hold):\n"
            + "\n".join(f"- {item}" for item in acceptance)
        )
    parts.append(
        "CONSTRAINTS:\n"
        "- Change only what this task needs. Do not reformat unrelated files.\n"
        "- Do not invent APIs, credentials or endpoints. If something is unknown, "
        "write it down in your summary instead of guessing.\n"
        "- Do not attempt to bypass login, CAPTCHA or rate limits.\n"
        "- Finish with a summary listing files changed and exactly how to verify them."
    )
    return "\n\n".join(parts)


class TaskScheduler:
    def __init__(
        self,
        settings: ControlSettings,
        db: ControlDatabase,
        events: EventStore,
        audit: AuditService,
        agents: CentralAgentRegistry,
        projects: ProjectService,
        workspaces: WorkspaceService,
        council: CouncilService,
        delivery: DeliveryService,
        agent_accounts: Any | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.events = events
        self.audit = audit
        self.agents = agents
        self.projects = projects
        self.workspaces = workspaces
        self.council = council
        self.delivery = delivery
        self.agent_accounts = agent_accounts
        self._runs: dict[str, asyncio.Task[Any]] = {}
        self._starting: set[str] = set()
        self._queue_loop: asyncio.Task[Any] | None = None
        self._cancel: set[str] = set()
        self._stopping = False

    # ------------------------------------------------------------------ task views
    async def _task(self, task_id: str) -> dict[str, Any]:
        async with self.db.session_scope() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                raise SchedulerError(f"Unknown task: {task_id}", code="not_found")
            worker_name = None
            if row.worker_id:
                worker = await session.get(WorkerRow, row.worker_id)
                worker_name = worker.name if worker else None
            workspace = (
                await session.execute(
                    select(WorkspaceRow)
                    .where(WorkspaceRow.task_id == task_id)
                    .order_by(WorkspaceRow.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()
            return _task_dict(row, worker_name=worker_name, workspace=workspace)

    async def detail(self, task_id: str) -> dict[str, Any]:
        task = await self._task(task_id)
        return {
            **task,
            "log": await self.delivery.task_log(task_id),
            "artifacts": await self.delivery.list_artifacts(task["project_id"]),
            "runs": await self.delivery.list_runs(task["project_id"], limit=10),
            "running": task_id in self._runs and not self._runs[task_id].done(),
        }

    # --------------------------------------------------------------------- planning
    async def plan_from_meeting(
        self,
        *,
        project_id: str,
        meeting_id: str,
        agent_kind: str | None = None,
        max_tasks: int = 12,
    ) -> dict[str, Any]:
        """Turn the winning proposal into queued tasks - only if the decision is usable."""
        meeting = await self.council.get_meeting(meeting_id)
        decision = meeting.get("decision")
        if decision is None:
            raise SchedulerError(
                "This meeting has no recorded decision, so there is nothing to plan from."
            )
        scores = dict(decision.get("scores") or {})
        details = scores.get("proposal_details") or proposal_details(meeting["proposals"])
        contract = decision_contract({**scores, "proposal_details": details})
        if not contract["ok"]:
            raise SchedulerError(
                "Council output is not implementable yet: " + "; ".join(contract["missing"])
            )
        winner = next(
            (
                row
                for row in scores.get("ranking", [])
                if row["proposal_id"] == contract["selected_proposal"]
            ),
            {"final": None, "raters": []},
        )
        proposal = next(item for item in details if item["id"] == contract["selected_proposal"])
        objections = [
            item for item in scores.get("disagreements", []) if item.get("proposal") == proposal["id"]
        ]
        mitigations = [
            item for item in scores.get("mitigations", []) if item.get("proposal") == proposal["id"]
        ]

        steps = proposal.get("steps") or [proposal.get("claim", "")]
        steps = [step for step in steps if step.strip()][: max(1, min(max_tasks, 20))]
        acceptance = proposal.get("acceptance") or []
        if not acceptance:
            raise SchedulerError(
                "The winning proposal carries no acceptance criteria, so the result of "
                "any task would be unverifiable. Ask the council to revise (revisions are "
                "stored per proposal) or record criteria as a human action first."
            )
        if agent_kind:
            await self._require_agent(agent_kind)

        async with self.db.session_scope() as session:
            project = await session.get(ProjectRow, project_id)
            if project is None:
                raise SchedulerError(f"Unknown project: {project_id}", code="not_found")
            created: list[dict[str, Any]] = []
            previous: str | None = None
            for index, step in enumerate(steps, start=1):
                task_id = f"tsk_{uuid.uuid4().hex[:10]}"
                depends = [previous] if previous else []
                session.add(
                    TaskRow(
                        id=task_id,
                        project_id=project_id,
                        title=f"{index}. {step}"[:300],
                        description=(
                            f"Step {index} of {len(steps)} for the council-selected "
                            f"proposal '{proposal['title']}' by {proposal['author']}."
                        ),
                        status="awaiting_approval",
                        priority=2,
                        agent_kind=agent_kind or "",
                        prompt=step,
                        acceptance=acceptance,
                        dependencies=depends,
                        allocation_reason=(
                            f"Ranked first by the weighted rubric "
                            f"(score {winner.get('final')}, {len(winner.get('raters', []))} "
                            f"raters). {len(objections)} documented objection(s), "
                            f"{len(mitigations)} mitigation(s)."
                        ),
                        risk="high" if any(o.get("severity") == "high" for o in objections) else "medium",
                    )
                )
                previous = task_id
                created.append(
                    {
                        "id": task_id,
                        "title": f"{index}. {step}"[:300],
                        "status": "awaiting_approval",
                        "dependencies": depends,
                    }
                )
            project.meta = {
                **(project.meta or {}),
                "council_meeting": meeting_id,
                "selected_proposal": proposal["id"],
            }
        await self.audit.record(
            "tasks.planned", project=project_id, meeting=meeting_id, tasks=len(created)
        )
        await self.events.publish(
            "tasks.planned",
            project_id=project_id,
            meeting_id=meeting_id,
            proposal_id=proposal["id"],
            count=len(created),
            blockers=contract["missing"],
        )
        return {
            "project_id": project_id,
            "meeting_id": meeting_id,
            "proposal": proposal,
            "contract": contract,
            "tasks": created,
            "next": "Approve blueprint, ui_spec and implementation, then queue these tasks.",
        }

    async def request_gate(self, project_id: str, *, summary: str = "") -> dict[str, Any]:
        """Ask the human for the approvals the gate needs - never self-approves."""
        project = await self.projects.get(project_id)
        stages = {stage: state for stage, state in (
            ("blueprint", await self.projects.approval_state(project_id, "blueprint")),
            ("ui_spec", await self.projects.approval_state(project_id, "ui_spec")),
            ("implementation", await self.projects.approval_state(project_id, "implementation")),
        )}
        requested = []
        for stage, state in stages.items():
            if state == "pending":
                continue
            if state == "approved":
                continue
            requested.append(
                await self.projects.request_approval(
                    project_id,
                    stage,
                    summary=summary
                    or f"Approval required before ForgeMesh can act on project {project['name']}.",
                    details={"current_state": state},
                )
            )
        return {"project_id": project_id, "states": stages, "requested": requested}

    async def queue(self, project_id: str, *, task_ids: list[str] | None = None) -> dict[str, Any]:
        """Move planned tasks into the queue - blocked until the gate is approved."""
        try:
            await self.projects.assert_ready_for_implementation(project_id)
        except ProjectError as exc:
            raise SchedulerError(exc.message, code="approval_required") from exc
        moved: list[str] = []
        blocked: list[dict[str, str]] = []
        async with self.db.session_scope() as session:
            statement = select(TaskRow).where(
                TaskRow.project_id == project_id,
                TaskRow.status.in_(["awaiting_approval", "draft", "failed"]),
            )
            rows = (await session.execute(statement)).scalars().all()
            wanted = set(task_ids or [])
            for row in rows:
                if wanted and row.id not in wanted:
                    continue
                if row.dependencies:
                    # A dependency that has not succeeded keeps this task waiting.
                    unmet = []
                    for dependency in row.dependencies:
                        parent = await session.get(TaskRow, dependency)
                        if parent is not None and parent.status != "completed":
                            unmet.append(dependency)
                    if unmet:
                        blocked.append({"id": row.id, "waiting_on": ", ".join(unmet)})
                        continue
                if not row.agent_kind and not row.worker_id:
                    blocked.append({"id": row.id, "waiting_on": "no agent or worker assigned"})
                    continue
                row.status = "queued"
                moved.append(row.id)
        if moved:
            await self.events.publish(
                "tasks.queued", project_id=project_id, count=len(moved), task_ids=moved[:20]
            )
        return {"queued": moved, "blocked": blocked}

    async def set_agent(self, task_id: str, kind: str) -> dict[str, Any]:
        info = await self._require_agent(kind)
        async with self.db.session_scope() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                raise SchedulerError(f"Unknown task: {task_id}", code="not_found")
            row.agent_kind = kind
            row.worker_id = None
        await self.audit.record("task.agent", task=task_id, kind=kind)
        return {**(await self._task(task_id)), "agent": info}

    async def set_worker(self, task_id: str, worker_id: str) -> dict[str, Any]:
        async with self.db.session_scope() as session:
            worker = await session.get(WorkerRow, worker_id)
            if worker is None:
                raise SchedulerError(f"Unknown worker: {worker_id}", code="not_found")
            if worker.revoked_at is not None or not worker.enabled:
                raise SchedulerError(f"Worker {worker.name} is not usable (revoked or disabled).")
            if worker.status != "online":
                raise SchedulerError(
                    f"Worker {worker.name} is {worker.status}; assignment needs an online "
                    "worker that has heartbeated recently."
                )
            row = await session.get(TaskRow, task_id)
            if row is None:
                raise SchedulerError(f"Unknown task: {task_id}", code="not_found")
            project_id = row.project_id
        # A worker executes as soon as it claims, so this is the last place the
        # human approval gate can still stop the run.
        try:
            await self.projects.assert_ready_for_implementation(project_id)
        except ProjectError as exc:
            raise SchedulerError(exc.message, code="approval_required") from exc
        async with self.db.session_scope() as session:
            worker = await session.get(WorkerRow, worker_id)
            row = await session.get(TaskRow, task_id)
            if worker is None or row is None:
                raise SchedulerError("Worker or task disappeared.", code="not_found")
            row.worker_id = worker_id
            row.agent_kind = ""
            row.status = "assigned"
            task_payload = _task_dict(row, worker_name=worker.name)
        await self.events.publish("task.assigned", task_id=task_id, worker_id=worker_id)
        return task_payload

    # -------------------------------------------------------------------- execution
    async def _require_agent(self, kind: str) -> dict[str, Any]:
        info = await self.agents.get_agent(kind)
        if not info.get("enabled", False):
            raise SchedulerError(f"{kind} is disabled in the agent registry.")
        if info.get("availability") != "available":
            raise SchedulerError(
                f"{kind} is not available for coding (availability="
                f"{info.get('availability')}, auth={info.get('auth_status')}). "
                f"{info.get('last_error') or 'No recorded reason.'}"
            )
        if not info.get("path"):
            raise SchedulerError(f"{kind} has no executable path recorded.")
        return info

    def is_running(self, task_id: str) -> bool:
        task = self._runs.get(task_id)
        return task is not None and not task.done()

    async def start(self, task_id: str, *, wait: bool = False) -> dict[str, Any]:
        task = await self._task(task_id)
        # Reserve the task before the first await: two concurrent start clicks
        # must not spawn two agent runs and two commits.
        if self.is_running(task_id) or task_id in self._starting:
            return await self.detail(task_id)
        if task["status"] in TERMINAL_STATUSES and task["status"] not in {
            "failed",
            "interrupted",
        }:
            raise SchedulerError(
                f"Task {task_id} is {task['status']}; create a new task to run it again."
            )
        self._starting.add(task_id)
        try:
            try:
                await self.projects.assert_ready_for_implementation(task["project_id"])
            except ProjectError as exc:
                raise SchedulerError(exc.message, code="approval_required") from exc
            if task["worker_id"]:
                raise SchedulerError(
                    "This task is assigned to a mesh worker; the worker claims it over its "
                    "own connection rather than running on the server."
                )
            kind = task["agent_kind"]
            if not kind:
                kind = await self._pick_agent()
                info = await self._require_agent(kind)
            else:
                info = await self._require_agent(kind)
            if not task["prompt"]:
                raise SchedulerError("Task has no prompt - the plan did not carry one.")
            self._cancel.discard(task_id)
            run = asyncio.create_task(self._execute(task_id, kind, info))
            self._runs[task_id] = run
            run.add_done_callback(lambda _t: self._runs.pop(task_id, None))
        finally:
            self._starting.discard(task_id)
        if wait:
            with contextlib.suppress(Exception):
                await run
        return await self.detail(task_id)

    async def _pick_agent(self) -> str:
        """First central agent that is genuinely available, ordered by the registry."""
        for kind in ("opencode", "qoder"):
            try:
                await self._require_agent(kind)
                return kind
            except SchedulerError:
                continue
        raise SchedulerError(
            "No coding agent is available on the central server. Verify an agent in the "
            "Agents page (install, verify and authentication are separate steps)."
        )

    async def cancel(self, task_id: str) -> dict[str, Any]:
        if not self.is_running(task_id):
            async with self.db.session_scope() as session:
                row = await session.get(TaskRow, task_id)
                if row is None:
                    raise SchedulerError(f"Unknown task: {task_id}", code="not_found")
                if row.status not in TERMINAL_STATUSES:
                    row.status = "cancelled"
                    row.ended_at = utcnow()
            await self.events.publish("task.cancelled", task_id=task_id)
            return await self._task(task_id)
        self._cancel.add(task_id)
        task = self._runs.get(task_id)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # Cancellation raises inside _execute before its success path runs, so
        # the row must be closed here. A task that finished just before the
        # cancel keeps its terminal status.
        current = await self._task(task_id)
        if current["status"] not in TERMINAL_STATUSES:
            await self._set_task(
                task_id,
                status="cancelled",
                ended_at=utcnow(),
                error="Cancelled by operator",
            )
        self._cancel.discard(task_id)
        await self.events.publish("task.cancelled", task_id=task_id)
        return await self._task(task_id)

    async def _log_event(
        self, task_id: str, kind: str, message: str, data: dict[str, Any] | None = None
    ) -> None:
        async with self.db.session_scope() as session:
            session.add(
                TaskEventRow(
                    task_id=task_id, kind=kind, message=message[:4000], data=data or {}
                )
            )

    async def _set_task(self, task_id: str, **fields: Any) -> None:
        async with self.db.session_scope() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                return
            for key, value in fields.items():
                setattr(row, key, value)

    async def _execute(self, task_id: str, kind: str, info: dict[str, Any]) -> dict[str, Any]:
        task = await self._task(task_id)
        project = await self.projects.get(task["project_id"])
        started = datetime.now(UTC)
        await self._set_task(
            task_id, status="running", started_at=started, progress=10, attempts=task["attempts"] + 1
        )
        await self._log_event(task_id, "status", f"Running on central agent {kind} ({info['version']})")
        await self.events.publish(
            "task.started", task_id=task_id, agent=kind, project_id=task["project_id"]
        )
        workspace_path: Path | None = None
        try:
            workspace_path = await self._prepare_workspace(project, task)
        except Exception as exc:  # noqa: BLE001 - a failed workspace is a real failure
            message = f"Workspace preparation failed: {exc}"
            await self._log_event(task_id, "error", message)
            await self._set_task(
                task_id, status="failed", error=message, ended_at=utcnow(), progress=10
            )
            await self.events.publish("task.failed", task_id=task_id, error=message)
            return {"ok": False, "error": message}

        decision = await self._decision_for(project)
        prompt = build_task_prompt(
            project=project, task=task, decision=decision, workspace=str(workspace_path)
        )
        adapter = self.agents.adapter(kind, info["path"])
        account: dict[str, Any] | None = None
        if self.agent_accounts is not None:
            try:
                account = await self.agent_accounts.active_account(kind)
            except Exception as exc:  # noqa: BLE001 - fall back to the CLI's own default
                logger.warning("Could not resolve the active agent account: %s", exc)
        if account:
            await self._log_event(
                task_id, "status", f"Using coding-agent account '{account['name']}'"
            )
        buffer: list[str] = []
        state = {"last_flush": asyncio.get_running_loop().time(), "lines": 0}

        async def on_line(line: str) -> None:
            state["lines"] += 1
            stripped = line.strip()
            if stripped:
                buffer.append(stripped)
            now = asyncio.get_running_loop().time()
            if len(buffer) >= 40 or (buffer and now - state["last_flush"] > 3.0):
                await self._log_event(
                    task_id, "output", "\n".join(buffer)[:4000], {"lines": len(buffer)}
                )
                buffer.clear()
                state["last_flush"] = now
            if state["lines"] % 200 == 0:
                await self.events.publish(
                    "task.output", task_id=task_id, lines=state["lines"], important=False
                )

        try:
            result = await adapter.submit_task(
                prompt,
                cwd=workspace_path,
                model=task.get("model_id") or None,
                files=[Path(item) for item in task.get("files", []) if Path(item).is_file()],
                on_line=on_line,
                timeout_seconds=self.settings.agent_timeout_seconds,
                account=account,
            )
        except Exception as exc:  # noqa: BLE001 - surface the crash as a task failure
            logger.exception("Agent run crashed for task %s", task_id)
            message = f"{type(exc).__name__}: {exc}"
            await self._set_task(
                task_id, status="failed", error=message, ended_at=utcnow(), progress=50
            )
            await self.events.publish("task.failed", task_id=task_id, error=message)
            return {"ok": False, "error": message}
        finally:
            if buffer:
                await self._log_event(task_id, "output", "\n".join(buffer)[:4000])

        # The adapter reports the model that really ran: it can differ from the
        # one selected, so the task log says which model answered.
        if result.model:
            await self._log_event(task_id, "status", f"Model used: {result.model}")

        if task_id in self._cancel:
            self._cancel.discard(task_id)
            await self._set_task(
                task_id, status="cancelled", ended_at=utcnow(), progress=50,
                output=result.text[:MAX_OUTPUT_STORED], error="Cancelled by operator",
            )
            await self.events.publish("task.cancelled", task_id=task_id)
            return {"ok": False, "cancelled": True}

        commit: dict[str, Any] | None = None
        artifacts: dict[str, Any] | None = None
        follow_up_error: str | None = None
        if result.ok:
            await self._set_task(task_id, progress=85)
            try:
                commit = await self.workspaces.commit(
                    str(workspace_path), f"forgemesh: {task['title'][:120]}"
                )
                artifacts = await self.delivery.collect_artifacts(
                    task["project_id"], task_id=task_id
                )
            except Exception as exc:  # noqa: BLE001 - commit problems are reported, not hidden
                follow_up_error = f"Agent finished but post-processing failed: {exc}"
                await self._log_event(task_id, "error", follow_up_error)

        status = "completed" if result.ok and follow_up_error is None else "failed"
        await self._set_task(
            task_id,
            status=status,
            progress=100 if status == "completed" else 90,
            output=result.text[:MAX_OUTPUT_STORED],
            error=result.error or follow_up_error,
            ended_at=utcnow(),
        )
        await self._log_event(
            task_id,
            "result",
            f"exit={result.exit_code} ok={result.ok} duration={result.duration_ms}ms",
            {
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "session_id": result.session_id,
                "commit": (commit or {}).get("commit"),
                "artifacts": (artifacts or {}).get("collected"),
            },
        )
        await self.audit.record(
            "task.finished", task=task_id, status=status, agent=kind, exit=result.exit_code
        )
        await self.events.publish(
            "task.finished",
            task_id=task_id,
            status=status,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            commit=(commit or {}).get("commit"),
        )
        return {"ok": result.ok, "status": status, "exit_code": result.exit_code}

    async def _prepare_workspace(self, project: dict[str, Any], task: dict[str, Any]) -> Path:
        raw = project.get("workspace_path") or str(self.settings.workspaces / project["id"])
        path = Path(raw)
        path.mkdir(parents=True, exist_ok=True)
        if project.get("git_repo") and not (path / ".git").is_dir():
            await self.workspaces.clone(project["git_repo"], str(path))
        else:
            await self.workspaces.init_repo(str(path))
        branch = self.workspaces.branch_name(task["id"], task["title"])
        await self.workspaces.create_branch(str(path), branch)
        async with self.db.session_scope() as session:
            session.add(
                WorkspaceRow(
                    id=f"wsp_{uuid.uuid4().hex[:10]}",
                    project_id=project["id"],
                    task_id=task["id"],
                    path=str(path),
                    branch=branch,
                    status="active",
                )
            )
        await self._log_event(task_id=task["id"], kind="workspace", message=f"{path} @ {branch}")
        return path

    async def _decision_for(self, project: dict[str, Any]) -> dict[str, Any] | None:
        meeting_id = (project.get("meta") or {}).get("council_meeting")
        proposal_id = (project.get("meta") or {}).get("selected_proposal")
        if not meeting_id:
            return None
        try:
            meeting = await self.council.get_meeting(meeting_id)
        except Exception as exc:  # noqa: BLE001 - prompt building must not break on bad state
            logger.warning("Could not load council meeting %s: %s", meeting_id, exc)
            return None
        decision = meeting.get("decision")
        if decision is None:
            return None
        scores = decision.get("scores") or {}
        winner = next(
            (row for row in scores.get("ranking", []) if row.get("proposal_id") == proposal_id),
            (scores.get("ranking") or [None])[0],
        )
        if winner is None:
            return None
        return {
            "id": winner.get("proposal_id"),
            "title": winner.get("title"),
            "claim": next(
                (
                    item.get("claim")
                    for item in (scores.get("proposal_details") or [])
                    if item.get("id") == winner.get("proposal_id")
                ),
                "",
            ),
            "summary": decision.get("summary"),
            "disagreements": decision.get("disagreements") or [],
        }

    # --------------------------------------------------------------------- queue loop
    def queue_running(self) -> bool:
        return self._queue_loop is not None and not self._queue_loop.done()

    async def start_queue(self) -> dict[str, Any]:
        if self.queue_running():
            return {"running": True, "changed": False}
        self._stopping = False
        self._queue_loop = asyncio.create_task(self._queue_worker())
        await self.events.publish("scheduler.started")
        return {"running": True, "changed": True}

    async def reconcile(self) -> dict[str, int]:
        """Close rows a previous process left in flight.

        Local agent runs cannot survive a restart, so they become
        ``interrupted``. Worker-assigned tasks stay assigned: the worker may
        still be running them and reports its own result. Workers are shown
        offline until their next heartbeat.
        """
        async with self.db.session_scope() as session:
            running = (
                (
                    await session.execute(
                        select(TaskRow).where(TaskRow.status == "running")
                    )
                )
                .scalars()
                .all()
            )
            for row in running:
                row.status = "interrupted"
                row.error = (
                    "The control server restarted while this task was running; "
                    "the agent run was interrupted."
                )
                row.ended_at = utcnow()
            workers = (
                (
                    await session.execute(
                        select(WorkerRow).where(WorkerRow.status != "offline")
                    )
                )
                .scalars()
                .all()
            )
            for worker in workers:
                worker.status = "offline"
        if running:
            await self.events.publish("scheduler.reconciled", tasks=len(running))
        return {"tasks": len(running), "workers": len(workers)}

    async def shutdown(self) -> dict[str, Any]:
        """Stop taking work and interrupt live runs, recording the interruption."""
        stopped = await self.stop_queue()
        interrupted = 0
        for task_id, handle in list(self._runs.items()):
            if handle.done():
                continue
            self._cancel.add(task_id)
            handle.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await handle
            await self._set_task(
                task_id,
                status="interrupted",
                error="The control server shut down while this task was running.",
                ended_at=utcnow(),
            )
            interrupted += 1
        if interrupted:
            await self.events.publish("scheduler.interrupted", tasks=interrupted)
        return {**stopped, "interrupted": interrupted}

    async def stop_queue(self) -> dict[str, Any]:
        self._stopping = True
        if self._queue_loop is not None:
            self._queue_loop.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._queue_loop
            self._queue_loop = None
        await self.events.publish("scheduler.stopped")
        return {"running": False}

    async def _queue_worker(self) -> None:
        while not self._stopping:
            try:
                await self._drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive, report honestly
                logger.exception("Scheduler loop error")
                await self.events.publish("scheduler.error", error=str(exc))
            await asyncio.sleep(3.0)

    async def _drain_once(self) -> int:
        if len(self._runs) >= self.settings.agent_max_concurrency:
            return 0
        async with self.db.session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(TaskRow)
                        .where(TaskRow.status == "queued", TaskRow.worker_id.is_(None))
                        .order_by(TaskRow.priority, TaskRow.created_at)
                        .limit(5)
                    )
                )
                .scalars()
                .all()
            )
            candidates = [row.id for row in rows]
        started = 0
        for task_id in candidates:
            if len(self._runs) >= self.settings.agent_max_concurrency:
                break
            try:
                await self.start(task_id)
                started += 1
            except SchedulerError as exc:
                await self._set_task(task_id, status="blocked", error=exc.message)
                await self.events.publish(
                    "task.blocked", task_id=task_id, reason=exc.message, code=exc.code
                )
        return started

    async def snapshot(self) -> dict[str, Any]:
        async with self.db.session_scope() as session:
            rows = (
                await session.execute(
                    select(TaskRow.status, func.count(TaskRow.id)).group_by(TaskRow.status)
                )
            ).all()
        return {
            "by_status": {status: int(total) for status, total in rows},
            "queue_loop": self.queue_running(),
            "active_runs": len([handle for handle in self._runs.values() if not handle.done()]),
            "max_concurrency": self.settings.agent_max_concurrency,
        }


def _task_dict(
    row: TaskRow,
    *,
    worker_name: str | None = None,
    workspace: WorkspaceRow | None = None,
) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "title": row.title,
        "description": row.description,
        "status": row.status,
        "priority": row.priority,
        "worker_id": row.worker_id,
        "worker_name": worker_name,
        "agent_kind": row.agent_kind,
        "model_id": row.model_id,
        "prompt": row.prompt,
        "acceptance": row.acceptance or [],
        "dependencies": row.dependencies or [],
        "files": row.files or [],
        "allocation_reason": row.allocation_reason,
        "risk": row.risk,
        "progress": row.progress,
        "attempts": row.attempts,
        "output": (row.output or "")[-8000:],
        "error": row.error,
        "workspace": (
            {"path": workspace.path, "branch": workspace.branch, "status": workspace.status}
            if workspace
            else None
        ),
        "started_at": iso(row.started_at),
        "ended_at": iso(row.ended_at),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }
