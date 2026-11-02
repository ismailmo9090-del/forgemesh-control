"""Projects, approvals, human actions and encrypted secret references."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from forgemesh_control.api.deps import get_runtime
from forgemesh_control.projects import ProjectError

router = APIRouter(prefix="/api/projects", tags=["projects"])


class CreateBody(BaseModel):
    name: str
    description: str = ""
    mode: str = "normal"
    meta: dict[str, Any] = Field(default_factory=dict)


class UpdateBody(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    progress: int | None = None
    git_repo: str | None = None
    meta: dict[str, Any] | None = None


class ContentBody(BaseModel):
    content: dict[str, Any]
    status: str = "draft"


class ApprovalBody(BaseModel):
    stage: str
    summary: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class DecisionBody(BaseModel):
    approved: bool
    note: str = ""


class HumanActionBody(BaseModel):
    title: str
    description: str = ""
    why: str = ""
    feature: str = ""
    credential_name: str = ""
    blocking: bool = True


class ActionResolveBody(BaseModel):
    status: str
    note: str = ""


class SecretBody(BaseModel):
    name: str
    value: str


def _service(request: Request):
    return get_runtime(request).projects


@router.get("")
async def list_projects(request: Request) -> dict[str, Any]:
    return {"projects": await _service(request).list()}


@router.post("")
async def create_project(body: CreateBody, request: Request) -> dict[str, Any]:
    try:
        return await _service(request).create(
            body.name, description=body.description, mode=body.mode, meta=body.meta
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.get("/{project_id}")
async def get_project(project_id: str, request: Request) -> dict[str, Any]:
    try:
        payload = await _service(request).get(project_id)
        payload["human_actions"] = await _service(request).list_human_actions(project_id)
        payload["secrets"] = await _service(request).list_secrets(project_id)
        return payload
    except ProjectError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.patch("/{project_id}")
async def update_project(
    project_id: str, body: UpdateBody, request: Request
) -> dict[str, Any]:
    try:
        return await _service(request).update(
            project_id, **body.model_dump(exclude_none=True)
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.delete("/{project_id}")
async def delete_project(
    project_id: str, request: Request, confirm: bool = False
) -> dict[str, Any]:
    try:
        return await _service(request).delete(project_id, confirm=confirm)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post("/{project_id}/blueprint")
async def save_blueprint(
    project_id: str, body: ContentBody, request: Request
) -> dict[str, Any]:
    try:
        return await _service(request).save_blueprint(
            project_id, body.content, status=body.status
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post("/{project_id}/ui-spec")
async def save_ui_spec(
    project_id: str, body: ContentBody, request: Request
) -> dict[str, Any]:
    try:
        return await _service(request).save_ui_spec(
            project_id, body.content, status=body.status
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post("/{project_id}/approvals")
async def request_approval(
    project_id: str, body: ApprovalBody, request: Request
) -> dict[str, Any]:
    try:
        return await _service(request).request_approval(
            project_id, body.stage, summary=body.summary, details=body.details
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post("/{project_id}/approvals/{approval_id}/decide")
async def decide_approval(
    project_id: str, approval_id: str, body: DecisionBody, request: Request
) -> dict[str, Any]:
    try:
        return await _service(request).decide_approval(
            approval_id, approved=body.approved, note=body.note
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post("/{project_id}/human-actions")
async def add_human_action(
    project_id: str, body: HumanActionBody, request: Request
) -> dict[str, Any]:
    try:
        return await _service(request).add_human_action(
            project_id, **body.model_dump()
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post("/{project_id}/human-actions/{action_id}/resolve")
async def resolve_human_action(
    project_id: str, action_id: str, body: ActionResolveBody, request: Request
) -> dict[str, Any]:
    try:
        return await _service(request).resolve_human_action(
            action_id, status=body.status, note=body.note
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post("/{project_id}/secrets")
async def set_secret(
    project_id: str, body: SecretBody, request: Request
) -> dict[str, Any]:
    try:
        return await _service(request).set_secret(project_id, body.name, body.value)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.delete("/{project_id}/secrets/{name}")
async def delete_secret(project_id: str, name: str, request: Request) -> dict[str, Any]:
    try:
        return await _service(request).delete_secret(project_id, name)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
