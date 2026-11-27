"""AI partial editing (WO-C07).

Flow
----
1. The user selects a range in the Canvas and asks for a change.
2. ``build_edit_envelope`` composes a **bounded** request: the selection, a limited amount of
   surrounding context, artifact type/language and the instruction. A selection edit never ships the
   whole document; the budget decides how much context fits and the envelope records exactly what
   was sent (``full_document_sent``, ``truncated_*``).
3. An adapter returns a **structured edit proposal** (operation + range + expected original text +
   replacement). Nothing is applied at this step: the artifact keeps its current revision.
4. The user previews the diff and applies or rejects it. Apply re-checks the base revision and the
   expected original text, so a user edit made in the meantime turns the proposal into
   ``409 proposal_stale`` instead of silently overwriting it.

Honesty about AI integration
----------------------------
No LLM is called in R2 backend work. ``DevScriptedEditAdapter`` is deterministic and only produces
a replacement when the request contains a scripted replacement or matches one of its documented
rules; otherwise it refuses. Every proposal carries ``ai_integration_verified: false`` and
``adapter: "dev"``. ``RuntimeEditAdapter`` fails closed until the R1.3 runtime integration is done,
so "AI integration complete" can never be claimed from a dev run.

Prompt-injection boundary
-------------------------
Artifact content is inserted into the envelope as *untrusted data* between explicit markers, the
instruction is kept separate, and ``reject_scope_override`` refuses payloads that try to carry
tenant/owner/service/role fields.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Protocol, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.canvas.config import CanvasSettings
from app.canvas.diffing import diff_texts
from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.events import artifact_event
from app.canvas.manager import ArtifactManager, visible_artifact
from app.canvas.models import (
    EditProposal,
    PatchOperation,
    PatchRequest,
)
from app.canvas.offsets import (
    OffsetUnit,
    Selection,
    codepoint_to_utf16_index,
    content_metrics,
    to_codepoint_range,
)
from app.canvas.revisions import PatchEngine
from app.canvas.security import estimate_tokens, reject_scope_override
from app.canvas.storage import CanvasStorage, iso_now, new_proposal_id

UNTRUSTED_BEGIN = "<<<UNTRUSTED_ARTIFACT_CONTENT>>>"
UNTRUSTED_END = "<<<END_UNTRUSTED_ARTIFACT_CONTENT>>>"
CHARS_PER_TOKEN = 2.5

SUPPORTED_INSTRUCTIONS = (
    "edit_selection",
    "change_style",
    "adjust_length",
    "fix_grammar",
    "explain_content",
    "edit_document",
    "explain_code",
    "improve_code",
)


class EditEnvelope(BaseModel):
    """Exactly what would be sent to a model adapter."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    base_revision: int
    artifact_type: str
    language: Optional[str] = None
    instruction: str
    instruction_kind: str = "edit_selection"
    operation: PatchOperation = "replace_selection"
    selection: Optional[Selection] = None
    selected_text: str = ""
    context_before: str = ""
    context_after: str = ""
    context_text: str = ""
    budget: Dict[str, Any] = Field(default_factory=dict)
    content_is_untrusted: bool = True
    markers: Dict[str, str] = Field(default_factory=lambda: {"begin": UNTRUSTED_BEGIN, "end": UNTRUSTED_END})
    scope_fields_accepted: bool = False


class EditProposalDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replacement_text: str
    operation: PatchOperation = "replace_selection"
    change_reason: Optional[str] = None
    adapter: str = "dev"
    model_label: Optional[str] = None
    notes: List[str] = Field(default_factory=list)
    ai_integration_verified: bool = False


class EditAdapter(Protocol):
    mode: str

    def propose(self, envelope: EditEnvelope, *, request: Dict[str, Any]) -> EditProposalDraft: ...


