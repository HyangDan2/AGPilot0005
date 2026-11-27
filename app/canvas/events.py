"""AI artifact event contract and streaming sessions (WO-C06).

Seven event types, compatible with the existing SSE envelope (``app.schemas.events.SSEEvent``:
``run_id`` / ``event_id`` / ``timestamp`` / ``status`` + flattened ``data``):

    artifact.created
    artifact.delta
    artifact.updated
    artifact.edit.proposed
    artifact.edit.applied
    artifact.edit.rejected
    artifact.error

Rules implemented here
----------------------
* **Explicit creation only.** A generation starts because of an explicit tool call, artifact event
  or user request. Nothing scans model output for code fences, so a normal Markdown answer never
  silently becomes an artifact.
* **Deltas are provisional.** ``artifact.delta`` accumulates in memory only. No row is written until
  the stream completes, so an aborted or failed stream leaves no artifact and no revision behind.
* **One flushed revision per stream.** Completion performs exactly one
  ``ArtifactManager.create`` with the whole accumulated content.
* **Idempotent replay.** Every generation carries an idempotency key; a reconnect or duplicate
  delivery resolves to the same artifact (``duplicate: true``) instead of a second artifact or
  revision. Duplicate delta sequences within a session return the original frame.
"""
from __future__ import annotations

import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.manager import ArtifactManager, assert_workspace_level, scope_ids
from app.canvas.models import Artifact, Revision
from app.canvas.schemas import ARTIFACT_EVENT_TYPES  # noqa: F401  (re-exported contract constant)
from app.schemas.events import SSEEvent

SESSION_STATUS = ("started", "streaming", "completed", "aborted", "failed")


def artifact_event(
    event: str,
    *,
    artifact_id: str,
    data: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    event_id: Optional[str] = None,
    status: str = "streaming",
) -> SSEEvent:
    if event not in ARTIFACT_EVENT_TYPES:
        raise CanvasError(CanvasErrorCode.INVALID_REQUEST, f"unknown artifact event '{event}'")
    payload = {"artifact_id": artifact_id, **(data or {})}
    return SSEEvent(
        run_id=run_id or f"canvas-{uuid.uuid4().hex[:8]}",
        event_id=event_id or uuid.uuid4().hex[:16],
        event=event,  # type: ignore[arg-type]
        status=status,
        data=payload,
    )


