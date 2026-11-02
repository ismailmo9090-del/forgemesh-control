"""Projects, blueprints, UI specifications, approvals and human actions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from forgemesh_browser.security.vault import Vault
from forgemesh_control.audit import AuditService
from forgemesh_control.config import ControlSettings
from forgemesh_control.events import EventStore
from forgemesh_control.models import (
    ApprovalRow,
    BlueprintRow,
    HumanActionRow,
    ProjectRow,
    SecretRefRow,
    TaskRow,
    UISpecRow,
    iso,
)
from forgemesh_control.storage import ControlDatabase


class ProjectError(RuntimeError):
    def __init__(self, message: str, *, code: str = "project_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


BLUEPRINT_SECTIONS = (
    "vision",
    "target_users",
    "problem_definition",
    "solution_approach",
    "features",
    "differentiating_features",
    "architecture",
    "technology_stack",
    "database_design",
    "apis",
    "security",
    "scalability",
    "risks",
    "roadmap",
    "demo_plan",
    "testing_strategy",
)

UI_SECTIONS = (
    "platform",
    "user_roles",
    "navigation",
    "screens",
    "design_system",
    "typography",
    "colors",
    "spacing",
    "components",
    "responsive",
    "authentication",
    "empty_states",
    "loading_states",
    "error_states",
    "accessibility",
    "user_journeys",
)

APPROVAL_STAGES = ("blueprint", "ui_spec", "implementation")


def _row(row: ProjectRow, task_counts: dict[str, int] | None = None) -> dict[str, Any]:
    counts = task_counts or {}
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "mode": row.mode,
        "status": row.status,
        "progress": row.progress,
        "workspace_path": row.workspace_path,
        "git_repo": row.git_repo,
        "meta": row.meta or {},
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
        "tasks_total": counts.get("total", 0),
        "tasks_completed": counts.get("completed", 0),
        "tasks_running": counts.get("running", 0),
    }


class ProjectService:
    def __init__(
        self,
        settings: ControlSettings,
        db: ControlDatabase,
        events: EventStore,
        audit: AuditService,
    ) -> None:
        self.settings = settings
        self.db = db
        self.events = events
        self.audit = audit
        self.vault = Vault(settings)  # type: ignore[arg-type]  # shared API shape

    # ------------------------------------------------------------------ projects
    async def create(
        self,
        name: str,
        *,
        description: str = "",
        mode: str = "normal",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ProjectError("Project name is required.")
        if mode not in {"normal", "hackathon"}:
            raise ProjectError("mode must be 'normal' or 'hackathon'.")
        project_id = f"prj_{uuid.uuid4().hex[:10]}"
        workspace = self.settings.workspaces / project_id
        workspace.mkdir(parents=True, exist_ok=True)
        async with self.db.session_scope() as session:
            row = ProjectRow(
                id=project_id,
                name=name[:160],
                description=description,
                mode=mode,
                status="planning",
                workspace_path=str(workspace),
                meta=meta or {},
            )
            session.add(row)
            await session.flush()
            payload = _row(row)
        await self.audit.record("project.create", project=project_id, mode=mode)
        await self.events.publish("project.created", project_id=project_id, name=name)
        return payload

    async def list(self) -> list[dict[str, Any]]:
        async with self.db.session_scope() as session:
            rows = (
                (await session.execute(select(ProjectRow).order_by(ProjectRow.updated_at.desc())))
                .scalars()
                .all()
            )
            counts = await self._task_counts(session)
        return [_row(row, counts.get(row.id)) for row in rows]

    async def _task_counts(self, session) -> dict[str, dict[str, int]]:
        rows = (
            await session.execute(
                select(TaskRow.project_id, TaskRow.status, func.count(TaskRow.id)).group_by(
                    TaskRow.project_id, TaskRow.status
                )
            )
        ).all()
        result: dict[str, dict[str, int]] = {}
        for project_id, status, count in rows:
            entry = result.setdefault(
                project_id, {"total": 0, "completed": 0, "running": 0}
            )
            entry["total"] += count
            if status in {"completed", "cancelled"}:
                entry["completed"] += count
            if status == "running":
                entry["running"] += count
        return result

    async def get(self, project_id: str) -> dict[str, Any]:
        async with self.db.session_scope() as session:
            row = await session.get(ProjectRow, project_id)
            if row is None:
                raise ProjectError(f"Unknown project: {project_id}", code="not_found")
            counts = await self._task_counts(session)
            payload = _row(row, counts.get(project_id))
            payload["blueprint"] = await self._latest(session, BlueprintRow, project_id)
            payload["ui_spec"] = await self._latest(session, UISpecRow, project_id)
            payload["approvals"] = [
                self._approval(item)
                for item in (
                    await session.execute(
                        select(ApprovalRow)
                        .where(ApprovalRow.project_id == project_id)
                        .order_by(ApprovalRow.requested_at.desc())
                    )
                ).scalars()
            ]
        return payload

    async def update(self, project_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"name", "description", "status", "progress", "meta", "git_repo"}
        async with self.db.session_scope() as session:
            row = await session.get(ProjectRow, project_id)
            if row is None:
                raise ProjectError(f"Unknown project: {project_id}", code="not_found")
            for key, value in fields.items():
                if key in allowed and value is not None:
                    if key == "git_repo" and value:
                        from forgemesh_control.workspaces import (
                            WorkspaceError,
                            validate_remote,
                        )

                        try:
                            value = validate_remote(str(value))
                        except WorkspaceError as exc:
                            raise ProjectError(str(exc)) from exc
                    setattr(row, key, value)
            await session.flush()
            payload = _row(row)
        await self.events.publish("project.updated", project_id=project_id)
        return payload

    async def delete(self, project_id: str, *, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            raise ProjectError("Project deletion requires confirm=true.")
        async with self.db.session_scope() as session:
            row = await session.get(ProjectRow, project_id)
            if row is None:
                raise ProjectError(f"Unknown project: {project_id}", code="not_found")
            if row.workspace_path:
                # Keep the workspace on disk; deleting source is destructive and
                # outside what the dashboard is allowed to do implicitly.
                pass
            await session.delete(row)
        await self.audit.record("project.delete", project=project_id, workspace_kept=True)
        await self.events.publish("project.deleted", project_id=project_id)
        return {"deleted": True, "workspace_kept": True}

    # ----------------------------------------------------------------- blueprint
    @staticmethod
    async def _latest(session, model, project_id: str) -> dict[str, Any] | None:
        row = (
            await session.execute(
                select(model)
                .where(model.project_id == project_id)
                .order_by(model.version.desc())
                .limit(1)
            )
        ).scalars().first()
        if row is None:
            return None
        return {
            "id": row.id,
            "version": row.version,
            "content": row.content or {},
            "status": row.status,
            "created_at": iso(row.created_at),
        }

    async def save_blueprint(
        self, project_id: str, content: dict[str, Any], *, status: str = "draft"
    ) -> dict[str, Any]:
        if not isinstance(content, dict):
            raise ProjectError("Blueprint content must be an object.")
        async with self.db.session_scope() as session:
            if await session.get(ProjectRow, project_id) is None:
                raise ProjectError(f"Unknown project: {project_id}", code="not_found")
            latest = (
                await session.execute(
                    select(BlueprintRow)
                    .where(BlueprintRow.project_id == project_id)
                    .order_by(BlueprintRow.version.desc())
                    .limit(1)
                )
            ).scalars().first()
            version = (latest.version + 1) if latest else 1
            row = BlueprintRow(
                project_id=project_id, version=version, content=content, status=status
            )
            session.add(row)
            await session.flush()
            payload = {
                "id": row.id,
                "version": row.version,
                "content": row.content,
                "status": row.status,
                "created_at": iso(row.created_at),
            }
        await self.audit.record("blueprint.save", project=project_id, version=version)
        await self.events.publish(
            "project.blueprint.saved", project_id=project_id, version=version
        )
        return payload

    async def save_ui_spec(
        self, project_id: str, content: dict[str, Any], *, status: str = "draft"
    ) -> dict[str, Any]:
        if not isinstance(content, dict):
            raise ProjectError("UI specification content must be an object.")
        async with self.db.session_scope() as session:
            if await session.get(ProjectRow, project_id) is None:
                raise ProjectError(f"Unknown project: {project_id}", code="not_found")
            latest = (
                await session.execute(
                    select(UISpecRow)
                    .where(UISpecRow.project_id == project_id)
                    .order_by(UISpecRow.version.desc())
                    .limit(1)
                )
            ).scalars().first()
            version = (latest.version + 1) if latest else 1
            row = UISpecRow(
                project_id=project_id, version=version, content=content, status=status
            )
            session.add(row)
            await session.flush()
            payload = {
                "id": row.id,
                "version": row.version,
                "content": row.content,
                "status": row.status,
                "created_at": iso(row.created_at),
            }
        await self.audit.record("ui_spec.save", project=project_id, version=version)
        await self.events.publish("project.ui_spec.saved", project_id=project_id, version=version)
        return payload

    # ----------------------------------------------------------------- approvals
    @staticmethod
    def _approval(row: ApprovalRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "project_id": row.project_id,
            "stage": row.stage,
            "summary": row.summary,
            "details": row.details or {},
            "status": row.status,
            "requested_at": iso(row.requested_at),
            "decided_at": iso(row.decided_at),
            "note": row.note,
        }

    async def request_approval(
        self, project_id: str, stage: str, *, summary: str = "", details: dict | None = None
    ) -> dict[str, Any]:
        if stage not in APPROVAL_STAGES:
            raise ProjectError(f"Unknown approval stage: {stage}")
        async with self.db.session_scope() as session:
            if await session.get(ProjectRow, project_id) is None:
                raise ProjectError(f"Unknown project: {project_id}", code="not_found")
            row = ApprovalRow(
                id=f"apr_{uuid.uuid4().hex[:10]}",
                project_id=project_id,
                stage=stage,
                summary=summary,
                details=details or {},
            )
            session.add(row)
            await session.flush()
            payload = self._approval(row)
        await self.audit.record("approval.request", project=project_id, stage=stage)
        await self.events.publish(
            "approval.requested", project_id=project_id, stage=stage, approval_id=payload["id"]
        )
        return payload

    async def decide_approval(
        self, approval_id: str, *, approved: bool, note: str = ""
    ) -> dict[str, Any]:
        async with self.db.session_scope() as session:
            row = await session.get(ApprovalRow, approval_id)
            if row is None:
                raise ProjectError(f"Unknown approval: {approval_id}", code="not_found")
            if row.status != "pending":
                raise ProjectError(f"Approval already {row.status}.")
            row.status = "approved" if approved else "rejected"
            row.note = note
            row.decided_at = datetime.now(UTC)
            await session.flush()
            payload = self._approval(row)
        await self.audit.record(
            "approval.decide",
            project=payload["project_id"],
            stage=payload["stage"],
            approved=approved,
        )
        await self.events.publish(
            "approval.completed",
            project_id=payload["project_id"],
            stage=payload["stage"],
            approved=approved,
        )
        return payload

    async def approval_state(self, project_id: str, stage: str) -> str:
        async with self.db.session_scope() as session:
            row = (
                await session.execute(
                    select(ApprovalRow)
                    .where(
                        ApprovalRow.project_id == project_id,
                        ApprovalRow.stage == stage,
                    )
                    .order_by(ApprovalRow.requested_at.desc())
                    .limit(1)
                )
            ).scalars().first()
            return row.status if row else "missing"

    async def assert_ready_for_implementation(self, project_id: str) -> None:
        blueprint = await self.approval_state(project_id, "blueprint")
        ui_spec = await self.approval_state(project_id, "ui_spec")
        implementation = await self.approval_state(project_id, "implementation")
        missing = [
            name
            for name, state in (
                ("blueprint", blueprint),
                ("ui_spec", ui_spec),
                ("implementation", implementation),
            )
            if state != "approved"
        ]
        if missing:
            raise ProjectError(
                "Implementation is blocked until these approvals are approved: "
                + ", ".join(missing)
            )

    # ------------------------------------------------------------- human actions
    @staticmethod
    def _action(row: HumanActionRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "project_id": row.project_id,
            "title": row.title,
            "description": row.description,
            "why": row.why,
            "feature": row.feature,
            "credential_name": row.credential_name,
            "status": row.status,
            "blocking": row.blocking,
            "created_at": iso(row.created_at),
            "resolved_at": iso(row.resolved_at),
        }

    async def add_human_action(
        self,
        project_id: str,
        *,
        title: str,
        description: str = "",
        why: str = "",
        feature: str = "",
        credential_name: str = "",
        blocking: bool = True,
    ) -> dict[str, Any]:
        if not title.strip():
            raise ProjectError("A human action needs a title.")
        async with self.db.session_scope() as session:
            if await session.get(ProjectRow, project_id) is None:
                raise ProjectError(f"Unknown project: {project_id}", code="not_found")
            row = HumanActionRow(
                id=f"act_{uuid.uuid4().hex[:10]}",
                project_id=project_id,
                title=title.strip()[:300],
                description=description,
                why=why,
                feature=feature,
                credential_name=credential_name,
                blocking=blocking,
            )
            session.add(row)
            await session.flush()
            payload = self._action(row)
        await self.events.publish(
            "human_action.created", project_id=project_id, action_id=payload["id"]
        )
        return payload

    async def list_human_actions(self, project_id: str) -> list[dict[str, Any]]:
        async with self.db.session_scope() as session:
            rows = (
                await session.execute(
                    select(HumanActionRow)
                    .where(HumanActionRow.project_id == project_id)
                    .order_by(HumanActionRow.created_at)
                )
            ).scalars()
            return [self._action(row) for row in rows]

    async def resolve_human_action(
        self, action_id: str, *, status: str, note: str = ""
    ) -> dict[str, Any]:
        if status not in {"pending", "in_progress", "done", "not_needed", "blocked"}:
            raise ProjectError(f"Unknown action status: {status}")
        async with self.db.session_scope() as session:
            row = await session.get(HumanActionRow, action_id)
            if row is None:
                raise ProjectError(f"Unknown human action: {action_id}", code="not_found")
            row.status = status
            if status in {"done", "not_needed"}:
                row.resolved_at = datetime.now(UTC)
            if note:
                row.description = f"{row.description}\n{note}".strip()
            await session.flush()
            payload = self._action(row)
        await self.audit.record(
            "human_action.update",
            project=payload["project_id"],
            action_id=action_id,
            status=status,
        )
        await self.events.publish(
            "human_action.updated",
            project_id=payload["project_id"],
            action_id=action_id,
            status=status,
        )
        return payload

    # ------------------------------------------------------------------- secrets
    def _vault_key(self, project_id: str) -> str:
        return f"project-{project_id}@forgemesh.local"

    async def set_secret(self, project_id: str, name: str, value: str) -> dict[str, Any]:
        from forgemesh_control.security import safe_slug

        if not value:
            raise ProjectError("Refusing to store an empty secret.")
        key = safe_slug(name, fallback="secret", max_length=80)
        self.vault.set_password(self._vault_key(project_id), value, provider=key)
        async with self.db.session_scope() as session:
            existing = (
                await session.execute(
                    select(SecretRefRow).where(
                        SecretRefRow.project_id == project_id, SecretRefRow.name == key
                    )
                )
            ).scalars().first()
            if existing is None:
                session.add(
                    SecretRefRow(
                        project_id=project_id,
                        name=key,
                        vault_email=self._vault_key(project_id),
                        vault_provider=key,
                    )
                )
        await self.audit.record(
            "project.secret.set", project=project_id, name=key
        )
        await self.events.publish(
            "project.secret.stored", project_id=project_id, name=key
        )
        return {"name": key, "stored": True}

    async def list_secrets(self, project_id: str) -> list[dict[str, Any]]:
        async with self.db.session_scope() as session:
            rows = (
                await session.execute(
                    select(SecretRefRow)
                    .where(SecretRefRow.project_id == project_id)
                    .order_by(SecretRefRow.name)
                )
            ).scalars()
            index = self.vault.index()
            result = []
            for row in rows:
                entry = index.get(row.vault_email, {})
                result.append(
                    {
                        "name": row.name,
                        "configured": bool(entry.get("providers") and row.vault_provider in entry["providers"]),
                        "created_at": iso(row.created_at),
                    }
                )
            return result

    async def delete_secret(self, project_id: str, name: str) -> dict[str, Any]:
        removed = self.vault.delete_password(
            self._vault_key(project_id), provider=name
        )
        async with self.db.session_scope() as session:
            row = (
                await session.execute(
                    select(SecretRefRow).where(
                        SecretRefRow.project_id == project_id, SecretRefRow.name == name
                    )
                )
            ).scalars().first()
            if row is not None:
                await session.delete(row)
        await self.audit.record("project.secret.delete", project=project_id, name=name)
        return {"removed": removed}
