"""Council and problem-statement endpoints.

Every response comes from stored meeting state or a live browser-runtime probe;
nothing here synthesises a participant, a score or a decision.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from forgemesh_control.api.deps import get_runtime
from forgemesh_control.council import COUNCIL_PROVIDERS, decision_contract

router = APIRouter(prefix="/api", tags=["council"])


class SeatBody(BaseModel):
    provider_id: str = Field(min_length=1, max_length=40)
    account_id: str = Field(min_length=1, max_length=40)
    council_role: str = Field(default="", max_length=60)


class MeetingBody(BaseModel):
    agenda: str
    mode: str = "normal"
    project_id: str | None = None
    problem_id: str | None = None
    providers: list[str] = Field(default_factory=list)
    seats: list[SeatBody] = Field(default_factory=list)
    rounds: int | None = Field(default=None, ge=2, le=10)


class ProblemBody(BaseModel):
    title: str
    description: str = ""
    source_document_id: str | None = None
    structured: dict[str, Any] = Field(default_factory=dict)


class ProblemUpdate(BaseModel):
    status: str
    notes: str = ""


class InstructionBody(BaseModel):
    text: str


class StopBody(BaseModel):
    reason: str = "cancelled"


def _council(request: Request):
    return get_runtime(request).council


@router.get("/council/providers")
async def council_providers(request: Request) -> dict[str, Any]:
    roster = await _council(request).roster()
    return {**roster, "expected": list(COUNCIL_PROVIDERS)}


@router.get("/council/summary")
async def council_summary(request: Request) -> dict[str, Any]:
    return await _council(request).summary()


@router.get("/council/meetings")
async def list_meetings(
    request: Request, limit: int = Query(default=50, ge=1, le=200)
) -> dict[str, Any]:
    return {"meetings": await _council(request).list_meetings(limit=limit)}


@router.post("/council/meetings")
async def create_meeting(body: MeetingBody, request: Request) -> dict[str, Any]:
    return await _council(request).create_meeting(
        agenda=body.agenda,
        mode=body.mode,
        project_id=body.project_id,
        problem_id=body.problem_id,
        providers=body.providers,
        seats=[seat.model_dump() for seat in body.seats],
        rounds=body.rounds,
    )


@router.get("/council/meetings/{meeting_id}")
async def get_meeting(meeting_id: str, request: Request) -> dict[str, Any]:
    return await _council(request).get_meeting(meeting_id)


@router.post("/council/meetings/{meeting_id}/start")
async def start_meeting(meeting_id: str, request: Request) -> dict[str, Any]:
    return await _council(request).start(meeting_id)


@router.post("/council/meetings/{meeting_id}/stop")
async def stop_meeting(
    meeting_id: str, body: StopBody, request: Request
) -> dict[str, Any]:
    return await _council(request).stop(meeting_id, reason=body.reason)


@router.post("/council/meetings/{meeting_id}/instructions")
async def add_instruction(
    meeting_id: str, body: InstructionBody, request: Request
) -> dict[str, Any]:
    return await _council(request).add_instruction(meeting_id, body.text)


@router.get("/council/meetings/{meeting_id}/decision")
async def meeting_decision(meeting_id: str, request: Request) -> dict[str, Any]:
    meeting = await _council(request).get_meeting(meeting_id)
    decision = meeting.get("decision")
    if decision is None:
        return {"decision": None, "contract": {"ok": False, "missing": ["no decision recorded"]}}
    scores = dict(decision.get("scores") or {})
    scores.setdefault("proposal_details", [
        {
            "id": proposal["id"],
            "title": proposal["title"],
            "author": proposal["author"],
            "steps": [
                step
                for response in proposal.get("responses", [])
                for step in response.get("steps", [])
            ],
            "acceptance": [
                item
                for response in proposal.get("responses", [])
                for item in response.get("acceptance", [])
            ],
            "risks": proposal.get("risks", []),
        }
        for proposal in meeting.get("proposals", [])
    ])
    return {"decision": decision, "contract": decision_contract(scores)}


@router.post("/council/meetings/{meeting_id}/blueprint")
async def meeting_blueprint(
    meeting_id: str, request: Request, project_id: str | None = None
) -> dict[str, Any]:
    return await _council(request).to_blueprint(meeting_id, project_id=project_id)


@router.get("/problems")
async def list_problems(
    request: Request, limit: int = Query(default=100, ge=1, le=200)
) -> dict[str, Any]:
    return {"problems": await _council(request).list_problems(limit=limit)}


@router.post("/problems")
async def create_problem(body: ProblemBody, request: Request) -> dict[str, Any]:
    return await _council(request).create_problem(
        title=body.title,
        description=body.description,
        source_document_id=body.source_document_id,
        structured=body.structured,
    )


@router.get("/problems/{problem_id}")
async def get_problem(problem_id: str, request: Request) -> dict[str, Any]:
    return await _council(request).get_problem(problem_id)


@router.patch("/problems/{problem_id}")
async def update_problem(
    problem_id: str, body: ProblemUpdate, request: Request
) -> dict[str, Any]:
    return await _council(request).update_problem(
        problem_id, status=body.status, notes=body.notes
    )
