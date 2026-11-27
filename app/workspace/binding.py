"""Immutable Workspace context for an authenticated, managed conversation."""
import re
from typing import Optional

from app.config import is_workspace_enabled, is_workspace_enabled_for_service
from app.core.auth import ConversationWorkspace, ExecutionScope
from app.workspace.config import get_settings
from app.workspace.manager import WorkspaceFailure
from app.workspace.runtime import get_runtime
from app.workspace.store import WorkspaceStore


class ConversationWorkspaceError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def find_conversation_workspace(
    scope: ExecutionScope, conversation_id: Optional[str],
) -> Optional[ConversationWorkspace]:
    if not conversation_id or not getattr(scope, "authenticated", False):
        return None
    key = (scope.tenant_id, scope.owner_id, scope.service_id, conversation_id.strip())
    if is_workspace_enabled():
        row = get_runtime().store.find_conversation_workspace(*key)
    else:
        row = WorkspaceStore.find_existing_conversation_workspace(get_settings().resolved_db_path(), *key)
    return ConversationWorkspace(**row) if row is not None else None


def check_workspace_binding(scope: ExecutionScope, binding: ConversationWorkspace) -> None:
    if not scope.authenticated:
        raise ConversationWorkspaceError("workspace_disabled_for_service", 403)
    if binding.workspace_id is None:
        return
    if not is_workspace_enabled_for_service(scope.service_id):
        raise ConversationWorkspaceError("workspace_disabled_for_service", 403)
    try:
        workspace = get_runtime().manager.ensure_open(binding.workspace_id, scope)
    except WorkspaceFailure as exc:
        raise ConversationWorkspaceError(exc.error.code.value, exc.status_code) from exc
    if workspace.service_id != scope.service_id:
        raise ConversationWorkspaceError("workspace_not_found", 404)


def bind_conversation_workspace(
    scope: ExecutionScope, conversation_id: str, workspace_id: Optional[str],
) -> ConversationWorkspace:
    conversation_id = (conversation_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", conversation_id):
        raise ConversationWorkspaceError("invalid_conversation_id", 422)
    if workspace_id is not None:
        workspace_id = workspace_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workspace_id):
            raise ConversationWorkspaceError("invalid_workspace_id", 422)
    requested = ConversationWorkspace(conversation_id, workspace_id)
    existing = find_conversation_workspace(scope, conversation_id)
    if existing is not None and existing != requested:
        raise ConversationWorkspaceError("conversation_workspace_conflict", 409)
    if existing is None and not is_workspace_enabled_for_service(scope.service_id):
        raise ConversationWorkspaceError("workspace_disabled_for_service", 403)
    check_workspace_binding(scope, requested)
    if existing is not None:
        return existing
    row = get_runtime().store.bind_conversation_workspace(
        scope.tenant_id, scope.owner_id, scope.service_id, conversation_id, workspace_id,
    )
    stored = ConversationWorkspace(**row)
    if stored != requested:
        raise ConversationWorkspaceError("conversation_workspace_conflict", 409)
    return stored
