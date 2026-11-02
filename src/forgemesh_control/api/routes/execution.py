"""Execution endpoints: planning, queueing, agent runs, git workspaces, delivery.

Every response reflects stored state or a real subprocess result. An operation
that the approval gate forbids answers with the gate's own wording, not a
generic error.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from forgemesh_control.api.deps import get_runtime

router = APIRouter(prefix="/api", tags=["execution"])


class PlanBody(BaseModel):
    project_id: str
    meeting_id: str
    agent_kind: str | None = None
    max_tasks: int = Field(default=12, ge=1, le=20)


class GateBody(BaseModel):
    summary: str = ""


class QueueBody(BaseModel):
    task_ids: list[str] = Field(default_factory=list)


class AgentBody(BaseModel):
    kind: str


class WorkerBody(BaseModel):
    worker_id: str


class CommandBody(BaseModel):
    project_id: str
    command: str
    task_id: str | None = None
    cwd: str | None = None


class WorkspaceInitBody(BaseModel):
    path: str
    default_branch: str = "main"


class CloneBody(BaseModel):
    path: str
    remote: str


class BranchBody(BaseModel):
    path: str
    branch: str
    base: str | None = None


class CommitBody(BaseModel):
    path: str
    message: str
    files: list[str] = Field(default_factory=list)


class MergeBody(BaseModel):
    path: str
    branch: str
    into: str | None = None
    squash: bool = False


class ReportBody(BaseModel):
    project_id: str
    # The kind becomes a filename; only a short slug is accepted.
    kind: str = Field(default="summary", pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")


class ArtifactsBody(BaseModel):
    project_id: str
    task_id: str | None = None


# --------------------------------------------------------------------- scheduler
@router.post("/scheduler/plan")
async def plan(body: PlanBody, request: Request) -> dict[str, Any]:
    return await get_runtime(request).scheduler.plan_from_meeting(
        project_id=body.project_id,
        meeting_id=body.meeting_id,
        agent_kind=body.agent_kind,
        max_tasks=body.max_tasks,
    )


@router.get("/scheduler/snapshot")
async def scheduler_snapshot(request: Request) -> dict[str, Any]:
    return await get_runtime(request).scheduler.snapshot()


@router.post("/scheduler/queue/start")
async def scheduler_queue_start(request: Request) -> dict[str, Any]:
    return await get_runtime(request).scheduler.start_queue()


@router.post("/scheduler/queue/stop")
async def scheduler_queue_stop(request: Request) -> dict[str, Any]:
    return await get_runtime(request).scheduler.stop_queue()


@router.post("/projects/{project_id}/gate")
async def request_gate(project_id: str, body: GateBody, request: Request) -> dict[str, Any]:
    return await get_runtime(request).scheduler.request_gate(project_id, summary=body.summary)


@router.post("/projects/{project_id}/tasks/queue")
async def queue_project_tasks(
    project_id: str, body: QueueBody, request: Request
) -> dict[str, Any]:
    return await get_runtime(request).scheduler.queue(
        project_id, task_ids=body.task_ids or None
    )


@router.get("/tasks/{task_id}")
async def task_detail(task_id: str, request: Request) -> dict[str, Any]:
    return await get_runtime(request).scheduler.detail(task_id)


@router.post("/tasks/{task_id}/start")
async def start_task(task_id: str, request: Request, wait: bool = False) -> dict[str, Any]:
    return await get_runtime(request).scheduler.start(task_id, wait=wait)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request) -> dict[str, Any]:
    return await get_runtime(request).scheduler.cancel(task_id)


@router.post("/tasks/{task_id}/agent")
async def set_task_agent(task_id: str, body: AgentBody, request: Request) -> dict[str, Any]:
    return await get_runtime(request).scheduler.set_agent(task_id, body.kind)


@router.post("/tasks/{task_id}/worker")
async def set_task_worker(task_id: str, body: WorkerBody, request: Request) -> dict[str, Any]:
    return await get_runtime(request).scheduler.set_worker(task_id, body.worker_id)


# --------------------------------------------------------------------- workspaces
@router.get("/workspaces/git")
async def git_probe(request: Request) -> dict[str, Any]:
    return await get_runtime(request).workspaces.probe_git()


@router.post("/workspaces/init")
async def workspace_init(body: WorkspaceInitBody, request: Request) -> dict[str, Any]:
    return await get_runtime(request).workspaces.init_repo(
        body.path, default_branch=body.default_branch
    )


@router.post("/workspaces/clone")
async def workspace_clone(body: CloneBody, request: Request) -> dict[str, Any]:
    return await get_runtime(request).workspaces.clone(body.remote, body.path)


@router.post("/workspaces/branch")
async def workspace_branch(body: BranchBody, request: Request) -> dict[str, Any]:
    return await get_runtime(request).workspaces.create_branch(
        body.path, body.branch, base=body.base
    )


@router.get("/workspaces/status")
async def workspace_status(request: Request, path: str = Query(min_length=1)) -> dict[str, Any]:
    return await get_runtime(request).workspaces.status(path, required=False)


@router.get("/workspaces/diff")
async def workspace_diff(request: Request, path: str = Query(min_length=1)) -> dict[str, Any]:
    return await get_runtime(request).workspaces.diff_stat(path)


@router.get("/workspaces/log")
async def workspace_log(
    request: Request, path: str = Query(min_length=1), limit: int = Query(20, ge=1, le=100)
) -> dict[str, Any]:
    return {"entries": await get_runtime(request).workspaces.log(path, limit=limit)}


@router.post("/workspaces/commit")
async def workspace_commit(body: CommitBody, request: Request) -> dict[str, Any]:
    return await get_runtime(request).workspaces.commit(
        body.path, body.message, files=body.files or None
    )


@router.post("/workspaces/merge")
async def workspace_merge(body: MergeBody, request: Request) -> dict[str, Any]:
    return await get_runtime(request).workspaces.merge(
        body.path, body.branch, into=body.into, squash=body.squash
    )


# ----------------------------------------------------------------------- delivery
@router.post("/delivery/run")
async def delivery_run(body: CommandBody, request: Request) -> dict[str, Any]:
    return await get_runtime(request).delivery.run_command(
        body.project_id, body.command, task_id=body.task_id, cwd=body.cwd
    )


@router.get("/delivery/runs")
async def delivery_runs(
    request: Request,
    project_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    return {"runs": await get_runtime(request).delivery.list_runs(project_id, limit=limit)}


@router.post("/delivery/artifacts")
async def delivery_artifacts(body: ArtifactsBody, request: Request) -> dict[str, Any]:
    return await get_runtime(request).delivery.collect_artifacts(
        body.project_id, task_id=body.task_id
    )


@router.get("/delivery/artifacts")
async def delivery_artifact_list(
    request: Request, project_id: str = Query(min_length=1)
) -> dict[str, Any]:
    return {"artifacts": await get_runtime(request).delivery.list_artifacts(project_id)}


@router.post("/delivery/report")
async def delivery_report(body: ReportBody, request: Request) -> dict[str, Any]:
    return await get_runtime(request).delivery.write_report(body.project_id, body.kind)


@router.get("/delivery/reports")
async def delivery_report_list(
    request: Request, project_id: str | None = None, limit: int = Query(50, ge=1, le=200)
) -> dict[str, Any]:
    return {"reports": await get_runtime(request).delivery.list_reports(project_id, limit)}