class DevScriptedEditAdapter:
    """Deterministic development adapter — not a model."""

    mode = "dev"
    RULES = {
        "upper": lambda text: text.upper(),
        "lower": lambda text: text.lower(),
        "trim": lambda text: " ".join(text.split()),
    }

    def propose(self, envelope: EditEnvelope, *, request: Dict[str, Any]) -> EditProposalDraft:
        scripted = request.get("scripted_replacement")
        if scripted is not None:
            return EditProposalDraft(
                replacement_text=str(scripted),
                operation=envelope.operation,
                change_reason=f"dev adapter (scripted): {envelope.instruction[:120]}",
                adapter=self.mode,
                notes=["scripted_replacement supplied by the caller; no model was involved"],
            )
        rule = str(request.get("dev_rule") or "").strip().lower()
        if rule in self.RULES:
            return EditProposalDraft(
                replacement_text=self.RULES[rule](envelope.selected_text),
                operation=envelope.operation,
                change_reason=f"dev adapter rule '{rule}': {envelope.instruction[:120]}",
                adapter=self.mode,
                notes=[f"deterministic dev rule '{rule}'"],
            )
        raise CanvasError(
            CanvasErrorCode.AI_ADAPTER_UNAVAILABLE,
            "development edit adapter has no scripted replacement or matching rule; "
            "a real model adapter requires the R1.3 runtime integration",
            retryable=True,
            context={"adapter": self.mode, "ai_integration_verified": False},
        )


class RuntimeEditAdapter:
    """Placeholder for the R1.3-integrated adapter; fails closed until integration."""

    mode = "runtime"

    def propose(self, envelope: EditEnvelope, *, request: Dict[str, Any]) -> EditProposalDraft:
        raise CanvasError(
            CanvasErrorCode.AI_ADAPTER_UNAVAILABLE,
            "AI edit adapter is not integrated with the R1.3 runtime in this build",
            retryable=True,
            context={"adapter": self.mode, "ai_integration_verified": False},
        )


def build_edit_adapter(settings: CanvasSettings) -> EditAdapter:
    if settings.ai_edit_adapter == "dev" and settings.dev_adapters_enabled and not settings.production:
        return DevScriptedEditAdapter()
    return RuntimeEditAdapter()


