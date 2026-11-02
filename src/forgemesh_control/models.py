"""Durable data model for the control center.

SQLite is used deliberately: ForgeMesh targets a single Ubuntu laptop, and the
existing browser runtime already uses SQLite. The schema uses foreign keys,
indexes and explicit status columns; nothing critical lives only in memory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def iso(value: datetime | None) -> str | None:
    restored = as_utc(value)
    return restored.isoformat() if restored else None


class Base(DeclarativeBase):
    pass


class SettingRow(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    important: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class WorkerRow(Base):
    __tablename__ = "workers"
    __table_args__ = (UniqueConstraint("device_key", name="uq_worker_device"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    device_key: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    os_family: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    os_version: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    arch: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="offline", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    inventory: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    current_task_id: Mapped[str | None] = mapped_column(String(40))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PairingCodeRow(Base):
    __tablename__ = "pairing_codes"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label: Mapped[str] = mapped_column(String(120), default="", nullable=False)


class CentralAgentRow(Base):
    """A coding CLI installed on the central server (not a worker agent).

    Installation, verification and authentication are deliberately separate
    columns: an executable that starts is not the same thing as a signed-in
    agent that can run a coding task.
    """

    __tablename__ = "central_agents"

    kind: Mapped[str] = mapped_column(String(40), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    installed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    path: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    version: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    install_status: Mapped[str] = mapped_column(
        String(24), default="not_detected", nullable=False
    )
    install_stage: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    install_source: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    install_log: Mapped[str] = mapped_column(String(700), default="", nullable=False)
    configuration_status: Mapped[str] = mapped_column(
        String(24), default="unconfigured", nullable=False
    )
    auth_status: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    availability: Mapped[str] = mapped_column(
        String(24), default="unavailable", nullable=False
    )
    os_supported: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    update_policy: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    auto_update: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    last_install_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class AgentRow(Base):
    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("worker_id", "kind", name="uq_worker_agent"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_id: Mapped[str] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    path: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    installed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    usable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ModelRow(Base):
    __tablename__ = "models"
    __table_args__ = (UniqueConstraint("worker_id", "provider", "model_id", name="uq_model"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_id: Mapped[str | None] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    context_window: Mapped[int | None] = mapped_column(Integer)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    availability: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="detected", nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectRow(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default="normal", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    workspace_path: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    git_repo: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    problem_id: Mapped[str | None] = mapped_column(String(40), index=True)
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    path: Mapped[str] = mapped_column(String(700), nullable=False)
    mime: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="uploaded", nullable=False)
    text_path: Mapped[str] = mapped_column(String(700), default="", nullable=False)
    extracted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ProblemRow(Base):
    __tablename__ = "problems"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )
    structured: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="intake", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class MeetingRow(Base):
    __tablename__ = "council_meetings"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    problem_id: Mapped[str | None] = mapped_column(
        ForeignKey("problems.id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(String(20), default="normal", nullable=False)
    agenda: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    current_round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rounds_total: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, default="", nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ParticipantRow(Base):
    __tablename__ = "council_participants"
    __table_args__ = (UniqueConstraint("meeting_id", "provider_id", name="uq_participant"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("council_meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_id: Mapped[str] = mapped_column(String(40), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    #: The ai_accounts row this seat speaks as. provider_id alone cannot tell two
    #: accounts of one provider apart, and the brief requires account provenance
    #: in the meeting and execution logs.
    account_id: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    account_label: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    council_role: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="idle", nullable=False)
    included: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageRow(Base):
    __tablename__ = "council_messages"
    __table_args__ = (Index("ix_message_meeting_round", "meeting_id", "round"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("council_meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_id: Mapped[str] = mapped_column(String(40), default="system", nullable=False)
    account_id: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    profile_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="participant", nullable=False)
    kind: Mapped[str] = mapped_column(String(30), default="response", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    structured: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ProposalRow(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("council_meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    problem_id: Mapped[str | None] = mapped_column(String(40), index=True)
    author: Mapped[str] = mapped_column(String(40), nullable=False)
    account_id: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    account_label: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    feasibility: Mapped[int | None] = mapped_column(Integer)
    risks: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    confidence: Mapped[int | None] = mapped_column(Integer)
    responses: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="open", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class DecisionRow(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(
        ForeignKey("council_meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    disagreements: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class BlueprintRow(Base):
    __tablename__ = "blueprints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class UISpecRow(Base):
    __tablename__ = "ui_specs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)


class HumanActionRow(Base):
    __tablename__ = "human_actions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    why: Mapped[str] = mapped_column(Text, default="", nullable=False)
    feature: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    credential_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    blocking: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_project_status", "project_id", "status"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(
        ForeignKey("workers.id", ondelete="SET NULL"), index=True
    )
    agent_kind: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    model_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    acceptance: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    dependencies: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    files: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    allocation_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    risk: Mapped[str] = mapped_column(String(20), default="medium", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class TaskEventRow(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class WorkspaceRow(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    path: Mapped[str] = mapped_column(String(700), nullable=False)
    branch: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    base_branch: Mapped[str] = mapped_column(String(200), default="main", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class TestRunRow(Base):
    __tablename__ = "test_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), index=True
    )
    command: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False)
    output: Mapped[str] = mapped_column(Text, default="", nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[str | None] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="file", nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    path: Mapped[str] = mapped_column(String(700), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ReportRow(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), default="summary", nullable=False)
    path: Mapped[str] = mapped_column(String(700), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class NotificationRow(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    level: Mapped[str] = mapped_column(String(20), default="info", nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SecretRefRow(Base):
    """A named pointer to a vault entry; the secret itself never lands here."""

    __tablename__ = "secret_refs"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_secret_ref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    vault_email: Mapped[str] = mapped_column(String(320), nullable=False)
    vault_provider: Mapped[str] = mapped_column(String(80), default="*", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AuditRow(Base):
    __tablename__ = "audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(60), default="dashboard", nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class MetricSampleRow(Base):
    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cpu_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    ram_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    disk_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    rx_kbps: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tx_kbps: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class AiAccountRow(Base):
    """One provider account the user owns.

    The session itself lives in the ForgeMesh Browser profile named by
    ``profile_id``; this row is the control center's registry: which label,
    which isolated profile, whether the user wants it in the council, and when
    a real verification last succeeded. No cookie, token or password is stored
    here - the browser runtime keeps those in its own profile directory.
    """

    __tablename__ = "ai_accounts"
    __table_args__ = (UniqueConstraint("provider_id", "label", name="uq_ai_account_label"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Typed by the user so they can tell their own accounts apart. Never
    #: harvested from the page and never used to select or rotate accounts.
    email: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    council_role: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="unverified", nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class AgentAccountRow(Base):
    """A coding-agent credential the CLI itself reports.

    Rows are rebuilt from ``opencode providers list`` / ``qodercli status`` so
    the dashboard cannot drift from what the tool actually has. ``switchable``
    records the CLI's real capability rather than an assumed one.
    """

    __tablename__ = "agent_accounts"
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_agent_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: ``signed_in``, ``signed_out``, ``unknown`` or ``error`` - as reported by
    #: the CLI itself, never inferred from a button press.
    status: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: False when the CLI exposes no way to hold or change more than one account.
    switchable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: The CLI's own config root for this account. Empty means the tool's
    #: default location; ``qodercli --config-dir`` makes a separate root real.
    config_dir: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="cli", nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class AgentAuthSessionRow(Base):
    """Audit trail for an interactive login run in the app's own terminal.

    ``tail`` keeps the last screen of real output so a closed session can still
    be explained; credentials typed by the user are never captured.
    """

    __tablename__ = "agent_auth_sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    command: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
