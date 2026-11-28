"""Unified Document Reference & Evidence Coverage (WO-R21-ADD-01).

One contract for the two ways a chat turn can point at a document — a file inside the
conversation's Workspace and a Session Attachment registered in AGTools — plus the evidence
bookkeeping that decides whether a grounded answer may finish as ``completed`` or must finish
as ``partial``.

Extension ids (additive on top of the frozen ``dk/v1`` surface)
--------------------------------------------------------------
* ``unified-document-reference`` — :class:`DocumentReference`, :class:`ReferenceResolution`
* ``evidence-coverage``          — :class:`EvidenceCoverage`, :class:`EvidenceEntry`
* ``bounded-enrichment``         — :func:`plan_enrichment`
* ``synthesis-budget-reservation`` — :class:`SynthesisBudget`

Design rules
------------
* The module is pure: no HTTP, no workspace runtime, no runtime state. AGCore's LangGraph
  runtime, the R3 workflow engine and the HD2AI client all import these types instead of
  inventing their own. Callers hand in plain inventory rows and tool results.
* ``available_refs`` (what this conversation may read) and ``requested_refs`` (what the user
  actually asked to analyse) are separate structures. A chat turn with no requested reference
  keeps the pre-R2.1 path: no document search, no extra model call.
* Anything that cannot be read is recorded, not hidden: the answer's status becomes
  ``partial`` and the missing reference carries a machine reason and the citation reference.
* ``citation_ref`` is deterministic — the same source identity and location always produce the
  same string — so a client, an SSE frame and a REST payload can be joined without a lookup.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.knowledge.contracts import CONTRACT_VERSION, normalize_mime

UNIFIED_REFERENCE_CONTRACT_VERSION = CONTRACT_VERSION
UNIFIED_REFERENCE_EXTENSION = "unified-document-reference"
EVIDENCE_COVERAGE_EXTENSION = "evidence-coverage"
BOUNDED_ENRICHMENT_EXTENSION = "bounded-enrichment"
SYNTHESIS_BUDGET_EXTENSION = "synthesis-budget-reservation"

#: Prefix of a workspace file id issued by ``WorkspaceManager`` (``_new_id("wf")``).
WORKSPACE_FILE_PREFIX = "wf"
#: Default number of bounded enrichment reads for one run.
DEFAULT_ENRICHMENT_LIMIT = 2

DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
IDENTIFIER_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{4,}")
_STOPWORDS = frozenset(
    {
        "workspace", "워크스페이스", "attachment", "첨부", "첨부파일", "document", "문서", "file", "파일",
        "analysis", "분석", "요약", "summary", "please", "the", "and", "with", "from",
    }
)


def _strict(**kwargs: Any) -> Dict[str, Any]:
    return {"model_config": ConfigDict(extra="forbid", populate_by_name=True), **kwargs}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------


class ReferenceSourceType(str, Enum):
    WORKSPACE_FILE = "workspace_file"
    SESSION_ATTACHMENT = "session_attachment"


class AnalysisTarget(str, Enum):
    """What the user asked to do with the reference, as far as the request can tell."""

    FULL = "full"
    OUTLINE = "outline"
    PAGES = "pages"
    SECTION = "section"
    SEARCH = "search"


class ReferenceOrigin(str, Enum):
    EXPLICIT = "explicit"
    NATURAL_LANGUAGE = "natural_language"


class ReferenceAvailability(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


class ReferenceScope(_Strict):
    """The authenticated scope a reference was resolved in. Never taken from a browser header."""

    workspace_id: Optional[str] = Field(default=None, max_length=128)
    conversation_id: Optional[str] = Field(default=None, max_length=128)
    session_id: Optional[str] = Field(default=None, max_length=128)


def make_citation_ref(
    source_type: ReferenceSourceType | str,
    document_id: str,
    revision: int = 1,
    locator: Optional[str] = None,
) -> str:
    """Deterministic citation reference: ``<source>:<document>@<revision>[#<locator>]``."""

    source = source_type.value if isinstance(source_type, ReferenceSourceType) else str(source_type)
    reference = f"{source}:{document_id}@{max(0, int(revision))}"
    if locator:
        reference += "#" + str(locator).lstrip("#")
    return reference


class DocumentReference(_Strict):
    """One document a turn may read, pinned to a revision and a scope."""

    source_type: ReferenceSourceType
    document_id: str = Field(min_length=3, max_length=128)
    file_id: Optional[str] = Field(default=None, max_length=128)
    name: str = Field(default="", max_length=512)
    mime_type: str = "application/octet-stream"
    scope: ReferenceScope = Field(default_factory=ReferenceScope)
    revision: int = Field(default=1, ge=0)
    analysis_target: AnalysisTarget = AnalysisTarget.FULL
    citation_ref: str = Field(default="", max_length=320)
    origin: ReferenceOrigin = ReferenceOrigin.EXPLICIT
    availability: ReferenceAvailability = ReferenceAvailability.AVAILABLE
    matched_by: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("document_id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not DOCUMENT_ID_PATTERN.match(value):
            raise ValueError("document_id must be a plain identifier")
        return value

    @field_validator("mime_type")
    @classmethod
    def _mime(cls, value: str) -> str:
        return normalize_mime(value)

    def model_post_init(self, __context: Any) -> None:  # noqa: D401 - pydantic hook
        if not self.citation_ref:
            object.__setattr__(
                self, "citation_ref", make_citation_ref(self.source_type, self.document_id, self.revision)
            )

    @property
    def document_key(self) -> str:
        return f"{self.source_type.value}:{self.document_id}"

    @property
    def path(self) -> Optional[str]:
        value = self.metadata.get("path")
        return str(value) if value else None

    def with_target(self, target: AnalysisTarget) -> "DocumentReference":
        return self.model_copy(update={"analysis_target": target})


class UnresolvedReference(_Strict):
    """A reference the user named (or passed) that this turn cannot read."""

    mention: str = Field(min_length=1, max_length=512)
    source_type: ReferenceSourceType
    reason: str = "reference_not_available"
    document_id: Optional[str] = None


class ReferenceResolution(_Strict):
    """Everything one turn knows about documents before any tool has run."""

    contract_version: str = UNIFIED_REFERENCE_CONTRACT_VERSION
    extension: str = UNIFIED_REFERENCE_EXTENSION
    available_refs: List[DocumentReference] = Field(default_factory=list)
    requested_refs: List[DocumentReference] = Field(default_factory=list)
    unresolved: List[UnresolvedReference] = Field(default_factory=list)
    route: str = "chat"  # "chat" | "document"
    requested_target: AnalysisTarget = AnalysisTarget.FULL

    @property
    def has_requested(self) -> bool:
        return bool(self.requested_refs)

    @property
    def requested_keys(self) -> List[str]:
        return [ref.document_key for ref in self.requested_refs]

    @property
    def available_keys(self) -> List[str]:
        return [ref.document_key for ref in self.available_refs]

    @property
    def requested_citation_refs(self) -> List[str]:
        return [ref.citation_ref for ref in self.requested_refs]

    @property
    def available_citation_refs(self) -> List[str]:
        return [ref.citation_ref for ref in self.available_refs]

    def requested_by_key(self, key: str) -> Optional[DocumentReference]:
        for ref in self.requested_refs:
            if ref.document_key == key:
                return ref
        return None

    def for_report(self) -> Dict[str, Any]:
        return {
            "route": self.route,
            "requested_refs": self.requested_citation_refs,
            "available_refs": self.available_citation_refs,
            "unresolved": [item.model_dump(mode="json") for item in self.unresolved],
        }


# --------------------------------------------------------------------------------------
# Inventory rows (plain dicts from Workspace / AGTools stay plain here)
# --------------------------------------------------------------------------------------


def workspace_reference(
    *,
    file_id: str,
    name: str,
    mime_type: str = "application/octet-stream",
    revision: int = 1,
    path: Optional[str] = None,
    scope: Optional[ReferenceScope] = None,
    origin: ReferenceOrigin = ReferenceOrigin.EXPLICIT,
    availability: ReferenceAvailability = ReferenceAvailability.AVAILABLE,
    analysis_target: AnalysisTarget = AnalysisTarget.FULL,
    matched_by: Optional[Sequence[str]] = None,
    size_bytes: Optional[int] = None,
) -> DocumentReference:
    return DocumentReference(
        source_type=ReferenceSourceType.WORKSPACE_FILE,
        document_id=file_id,
        file_id=file_id,
        name=name,
        mime_type=mime_type,
        scope=scope or ReferenceScope(),
        revision=max(0, int(revision or 0)),
        analysis_target=analysis_target,
        origin=origin,
        availability=availability,
        matched_by=list(matched_by or ["explicit_id"]),
        metadata={"path": path, "size_bytes": size_bytes} if (path or size_bytes is not None) else {},
    )


def attachment_reference(
    *,
    document_id: str,
    name: str = "",
    mime_type: str = "application/pdf",
    revision: int = 1,
    scope: Optional[ReferenceScope] = None,
    origin: ReferenceOrigin = ReferenceOrigin.EXPLICIT,
    availability: ReferenceAvailability = ReferenceAvailability.AVAILABLE,
    analysis_target: AnalysisTarget = AnalysisTarget.FULL,
    matched_by: Optional[Sequence[str]] = None,
) -> DocumentReference:
    return DocumentReference(
        source_type=ReferenceSourceType.SESSION_ATTACHMENT,
        document_id=document_id,
        file_id=document_id,
        name=name,
        mime_type=mime_type,
        scope=scope or ReferenceScope(),
        revision=max(0, int(revision or 0)),
        analysis_target=analysis_target,
        origin=origin,
        availability=availability,
        matched_by=list(matched_by or ["explicit_id"]),
    )


def is_attachment_reference_id(document_id: str) -> bool:
    return document_id.startswith("att-")


def is_workspace_reference_id(document_id: str) -> bool:
    return document_id.startswith(WORKSPACE_FILE_PREFIX)


# --------------------------------------------------------------------------------------
# Requested-reference resolution
# --------------------------------------------------------------------------------------


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _name_tokens(name: str) -> List[str]:
    """Tokens a person could realistically type: full name, stem, and identifier chunks."""

    if not name:
        return []
    lowered = _normalize_text(name)
    tokens: List[str] = [lowered]
    stem = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", lowered)
    if stem and stem != lowered:
        tokens.append(stem)
    for match in IDENTIFIER_TOKEN_PATTERN.findall(name):
        token = match.lower().strip(".-")
        if len(token) >= 5 and token not in _STOPWORDS and not re.fullmatch(r"\d{6,}", token):
            tokens.append(token)
    seen: List[str] = []
    for token in tokens:
        if len(token) >= 3 and token not in seen:
            seen.append(token)
    return seen


def match_mentions(message: str, reference: DocumentReference) -> List[str]:
    """Which parts of ``reference`` the message names (empty list when it names none)."""

    haystack = _normalize_text(message)
    if not haystack:
        return []
    matched: List[str] = []
    for token in _name_tokens(reference.name):
        if token and token in haystack:
            matched.append(token)
    if matched and reference.name and _normalize_text(reference.name) in haystack:
        matched.insert(0, "name")
    return matched


def _infer_target(message: str) -> AnalysisTarget:
    text = _normalize_text(message)
    if not text:
        return AnalysisTarget.FULL
    if re.search(r"\b(검색|찾아|search|where)\b", text):
        return AnalysisTarget.SEARCH
    if re.search(r"\b(목차|outline|목록)\b", text):
        return AnalysisTarget.OUTLINE
    if re.search(r"\d+\s*(페이지|쪽|page)", text):
        return AnalysisTarget.PAGES
    if re.search(r"\b(섹션|section|장|절)\b", text):
        return AnalysisTarget.SECTION
    return AnalysisTarget.FULL


def resolve_references(
    *,
    message: str,
    explicit_ids: Sequence[str] = (),
    available: Sequence[DocumentReference] = (),
    scope: Optional[ReferenceScope] = None,
    missing_explicit: Sequence[Tuple[str, str]] = (),
    infer_natural_language: bool = True,
) -> ReferenceResolution:
    """Split available references into requested/available, matching explicit ids and names.

    ``available`` is the inventory this conversation may read (Workspace files plus the
    attachments bound to the turn). ``explicit_ids`` are the ids the client selected for this
    turn — an attachment ``att-...`` id or a Workspace file id. A document the user names in
    the message ("R2-BETA-WORKSPACE-731.pdf 요약해줘") is requested as well, while an unnamed
    document stays available but unrequested, so the simple chat path never widens.
    """

    scope = scope or ReferenceScope()
    by_id: Dict[str, DocumentReference] = {ref.document_id: ref for ref in available}
    by_key: Dict[str, DocumentReference] = {ref.document_key: ref for ref in available}
    requested: List[DocumentReference] = []
    unresolved: List[UnresolvedReference] = []
    target = _infer_target(message)

    def _add(ref: DocumentReference) -> None:
        if any(item.document_key == ref.document_key for item in requested):
            return
        requested.append(ref)

    for raw_id in explicit_ids:
        document_id = (raw_id or "").strip()
        if not document_id:
            continue
        source = (
            ReferenceSourceType.SESSION_ATTACHMENT
            if is_attachment_reference_id(document_id)
            else ReferenceSourceType.WORKSPACE_FILE
        )
        known = by_id.get(document_id)
        if known is not None:
            _add(known.model_copy(update={"analysis_target": target}) if target is not AnalysisTarget.FULL else known)
            continue
        if is_attachment_reference_id(document_id):
            # An attachment id that is not in this turn's inventory is an authorization problem,
            # not a search problem — report it instead of quietly reading something else.
            unresolved.append(
                UnresolvedReference(
                    mention=document_id,
                    source_type=ReferenceSourceType.SESSION_ATTACHMENT,
                    reason="attachment_not_bound_to_conversation",
                    document_id=document_id,
                )
            )
            _add(
                attachment_reference(
                    document_id=document_id,
                    scope=scope,
                    origin=ReferenceOrigin.EXPLICIT,
                    availability=ReferenceAvailability.MISSING,
                    analysis_target=target,
                )
            )
            continue
        unresolved.append(
            UnresolvedReference(
                mention=document_id,
                source_type=ReferenceSourceType.WORKSPACE_FILE,
                reason="workspace_file_not_found",
                document_id=document_id,
            )
        )
        _add(
            workspace_reference(
                file_id=document_id,
                name="",
                scope=scope,
                origin=ReferenceOrigin.EXPLICIT,
                availability=ReferenceAvailability.MISSING,
                analysis_target=target,
            )
        )

    for mention, reason in missing_explicit:
        unresolved.append(
            UnresolvedReference(
                mention=mention,
                source_type=ReferenceSourceType.WORKSPACE_FILE,
                reason=reason,
                document_id=mention if DOCUMENT_ID_PATTERN.match(mention or "") else None,
            )
        )

    if infer_natural_language and message:
        explicit_keys = {ref.document_key for ref in requested}
        for ref in available:
            if ref.document_key in explicit_keys:
                continue
            matched = match_mentions(message, ref)
            if not matched:
                continue
            _add(
                ref.model_copy(
                    update={
                        "origin": ReferenceOrigin.NATURAL_LANGUAGE,
                        "matched_by": matched,
                        "analysis_target": target,
                    }
                )
            )

    for ref in list(requested):
        if ref.availability is ReferenceAvailability.AVAILABLE and ref.document_key not in by_key:
            idx = requested.index(ref)
            requested[idx] = ref.model_copy(update={"availability": ReferenceAvailability.MISSING})

    route = "document" if requested else "chat"
    return ReferenceResolution(
        available_refs=list(available),
        requested_refs=requested,
        unresolved=unresolved,
        route=route,
        requested_target=target,
    )


# --------------------------------------------------------------------------------------
# Evidence coverage
# --------------------------------------------------------------------------------------


class CoverageStatus(str, Enum):
    COVERED = "covered"
    MISSING = "missing"
    FAILED = "failed"


class EvidenceEntry(_Strict):
    reference: DocumentReference
    status: CoverageStatus = CoverageStatus.MISSING
    tool_calls: int = 0
    reads: int = 0
    search_calls: int = 0
    chunks: List[str] = Field(default_factory=list)
    pages: List[int] = Field(default_factory=list)
    citation_refs: List[str] = Field(default_factory=list)
    missing_reason: Optional[str] = None
    last_error_code: Optional[str] = None

    @property
    def key(self) -> str:
        return self.reference.document_key


class EnrichmentState(_Strict):
    attempts: int = 0
    limit: int = DEFAULT_ENRICHMENT_LIMIT
    attempted_refs: List[str] = Field(default_factory=list)

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.limit


class SynthesisBudget(_Strict):
    """Input tokens set aside so the final grounded answer is never squeezed out by tool text."""

    contract_version: str = UNIFIED_REFERENCE_CONTRACT_VERSION
    extension: str = SYNTHESIS_BUDGET_EXTENSION
    reserved_tokens: int = 0
    evidence_tokens: int = 0
    total_input_budget_tokens: int = 0

    @property
    def ratio(self) -> float:
        if not self.total_input_budget_tokens:
            return 0.0
        return round(self.reserved_tokens / float(self.total_input_budget_tokens), 4)


class EvidenceCoverage(_Strict):
    contract_version: str = UNIFIED_REFERENCE_CONTRACT_VERSION
    extension: str = EVIDENCE_COVERAGE_EXTENSION
    status: str = "not_requested"  # not_requested | covered | partial | missing
    requested: int = 0
    covered: int = 0
    missing: int = 0
    failed: int = 0
    entries: List[EvidenceEntry] = Field(default_factory=list)
    enrichment: EnrichmentState = Field(default_factory=EnrichmentState)
    budget: SynthesisBudget = Field(default_factory=SynthesisBudget)

    @property
    def is_complete(self) -> bool:
        return self.status in ("not_requested", "covered")

    @property
    def incomplete(self) -> bool:
        return self.status in ("partial", "missing")

    def missing_references(self) -> List[str]:
        return [entry.reference.citation_ref for entry in self.entries if entry.status is not CoverageStatus.COVERED]

    def reasons(self) -> Dict[str, str]:
        return {
            entry.reference.citation_ref: (entry.missing_reason or entry.status.value)
            for entry in self.entries
            if entry.status is not CoverageStatus.COVERED
        }

    def for_report(self) -> Dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["missing_references"] = self.missing_references()
        payload["reasons"] = self.reasons()
        return payload


def compute_coverage(
    entries: Sequence[EvidenceEntry],
    *,
    enrichment: Optional[EnrichmentState] = None,
    budget: Optional[SynthesisBudget] = None,
) -> EvidenceCoverage:
    if not entries:
        return EvidenceCoverage(
            status="not_requested",
            enrichment=enrichment or EnrichmentState(),
            budget=budget or SynthesisBudget(),
        )
    covered = sum(1 for entry in entries if entry.status is CoverageStatus.COVERED)
    failed = sum(1 for entry in entries if entry.status is CoverageStatus.FAILED)
    missing = len(entries) - covered - failed
    if covered == len(entries):
        status = "covered"
    elif covered == 0 and failed == 0:
        status = "missing"
    else:
        status = "partial"
    return EvidenceCoverage(
        status=status,
        requested=len(entries),
        covered=covered,
        missing=missing,
        failed=failed,
        entries=list(entries),
        enrichment=enrichment or EnrichmentState(),
        budget=budget or SynthesisBudget(),
    )


def _parse_payload(content: Any) -> Dict[str, Any]:
    if isinstance(content, dict):
        parsed: Dict[str, Any] = content
    elif isinstance(content, str) and content.strip():
        try:
            value = json.loads(content)
        except ValueError:
            return {}
        if not isinstance(value, dict):
            return {}
        parsed = value
    else:
        return {}
    # Workspace tools answer with the adapter envelope {status, tool, side_effects, result}.
    depth = 0
    while (
        isinstance(parsed.get("result"), dict)
        and parsed.get("tool")
        and depth < 3
    ):
        inner = dict(parsed["result"])
        for key in ("status", "error"):
            if key in parsed and key not in inner:
                inner[key] = parsed[key]
        parsed = inner
        depth += 1
    return parsed


def _locator_refs(reference: DocumentReference, payload: Dict[str, Any]) -> Tuple[List[str], List[int], List[str]]:
    """Chunk ids, page numbers and per-location citation refs found in one tool result."""

    chunks: List[str] = []
    pages: List[int] = []
    citation_refs: List[str] = []

    def _add(chunk_id: Any, page: Any = None, citation_id: Any = None) -> None:
        chunk = str(chunk_id) if chunk_id else ""
        if chunk and chunk not in chunks:
            chunks.append(chunk)
        page_number: Optional[int] = None
        if isinstance(page, int) and not isinstance(page, bool) and page > 0:
            page_number = page
        elif isinstance(page, str) and page.isdigit():
            page_number = int(page)
        if page_number is not None and page_number not in pages:
            pages.append(page_number)
        locator = f"{chunk or 'doc'}:p{page_number}" if page_number is not None else (chunk or "doc")
        ref = make_citation_ref(reference.source_type, reference.document_id, reference.revision, locator)
        citation_refs.append(ref)
        if citation_id:
            citation_refs.append(str(citation_id))

    if payload.get("chunk_id") or payload.get("section_id"):
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        _add(payload.get("chunk_id") or payload.get("section_id"), source.get("page"))
    for key in ("chunks", "results", "matches", "entries"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            _add(
                item.get("chunk_id") or item.get("section_id") or item.get("hit_id"),
                item.get("page") or item.get("page_number") or source.get("page"),
                item.get("citation_id"),
            )
    for item in payload.get("sources") or payload.get("citations") or []:
        if isinstance(item, dict):
            _add(
                item.get("chunk_id") or item.get("citation_id"),
                item.get("page_number") or item.get("page"),
                item.get("citation_id"),
            )
    for item in payload.get("retrieved") or []:
        if isinstance(item, dict):
            _add(item.get("chunk_id"), item.get("page_number"))
    return chunks, sorted(pages), citation_refs


_ERROR_REASON = {
    "document_not_referenced": "reference_not_authorized",
    "document_not_found": "document_not_found",
    "provider_unavailable": "provider_unavailable",
    "agtools_provider_unavailable": "provider_unavailable",
    "index_not_ready": "index_not_ready",
    "execution_error": "read_failed",
    "tool_timeout": "read_timeout",
    "permission_denied": "permission_denied",
    "file_not_found": "file_not_found",
    "binary_content": "binary_without_text",
    "no_matches": "no_matching_evidence",
    "empty_result": "empty_result",
}


def failure_reason(code: Optional[str], default: str = "read_failed") -> str:
    if not code:
        return default
    return _ERROR_REASON.get(str(code), str(code))


class EvidenceTracker:
    """Mutable bookkeeping over one run's requested references.

    Every tool result the runtime executes goes through :meth:`record_tool_result`; the tracker
    only ever credits a reference when a result actually carries content (text, chunk, page or
    citation) from that reference. Availability alone is not evidence.
    """

    def __init__(self, requested: Sequence[DocumentReference], *, enrichment_limit: int = DEFAULT_ENRICHMENT_LIMIT):
        self._entries: List[EvidenceEntry] = [EvidenceEntry(reference=ref) for ref in requested]
        self._enrichment = EnrichmentState(limit=max(0, int(enrichment_limit)))
        self._budget = SynthesisBudget()

    # -- construction -----------------------------------------------------------------
    @classmethod
    def from_coverage(cls, coverage: EvidenceCoverage) -> "EvidenceTracker":
        tracker = cls([entry.reference for entry in coverage.entries], enrichment_limit=coverage.enrichment.limit)
        tracker._entries = [entry.model_copy(deep=True) for entry in coverage.entries]
        tracker._enrichment = coverage.enrichment.model_copy(deep=True)
        tracker._budget = coverage.budget.model_copy(deep=True)
        return tracker

    @classmethod
    def empty(cls) -> "EvidenceTracker":
        return cls([])

    # -- state ------------------------------------------------------------------------
    def set_budget(self, budget: SynthesisBudget) -> None:
        self._budget = budget

    def note_enrichment_attempt(self, keys: Sequence[str]) -> None:
        self._enrichment.attempts += 1
        for key in keys:
            if key not in self._enrichment.attempted_refs:
                self._enrichment.attempted_refs.append(key)

    @property
    def entries(self) -> List[EvidenceEntry]:
        return self._entries

    def coverage(self) -> EvidenceCoverage:
        return compute_coverage(self._entries, enrichment=self._enrichment, budget=self._budget)

    def missing_references(self) -> List[DocumentReference]:
        return [entry.reference for entry in self._entries if entry.status is not CoverageStatus.COVERED]

    def enrichment_candidates(self, *, attempts_left: int) -> List[DocumentReference]:
        if attempts_left <= 0:
            return []
        attempted = set(self._enrichment.attempted_refs)
        return [entry.reference for entry in self._entries if entry.status is not CoverageStatus.COVERED and entry.key not in attempted]

    # -- recording --------------------------------------------------------------------
    def _set(self, key: str, **fields: Any) -> bool:
        for index, existing in enumerate(self._entries):
            if existing.key == key:
                self._entries[index] = existing.model_copy(update=fields)
                return True
        return False

    def _mark(self, key: str, status: CoverageStatus, reason: Optional[str] = None, error_code: Optional[str] = None) -> None:
        entry = next((item for item in self._entries if item.key == key), None)
        if entry is None:
            return
        if entry.status is CoverageStatus.COVERED and status is not CoverageStatus.COVERED:
            return  # a later failure never removes evidence already secured
        entry = entry.model_copy(
            update={
                "status": status,
                "missing_reason": reason if status is not CoverageStatus.COVERED else None,
                "last_error_code": error_code,
            }
        )
        self._entries = [entry if item.key == key else item for item in self._entries]

    def record_workspace_result(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        content: Any,
        *,
        is_error: bool = False,
    ) -> bool:
        payload = _parse_payload(content)
        changed = False
        file_id = str(
            payload.get("file_id")
            or (payload.get("file") or {}).get("file_id")
            or arguments.get("file_id")
            or ""
        )
        path = str(payload.get("path") or arguments.get("path") or "")
        targets = [entry for entry in self._entries if entry.reference.source_type is ReferenceSourceType.WORKSPACE_FILE]
        candidates = [
            entry
            for entry in targets
            if (file_id and entry.reference.document_id == file_id)
            or (path and entry.reference.path and entry.reference.path == path)
        ]
        if not candidates and len(targets) == 1 and tool_name in ("workspace_search_text", "workspace_retrieve"):
            candidates = targets

        for entry in candidates:
            key = entry.key
            base = {
                "tool_calls": entry.tool_calls + 1,
                "reads": entry.reads + (1 if tool_name == "workspace_read_file" else 0),
                "search_calls": entry.search_calls + (1 if tool_name in ("workspace_retrieve", "workspace_search_text") else 0),
            }
            if tool_name in ("workspace_list_files", "workspace_find_files", "workspace_get_file_info"):
                # Inventory/metadata is not evidence: count the call, leave the status alone.
                self._set(key, **base)
                changed = True
                continue
            if is_error or payload.get("error"):
                code = (payload.get("error") or {}).get("code") if isinstance(payload.get("error"), dict) else payload.get("code")
                self._set(key, **base)
                self._mark(key, CoverageStatus.FAILED, failure_reason(code), str(code) if code else None)
                changed = True
                continue

            chunks, pages, citation_refs = _locator_refs(entry.reference, payload)
            text = payload.get("text")
            # A workspace_retrieve answer that only cites other files is not evidence for this one.
            if tool_name in ("workspace_retrieve", "workspace_search_text"):
                source_ids = {
                    str(item.get("file_id"))
                    for item in (payload.get("sources") or []) + (payload.get("retrieved") or [])
                    if isinstance(item, dict) and item.get("file_id")
                }
                match_items = payload.get("matches") or []
                if not source_ids and match_items:
                    source_ids = {
                        str(item.get("file_id")) for item in match_items if isinstance(item, dict) and item.get("file_id")
                    }
                if source_ids and entry.reference.document_id not in source_ids:
                    self._set(key, **base)
                    self._mark(key, CoverageStatus.MISSING, "no_matching_evidence", "no_matches")
                    changed = True
                    continue

            has_evidence = bool(chunks or pages or citation_refs) or (
                isinstance(text, str) and bool(text.strip()) and not payload.get("binary")
            )
            merged_chunks = list(dict.fromkeys(entry.chunks + chunks))
            merged_pages = sorted(set(entry.pages + pages))
            merged_refs = list(dict.fromkeys(entry.citation_refs + citation_refs))
            if has_evidence:
                self._set(
                    key,
                    **base,
                    status=CoverageStatus.COVERED,
                    chunks=merged_chunks,
                    pages=merged_pages,
                    citation_refs=merged_refs,
                    missing_reason=None,
                    last_error_code=None,
                )
            else:
                reason = "binary_without_text" if payload.get("binary") else "empty_result"
                self._set(key, **base, chunks=merged_chunks, pages=merged_pages, citation_refs=merged_refs)
                self._mark(key, CoverageStatus.MISSING, reason, "binary_content" if payload.get("binary") else "empty_result")
            changed = True
        return changed

    def record_document_result(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        content: Any,
        *,
        is_error: bool = False,
    ) -> bool:
        payload = _parse_payload(content)
        document_id = str(payload.get("doc_id") or payload.get("document_id") or arguments.get("doc_id") or "")
        entry = next(
            (
                item
                for item in self._entries
                if item.reference.source_type is ReferenceSourceType.SESSION_ATTACHMENT
                and item.reference.document_id == document_id
            ),
            None,
        )
        if entry is None:
            return False
        key = entry.key
        base = {
            "tool_calls": entry.tool_calls + 1,
            "reads": entry.reads + (1 if tool_name in ("document.read_section", "document.read_chunk") else 0),
            "search_calls": entry.search_calls + (1 if tool_name == "document.search" else 0),
        }
        if is_error or payload.get("error"):
            code = payload.get("code") or (payload.get("error") or {}).get("code") if isinstance(payload.get("error"), dict) else payload.get("code")
            self._set(key, **base)
            self._mark(key, CoverageStatus.FAILED, failure_reason(str(code) if code else None), str(code) if code else None)
            return True

        chunks, pages, citation_refs = _locator_refs(entry.reference, payload)
        if tool_name == "document.get_outline":
            sections = payload.get("sections") or []
            if not payload.get("total_sections") and not sections:
                self._set(key, **base)
                self._mark(key, CoverageStatus.MISSING, "outline_empty", "empty_result")
                return True
            # An outline is structure, not content: it keeps the entry pending rather than covered.
            self._set(key, **base)
            return True

        if tool_name == "document.search":
            total = payload.get("total_matches")
            if total is None:
                total = len(payload.get("results") or [])
            if not total:
                self._set(key, **base)
                self._mark(key, CoverageStatus.MISSING, "no_matching_evidence", "no_matches")
                return True

        text = payload.get("text")
        has_text = isinstance(text, str) and bool(text.strip())
        has_evidence = bool(chunks or pages or citation_refs) or has_text
        merged_chunks = list(dict.fromkeys(entry.chunks + chunks))
        merged_pages = sorted(set(entry.pages + pages))
        merged_refs = list(dict.fromkeys(entry.citation_refs + citation_refs))
        if has_evidence:
            self._set(
                key,
                **base,
                status=CoverageStatus.COVERED,
                chunks=merged_chunks,
                pages=merged_pages,
                citation_refs=merged_refs,
                missing_reason=None,
                last_error_code=None,
            )
        else:
            self._set(key, **base, chunks=merged_chunks, pages=merged_pages, citation_refs=merged_refs)
            self._mark(key, CoverageStatus.MISSING, "empty_result", "empty_result")
        return True

    def record_tool_result(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]],
        content: Any,
        *,
        is_error: bool = False,
    ) -> bool:
        args = arguments or {}
        if tool_name.startswith("document."):
            return self.record_document_result(tool_name, args, content, is_error=is_error)
        if tool_name.startswith("workspace_"):
            return self.record_workspace_result(tool_name, args, content, is_error=is_error)
        return False

    def mark_execution_failure(self, code: Optional[str]) -> None:
        """A run that ended before a reference was read leaves that reference explicitly failed."""

        for entry in list(self._entries):
            if entry.status is CoverageStatus.COVERED:
                continue
            self._mark(entry.key, CoverageStatus.FAILED, failure_reason(code, "not_read"), code)


# --------------------------------------------------------------------------------------
# Bounded enrichment
# --------------------------------------------------------------------------------------


class EnrichmentCall(_Strict):
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    reference_key: str
    reason: str = "coverage_missing"


def enrichment_query(message: str, *, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", (message or "").strip())
    return text[:limit] or "요청한 문서의 핵심 내용"


def plan_enrichment(
    references: Iterable[DocumentReference],
    *,
    query: str,
    per_reference_top_k: int = 4,
) -> List[EnrichmentCall]:
    """Bounded reads for exactly the references whose evidence is still missing.

    Only the existing read/search tools are used, inside the same authenticated conversation
    scope the model already runs in — no new retrieval engine, no wider document set.
    """

    calls: List[EnrichmentCall] = []
    for ref in references:
        if ref.source_type is ReferenceSourceType.SESSION_ATTACHMENT:
            calls.append(
                EnrichmentCall(
                    tool_name="document.search",
                    arguments={"doc_id": ref.document_id, "query": query, "limit": per_reference_top_k},
                    reference_key=ref.document_key,
                    reason="attachment_evidence_missing",
                )
            )
        else:
            arguments: Dict[str, Any] = {
                "query": query,
                "file_ids": [ref.document_id],
                "top_k": per_reference_top_k,
                "include_context": False,
            }
            if ref.scope.workspace_id:
                # The binding supplies it when the run is bound; an explicit workspace run names it.
                arguments["workspace_id"] = ref.scope.workspace_id
            calls.append(
                EnrichmentCall(
                    tool_name="workspace_retrieve",
                    arguments=arguments,
                    reference_key=ref.document_key,
                    reason="workspace_evidence_missing",
                )
            )
    return calls


def coverage_summary_text(coverage: EvidenceCoverage, *, limit: int = 4) -> str:
    """One short line a client can render next to a partial answer."""

    if not coverage.incomplete:
        return ""
    names: List[str] = []
    for entry in coverage.entries:
        if entry.status is CoverageStatus.COVERED:
            continue
        label = entry.reference.name or entry.reference.document_id
        reason = entry.missing_reason or entry.status.value
        names.append(f"{label} ({reason})")
        if len(names) >= limit:
            break
    return "근거를 확보하지 못한 자료: " + ", ".join(names)


__all__ = [
    "UNIFIED_REFERENCE_CONTRACT_VERSION",
    "UNIFIED_REFERENCE_EXTENSION",
    "EVIDENCE_COVERAGE_EXTENSION",
    "BOUNDED_ENRICHMENT_EXTENSION",
    "SYNTHESIS_BUDGET_EXTENSION",
    "DEFAULT_ENRICHMENT_LIMIT",
    "WORKSPACE_FILE_PREFIX",
    "ReferenceSourceType",
    "AnalysisTarget",
    "ReferenceOrigin",
    "ReferenceAvailability",
    "ReferenceScope",
    "DocumentReference",
    "UnresolvedReference",
    "ReferenceResolution",
    "CoverageStatus",
    "EvidenceEntry",
    "EnrichmentState",
    "SynthesisBudget",
    "EvidenceCoverage",
    "EvidenceTracker",
    "EnrichmentCall",
    "make_citation_ref",
    "workspace_reference",
    "attachment_reference",
    "resolve_references",
    "match_mentions",
    "compute_coverage",
    "plan_enrichment",
    "enrichment_query",
    "coverage_summary_text",
    "failure_reason",
    "is_attachment_reference_id",
    "is_workspace_reference_id",
]
