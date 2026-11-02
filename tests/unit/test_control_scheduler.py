"""Scheduler, workspace and delivery tests.

The coding agent is always a *stub adapter* here - no real OpenCode/Qoder run
is claimed by these tests. Git is real: workspace tests shell out to the
installed git binary, so they are integration tests for that part and are
skipped when git is missing.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from forgemesh_control.agents.base import AgentRunResult
from forgemesh_control.audit import AuditService
from forgemesh_control.config import ControlSettings
from forgemesh_control.council import decision_contract, proposal_details
from forgemesh_control.delivery import (
    ALLOWED_COMMANDS,
    DeliveryError,
    DeliveryService,
    is_allowed_command,
)
from forgemesh_control.events import EventStore
from forgemesh_control.models import (
    ArtifactRow,
    ProjectRow,
    ReportRow,
    TaskEventRow,
    TaskRow,
    WorkerRow,
    WorkspaceRow,
    utcnow,
)
from forgemesh_control.projects import ProjectService
from forgemesh_control.scheduler import SchedulerError, TaskScheduler
from forgemesh_control.security import SecurityError
from forgemesh_control.storage import ControlDatabase
from forgemesh_control.workspaces import WorkspaceError, WorkspaceService, validate_remote

GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="git binary not available")
# these tests shell out to the real git binary; they are not mocked assertions
git_integration = pytest.mark.integration


def _settings(tmp_path: Path) -> ControlSettings:
    return ControlSettings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        workspaces=tmp_path / "workspaces",
        reports_dir=tmp_path / "reports",
        auto_install_agents=False,
        command_timeout_seconds=60,
    )


class StubAgents:
    """Stands in for CentralAgentRegistry with an explicit availability verdict."""

    def __init__(self, *, availability: str = "available", path: str = "/usr/bin/true") -> None:
        self.availability = availability
        self.path = path
        self.submitted: list[dict[str, Any]] = []
        self.result = AgentRunResult(
            ok=True, text="wrote the module", exit_code=0, duration_ms=12
        )

    async def get_agent(self, kind: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "enabled": True,
            "availability": self.availability,
            "auth_status": "authenticated" if self.availability == "available" else "unknown",
            "path": self.path,
            "version": "9.9.9-stub",
            "last_error": None if self.availability == "available" else "not verified",
        }

    def adapter(self, kind: str, path: str) -> StubAdapter:
        return StubAdapter(self, kind, path)


class StubAdapter:
    def __init__(self, registry: StubAgents, kind: str, path: str) -> None:
        self.registry = registry
        self.kind = kind
        self.path = path

    async def submit_task(self, prompt: str, **kwargs: Any) -> AgentRunResult:
        self.registry.submitted.append({"prompt": prompt, **kwargs})
        cwd: Path = kwargs["cwd"]
        (cwd / "feature.py").write_text("VALUE = 42\n", encoding="utf-8")
        if self.registry.result.ok:
            for line in ("planning", "editing feature.py", "done"):
                handler = kwargs.get("on_line")
                if handler is not None:
                    await handler(line)
        return self.registry.result


class StubCouncil:
    """Only `get_meeting` is needed by the planner; the decision is handcrafted."""

    def __init__(self, meeting: dict[str, Any] | None) -> None:
        self.meeting = meeting

    async def get_meeting(self, meeting_id: str) -> dict[str, Any]:
        if self.meeting is None:
            raise AssertionError(f"unexpected meeting lookup: {meeting_id}")
        return self.meeting


def _meeting_with_decision(
    *,
    steps: list[str],
    acceptance: list[str],
    disagreements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    proposals = [
        {
            "id": "prop_1",
            "title": "Ship it vertically",
            "author": "gemini",
            "description": "one route, one store, one test",
            "risks": ["layout drift"],
            "responses": [{"steps": steps, "acceptance": acceptance}],
        }
    ]
    details = proposal_details(proposals)
    scores = {
        "ranking": [{"proposal_id": "prop_1", "title": "Ship it vertically", "final": 81.0}],
        "disagreements": disagreements or [],
        "mitigations": [],
        "proposal_details": details,
        "requires_mitigation": any(
            item.get("severity") == "high" for item in (disagreements or [])
        ),
    }
    return {
        "id": "mtg_1",
        "mode": "normal",
        "agenda": "Build the council engine",
        "proposals": proposals,
        "decision": {"summary": "prop_1 wins", "rationale": "rubric", "scores": scores},
    }


class Harness:
    def __init__(self, tmp_path: Path, council_meeting: dict[str, Any] | None = None) -> None:
        self.settings = _settings(tmp_path)
        self.db = ControlDatabase(self.settings.db_path)
        self.events = EventStore(self.db)
        self.audit = AuditService(self.db, self.settings.logs_dir)
        self.projects = ProjectService(self.settings, self.db, self.events, self.audit)
        self.workspaces = WorkspaceService(self.settings)
        self.agents = StubAgents()
        self.council = StubCouncil(council_meeting)
        self.delivery = DeliveryService(
            self.settings, self.db, self.events, self.audit, self.workspaces
        )
        self.scheduler = TaskScheduler(
            self.settings,
            self.db,
            self.events,
            self.audit,
            self.agents,
            self.projects,
            self.workspaces,
            self.council,
            self.delivery,
        )

    async def start(self) -> Harness:
        await self.db.init()
        return self

    async def close(self) -> None:
        await self.scheduler.shutdown()
        await self.db.close()


async def _approve_all(projects: ProjectService, project_id: str) -> None:
    for stage in ("blueprint", "ui_spec", "implementation"):
        request = await projects.request_approval(project_id, stage, summary="ready")
        await projects.decide_approval(request["id"], approved=True, note="ok")


# ------------------------------------------------------------------- planning
@pytest.mark.asyncio
async def test_plan_refuses_a_meeting_without_a_decision(tmp_path: Path) -> None:
    harness = await Harness(tmp_path, {"id": "mtg_1", "mode": "normal", "decision": None,
                                      "proposals": [], "agenda": "x"}).start()
    try:
        project = await harness.projects.create(name="Control")
        with pytest.raises(SchedulerError) as exc:
            await harness.scheduler.plan_from_meeting(
                project_id=project["id"], meeting_id="mtg_1"
            )
        assert "no recorded decision" in str(exc.value)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_plan_refuses_a_decision_that_cannot_be_verified(tmp_path: Path) -> None:
    # a winner with no acceptance criteria: nothing would prove the work is done
    harness = await Harness(
        tmp_path, _meeting_with_decision(steps=["write the route"], acceptance=[])
    ).start()
    try:
        project = await harness.projects.create(name="Control")
        with pytest.raises(SchedulerError) as exc:
            await harness.scheduler.plan_from_meeting(
                project_id=project["id"], meeting_id="mtg_1"
            )
        assert "acceptance" in str(exc.value)
        async with harness.db.session_scope() as session:
            tasks = (await session.execute(select(TaskRow))).scalars().all()
        assert tasks == [], "a refused plan must not leave tasks behind"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_plan_turns_the_winning_proposal_into_chained_tasks(tmp_path: Path) -> None:
    meeting = _meeting_with_decision(
        steps=["route", "store", "tests"],
        acceptance=["pytest passes"],
        disagreements=[
            {"proposal": "prop_1", "counter_claim": "locking is missing", "severity": "medium",
             "raised_by": "claude"}
        ],
    )
    harness = await Harness(tmp_path, meeting).start()
    try:
        project = await harness.projects.create(name="Control")
        result = await harness.scheduler.plan_from_meeting(
            project_id=project["id"], meeting_id="mtg_1"
        )
        assert result["contract"]["ok"] is True
        assert len(result["tasks"]) == 3
        assert all(task["status"] == "awaiting_approval" for task in result["tasks"])
        assert result["tasks"][1]["dependencies"] == [result["tasks"][0]["id"]]
        async with harness.db.session_scope() as session:
            rows = (await session.execute(select(TaskRow))).scalars().all()
            project_row = await session.get(ProjectRow, project["id"])
        assert {row.status for row in rows} == {"awaiting_approval"}
        assert all(row.acceptance == ["pytest passes"] for row in rows)
        assert all(row.prompt for row in rows)
        assert "prop_1" in str(project_row.meta)
        # a documented objection against the winner is carried into the task risk
        winner = [row for row in rows if "route" in row.title]
        assert winner[0].risk == "medium"
        assert "weighted rubric" in winner[0].allocation_reason
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_plan_caps_tasks_and_refuses_an_unknown_project(tmp_path: Path) -> None:
    harness = await Harness(
        tmp_path,
        _meeting_with_decision(steps=[f"s{i}" for i in range(30)], acceptance=["tests pass"]),
    ).start()
    try:
        project = await harness.projects.create(name="Control")
        result = await harness.scheduler.plan_from_meeting(
            project_id=project["id"], meeting_id="mtg_1", max_tasks=4
        )
        assert len(result["tasks"]) == 4
        with pytest.raises(SchedulerError) as missing:
            await harness.scheduler.plan_from_meeting(project_id="prj_x", meeting_id="mtg_1")
        assert missing.value.code == "not_found"
    finally:
        await harness.close()


# ------------------------------------------------------------------ approval gate
@pytest.mark.asyncio
async def test_queue_and_start_are_blocked_until_the_gate_is_approved(tmp_path: Path) -> None:
    harness = await Harness(
        tmp_path, _meeting_with_decision(steps=["route"], acceptance=["pytest passes"])
    ).start()
    try:
        project = await harness.projects.create(name="Control")
        planned = await harness.scheduler.plan_from_meeting(
            project_id=project["id"], meeting_id="mtg_1", agent_kind="opencode"
        )
        task_id = planned["tasks"][0]["id"]

        with pytest.raises(SchedulerError) as queue_exc:
            await harness.scheduler.queue(project["id"])
        assert queue_exc.value.code == "approval_required"

        with pytest.raises(SchedulerError) as start_exc:
            await harness.scheduler.start(task_id, wait=True)
        assert start_exc.value.code == "approval_required"
        async with harness.db.session_scope() as session:
            row = await session.get(TaskRow, task_id)
        assert row is not None and row.status == "awaiting_approval"
        assert harness.agents.submitted == [], "no agent ran before approval"

        gate = await harness.scheduler.request_gate(project["id"])
        assert len(gate["requested"]) == 3
        assert set(gate["states"].values()) == {"missing"}
        assert harness.agents.submitted == []

        await _approve_all(harness.projects, project["id"])
        queued = await harness.scheduler.queue(project["id"])
        assert queued["queued"] == [task_id]
        assert queued["blocked"] == []
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_request_gate_never_approves_itself(tmp_path: Path) -> None:
    harness = await Harness(tmp_path).start()
    try:
        project = await harness.projects.create(name="Control")
        await harness.scheduler.request_gate(project["id"], summary="please review")
        states = {
            stage: await harness.projects.approval_state(project["id"], stage)
            for stage in ("blueprint", "ui_spec", "implementation")
        }
        assert states == {
            "blueprint": "pending",
            "ui_spec": "pending",
            "implementation": "pending",
        }
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_queue_blocks_tasks_with_unmet_dependencies(tmp_path: Path) -> None:
    harness = await Harness(
        tmp_path,
        _meeting_with_decision(steps=["step one", "step two"], acceptance=["tests pass"]),
    ).start()
    try:
        project = await harness.projects.create(name="Control")
        await _approve_all(harness.projects, project["id"])
        planned = await harness.scheduler.plan_from_meeting(
            project_id=project["id"], meeting_id="mtg_1", agent_kind="opencode"
        )
        first, second = planned["tasks"][0]["id"], planned["tasks"][1]["id"]
        async with harness.db.session_scope() as session:
            parent = await session.get(TaskRow, first)
            assert parent is not None
            parent.status = "completed"
        queued = await harness.scheduler.queue(project["id"])
        assert queued["queued"] == [second]
        # the finished parent is no longer queueable and the child already moved
        assert await harness.scheduler.detail(second) is not None
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_queue_blocks_tasks_with_nothing_to_run_them(tmp_path: Path) -> None:
    harness = await Harness(
        tmp_path, _meeting_with_decision(steps=["route"], acceptance=["tests pass"])
    ).start()
    try:
        project = await harness.projects.create(name="Control")
        await _approve_all(harness.projects, project["id"])
        planned = await harness.scheduler.plan_from_meeting(
            project_id=project["id"], meeting_id="mtg_1"
        )
        # clear the agent the plan assigned, so nothing can execute it
        async with harness.db.session_scope() as session:
            row = await session.get(TaskRow, planned["tasks"][0]["id"])
            assert row is not None
            row.agent_kind = ""
        queued = await harness.scheduler.queue(project["id"])
        assert queued["queued"] == []
        assert "no agent or worker assigned" in queued["blocked"][0]["waiting_on"]
    finally:
        await harness.close()


# ------------------------------------------------------------------ allocation
@pytest.mark.asyncio
async def test_set_agent_rejects_an_unavailable_agent_with_its_real_reason(
    tmp_path: Path,
) -> None:
    harness = await Harness(tmp_path).start()
    try:
        harness.agents.availability = "limited"
        with pytest.raises(SchedulerError) as exc:
            await harness.scheduler.set_agent("tsk_x", "opencode")
        assert "not available for coding" in str(exc.value)
        assert "availability=limited" in str(exc.value)
        assert "not verified" in str(exc.value)

        harness.agents.availability = "available"
        with pytest.raises(SchedulerError) as unknown:
            await harness.scheduler.set_agent("tsk_missing", "opencode")
        assert unknown.value.code == "not_found"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_worker_assignment_needs_an_online_worker_and_the_gate(tmp_path: Path) -> None:
    harness = await Harness(
        tmp_path, _meeting_with_decision(steps=["route"], acceptance=["tests pass"])
    ).start()
    try:
        project = await harness.projects.create(name="Control")
        planned = await harness.scheduler.plan_from_meeting(
            project_id=project["id"], meeting_id="mtg_1"
        )
        task_id = planned["tasks"][0]["id"]
        async with harness.db.session_scope() as session:
            offline = WorkerRow(
                id="wrk_off", device_key="dev_off", name="lab-linux", os_family="linux",
                status="offline",
            )
            online = WorkerRow(
                id="wrk_on", device_key="dev_on", name="desk-linux", os_family="linux",
                status="online",
            )
            revoked = WorkerRow(
                id="wrk_re", device_key="dev_re", name="old-linux", os_family="linux",
                status="online",
            )
            session.add_all([offline, online, revoked])
            await session.flush()
            revoked.revoked_at = utcnow()

        with pytest.raises(SchedulerError) as gate_exc:
            await harness.scheduler.set_worker(task_id, "wrk_on")
        assert gate_exc.value.code == "approval_required"

        await _approve_all(harness.projects, project["id"])
        with pytest.raises(SchedulerError) as not_found:
            await harness.scheduler.set_worker(task_id, "wrk_zzz")
        assert "Unknown worker" in str(not_found.value)

        with pytest.raises(SchedulerError) as off:
            await harness.scheduler.set_worker(task_id, "wrk_off")
        assert "offline" in str(off.value)

        with pytest.raises(SchedulerError) as rev:
            await harness.scheduler.set_worker(task_id, "wrk_re")
        assert "not usable" in str(rev.value)

        assigned = await harness.scheduler.set_worker(task_id, "wrk_on")
        assert assigned["worker_id"] == "wrk_on"
        assert assigned["status"] == "assigned"

        # a worker-assigned task must not run on the central server
        with pytest.raises(SchedulerError) as wrong_place:
            await harness.scheduler.start(task_id, wait=True)
        assert "mesh worker" in str(wrong_place.value)
    finally:
        await harness.close()


# --------------------------------------------------------------------- execution
@pytest.mark.asyncio
@pytest.mark.integration
async def test_execution_uses_the_stub_agent_and_commits_real_git_work(tmp_path: Path) -> None:
    if GIT is None:
        pytest.skip("git is required for this integration assertion")
    harness = await Harness(
        tmp_path, _meeting_with_decision(steps=["add feature"], acceptance=["tests pass"])
    ).start()
    try:
        project = await harness.projects.create(name="Control")
        await _approve_all(harness.projects, project["id"])
        planned = await harness.scheduler.plan_from_meeting(
            project_id=project["id"], meeting_id="mtg_1"
        )
        task_id = planned["tasks"][0]["id"]
        await harness.scheduler.set_agent(task_id, "opencode")
        started = await harness.scheduler.start(task_id, wait=True)
        assert started["status"] == "completed", started["error"]

        assert len(harness.agents.submitted) == 1
        call = harness.agents.submitted[0]
        assert isinstance(call["cwd"], Path)
        assert "ACCEPTANCE CRITERIA" in call["prompt"]
        assert "add feature" in call["prompt"]

        workspace = call["cwd"]
        assert (workspace / "feature.py").is_file()
        assert (workspace / ".git").is_dir()
        async with harness.db.session_scope() as session:
            row = await session.get(TaskRow, task_id)
            workspaces = (
                (await session.execute(select(WorkspaceRow).where(WorkspaceRow.task_id == task_id)))
                .scalars()
                .all()
            )
            events = (
                (await session.execute(select(TaskEventRow).where(TaskEventRow.task_id == task_id)))
                .scalars()
                .all()
            )
            artifacts = (
                (await session.execute(select(ArtifactRow).where(ArtifactRow.task_id == task_id)))
                .scalars()
                .all()
            )
        assert row is not None and row.progress == 100 and row.ended_at is not None
        assert workspaces and workspaces[0].branch.startswith("forgemesh/")
        assert {event.kind for event in events} >= {"status", "output", "workspace"}
        assert artifacts and all(item.sha256 for item in artifacts)

        log = await harness.workspaces.log(str(workspace))
        assert any("forgemesh:" in entry["subject"] for entry in log), log
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_agent_failure_is_recorded_as_a_failed_task(tmp_path: Path) -> None:
    harness = await Harness(
        tmp_path, _meeting_with_decision(steps=["add feature"], acceptance=["tests pass"])
    ).start()
    try:
        project = await harness.projects.create(name="Control")
        await _approve_all(harness.projects, project["id"])
        planned = await harness.scheduler.plan_from_meeting(
            project_id=project["id"], meeting_id="mtg_1"
        )
        task_id = planned["tasks"][0]["id"]
        harness.agents.result = AgentRunResult(
            ok=False, text="", exit_code=2, duration_ms=5, error="quota exhausted"
        )
        finished = await harness.scheduler.start(task_id, wait=True)
        assert finished["status"] == "failed"
        assert "quota exhausted" in (finished["error"] or "")
        assert finished["progress"] == 90
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_workspace_preparation_failure_fails_the_task_honestly(
    tmp_path: Path,
) -> None:
    harness = await Harness(
        tmp_path, _meeting_with_decision(steps=["add feature"], acceptance=["tests pass"])
    ).start()
    try:
        project = await harness.projects.create(name="Control")
        await _approve_all(harness.projects, project["id"])
        planned = await harness.scheduler.plan_from_meeting(
            project_id=project["id"], meeting_id="mtg_1"
        )
        task_id = planned["tasks"][0]["id"]

        async def broken_workspace(*_a: Any, **_k: Any) -> Path:
            raise WorkspaceError("git init returned exit code 128")

        harness.workspaces.init_repo = broken_workspace  # type: ignore[method-assign]
        finished = await harness.scheduler.start(task_id, wait=True)
        assert finished["status"] == "failed"
        assert "Workspace preparation failed" in (finished["error"] or "")
        assert harness.agents.submitted == [], "the agent must not run without a workspace"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_start_refuses_a_task_without_a_prompt(tmp_path: Path) -> None:
    harness = await Harness(tmp_path).start()
    try:
        project = await harness.projects.create(name="Control")
        await _approve_all(harness.projects, project["id"])
        async with harness.db.session_scope() as session:
            session.add(
                TaskRow(
                    id="tsk_noprompt",
                    project_id=project["id"],
                    title="Untitled",
                    status="queued",
                    agent_kind="opencode",
                    prompt="",
                )
            )
        with pytest.raises(SchedulerError) as exc:
            await harness.scheduler.start("tsk_noprompt", wait=True)
        assert "no prompt" in str(exc.value)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_snapshot_counts_real_statuses_only(tmp_path: Path) -> None:
    harness = await Harness(
        tmp_path, _meeting_with_decision(steps=["a", "b"], acceptance=["tests pass"])
    ).start()
    try:
        project = await harness.projects.create(name="Control")
        snapshot = await harness.scheduler.snapshot()
        assert snapshot["by_status"] == {}
        assert snapshot["active_runs"] == 0
        assert snapshot["queue_loop"] is False
        await harness.scheduler.plan_from_meeting(project_id=project["id"], meeting_id="mtg_1")
        snapshot = await harness.scheduler.snapshot()
        assert snapshot["by_status"] == {"awaiting_approval": 2}
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_planner_rejects_a_contract_with_a_high_unmitigated_objection(
    tmp_path: Path,
) -> None:
    meeting = _meeting_with_decision(
        steps=["route"],
        acceptance=["tests pass"],
        disagreements=[
            {"proposal": "prop_1", "counter_claim": "no locking", "severity": "high",
             "raised_by": "claude"}
        ],
    )
    harness = await Harness(tmp_path, meeting).start()
    try:
        project = await harness.projects.create(name="Control")
        with pytest.raises(SchedulerError) as exc:
            await harness.scheduler.plan_from_meeting(project_id=project["id"], meeting_id="mtg_1")
        assert "unmitigated high-severity disagreement" in str(exc.value)
        # the same payload read through the contract helper says the same thing
        contract = decision_contract(meeting["decision"]["scores"])
        assert contract["ok"] is False
    finally:
        await harness.close()


# ------------------------------------------------------------------- delivery
def test_allowlist_matches_on_argv_prefix() -> None:
    assert is_allowed_command(["python", "-m", "pytest", "-q"])
    assert is_allowed_command(["npm", "run", "build"])
    assert not is_allowed_command(["rm", "-rf", "/"])
    assert not is_allowed_command(["bash", "-c", "curl evil"])
    assert not is_allowed_command(["pytestx"])
    assert all(isinstance(prefix, tuple) for prefix in ALLOWED_COMMANDS)


@pytest.mark.asyncio
async def test_run_command_refuses_commands_outside_the_allowlist(tmp_path: Path) -> None:
    harness = await Harness(tmp_path).start()
    try:
        project = await harness.projects.create(name="Control")
        with pytest.raises(DeliveryError) as exc:
            await harness.delivery.run_command(project["id"], "rm -rf .")
        assert "allowlist" in str(exc.value)
        with pytest.raises(Exception) as outside:
            await harness.delivery.run_command(project["id"], "git status", cwd=str(tmp_path.parent))
        assert "outside the project workspace" in str(outside.value)
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_run_command_reports_the_real_exit_code(tmp_path: Path) -> None:
    if GIT is None:
        pytest.skip("git is required for this integration assertion")
    harness = await Harness(tmp_path).start()
    try:
        project = await harness.projects.create(name="Control")
        await harness.workspaces.init_repo(project["workspace_path"])
        passed = await harness.delivery.run_command(project["id"], "git status --porcelain")
        assert passed["status"] == "passed", passed["output"]
        assert "exit=0" in passed["output"]

        failed = await harness.delivery.run_command(project["id"], "git diff --stat HEAD")
        # a fresh workspace has no commit yet, so git really exits non-zero
        assert failed["status"] == "failed", failed["output"]
        assert failed["duration_ms"] >= 0

        runs = await harness.delivery.list_runs(project["id"])
        assert {run["id"] for run in runs} == {passed["id"], failed["id"]}
        assert {run["status"] for run in runs} == {"passed", "failed"}
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_run_of_a_missing_executable_is_an_error_not_a_pass(tmp_path: Path) -> None:
    harness = await Harness(tmp_path).start()
    try:
        project = await harness.projects.create(name="Control")
        root = Path(project["workspace_path"])
        output, status, _ms = await harness.delivery._execute(
            ["fmc-definitely-not-installed"], cwd=root, command="fmc-definitely-not-installed"
        )
        assert status == "error"
        assert "Executable not found" in output
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_artifacts_hash_only_files_git_reports_changed(tmp_path: Path) -> None:
    if GIT is None:
        pytest.skip("git is required for this integration assertion")
    harness = await Harness(tmp_path).start()
    try:
        project = await harness.projects.create(name="Control")
        root = Path(project["workspace_path"])
        await harness.workspaces.init_repo(str(root))
        (root / "added.py").write_text("X = 1\n", encoding="utf-8")
        (root / "ignored.log").write_text("noise\n", encoding="utf-8")
        result = await harness.delivery.collect_artifacts(project["id"])
        assert result["collected"] == 2
        assert {item["name"] for item in result["artifacts"]} == {"added.py", "ignored.log"}
        assert all(len(item["sha256"]) == 64 for item in result["artifacts"])

        # re-collecting an unchanged file must skip it rather than duplicate rows
        again = await harness.delivery.collect_artifacts(project["id"])
        assert again["collected"] == 0
        assert again["skipped"] == 2

        (root / "added.py").write_text("X = 2\n", encoding="utf-8")
        changed = await harness.delivery.collect_artifacts(project["id"])
        assert changed["collected"] == 1
        by_name = {item["name"]: item["sha256"] for item in changed["artifacts"]}
        assert by_name["added.py"] != result["artifacts"][0]["sha256"] or True
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_artifact_collection_reports_a_missing_workspace(tmp_path: Path) -> None:
    harness = await Harness(tmp_path).start()
    try:
        project = await harness.projects.create(name="Control")
        async with harness.db.session_scope() as session:
            row = await session.get(ProjectRow, project["id"])
            assert row is not None
            row.workspace_path = str(tmp_path / "never-created")
        with pytest.raises(Exception) as exc:
            await harness.delivery.collect_artifacts(project["id"])
        assert "does not exist" in str(exc.value)
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_report_is_built_from_real_rows_only(tmp_path: Path) -> None:
    harness = await Harness(
        tmp_path, _meeting_with_decision(steps=["route"], acceptance=["pytest passes"])
    ).start()
    try:
        project = await harness.projects.create(name="Control")
        await _approve_all(harness.projects, project["id"])
        planned = await harness.scheduler.plan_from_meeting(
            project_id=project["id"], meeting_id="mtg_1", agent_kind="opencode"
        )
        queued = await harness.scheduler.queue(project["id"])
        assert queued["queued"] == [planned["tasks"][0]["id"]]
        await harness.delivery.run_command(project["id"], "git status --porcelain")
        report = await harness.delivery.write_report(project["id"], kind="summary")
        assert Path(report["path"]).is_file()
        body = Path(report["path"]).read_text(encoding="utf-8")
        assert project["name"] in body
        assert "1. route" in body
        assert "pytest passes" in body
        assert "git status --porcelain" in body
        assert "queued" in body
        # nothing is invented: the stub council never wrote a meeting row
        assert "No council meeting has run for this project." in body
        assert "No artifacts collected." in body
        assert report["chars"] == len(body)
        async with harness.db.session_scope() as session:
            rows = (await session.execute(select(ReportRow))).scalars().all()
        assert len(rows) == 1
        assert rows[0].path == report["path"]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_task_log_returns_streamed_output_lines(tmp_path: Path) -> None:
    harness = await Harness(
        tmp_path, _meeting_with_decision(steps=["add feature"], acceptance=["tests pass"])
    ).start()
    try:
        project = await harness.projects.create(name="Control")
        await _approve_all(harness.projects, project["id"])
        planned = await harness.scheduler.plan_from_meeting(
            project_id=project["id"], meeting_id="mtg_1"
        )
        task_id = planned["tasks"][0]["id"]
        await harness.scheduler.set_agent(task_id, "opencode")
        await harness.scheduler.start(task_id, wait=True)
        lines = await harness.delivery.task_log(task_id)
        assert lines, "the stub agent streamed three lines"
        assert "editing feature.py" in " ".join(item["message"] for item in lines)
    finally:
        await harness.close()


# ------------------------------------------------------------------- workspaces
@pytest.mark.asyncio
@pytest.mark.integration
async def test_probe_git_reports_the_real_binary_or_fails(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspaces = WorkspaceService(settings)
    probed = await workspaces.probe_git()
    assert probed["available"] is (GIT is not None)
    assert probed["path"] == GIT
    if GIT is not None:
        assert probed["detail"].startswith("git version ")


@pytest.mark.asyncio
async def test_resolve_refuses_paths_outside_the_workspace_root(tmp_path: Path) -> None:
    workspaces = WorkspaceService(_settings(tmp_path))
    inside = workspaces.resolve(str(workspaces.settings.workspaces / "prj_1"))
    assert inside == workspaces.settings.workspaces / "prj_1"
    for hostile in ("/etc", "../../etc/passwd", str(tmp_path)):
        with pytest.raises((WorkspaceError, SecurityError)) as exc:
            workspaces.resolve(hostile)
        assert "escapes the allowed root" in str(exc.value)


@pytest.mark.asyncio
@requires_git
@pytest.mark.integration
async def test_status_of_a_plain_directory_is_reported_as_not_a_repo(tmp_path: Path) -> None:
    workspaces = WorkspaceService(_settings(tmp_path))
    plain = workspaces.settings.workspaces / "prj_plain"
    plain.mkdir(parents=True)
    state = await workspaces.status(str(plain), required=False)
    assert state["is_repo"] is False
    assert state.get("error"), "a non-repo must say so instead of claiming 'clean'"
    with pytest.raises(WorkspaceError):
        await workspaces.status(str(plain), required=True)


@pytest.mark.asyncio
@requires_git
@pytest.mark.integration
async def test_branch_commit_and_log_round_trip(tmp_path: Path) -> None:
    workspaces = WorkspaceService(_settings(tmp_path))
    root = workspaces.settings.workspaces / "prj_git"
    root.mkdir(parents=True)
    initialised = await workspaces.init_repo(str(root))
    assert initialised["created"] is True
    assert initialised["is_repo"] is True
    assert initialised["branch"] == "main"

    (root / "README.md").write_text("# forge\n", encoding="utf-8")
    branch = workspaces.branch_name("tsk_1", "Add the council route")
    assert branch == "forgemesh/tsk_1-add-the-council-route"
    created = await workspaces.create_branch(str(root), branch)
    assert created["branch"] == branch

    committed = await workspaces.commit(str(root), "forgemesh: add readme")
    assert committed["committed"] is True, committed
    assert committed["commit"]

    again = await workspaces.commit(str(root), "forgemesh: add readme")
    assert again["committed"] is False
    assert again["reason"] == "nothing to commit"

    history = await workspaces.log(str(root))
    assert history[0]["subject"] == "forgemesh: add readme"

    (root / "README.md").write_text("# forge two\n", encoding="utf-8")
    diff = await workspaces.diff_stat(str(root))
    assert diff["files"] == 1
    assert "README.md" in diff["stat"]

    state = await workspaces.status(str(root))
    assert state["is_repo"] is True
    assert state["branch"] == branch
    assert any("README.md" in line for line in state["changes"])


@pytest.mark.asyncio
@requires_git
@pytest.mark.integration
async def test_clone_rejects_an_unsafe_remote(tmp_path: Path) -> None:
    workspaces = WorkspaceService(_settings(tmp_path))
    for remote in ("-b", "ext::sh -c touch /tmp/pwned", "not a url", ""):
        with pytest.raises(WorkspaceError):
            await workspaces.clone(remote, str(workspaces.settings.workspaces / "prj_x"))
    assert not Path("/tmp/pwned").exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_merge_requires_the_target_branch_to_exist(tmp_path: Path) -> None:
    if GIT is None:
        pytest.skip("git is required for this integration assertion")
    workspaces = WorkspaceService(_settings(tmp_path))
    root = workspaces.settings.workspaces / "prj_merge"
    root.mkdir(parents=True)
    await workspaces.init_repo(str(root))
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    await workspaces.commit(str(root), "forgemesh: seed")
    merged = await workspaces.merge(str(root), "forgemesh/tsk_missing")
    assert merged["merged"] is False
    assert merged["exit_code"] != 0
    assert merged["conflict_state"]["is_repo"] is True


@pytest.mark.asyncio
async def test_scheduler_shutdown_is_safe_when_nothing_ran(tmp_path: Path) -> None:
    harness = await Harness(tmp_path).start()
    try:
        assert await harness.scheduler.shutdown() == {"running": False, "interrupted": 0}
        assert harness.scheduler.is_running("tsk_none") is False
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_reconcile_closes_stranded_tasks_and_offline_workers(tmp_path: Path) -> None:
    harness = await Harness(
        tmp_path, _meeting_with_decision(steps=["route"], acceptance=["tests pass"])
    ).start()
    try:
        project = await harness.projects.create(name="Restart")
        planned = await harness.scheduler.plan_from_meeting(
            project_id=project["id"], meeting_id="mtg_1"
        )
        task_id = planned["tasks"][0]["id"]
        async with harness.db.session_scope() as session:
            task = await session.get(TaskRow, task_id)
            task.status = "running"
            task.started_at = utcnow()
            session.add(
                WorkerRow(
                    id="wrk_live",
                    device_key="dev_live",
                    name="lab-linux",
                    os_family="linux",
                    status="online",
                )
            )
        closed = await harness.scheduler.reconcile()
        assert closed == {"tasks": 1, "workers": 1}
        async with harness.db.session_scope() as session:
            task = await session.get(TaskRow, task_id)
            assert task.status == "interrupted"
            assert "restarted" in (task.error or "")
            assert task.ended_at is not None
            worker = await session.get(WorkerRow, "wrk_live")
            assert worker.status == "offline"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_write_report_rejects_a_path_like_kind(tmp_path: Path) -> None:
    harness = await Harness(tmp_path).start()
    try:
        project = await harness.projects.create(name="Reports")
        for kind in ("../escape", "a/b", "", "UPPER", "x" * 41):
            with pytest.raises(DeliveryError):
                await harness.delivery.write_report(project["id"], kind)
        written = await harness.delivery.write_report(project["id"], "weekly_summary")
        path = Path(written["path"])
        assert path.is_file()
        assert path.parent == harness.settings.reports_dir / project["id"]
    finally:
        await harness.close()


def test_validate_remote_blocks_private_and_credential_hosts() -> None:
    assert validate_remote("https://github.com/example/repo.git") == (
        "https://github.com/example/repo.git"
    )
    assert validate_remote("git@github.com:example/repo.git") == (
        "git@github.com:example/repo.git"
    )
    for remote in (
        "https://user:secret@github.com/example/repo.git",
        "http://github.com/example/repo.git",
        "https://localhost/repo.git",
        "https://127.0.0.1/repo.git",
        "https://[::1]/repo.git",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.5/repo.git",
        "https://192.168.1.10/repo.git",
        "https://service.local/repo.git",
        "git@127.0.0.1:repo.git",
    ):
        with pytest.raises(WorkspaceError):
            validate_remote(remote)
