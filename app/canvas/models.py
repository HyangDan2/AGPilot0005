"""Canvas data models (WO-C02 / WO-C03 / WO-C04).

Field names follow the existing AGCore convention (snake_case) as required by WO-C02
("실제 필드명은 기존 AGCore Schema 규칙에 맞게 결정한다"). Timestamps are ISO-8601 UTC with a
``Z`` suffix, matching the R2-W workspace contract, while the SQLite rows keep epoch seconds so
ordering and range queries stay simple.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.canvas.offsets import OffsetUnit, Selection

CreationSource = Literal["user", "ai", "import", "tool"]
ActorType = Literal["user", "ai", "import", "system"]
ProposalStatus = Literal["proposed", "applied", "rejected", "stale"]
PatchOperation = Literal[
    "replace_content",
    "replace_selection",
    "insert_before",
    "insert_after",
    "append_content",
    "delete_selection",
]


class Artifact(BaseModel):
    """Artifact metadata — an object whose lifecycle is independent of any chat message."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    workspace_id: Optional[str] = None
    conversation_id: Optional[str] = None
    tenant_id: str
    owner_id: str
    service_id: str
    title: str
    artifact_type: str
    language: Optional[str] = None
    current_revision: int = 0
    content_hash: str = ""
    created_at: str
    updated_at: str
    creation_source: CreationSource = "user"
    deleted_at: Optional[str] = None


class Revision(BaseModel):
    """One immutable content revision of an artifact."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    revision: int
    content_hash: str
    size_bytes: int
    actor: ActorType
    change_reason: Optional[str] = None
    operation: str = "create"
    base_revision: Optional[int] = None
    created_at: str
    created_by: Optional[str] = None
    proposal_id: Optional[str] = None


class PatchRequest(BaseModel):
    """WO-C05 patch contract."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    base_revision: int = Field(ge=1)
    operation: PatchOperation
    selection: Optional[Selection] = None
    target_range: Optional[Selection] = None
    expected_original_text: Optional[str] = None
    replacement_text: Optional[str] = None
    change_reason: Optional[str] = Field(default=None, max_length=500)
    offset_unit: Optional[OffsetUnit] = None
    actor: ActorType = "user"

    def resolved_selection(self) -> Optional[Selection]:
        return self.selection or self.target_range


class PatchOutcome(BaseModel):
    """Result of applying one patch atomically."""

    model_config = ConfigDict(extra="forbid")

    artifact: Artifact
    revision: Revision
    changed: bool
    operation: PatchOperation
    applied_range_utf16: Optional[Dict[str, int]] = None
    content_metrics: Dict[str, int] = Field(default_factory=dict)


class EditProposal(BaseModel):
    """WO-C07 structured AI edit proposal. Never applied implicitly."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    artifact_id: str
    base_revision: int
    operation: PatchOperation
    selection: Optional[Selection] = None
    expected_original_text: str = ""
    replacement_text: str = ""
    change_reason: Optional[str] = None
    status: ProposalStatus = "proposed"
    actor: ActorType = "ai"
    adapter: str = "dev"
    model_label: Optional[str] = None
    ai_integration_verified: bool = False
    created_at: str
    updated_at: str
    applied_revision: Optional[int] = None
    request: Dict[str, Any] = Field(default_factory=dict)
    context_budget: Dict[str, Any] = Field(default_factory=dict)
    preview: Optional[Dict[str, Any]] = None


class ArtifactPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[Artifact]
    total: int
    limit: int
    offset: int


class RevisionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[Revision]
    total: int
    limit: int
    offset: int
