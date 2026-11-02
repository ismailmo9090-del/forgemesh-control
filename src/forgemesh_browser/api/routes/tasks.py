"""Task journal endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from forgemesh_browser.api.deps import get_runtime
from forgemesh_browser.core.runtime import Runtime

router = APIRouter(tags=["tasks"])


@router.get("/tasks")
async def list_tasks(limit: int = 50, runtime: Runtime = Depends(get_runtime)) -> dict:
    return {"tasks": await runtime.tasks.list_recent(max(1, min(limit, 500)))}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, runtime: Runtime = Depends(get_runtime)) -> dict:
    try:
        return await runtime.tasks.get(task_id)
    except KeyError:
        from forgemesh_browser.core.exceptions import ForgeMeshError

        error = ForgeMeshError(f"Task not found: {task_id}")
        error.http_status = 404
        error.code = "task_not_found"
        raise error from None
