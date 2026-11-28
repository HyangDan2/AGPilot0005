"""Artifact & document modification on AGCore (WP-08).

Artifacts live in the workspace engine that already exists: one blob store, one revision table,
one set of access rules. This module adds only the contract-shaped operations and the integrity
checks around them.

Guarantees
----------
* atomic write      — delegated to ``WorkspaceManager.write_file`` (temp file + os.replace)
* revisions         — every write is a new revision; history is never overwritten
* optimistic locking — ``expected_content_hash`` is compared before the write (workspace CP-02 #5)
* rollback          — an older revision's bytes are written forward as a new revision
* integrity         — hash + size are re-read from storage and compared with the recorded row
"""

from __future__ import annotations

import base64
import hashlib
from typing import Optional

from app.knowledge.contracts import (
    ArtifactCreateRequest,
    ArtifactResponse,
    ArtifactRollbackRequest,
    ArtifactUpdateRequest,
    ArtifactValidateRequest,
    ArtifactValidateResponse,
    DocumentNamespace,
    DocumentRef,
    IntegrityCheck,
    KnowledgeError,
    KnowledgeErrorBody,
    KnowledgeErrorCode,
    compute_content_hash,
    normalize_mime,
)
from app.workspace.contracts import ArtifactReferenceInput, WorkspaceErrorCode, WorkspaceWriteRequest
from app.workspace.manager import WorkspaceFailure