def build_edit_envelope(
    *,
    artifact_id: str,
    content: str,
    base_revision: int,
    artifact_type: str,
    language: Optional[str],
    instruction: str,
    selection: Optional[Selection],
    budget_tokens: int,
    instruction_kind: str = "edit_selection",
    context_char_cap: int = 4000,
) -> EditEnvelope:
    """Compose a bounded edit request. Selection edits never carry the whole document."""
    if instruction_kind not in SUPPORTED_INSTRUCTIONS:
        raise CanvasError(
            CanvasErrorCode.INVALID_REQUEST,
            f"unsupported instruction kind '{instruction_kind}'",
            context={"supported": list(SUPPORTED_INSTRUCTIONS)},
        )
    instruction_tokens = estimate_tokens(instruction)
    if selection is None:
        # Whole-document edit: the document is intentionally the subject of the request.
        doc_tokens = estimate_tokens(content)
        if doc_tokens > budget_tokens:
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST,
                "document exceeds the edit context budget; use a selection or raise the budget",
                context={"document_tokens": doc_tokens, "budget_tokens": budget_tokens},
            )
        return EditEnvelope(
            artifact_id=artifact_id,
            base_revision=base_revision,
            artifact_type=artifact_type,
            language=language,
            instruction=instruction,
            instruction_kind=instruction_kind,
            operation="replace_content",
            selected_text=content,
            context_text="",
            budget={
                "max_tokens": budget_tokens,
                "estimated_tokens": doc_tokens + instruction_tokens,
                "full_document_sent": True,
                "document_tokens": doc_tokens,
                "selection_truncated": False,
                "omitted_before_chars": 0,
                "omitted_after_chars": 0,
            },
        )

    start, end = to_codepoint_range(content, selection)
    selected_text = content[start:end]
    if not selected_text:
        raise CanvasError(
            CanvasErrorCode.INVALID_SELECTION,
            "selection is empty; select the text to edit",
            context={"start": selection.start, "end": selection.end},
        )
    selected_tokens = estimate_tokens(selected_text)
    reserve = 64
    available = budget_tokens - instruction_tokens - selected_tokens - reserve
    if available <= 0:
        raise CanvasError(
            CanvasErrorCode.INVALID_REQUEST,
            "selection alone exceeds the edit context budget; select a smaller range",
            context={
                "selection_tokens": selected_tokens,
                "instruction_tokens": instruction_tokens,
                "budget_tokens": budget_tokens,
            },
        )
    before_chars_available = max(0, start)
    after_chars_available = max(0, len(content) - end)
    per_side_chars = min(context_char_cap, int((available / 2) * CHARS_PER_TOKEN))
    before_chars = min(before_chars_available, per_side_chars)
    after_chars = min(after_chars_available, per_side_chars)
    context_before = content[start - before_chars:start]
    context_after = content[end:end + after_chars]
    context_text = f"{UNTRUSTED_BEGIN}\n{context_before}[[SELECTED]]{selected_text}[[/SELECTED]]{context_after}\n{UNTRUSTED_END}"
    estimated = estimate_tokens(context_text) + instruction_tokens
    return EditEnvelope(
        artifact_id=artifact_id,
        base_revision=base_revision,
        artifact_type=artifact_type,
        language=language,
        instruction=instruction,
        instruction_kind=instruction_kind,
        operation="replace_selection",
        selection=selection,
        selected_text=selected_text,
        context_before=context_before,
        context_after=context_after,
        context_text=context_text,
        budget={
            "max_tokens": budget_tokens,
            "estimated_tokens": estimated,
            "full_document_sent": False,
            "document_tokens": estimate_tokens(content),
            "selection_tokens": selected_tokens,
            "omitted_before_chars": before_chars_available - before_chars,
            "omitted_after_chars": after_chars_available - after_chars,
            "selection_truncated": False,
        },
    )


