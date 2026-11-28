"""Conversation Workspace binding endpoints and run contract."""
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.adapters.document import AGToolsHttpAdapter
from app.core.auth import (
    ConversationWorkspace, ExecutionScope, current_execution_scope,
    current_run_conversation_id, require_authenticated_caller,
)
from app.core.http_uploads import parse_multipart_upload
from app.core.session_attachments import ATTACHMENT_PREFIX, attachments_enabled, raw_attachment_id
from app.schemas.agent import AgentRunRequest
from app.workspace.binding import (
    ConversationWorkspaceError,
    bind_conversation_workspace,
    check_workspace_binding,
    find_conversation_workspace,
)

router = APIRouter(prefix="/conversations", tags=["Conversations"])
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


async def _attachment_scope(request: Request, conversation_id: str) -> ExecutionScope:
    scope = require_authenticated_caller(request)
    if not attachments_enabled(scope.service_id):
        raise HTTPException(403, "session_attachments_disabled")
    binding = await run_in_threadpool(find_conversation_workspace, scope, conversation_id)
    if binding is None:
        raise HTTPException(404, "conversation_not_found")
    try:
        await run_in_threadpool(check_workspace_binding, scope, binding)
    except ConversationWorkspaceError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    return scope


async def _parse_attachment_request(scope: ExecutionScope, conversation_id: str, operation, *args) -> dict:
    scope_token = current_execution_scope.set(scope)
    session_token = current_run_conversation_id.set(conversation_id)
    try:
        response = await operation(*args)
        if response.status_code in (403, 404):
            raise HTTPException(404, "attachment_not_found")
        if response.status_code in (400, 413, 415, 422, 429):
            raise HTTPException(response.status_code, "attachment_parser_rejected")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("invalid_parser_result")
        return result
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        raise HTTPException(503, "attachment_parser_unavailable") from exc
    finally:
        current_run_conversation_id.reset(session_token)
        current_execution_scope.reset(scope_token)


@router.post("/{conversation_id}/attachments", status_code=202, openapi_extra={"requestBody": {
    "required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["file"],
        "properties": {"file": {"type": "string", "format": "binary"}},
    }}},
}})
async def upload_session_attachment(conversation_id: str, request: Request):
    scope = await _attachment_scope(request, conversation_id)
    content_type = request.headers.get("content-type", "")
    if len(content_type) > 1024 or content_type.partition(";")[0].strip().lower() != "multipart/form-data":
        raise HTTPException(415, "attachment_multipart_required")
    filename, mime, file, _ = parse_multipart_upload(content_type, await request.body(), single_file=True)
    if not file:
        raise HTTPException(422, "attachment_empty")
    if len(file) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, "attachment_size_invalid")
    if len(filename) > 240 or any(c in filename for c in ("/", "\\", "\x00", "\r", "\n")):
        raise HTTPException(422, "invalid_attachment_filename")
    result = await _parse_attachment_request(
        scope, conversation_id, AGToolsHttpAdapter().upload_attachment,
        filename, file, mime or "application/octet-stream",
    )
    identifier = result.get("document_id")
    if not isinstance(identifier, str):
        raise HTTPException(503, "attachment_parser_unavailable")
    attachment_id = ATTACHMENT_PREFIX + identifier
    try:
        raw_attachment_id(attachment_id)
    except ValueError as exc:
        raise HTTPException(503, "attachment_parser_unavailable") from exc
    return {"document_id": attachment_id, "status": result.get("status"), "filename": filename}


@router.get("/{conversation_id}/attachments/{attachment_id}/{view}")
async def read_session_attachment(
    conversation_id: str, attachment_id: str, view: Literal["status", "text", "outline"], request: Request,
):
    scope = await _attachment_scope(request, conversation_id)
    try:
        raw_attachment_id(attachment_id)
    except ValueError as exc:
        raise HTTPException(404, "attachment_not_found") from exc
    result = await _parse_attachment_request(
        scope, conversation_id, AGToolsHttpAdapter().read_attachment, attachment_id, view,
    )
    fields = {"status": ("status", "page_count", "section_count"), "outline": ("sections",), "text": ("text",)}
    data = {field: result[field] for field in fields[view] if field in result}
    if view == "text" and isinstance(data.get("text"), str):
        data["text"] = data["text"][:40000]
    return {"document_id": attachment_id, **data}


class ConversationWorkspaceRequest(BaseModel):
    workspace_id: Optional[str]


@router.put("/{conversation_id}/workspace")
async def set_conversation_workspace(
    conversation_id: str, body: ConversationWorkspaceRequest, request: Request,
) -> ConversationWorkspace:
    scope = require_authenticated_caller(request)
    try:
        return await run_in_threadpool(bind_conversation_workspace, scope, conversation_id, body.workspace_id)
    except ConversationWorkspaceError as exc:
        raise HTTPException(exc.status, exc.code) from exc


def resolve_run_binding(req: AgentRunRequest, scope: ExecutionScope) -> AgentRunRequest:
    try:
        conversation_id = (req.conversation_id or "").strip()
        if req.bind_workspace and not conversation_id:
            raise ConversationWorkspaceError("conversation_id_required", 422)
        binding = find_conversation_workspace(scope, conversation_id)
        field_given = "workspace_id" in req.model_fields_set
        workspace_id = req.workspace_id.strip() if req.workspace_id is not None else None
        if binding is not None:
            if field_given and workspace_id != binding.workspace_id:
                raise ConversationWorkspaceError("conversation_workspace_conflict", 409)
            check_workspace_binding(scope, binding)
        elif req.bind_workspace:
            if not field_given:
                raise ConversationWorkspaceError("workspace_binding_value_required", 422)
            binding = bind_conversation_workspace(scope, conversation_id, workspace_id)
        if binding is None:
            return req
        return req.model_copy(update={
            "bind_workspace": True, "conversation_id": binding.conversation_id, "workspace_id": binding.workspace_id,
        })
    except ConversationWorkspaceError as exc:
        raise HTTPException(exc.status, exc.code) from exc