class ArtifactService:
    def __init__(self, runtime) -> None:
        self.runtime = runtime

    # ------------------------------------------------------------------ helpers
    def _payload(self, content_base64: Optional[str], text: Optional[str]) -> bytes:
        if content_base64:
            try:
                return base64.b64decode(content_base64.split(",", 1)[-1])
            except Exception as exc:
                raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "invalid content_base64") from exc
        if text is not None:
            return text.encode("utf-8")
        raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "content or text required")

    def _failure(self, exc: WorkspaceFailure) -> KnowledgeError:
        code = exc.error.code
        mapping = {
            WorkspaceErrorCode.CONTENT_CONFLICT: KnowledgeErrorCode.REVISION_CONFLICT,
            WorkspaceErrorCode.FILE_EXISTS: KnowledgeErrorCode.REVISION_CONFLICT,
            WorkspaceErrorCode.FILE_NOT_FOUND: KnowledgeErrorCode.DOCUMENT_NOT_FOUND,
            WorkspaceErrorCode.WORKSPACE_NOT_FOUND: KnowledgeErrorCode.DOCUMENT_NOT_FOUND,
            WorkspaceErrorCode.READ_ONLY_WORKSPACE: KnowledgeErrorCode.PERMISSION_DENIED,
            WorkspaceErrorCode.UNSUPPORTED_MEDIA_TYPE: KnowledgeErrorCode.UNSUPPORTED_FORMAT,
            WorkspaceErrorCode.INVALID_PATH: KnowledgeErrorCode.INVALID_REQUEST,
            WorkspaceErrorCode.INVALID_REQUEST: KnowledgeErrorCode.INVALID_REQUEST,
        }
        return KnowledgeError(
            mapping.get(code, KnowledgeErrorCode.INTERNAL_ERROR),
            exc.error.message or code.value,
            detail={"workspace_code": code.value},
            retryable=code is WorkspaceErrorCode.CONTENT_CONFLICT,
        )

    def _write(
        self,
        scope,
        *,
        workspace_id: str,
        path: str,
        mime_type: str,
        data: bytes,
        overwrite: bool,
        expected_content_hash: Optional[str],
        artifact_id: Optional[str],
        conversation_id: Optional[str],
    ):
        request = WorkspaceWriteRequest(
            path=path,
            content_base64=base64.b64encode(data).decode("ascii"),
            mime_type=normalize_mime(mime_type, filename=path),
            expected_content_hash=expected_content_hash,
            overwrite=overwrite,
            create_parents=True,
        )
        try:
            result = self.runtime.manager.write_file(workspace_id, scope, request)
        except WorkspaceFailure as exc:
            raise self._failure(exc) from exc
        if result.client_write is not None:
            raise KnowledgeError(
                KnowledgeErrorCode.PERMISSION_DENIED,
                "browser-local workspace requires a client write",
                detail={"client_handle_id": result.client_write.client_handle_id},
            )
        if result.file is None:
            raise KnowledgeError(KnowledgeErrorCode.INTERNAL_ERROR, "workspace write returned no file")
        if artifact_id:
            try:
                self.runtime.manager.register_artifact_reference(
                    workspace_id,
                    scope,
                    ArtifactReferenceInput(
                        artifact_id=artifact_id,
                        kind="artifact",
                        file_id=result.file.file_id,
                        metadata={"conversation_id": conversation_id} if conversation_id else {},
                    ),
                )
            except WorkspaceFailure:
                pass
        return result

    def _rel_path(self, workspace_id: str, record) -> str:
        """Rebuild the POSIX relative path for a managed blob workspace row."""

        parts = [record.get("name") or record["file_id"]]
        parent = record.get("parent_id")
        guard = 0
        while parent and guard < 64:
            row = self.runtime.store.get_file(workspace_id, parent)
            if row is None:
                break
            parts.append(row.get("name") or row["file_id"])
            parent = row.get("parent_id")
            guard += 1
        return "/".join(reversed(parts))

    def _ref(self, workspace_id: str, file_id: str) -> DocumentRef:
        record = self.runtime.store.get_file(workspace_id, file_id)
        if record is None:
            raise KnowledgeError(KnowledgeErrorCode.DOCUMENT_NOT_FOUND, file_id)
        return DocumentRef(
            namespace=DocumentNamespace.ARTIFACT,
            document_id=file_id,
            name=record.get("name") or file_id,
            mime_type=normalize_mime(record.get("mime_type"), filename=record.get("name")),
            revision=int(record.get("revision") or 1),
            content_hash=record.get("content_hash") or "",
            size_bytes=int(record.get("size_bytes") or 0),
            metadata={"workspace_id": workspace_id, "path": record.get("rel_path") or self._rel_path(workspace_id, record)},
        )

    # ------------------------------------------------------------------ operations
    def create(self, scope, request: ArtifactCreateRequest) -> ArtifactResponse:
        data = self._payload(request.content_base64, request.text)
        result = self._write(
            scope,
            workspace_id=request.workspace_id,
            path=request.path,
            mime_type=request.mime_type,
            data=data,
            overwrite=request.overwrite,
            expected_content_hash=request.expected_content_hash,
            artifact_id=request.artifact_id,
            conversation_id=request.conversation_id,
        )
        assert result.file is not None
        return ArtifactResponse(
            document=self._ref(request.workspace_id, result.file.file_id),
            workspace_id=request.workspace_id,
            path=result.path,
            revision=result.revision,
            content_hash=result.content_hash or "",
            created=True,
        )

    def update(self, scope, request: ArtifactUpdateRequest) -> ArtifactResponse:
        record = self.runtime.store.get_file(request.workspace_id, request.file_id)
        if record is None:
            raise KnowledgeError(KnowledgeErrorCode.DOCUMENT_NOT_FOUND, request.file_id)
        current_revision = int(record.get("revision") or 1)
        if request.expected_revision is not None and request.expected_revision != current_revision:
            raise KnowledgeError(
                KnowledgeErrorCode.REVISION_CONFLICT,
                f"expected revision {request.expected_revision}, current is {current_revision}",
                detail={"expected": request.expected_revision, "current": current_revision},
                retryable=True,
            )
        data = self._payload(request.content_base64, request.text)
        result = self._write(
            scope,
            workspace_id=request.workspace_id,
            path=record.get("rel_path") or self._rel_path(workspace_id=request.workspace_id, record=record),
            mime_type=request.mime_type or record.get("mime_type") or "application/octet-stream",
            data=data,
            overwrite=True,
            expected_content_hash=request.expected_content_hash,
            artifact_id=None,
            conversation_id=None,
        )
        assert result.file is not None
        return ArtifactResponse(
            document=self._ref(request.workspace_id, result.file.file_id),
            workspace_id=request.workspace_id,
            path=result.path,
            revision=result.revision,
            content_hash=result.content_hash or "",
            created=False,
        )

    def rollback(self, scope, request: ArtifactRollbackRequest) -> ArtifactResponse:
        record = self.runtime.store.get_file(request.workspace_id, request.file_id)
        if record is None:
            raise KnowledgeError(KnowledgeErrorCode.DOCUMENT_NOT_FOUND, request.file_id)
        try:
            historical = self.runtime.manager.read_file(
                request.workspace_id, request.file_id, scope, revision=request.to_revision
            )
        except WorkspaceFailure as exc:
            raise self._failure(exc) from exc
        data, mime_type = historical.bytes, historical.metadata.mime_type
        result = self._write(
            scope,
            workspace_id=request.workspace_id,
            path=record.get("rel_path") or self._rel_path(request.workspace_id, record),
            mime_type=mime_type or record.get("mime_type") or "application/octet-stream",
            data=data,
            overwrite=True,
            expected_content_hash=request.expected_content_hash,
            artifact_id=None,
            conversation_id=None,
        )
        assert result.file is not None
        return ArtifactResponse(
            document=self._ref(request.workspace_id, result.file.file_id),
            workspace_id=request.workspace_id,
            path=result.path,
            revision=result.revision,
            content_hash=result.content_hash or "",
            created=False,
            warnings=[f"rollback_to:{request.to_revision}"],
        )

    def validate(self, scope, request: ArtifactValidateRequest) -> ArtifactValidateResponse:
        record = self.runtime.store.get_file(request.workspace_id, request.file_id)
        if record is None:
            raise KnowledgeError(KnowledgeErrorCode.DOCUMENT_NOT_FOUND, request.file_id)
        ref = self._ref(request.workspace_id, request.file_id)
        checks = []
        try:
            data = self.runtime.manager.read_file(request.workspace_id, request.file_id, scope)
        except WorkspaceFailure as exc:
            raise self._failure(exc) from exc
        actual = compute_content_hash(data.bytes)
        recorded = data.content_hash or record.get("content_hash") or ""
        checks.append(
            IntegrityCheck(
                name="content_hash",
                ok=(not recorded) or actual == recorded,
                detail="" if actual == recorded else f"expected {recorded}",
            )
        )
        checks.append(
            IntegrityCheck(
                name="size",
                ok=len(data.bytes) == int(record.get("size_bytes") or len(data.bytes)),
                detail=f"{len(data.bytes)} bytes",
            )
        )
        try:
            revisions = self.runtime.store.list_revisions(request.file_id)
            checks.append(
                IntegrityCheck(
                    name="revision_row",
                    ok=any(int(row.get("revision") or 0) == int(record.get("revision") or 0) for row in revisions),
                    detail=f"{len(revisions)} revision(s)",
                )
            )
        except Exception:
            checks.append(IntegrityCheck(name="revision_row", ok=False, detail="revision store unavailable"))
        ok = all(check.ok for check in checks)
        errors = (
            []
            if ok
            else [
                KnowledgeErrorBody(
                    code=KnowledgeErrorCode.INTEGRITY_MISMATCH,
                    message="; ".join(f"{check.name}: {check.detail}" for check in checks if not check.ok),
                )
            ]
        )
        return ArtifactValidateResponse(document=ref, ok=ok, checks=checks, errors=errors)
