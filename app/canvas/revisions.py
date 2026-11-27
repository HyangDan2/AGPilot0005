"""Revision manager (WO-C04) and patch engine service (WO-C05).

Revision rules
--------------
* The first revision (1) is created with the artifact.
* Every content change appends a new revision; the previous revision stays readable.
* "Restore" never deletes or rewrites history — it appends a new revision whose content equals the
  restored one and records ``operation="restore"`` and the source revision in ``change_reason``.
* ``actor`` separates User / AI / Import / System authorship, and ``change_reason`` is kept per
  revision.

Conflict rules
--------------
* ``base_revision`` must equal the artifact's current revision → otherwise ``409 revision_conflict``.
* ``expected_original_text`` must equal the current text in that range → otherwise
  ``409 expected_text_mismatch``.
* Compare, transform and persist happen in a single ``BEGIN IMMEDIATE`` transaction
  (``CanvasStorage.apply_patch``), so a concurrent writer cannot slip between the check and the
  write. Silent overwrite is impossible by construction: there is no code path that updates content
  without a base revision.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.canvas.config import CanvasSettings
from app.canvas.diffing import diff_texts
from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.manager import visible_artifact
from app.canvas.models import Artifact, PatchOutcome, PatchRequest, Revision, RevisionPage
from app.canvas.patching import compute_patch
from app.canvas.storage import CanvasStorage, iso_now


class RevisionManager:
    def __init__(self, storage: CanvasStorage, settings: CanvasSettings, workspace: Any = None) -> None:
        self.storage = storage
        self.settings = settings
        self.workspace = workspace

    def history(
        self, scope: Any, artifact_id: str, *, limit: Optional[int] = None, offset: int = 0
    ) -> RevisionPage:
        artifact = visible_artifact(
            self.storage, artifact_id, scope, workspace=self.workspace,
            required="read", operation="revision.history",
        )
        page_limit = min(int(limit or self.settings.default_page_limit), self.settings.max_list_limit)
        items, total = self.storage.list_revisions(artifact.artifact_id, limit=page_limit, offset=max(0, int(offset)))
        return RevisionPage(items=items, total=total, limit=page_limit, offset=max(0, int(offset)))

    def get(self, scope: Any, artifact_id: str, revision: int) -> Dict[str, Any]:
        artifact = visible_artifact(
            self.storage, artifact_id, scope, workspace=self.workspace,
            required="read", operation="revision.get",
        )
        revision_row, content = self.storage.read_revision_content(artifact.artifact_id, revision)
        from app.canvas.offsets import content_metrics

        return {
            "artifact_id": artifact.artifact_id,
            "artifact": artifact.model_dump(),
            "revision": revision_row.model_dump(),
            "content": content,
            "content_metrics": content_metrics(content),
            "is_current": revision_row.revision == artifact.current_revision,
        }

    def diff(
        self,
        scope: Any,
        artifact_id: str,
        *,
        from_revision: Optional[int] = None,
        to_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        artifact = visible_artifact(
            self.storage, artifact_id, scope, workspace=self.workspace,
            required="read", operation="revision.diff",
        )
        current = artifact.current_revision
        target = int(to_revision if to_revision is not None else current)
        source = int(from_revision if from_revision is not None else max(1, target - 1))
        if source == target:
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST,
                "from_revision and to_revision must differ",
                context={"from_revision": source, "to_revision": target},
            )
        old_revision, old_content = self.storage.read_revision_content(artifact.artifact_id, source)
        new_revision, new_content = self.storage.read_revision_content(artifact.artifact_id, target)
        diff = diff_texts(
            old_content,
            new_content,
            from_revision=old_revision.revision,
            to_revision=new_revision.revision,
            artifact_id=artifact.artifact_id,
            max_lines=self.settings.max_diff_lines,
        )
        diff["from_created_at"] = old_revision.created_at
        diff["to_created_at"] = new_revision.created_at
        diff["from_actor"] = old_revision.actor
        diff["to_actor"] = new_revision.actor
        return diff

    def restore(
        self,
        scope: Any,
        artifact_id: str,
        revision: int,
        *,
        change_reason: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> PatchOutcome:
        """Restore an earlier revision by appending a new revision (never destructive)."""
        artifact = visible_artifact(
            self.storage, artifact_id, scope, workspace=self.workspace,
            required="write", operation="revision.restore",
        )
        target, target_content = self.storage.read_revision_content(artifact.artifact_id, revision)
        if target.revision == artifact.current_revision:
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST,
                "revision is already current",
                context={"revision": target.revision},
            )
        current_artifact, _current_revision, current_content = self.storage.read_current(artifact.artifact_id)
        request = PatchRequest(
            artifact_id=artifact.artifact_id,
            base_revision=current_artifact.current_revision,
            operation="replace_content",
            replacement_text=target_content,
            expected_original_text=current_content,
            change_reason=change_reason or f"restore of revision {target.revision}",
            actor="user",
        )
        stored, new_revision, computation = self.storage.apply_patch(
            request, actor="user", actor_id=actor_id
        )
        return PatchOutcome(
            artifact=stored,
            revision=new_revision,
            changed=computation.changed,
            operation="replace_content",
            applied_range_utf16=computation.applied_range_utf16,
            content_metrics=_metrics(computation.content),
        )


class PatchEngine:
    """Applies and previews partial edits with explicit conflict handling."""

    def __init__(self, storage: CanvasStorage, settings: CanvasSettings, workspace: Any = None) -> None:
        self.storage = storage
        self.settings = settings
        self.workspace = workspace

    def apply(
        self,
        scope: Any,
        request: PatchRequest,
        *,
        actor_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        proposal_id: Optional[str] = None,
    ) -> PatchOutcome:
        artifact = visible_artifact(
            self.storage, request.artifact_id, scope, workspace=self.workspace,
            required="write", operation="patch.apply",
        )
        stored, revision, computation = self.storage.apply_patch(
            request, actor=request.actor, actor_id=actor_id, event_key=idempotency_key,
            proposal_id=proposal_id,
        )
        return PatchOutcome(
            artifact=stored,
            revision=revision,
            changed=computation.changed,
            operation=request.operation,
            applied_range_utf16=computation.applied_range_utf16,
            content_metrics=_metrics(computation.content),
        )

    def preview(self, scope: Any, request: PatchRequest) -> Dict[str, Any]:
        from app.canvas.diffing import diff_texts

        artifact = visible_artifact(
            self.storage, request.artifact_id, scope, workspace=self.workspace,
            required="read", operation="patch.preview",
        )
        if request.base_revision != artifact.current_revision:
            raise CanvasError(
                CanvasErrorCode.REVISION_CONFLICT,
                "base_revision is not the current revision",
                retryable=True,
                context={
                    "base_revision": request.base_revision,
                    "current_revision": artifact.current_revision,
                },
            )
        _current_artifact, _current_revision, current_content = self.storage.read_current(artifact.artifact_id)
        computation = compute_patch(current_content, request)
        diff = diff_texts(
            current_content,
            computation.content,
            from_revision=artifact.current_revision,
            to_revision=None,
            artifact_id=artifact.artifact_id,
            max_lines=self.settings.max_diff_lines,
        )
        diff["applied_range_utf16"] = computation.applied_range_utf16
        diff["would_change"] = computation.changed
        diff["current_content_metrics"] = _metrics(current_content)
        diff["result_content_metrics"] = _metrics(computation.content)
        diff["operation"] = request.operation
        return diff


def _metrics(content: str) -> Dict[str, int]:
    from app.canvas.offsets import content_metrics

    return content_metrics(content)