class ProposalService:
    """Create, inspect, apply and reject AI edit proposals."""

    def __init__(
        self,
        storage: CanvasStorage,
        manager: ArtifactManager,
        patches: PatchEngine,
        settings: CanvasSettings,
        adapter: EditAdapter,
    ) -> None:
        self.storage = storage
        self.manager = manager
        self.patches = patches
        self.settings = settings
        self.adapter = adapter

    def create(
        self,
        scope: Any,
        artifact_id: str,
        *,
        instruction: str,
        base_revision: Optional[int] = None,
        selection: Optional[Selection] = None,
        offset_unit: Optional[OffsetUnit] = None,
        instruction_kind: str = "edit_selection",
        budget_tokens: Optional[int] = None,
        scripted_replacement: Optional[str] = None,
        dev_rule: Optional[str] = None,
        change_reason: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        request_payload = {
            "instruction": instruction,
            "selection": selection.model_dump() if selection else None,
            "scripted_replacement": scripted_replacement,
            "dev_rule": dev_rule,
            **(extra or {}),
        }
        reject_scope_override(request_payload, source="edit_proposal")
        if not instruction.strip():
            raise CanvasError(CanvasErrorCode.INVALID_REQUEST, "instruction must not be empty")
        artifact = visible_artifact(self.storage, artifact_id, scope)
        current = artifact.current_revision
        if base_revision is not None and int(base_revision) != current:
            raise CanvasError(
                CanvasErrorCode.REVISION_CONFLICT,
                "proposal must be based on the current revision",
                retryable=True,
                context={"base_revision": int(base_revision), "current_revision": current},
            )
        _current_artifact, _current_revision, content = self.storage.read_current(artifact.artifact_id)
        if selection is not None and offset_unit and selection.offset_unit == "utf16" and offset_unit != "utf16":
            selection = Selection(start=selection.start, end=selection.end, offset_unit=offset_unit)
        budget = int(budget_tokens or self.settings.max_context_tokens)
        envelope = build_edit_envelope(
            artifact_id=artifact.artifact_id,
            content=content,
            base_revision=current,
            artifact_type=artifact.artifact_type,
            language=artifact.language,
            instruction=instruction,
            selection=selection,
            budget_tokens=budget,
            instruction_kind=instruction_kind,
        )
        draft = self.adapter.propose(
            envelope, request={"scripted_replacement": scripted_replacement, "dev_rule": dev_rule}
        )
        expected_original = envelope.selected_text
        operation: PatchOperation = draft.operation
        if operation == "replace_content" and selection is not None:
            # A model may never widen a selection edit into a full rewrite.
            raise CanvasError(
                CanvasErrorCode.FORBIDDEN,
                "proposal attempted to replace the whole document for a selection edit",
                context={"artifact_id": artifact.artifact_id},
            )
        preview = self._preview(artifact, content, operation, selection, expected_original, draft.replacement_text, current)
        now = iso_now()
        proposal = EditProposal(
            proposal_id=new_proposal_id(),
            artifact_id=artifact.artifact_id,
            base_revision=current,
            operation=operation,
            selection=selection,
            expected_original_text=expected_original,
            replacement_text=draft.replacement_text,
            change_reason=change_reason or draft.change_reason or instruction[:200],
            status="proposed",
            actor="ai",
            adapter=draft.adapter,
            model_label=draft.model_label,
            ai_integration_verified=False,
            created_at=now,
            updated_at=now,
            request={
                "instruction": instruction,
                "instruction_kind": instruction_kind,
                "selection": selection.model_dump() if selection else None,
                "notes": draft.notes,
                "scripted": scripted_replacement is not None,
            },
            context_budget=envelope.budget,
            preview=preview,
        )
        stored = self.storage.create_proposal(
            proposal,
            tenant_id=artifact.tenant_id,
            owner_id=artifact.owner_id,
            service_id=artifact.service_id,
        )
        event = artifact_event(
            "artifact.edit.proposed",
            artifact_id=artifact.artifact_id,
            data={"proposal": stored.model_dump(), "ai_integration_verified": False},
            status="proposed",
        )
        return {"proposal": stored.model_dump(), "event": event.model_dump(), "envelope": envelope.model_dump()}

    def _preview(
        self,
        artifact,
        content: str,
        operation: str,
        selection: Optional[Selection],
        expected_original: str,
        replacement: str,
        base_revision: int,
    ) -> Dict[str, Any]:
        if operation == "replace_content":
            updated = replacement
            diff = diff_texts(
                content, updated, from_revision=base_revision, to_revision=None,
                artifact_id=artifact.artifact_id, max_lines=self.settings.max_diff_lines,
            )
        else:
            start, end = to_codepoint_range(content, selection)
            updated = content[:start] + replacement + content[end:]
            diff = diff_texts(
                content, updated, from_revision=base_revision, to_revision=None,
                artifact_id=artifact.artifact_id, max_lines=self.settings.max_diff_lines,
            )
            diff["applied_range_utf16"] = {
                "start": codepoint_to_utf16_index(updated, start),
                "end": codepoint_to_utf16_index(updated, start + len(replacement)),
            }
        diff["expected_original_length"] = len(expected_original)
        diff["result_content_metrics"] = content_metrics(updated)
        return diff

    def get(self, scope: Any, proposal_id: str) -> Dict[str, Any]:
        proposal = self._load(scope, proposal_id)
        artifact = visible_artifact(self.storage, proposal.artifact_id, scope)
        return {
            "proposal": proposal.model_dump(),
            "artifact": artifact.model_dump(),
            "is_stale": proposal.base_revision != artifact.current_revision,
        }

    def list(
        self,
        scope: Any,
        artifact_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        visible_artifact(self.storage, artifact_id, scope)
        items, total = self.storage.list_proposals(artifact_id, status=status, limit=limit, offset=offset)
        return {
            "items": [item.model_dump() for item in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def apply(
        self, scope: Any, proposal_id: str, *, change_reason: Optional[str] = None, actor_id: Optional[str] = None
    ) -> Dict[str, Any]:
        proposal = self._load(scope, proposal_id)
        artifact = visible_artifact(self.storage, proposal.artifact_id, scope)
        if proposal.status == "applied":
            return {
                "proposal": proposal.model_dump(),
                "artifact": artifact.model_dump(),
                "applied": False,
                "duplicate": True,
                "event": None,
            }
        if proposal.status == "rejected":
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST,
                "proposal was rejected and cannot be applied",
                context={"proposal_id": proposal.proposal_id},
            )
        if proposal.base_revision != artifact.current_revision:
            stale = proposal.model_copy(update={"status": "stale", "updated_at": iso_now()})
            self.storage.save_proposal(stale)
            raise CanvasError(
                CanvasErrorCode.PROPOSAL_STALE,
                "artifact changed after the proposal was created; nothing was applied",
                retryable=True,
                context={
                    "proposal_base_revision": proposal.base_revision,
                    "current_revision": artifact.current_revision,
                },
            )
        request = PatchRequest(
            artifact_id=proposal.artifact_id,
            base_revision=proposal.base_revision,
            operation=proposal.operation,
            selection=proposal.selection,
            expected_original_text=proposal.expected_original_text,
            replacement_text=proposal.replacement_text,
            change_reason=change_reason or proposal.change_reason,
            actor="ai",
        )
        outcome = self.patches.apply(scope, request, actor_id=actor_id, proposal_id=proposal.proposal_id)
        applied = proposal.model_copy(
            update={
                "status": "applied",
                "applied_revision": outcome.revision.revision,
                "updated_at": iso_now(),
            }
        )
        stored = self.storage.save_proposal(applied)
        event = artifact_event(
            "artifact.edit.applied",
            artifact_id=proposal.artifact_id,
            data={
                "proposal_id": proposal.proposal_id,
                "revision": outcome.revision.revision,
                "changed": outcome.changed,
                "actor": "ai",
                "ai_integration_verified": False,
            },
            status="applied",
        )
        return {
            "proposal": stored.model_dump(),
            "artifact": outcome.artifact.model_dump(),
            "revision": outcome.revision.model_dump(),
            "applied": True,
            "duplicate": False,
            "event": event.model_dump(),
        }

    def reject(self, scope: Any, proposal_id: str, *, reason: Optional[str] = None) -> Dict[str, Any]:
        proposal = self._load(scope, proposal_id)
        if proposal.status in ("rejected", "applied"):
            return {"proposal": proposal.model_dump(), "rejected": proposal.status == "rejected", "event": None}
        rejected = proposal.model_copy(update={"status": "rejected", "updated_at": iso_now()})
        if reason:
            rejected.change_reason = f"{proposal.change_reason or ''} | rejected: {reason}".strip(" |")
        stored = self.storage.save_proposal(rejected)
        event = artifact_event(
            "artifact.edit.rejected",
            artifact_id=proposal.artifact_id,
            data={"proposal_id": proposal.proposal_id, "reason": reason},
            status="rejected",
        )
        return {"proposal": stored.model_dump(), "rejected": True, "event": event.model_dump()}

    def _load(self, scope: Any, proposal_id: str) -> EditProposal:
        proposal = self.storage.get_proposal(proposal_id)
        if proposal is None:
            raise CanvasError(CanvasErrorCode.PROPOSAL_NOT_FOUND, context={"proposal_id": proposal_id})
        # Scope enforcement happens through the artifact the proposal belongs to.
        visible_artifact(self.storage, proposal.artifact_id, scope)
        return proposal
