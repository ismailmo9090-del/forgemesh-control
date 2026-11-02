"""Automation execution endpoint with a single documented action grammar."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from forgemesh_browser.api.deps import get_runtime
from forgemesh_browser.core.exceptions import AutomationError
from forgemesh_browser.core.runtime import Runtime

router = APIRouter(tags=["automation"])

ACTIONS = {
    "navigate",
    "reload",
    "back",
    "forward",
    "click",
    "fill",
    "type",
    "press",
    "select",
    "check",
    "hover",
    "scroll",
    "wait",
    "wait_for_selector",
    "wait_for_text",
    "inspect",
    "extract",
    "workflow",
}


class ExecuteRequest(BaseModel):
    action: str
    profile: str | None = None
    tab_id: str | None = None
    target: str | None = None
    text: str | None = None
    value: str | None = None
    key: str | None = None
    direction: str | None = None
    amount: float | None = None
    seconds: float | None = None
    selector: str | None = None
    state: str = "visible"
    checked: bool = True
    click_count: int = 1
    interactive_only: bool = True
    limit: int = Field(default=300, ge=1, le=2000)
    steps: list[dict[str, Any]] | None = None
    workflow_name: str = "workflow"


def _require(value: str | None, name: str) -> str:
    if not value:
        raise AutomationError(f"Action requires '{name}'")
    return value


def _journal_params(payload: ExecuteRequest) -> dict[str, Any]:
    """Parameters for the task journal, with typed content reduced to lengths.

    ``fill``/``type``/``select``/workflow steps can carry passwords or other
    form values, and those must not land in SQLite or `fmb tasks` output.
    """

    params: dict[str, Any] = {"action": payload.action.lower(), "state": payload.state,
                              "checked": payload.checked,
                              "click_count": payload.click_count,
                              "interactive_only": payload.interactive_only,
                              "limit": payload.limit}
    for field in ("tab_id", "target", "selector", "direction", "key", "amount", "seconds"):
        value = getattr(payload, field)
        if value is not None:
            params[field] = value
    if payload.text:
        params["text_chars"] = len(payload.text)
    if payload.value:
        params["value_chars"] = len(payload.value)
    if payload.steps:
        params["step_count"] = len(payload.steps)
        params["workflow_name"] = payload.workflow_name
    return params


async def _dispatch(runtime: Runtime, payload: ExecuteRequest, profile: str) -> dict:
    action = payload.action.lower()
    controller = runtime.automation
    tab_id = payload.tab_id
    if action == "navigate":
        return await controller.navigation.open(
            profile, _require(payload.text or payload.value or payload.target, "url"), tab_id=tab_id
        )
    if action == "reload":
        return await controller.navigation.reload(profile, tab_id=tab_id)
    if action == "back":
        return await controller.navigation.back(profile, tab_id=tab_id)
    if action == "forward":
        return await controller.navigation.forward(profile, tab_id=tab_id)
    if action == "click":
        return await controller.click(
            profile, _require(payload.target, "target"), tab_id=tab_id, click_count=payload.click_count
        )
    if action == "fill":
        return await controller.fill(
            profile, _require(payload.target, "target"), payload.text or "", tab_id=tab_id
        )
    if action == "type":
        return await controller.type_text(
            profile, _require(payload.target, "target"), payload.text or "", tab_id=tab_id
        )
    if action == "press":
        return await controller.press(
            profile, _require(payload.key, "key"), target=payload.target, tab_id=tab_id
        )
    if action == "select":
        return await controller.select_option(
            profile, _require(payload.target, "target"), _require(payload.value, "value"), tab_id=tab_id
        )
    if action == "check":
        return await controller.check(
            profile, _require(payload.target, "target"), checked=payload.checked, tab_id=tab_id
        )
    if action == "hover":
        return await controller.hover(profile, _require(payload.target, "target"), tab_id=tab_id)
    if action == "scroll":
        return await controller.scroll(
            profile, payload.direction or "down", amount=payload.amount or 1.0, tab_id=tab_id
        )
    if action == "wait":
        return await controller.wait_seconds(payload.seconds or 1.0)
    if action == "wait_for_selector":
        return await controller.wait_for_selector(
            profile, _require(payload.selector or payload.target, "selector"), state=payload.state, tab_id=tab_id
        )
    if action == "wait_for_text":
        return await controller.wait_for_text(
            profile, _require(payload.text, "text"), tab_id=tab_id
        )
    if action == "inspect":
        return await controller.inspect(
            profile,
            interactive_only=payload.interactive_only,
            limit=payload.limit,
            tab_id=tab_id,
        )
    if action == "extract":
        return await controller.extract(profile, tab_id=tab_id)
    if action == "workflow":
        if not payload.steps:
            raise AutomationError("Workflow requires a non-empty 'steps' list")
        return await controller.run_workflow(
            profile,
            {"name": payload.workflow_name, "steps": payload.steps},
            tab_id=tab_id,
        )
    raise AutomationError(
        f"Unknown action '{payload.action}'. Supported: {', '.join(sorted(ACTIONS))}"
    )


@router.post("/automation/execute")
async def execute(
    payload: ExecuteRequest, runtime: Runtime = Depends(get_runtime)
) -> dict:
    profile = payload.profile or runtime.profiles.current()
    await runtime.profiles.require(profile)
    record = await runtime.tasks.execute(
        f"automation.{payload.action.lower()}",
        lambda: _dispatch(runtime, payload, profile),
        _journal_params(payload),
        profile_id=profile,
    )
    result = record["result"]
    if isinstance(result, dict):
        return {**result, "task_id": record["task_id"], "duration_ms": record["duration_ms"]}
    return record