class GenerationRequest(BaseModel):
    """Explicit artifact generation request (dev API surface for WO-C06)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    artifact_type: str = "markdown"
    language: Optional[str] = None
    content: str = Field(default="", max_length=512 * 1024)
    conversation_id: Optional[str] = None
    workspace_id: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=120)
    chunk_size: int = Field(default=64, ge=1, le=4096)
    simulate: str = "complete"  # complete | abort
    change_reason: str = "AI artifact generation"


class ArtifactStreamSession:
    """In-memory provisional artifact for one generation stream."""

    def __init__(self, manager: ArtifactManager, scope: Any, *, key: str) -> None:
        self.manager = manager
        self.scope = scope
        self.key = key
        self.session_id = f"gen_{uuid.uuid4().hex[:12]}"
        self.artifact_id = "art_" + uuid.uuid4().hex[:12]
        self.status = "idle"
        self._chunks: List[str] = []
        self._seq = 0
        self._frames: Dict[int, SSEEvent] = {}
        self.events: List[SSEEvent] = []
        self.artifact: Optional[Artifact] = None
        self.revision: Optional[Revision] = None
        self.duplicate = False

    # ------------------------------------------------------------------ frames

    def _frame(self, event: str, data: Dict[str, Any], *, status: Optional[str] = None) -> SSEEvent:
        frame = artifact_event(
            event,
            artifact_id=self.artifact_id,
            data={"session_id": self.session_id, **data},
            run_id=self.session_id,
            event_id=f"{self.session_id}-{len(self.events) + 1}",
            status=status or self.status,
        )
        self.events.append(frame)
        return frame

    # ------------------------------------------------------------------ lifecycle

    def start(
        self,
        *,
        title: str,
        artifact_type: str,
        language: Optional[str],
        conversation_id: Optional[str],
        workspace_id: Optional[str],
        creation_source: str = "ai",
    ) -> SSEEvent:
        if self.status != "idle":
            # A repeated start on the same session is a duplicate delivery, not a new artifact.
            return self.events[0]
        if workspace_id is not None:
            # WO-C17 / C-FIX-01: fail fast when the caller may not write into the workspace.
            assert_workspace_level(
                self.manager.workspace, workspace_id, self.scope,
                required="write", operation="generation.start",
            )
        self.status = "started"
        self._request = {
            "title": title,
            "artifact_type": artifact_type,
            "language": language,
            "conversation_id": conversation_id,
            "workspace_id": workspace_id,
            "creation_source": creation_source,
        }
        return self._frame(
            "artifact.created",
            {
                "title": title,
                "artifact_type": artifact_type,
                "language": language,
                "conversation_id": conversation_id,
                "workspace_id": workspace_id,
                "creation_source": creation_source,
                "provisional": True,
                "persisted": False,
            },
        )

    def delta(self, text: str, *, seq: Optional[int] = None) -> Tuple[SSEEvent, bool]:
        """Append a provisional chunk. Returns ``(frame, duplicate)``."""
        if self.status not in ("started", "streaming"):
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST,
                f"session is '{self.status}' and cannot accept deltas",
                context={"session_id": self.session_id},
            )
        self.status = "streaming"
        next_seq = self._seq + 1
        incoming = next_seq if seq is None else int(seq)
        if incoming in self._frames:
            return self._frames[incoming], True
        if incoming < next_seq:
            # Out-of-order replay of an already applied frame.
            return self._frames.get(incoming, self.events[-1]), True
        if incoming > next_seq:
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST,
                "delta sequence gap",
                context={"expected_seq": next_seq, "received_seq": incoming},
            )
        self._seq = next_seq
        self._chunks.append(text)
        from app.canvas.offsets import utf16_length

        accumulated = "".join(self._chunks)
        frame = self._frame(
            "artifact.delta",
            {
                "seq": next_seq,
                "delta": text,
                "accumulated_utf16_units": utf16_length(accumulated),
                "provisional": True,
            },
            status="streaming",
        )
        self._frames[next_seq] = frame
        return frame, False

    def complete(self, *, change_reason: Optional[str] = None) -> Tuple[SSEEvent, Artifact, Revision]:
        if self.status == "completed" and self.artifact is not None and self.revision is not None:
            return self.events[-1], self.artifact, self.revision
        if self.status not in ("started", "streaming"):
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST,
                f"session is '{self.status}' and cannot be completed",
                context={"session_id": self.session_id},
            )
        content = "".join(self._chunks)
        if not content:
            self._fail("empty_stream", "stream produced no content")
        artifact, revision, duplicate, _meta = self.manager.create(
            self.scope,
            title=self._request["title"],
            artifact_type=self._request["artifact_type"],
            content=content,
            language=self._request["language"],
            conversation_id=self._request["conversation_id"],
            workspace_id=self._request["workspace_id"],
            creation_source=self._request["creation_source"],
            change_reason=change_reason or "AI artifact generation",
            actor="ai",
            artifact_id=self.artifact_id,
            idempotency_key=self.key,
        )
        self.artifact, self.revision, self.duplicate = artifact, revision, duplicate
        self.status = "completed"
        frame = self._frame(
            "artifact.updated",
            {
                "revision": revision.revision,
                "content_hash": revision.content_hash,
                "size_bytes": revision.size_bytes,
                "created": not duplicate,
                "duplicate": duplicate,
                "provisional": False,
                "persisted": True,
            },
            status="completed",
        )
        return frame, artifact, revision

    def _fail(self, code: str, message: str) -> None:
        self.status = "failed" if code != "stream_aborted" else "aborted"
        self._frame(
            "artifact.error",
            {
                "code": code,
                "message": message,
                "persisted": False,
                "provisional": True,
            },
            status=self.status,
        )
        # Nothing was written: the provisional id must not be persisted by the client either.
        raise CanvasError(
            CanvasErrorCode.INVALID_REQUEST,
            message,
            context={"code": code, "session_id": self.session_id, "persisted": False},
        )

    def abort(self, *, reason: str = "client aborted the stream") -> SSEEvent:
        if self.status in ("aborted", "failed"):
            return self.events[-1]
        self.status = "aborted"
        return self._frame(
            "artifact.error",
            {
                "code": "stream_aborted",
                "message": reason,
                "persisted": False,
                "provisional": True,
            },
            status="aborted",
        )


class GenerationService:
    """Drives explicit generation requests and exposes the event stream (no LLM call)."""

    def __init__(self, manager: ArtifactManager, *, dev_preview: bool = True) -> None:
        self.manager = manager
        self.dev_preview = bool(dev_preview)
        self._sessions: Dict[Tuple[str, str], ArtifactStreamSession] = {}
        self.last_sessions: Dict[str, ArtifactStreamSession] = {}

    def ensure_dev_preview(self) -> None:
        """WO-C17 / C-FIX-05: the simulated stream is a development surface, not a feature."""
        if not self.dev_preview:
            raise CanvasError(
                CanvasErrorCode.AI_ADAPTER_UNAVAILABLE,
                "artifact generation is a development preview surface; no production generation "
                "adapter is integrated (set AGCORE_CANVAS_DEV_PREVIEW=true for the dev contract)",
                retryable=True,
                context={"dev_preview": False, "ai_integration_verified": False, "persisted": False},
            )

    def _session(self, scope: Any, key: str) -> ArtifactStreamSession:
        lookup = (scope_key_of(scope), key)
        session = self._sessions.get(lookup)
        if session is None:
            session = ArtifactStreamSession(self.manager, scope, key=key)
            self._sessions[lookup] = session
            self.last_sessions[key] = session
        return session

    def generate(self, scope: Any, request: GenerationRequest) -> Dict[str, Any]:
        self.ensure_dev_preview()
        # Fail fast on an unusable type/language before any frame is emitted.
        self.manager.types.get(request.artifact_type)
        self.manager.types.normalize_language(request.artifact_type, request.language)
        key = (request.idempotency_key or "").strip() or f"gen-{uuid.uuid4().hex}"
        session = self._session(scope, key)
        already = session.status
        if already == "completed" and session.artifact is not None:
            return {
                "session_id": session.session_id,
                "artifact_id": session.artifact.artifact_id,
                "committed": True,
                "duplicate": True,
                "revision": session.artifact.current_revision,
                "events": [frame.model_dump() for frame in session.events],
            }
        if already == "aborted":
            return {
                "session_id": session.session_id,
                "artifact_id": None,
                "committed": False,
                "duplicate": True,
                "revision": None,
                "events": [frame.model_dump() for frame in session.events],
            }

        created = session.start(
            title=request.title,
            artifact_type=request.artifact_type,
            language=request.language,
            conversation_id=request.conversation_id,
            workspace_id=request.workspace_id,
            creation_source="ai",
        )
        content = request.content or ""
        for offset in range(0, len(content), request.chunk_size):
            session.delta(content[offset:offset + request.chunk_size])

        if request.simulate == "abort":
            session.abort(reason="simulated interruption after deltas")
            return {
                "session_id": session.session_id,
                "artifact_id": None,
                "committed": False,
                "duplicate": False,
                "provisional_artifact_id": session.artifact_id,
                "revision": None,
                "events": [frame.model_dump() for frame in session.events],
            }

        try:
            updated_frame, artifact, revision = session.complete(change_reason=request.change_reason)
        except CanvasError as exc:
            # Nothing was persisted; keep the original failure code (415/413/422) and say so.
            self._sessions.pop((scope_key_of(scope), key), None)
            raise CanvasError(
                exc.code,
                exc.message or "generation failed",
                retryable=exc.retryable,
                context={**exc.context, "session_id": session.session_id, "persisted": False},
            ) from exc
        return {
            "session_id": session.session_id,
            "artifact_id": artifact.artifact_id,
            "committed": True,
            "duplicate": session.duplicate,
            "revision": revision.revision,
            "content_hash": revision.content_hash,
            "events": [frame.model_dump() for frame in session.events],
            "first_event_id": created.event_id,
            "final_event": updated_frame.model_dump(),
        }

    async def stream(self, scope: Any, request: GenerationRequest) -> AsyncIterator[str]:
        """SSE rendering of the same events (contract verification without an LLM)."""
        import json

        self.ensure_dev_preview()
        result = self.generate(scope, request)
        for frame in result["events"]:
            payload = {
                "run_id": frame["run_id"],
                "event_id": frame["event_id"],
                "timestamp": frame["timestamp"],
                "status": frame["status"],
                **(frame.get("data") or {}),
            }
            yield f"event: {frame['event']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def scope_key_of(scope: Any) -> str:
    tenant_id, owner_id, service_id = scope_ids(scope)
    return f"{tenant_id}::{owner_id}::{service_id}"


def events_contract() -> Dict[str, Any]:
    """Machine-readable event contract for docs and the frontend."""
    return {
        "envelope": {
            "run_id": "generation session id",
            "event_id": "<session_id>-<n>",
            "timestamp": "epoch seconds",
            "status": "streaming | completed | aborted | failed",
            "data.artifact_id": "provisional until artifact.updated/persisted=true",
        },
        "events": list(ARTIFACT_EVENT_TYPES),
        "rules": [
            "artifact.delta frames are provisional and never persisted on their own",
            "an aborted or failed stream persists nothing (artifact.error.persisted=false)",
            "duplicate idempotency_key resolves to the same artifact (duplicate=true)",
            "duplicate delta seq returns the original frame",
        ],
    }
