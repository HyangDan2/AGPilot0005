"""Conversation Workspace binding endpoints and run contract."""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.core.auth import ConversationWorkspace, ExecutionScope, require_authenticated_caller
from app.schemas.agent import AgentRunRequest
from app.workspace.binding import (
    ConversationWorkspaceError,
    bind_conversation_workspace,
    check_workspace_binding,
    find_conversation_workspace,
)

router = APIRouter(prefix="/conversations", tags=["Conversations"])


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
