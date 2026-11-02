"""Document upload and extraction API."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select

from forgemesh_control.api.deps import get_runtime
from forgemesh_control.documents import (
    DocumentError,
    extract_problem_fields,
    split_problem_statements,
)
from forgemesh_control.models import DocumentRow, iso
from forgemesh_control.projects import ProjectError

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _row(row: DocumentRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "filename": row.filename,
        "mime": row.mime,
        "size": row.size,
        "sha256": row.sha256,
        "status": row.status,
        "extracted": row.extracted or {},
        "error": row.error,
        "created_at": iso(row.created_at),
    }


@router.get("")
async def list_documents(request: Request, project_id: str | None = None) -> dict[str, Any]:
    runtime = get_runtime(request)
    async with runtime.db.session_scope() as session:
        statement = select(DocumentRow).order_by(DocumentRow.created_at.desc()).limit(200)
        if project_id:
            statement = statement.where(DocumentRow.project_id == project_id)
        rows = (await session.execute(statement)).scalars().all()
    return {"documents": [_row(row) for row in rows]}


@router.get("/{document_id}")
async def get_document(document_id: str, request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    async with runtime.db.session_scope() as session:
        row = await session.get(DocumentRow, document_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown document")
        payload = _row(row)
    text_path = Path(row.text_path) if row.text_path else None
    if text_path and text_path.is_file():
        payload["text"] = text_path.read_text(encoding="utf-8", errors="replace")[
            : request.app.state.settings.max_extracted_chars
        ]
    return payload


@router.post("")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    project_id: str | None = Form(default=None),
) -> dict[str, Any]:
    runtime = get_runtime(request)
    if project_id:
        try:
            await runtime.projects.get(project_id)
        except ProjectError as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
    data = await file.read(runtime.settings.max_archive_bytes + 1)
    try:
        path, digest, size = runtime.documents.save(file.filename or "upload", data)
    except DocumentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    document_id = f"doc_{uuid.uuid4().hex[:10]}"
    extracted: dict[str, Any] = {}
    text = ""
    status = "uploaded"
    error: str | None = None
    try:
        # PDF/OCR/archive extraction shells out and can take minutes; keep the
        # event loop free for status, WebSocket and every other request.
        result = await asyncio.to_thread(runtime.documents.extract, path)
        text = str(result.get("text") or "")
        extracted = {
            "kind": result.get("kind"),
            "pages": result.get("pages"),
            "chars": len(text),
            "fields": extract_problem_fields(text),
        }
        if result.get("entries") is not None:
            extracted["entries"] = result["entries"]
        text_path = runtime.settings.uploads_dir / "extracted" / f"{document_id}.txt"
        text_path.parent.mkdir(parents=True, exist_ok=True)
        # Bound what is persisted so one huge PDF cannot fill the disk.
        text_path.write_text(
            text[: runtime.settings.max_extracted_chars], encoding="utf-8"
        )
        status = "extracted"
    except DocumentError as exc:
        status = "failed"
        error = str(exc)
        text_path = Path("")

    async with runtime.db.session_scope() as session:
        row = DocumentRow(
            id=document_id,
            project_id=project_id,
            filename=path.name,
            path=str(path),
            mime=file.content_type or "",
            size=size,
            sha256=digest,
            status=status,
            text_path=str(text_path) if text_path else "",
            extracted=extracted,
            error=error,
        )
        session.add(row)
        await session.flush()
        payload = _row(row)

    if text and status == "extracted":
        statements = split_problem_statements(text)
        if statements:
            payload["statements"] = statements
    await runtime.events.publish(
        "document.uploaded",
        document_id=document_id,
        project_id=project_id,
        status=status,
    )
    if status == "failed":
        payload["warning"] = (
            "The file was stored but text extraction failed. The error is shown "
            "verbatim; nothing was invented."
        )
    return payload
