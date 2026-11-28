"""Auth-derived AGTools namespace for documents attached to one conversation."""

import hashlib
import json
import os
import re

from app.core.auth import ExecutionScope


ATTACHMENT_PREFIX = "att-"
ATTACHMENT_RUN_PREFIX = "run-attachment-"


def is_session_attachment(document_id: str) -> bool:
    return document_id.startswith(ATTACHMENT_PREFIX)


def attachments_enabled(service_id: str) -> bool:
    return service_id in {entry.strip() for entry in os.environ.get("AGCORE_SESSION_ATTACHMENT_SERVICES", "").split(",")}


def attachment_tenant(scope: ExecutionScope, conversation_id: str | None) -> str:
    if not scope.authenticated or not attachments_enabled(scope.service_id):
        raise ValueError("session_attachments_disabled")
    if not conversation_id or not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", conversation_id):
        raise ValueError("attachment_conversation_required")
    key = json.dumps([scope.tenant_id, scope.owner_id, scope.service_id, conversation_id], ensure_ascii=True)
    return "session-attachment-" + hashlib.sha256(key.encode()).hexdigest()


def raw_attachment_id(attachment_id: str) -> str:
    raw_id = attachment_id.removeprefix(ATTACHMENT_PREFIX)
    if not is_session_attachment(attachment_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,124}", raw_id):
        raise ValueError("invalid_attachment_reference")
    return raw_id
