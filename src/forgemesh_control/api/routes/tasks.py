"""Task queue and workspace views backed by the control database."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from forgemesh_control.api.deps import get_runtime
from forgemesh_control.models import ProjectRow, TaskRow, WorkerRow, WorkspaceRow, iso

router = APIRouter(prefix="/api", tags=["tasks"])


class TaskBody(BaseModel):
    project_id: str
    title: str
    description: str = ""
    agent_kind: str = ""
    model_id: str = ""
    prompt: str = ""
    priority: int = 3


@router.get("/tasks")
async def list_tasks(
    request: Request,
    project_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    runtime = get_runtime(request)
    async with runtime.db.session_scope() as session:
        statement = (
            select(TaskRow, WorkerRow.name)
            .outerjoin(WorkerRow, TaskRow.worker_id == WorkerRow.id)
            .order_by(TaskRow.created_at.desc())
            .limit(limit)
        )
        if project_id:
            statement = statement.where(TaskRow.project_id == project_id)
        rows = (await session.execute(statement)).all()
    return {
        "tasks": [
            {
                "id": row.id,
                "project_id": row.project_id,
                "title": row.title,
                "description": row.description,
                "status": row.status,
                "priority": row.priority,
                "worker_id": row.worker_id,
                "worker_name": worker_name or "Server",
                "agent_kind": row.agent_kind,
                "model_id": row.model_id,
                "progress": row.progress,
                "attempts": row.attempts,
                "output": row.output,
                "error": row.error,
                "created_at": iso(row.created_at),
                "updated_at": iso(row.updated_at),
            }
            for row, worker_name in rows
        ]
    }


@router.post("/tasks")
async def create_task(body: TaskBody, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="Task title is required")
    async with runtime.db.session_scope() as session:
        if await session.get(ProjectRow, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Unknown project")
        row = TaskRow(
            id=f"tsk_{uuid.uuid4().hex[:10]}",
            project_id=body.project_id,
            title=body.title.strip()[:300],
            description=body.description,
            agent_kind=body.agent_kind,
            model_id=body.model_id,
            prompt=body.prompt,
            priority=max(1, min(5, body.priority)),
            status="draft",
        )
        session.add(row)
        await session.flush()
        task_id = row.id
    await runtime.events.publish("task.created", task_id=task_id, project_id=body.project_id)
    return {"id": task_id, "status": "draft"}


@router.get("/workspaces")
async def list_workspaces(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    async with runtime.db.session_scope() as session:
        rows = (
            await session.execute(
                select(WorkspaceRow).order_by(WorkspaceRow.created_at.desc()).limit(200)
            )
        ).scalars().all()
    return {
        "workspaces": [
            {
                "id": row.id,
                "project_id": row.project_id,
                "task_id": row.task_id,
                "path": row.path,
                "branch": row.branch,
                "base_branch": row.base_branch,
                "status": row.status,
                "created_at": iso(row.created_at),
            }
            for row in rows
        ]
    }
