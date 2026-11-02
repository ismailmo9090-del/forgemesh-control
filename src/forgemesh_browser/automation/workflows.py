"""Declarative multi-step automation with strict action whitelisting."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forgemesh_browser.automation import waits
from forgemesh_browser.core.exceptions import AutomationError

ALLOWED_ACTIONS = {
    "navigate",
    "click",
    "fill",
    "type",
    "press",
    "select",
    "scroll",
    "wait",
    "wait_for_selector",
    "wait_for_text",
    "hover",
    "check",
    "inspect",
}


class WorkflowStep(BaseModel):
    action: str
    target: str | None = None
    text: str | None = None
    value: str | None = None
    key: str | None = None
    seconds: float | None = None
    direction: str | None = None
    amount: float | None = None
    checked: bool = True


class WorkflowSpec(BaseModel):
    name: str = "workflow"
    steps: list[WorkflowStep] = Field(min_length=1, max_length=100)


async def run_workflow(
    controller,
    profile: str,
    spec: WorkflowSpec | dict[str, Any],
    *,
    tab_id: str | None = None,
) -> dict[str, Any]:
    spec = spec if isinstance(spec, WorkflowSpec) else WorkflowSpec.model_validate(spec)
    results: list[dict[str, Any]] = []
    for index, step in enumerate(spec.steps):
        action = step.action.lower()
        if action not in ALLOWED_ACTIONS:
            raise AutomationError(
                f"Step {index + 1}: action '{step.action}' is not allowed. "
                f"Allowed: {', '.join(sorted(ALLOWED_ACTIONS))}"
            )
        try:
            if action == "navigate":
                result = await controller.navigation.open(
                    profile, step.text or step.value or step.target or "", tab_id=tab_id
                )
            elif action == "click":
                result = await controller.click(profile, _require(step, "target"), tab_id=tab_id)
            elif action == "fill":
                result = await controller.fill(
                    profile, _require(step, "target"), step.text or "", tab_id=tab_id
                )
            elif action == "type":
                result = await controller.type_text(
                    profile, _require(step, "target"), step.text or "", tab_id=tab_id
                )
            elif action == "press":
                result = await controller.press(
                    profile, _require(step, "key"), target=step.target, tab_id=tab_id
                )
            elif action == "select":
                result = await controller.select_option(
                    profile, _require(step, "target"), _require(step, "value"), tab_id=tab_id
                )
            elif action == "scroll":
                result = await controller.scroll(
                    profile,
                    step.direction or "down",
                    amount=step.amount or 1.0,
                    tab_id=tab_id,
                )
            elif action == "wait":
                result = await waits.wait_seconds(step.seconds or 1.0)
            elif action == "wait_for_selector":
                page = await controller.browser.page(profile, tab_id)
                result = await waits.wait_for_selector(
                    page,
                    _require(step, "target"),
                    timeout_ms=controller.settings.action_timeout_ms,
                )
            elif action == "wait_for_text":
                page = await controller.browser.page(profile, tab_id)
                result = await waits.wait_for_text(
                    page,
                    _require(step, "text"),
                    timeout_ms=controller.settings.action_timeout_ms,
                )
            elif action == "hover":
                result = await controller.hover(profile, _require(step, "target"), tab_id=tab_id)
            elif action == "check":
                result = await controller.check(
                    profile, _require(step, "target"), checked=step.checked, tab_id=tab_id
                )
            else:  # inspect
                result = await controller.inspect(profile, tab_id=tab_id)
            results.append({"step": index + 1, "action": action, "result": result})
        except Exception as exc:
            return {
                "name": spec.name,
                "status": "failed",
                "failed_step": index + 1,
                "action": action,
                "error": str(exc),
                "completed_steps": results,
            }
    return {"name": spec.name, "status": "completed", "steps": results}


def _require(step: WorkflowStep, field: str) -> str:
    value = getattr(step, field)
    if not value:
        raise AutomationError(f"Step action '{step.action}' requires '{field}'")
    return value
