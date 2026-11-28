"""Document Knowledge Contract `dk/v1` — wire types (AGCore reference half).

This is the frozen contract AGCore publishes to every engine (`docs/r21/DOCUMENT_KNOWLEDGE_CONTRACT.md`).
AGTools keeps a structurally identical copy at `app/knowledge/contracts.py`; both sides carry
`contract_version` on every payload and the conformance test pins the field sets, so a drifted
field is a test failure rather than a runtime surprise.

Design rules
------------
* Every payload is ``extra="forbid"`` so a typo in a producer surfaces as a validation error
  instead of silently disappearing.
* Field names are the wire names. AGCore parses these payloads without translation.
* All operations are revision-scoped: a read or update names the revision it expects, so a
  concurrent writer can never be overwritten silently (see `ArtifactStore`).
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "dk/v1"

# Additive extensions on top of the first frozen surface. A consumer must ignore unknown
# values of this list; a producer must not remove a value once published.
CONTRACT_EXTENSIONS: Tuple[str, ...] = (
    "provider-federation",
    "page-citation",
    "slide-citation",
    "sheet-cell-citation",
    "structured-chunking",
    "artifact-revisions",
    "optimistic-concurrency",
    "vision-interface",
    "keyword-fallback",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------------------
# Identity, hashing, MIME
# --------------------------------------------------------------------------------------

CONTENT_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
DOCUMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{2,95}$")


def compute_content_hash(data: bytes) -> str:
    """Content identity used for de-duplication and integrity checks."""

    return "sha256:" + hashlib.sha256(data).hexdigest()


MIME_BY_EXTENSION: Dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".htm": "text/html",
    ".html": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}

MIME_ALIASES: Dict[str, str] = {
    "application/x-pdf": "application/pdf",
    "application/acrobat": "application/pdf",
    "application/vnd.pdf": "application/pdf",
    "application/zip": "application/octet-stream",  # a bare Office archive is not a format
    "image/jpg": "image/jpeg",
    "text/x-markdown": "text/markdown",
    "application/x-msword": "application/msword",
}


class FormatFamily(str, Enum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    TEXT = "text"
    CSV = "csv"
    IMAGE = "image"
    UNKNOWN = "unknown"


FAMILY_BY_MIME: Dict[str, FormatFamily] = {
    "application/pdf": FormatFamily.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FormatFamily.DOCX,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": FormatFamily.XLSX,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": FormatFamily.PPTX,
    "text/plain": FormatFamily.TEXT,
    "text/markdown": FormatFamily.TEXT,
    "text/html": FormatFamily.TEXT,
    "application/json": FormatFamily.TEXT,
    "text/csv": FormatFamily.CSV,
    "text/tab-separated-values": FormatFamily.CSV,
    "image/png": FormatFamily.IMAGE,
    "image/jpeg": FormatFamily.IMAGE,
    "image/webp": FormatFamily.IMAGE,
    "image/gif": FormatFamily.IMAGE,
    "image/tiff": FormatFamily.IMAGE,
    "image/bmp": FormatFamily.IMAGE,
}


def normalize_mime(mime_type: Optional[str], *, filename: Optional[str] = None) -> str:
    """Return the canonical MIME type for a document.

    A declared type wins when it is a real format; otherwise the extension decides. Unknown
    input becomes ``application/octet-stream`` rather than being guessed from content, so a
    caller can tell "not known" from "known to be unsupported".
    """

    declared = (mime_type or "").split(";", 1)[0].strip().lower()
    declared = MIME_ALIASES.get(declared, declared)
    if declared in FAMILY_BY_MIME:
        return declared
    if filename:
        suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix in MIME_BY_EXTENSION:
            return MIME_BY_EXTENSION[suffix]
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            guessed = MIME_ALIASES.get(guessed, guessed)
            if guessed in FAMILY_BY_MIME:
                return guessed
    return "application/octet-stream"


def format_family(mime_type: str) -> FormatFamily:
    return FAMILY_BY_MIME.get(normalize_mime(mime_type), FormatFamily.UNKNOWN)


class DocumentNamespace(str, Enum):
    """Where the bytes live. The namespace decides which provider serves the document."""

    AGTOOLS = "agtools"           # managed by this service (uploads, artifacts)
    WORKSPACE = "workspace"       # AGCore workspace file
    SESSION_ATTACHMENT = "session_attachment"  # AGCore conversation-scoped upload
    ARTIFACT = "artifact"         # generated file attached to a conversation
    EXTERNAL = "external"         # caller-supplied URL/bytes, read-only


class RevisionStatus(str, Enum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class IndexState(str, Enum):
    PENDING = "PENDING"
    INDEXING = "INDEXING"
    READY = "READY"
    STALE = "STALE"
    FAILED = "FAILED"


class DocumentRef(_Strict):
    """Revision-pinned identity of one document."""

    namespace: DocumentNamespace = DocumentNamespace.AGTOOLS
    document_id: str = Field(min_length=3, max_length=96)
    name: str = Field(default="", max_length=512)
    mime_type: str = "application/octet-stream"
    revision: int = Field(default=1, ge=0)
    content_hash: str = ""
    size_bytes: int = Field(default=0, ge=0)
    status: RevisionStatus = RevisionStatus.CURRENT
    index_state: Optional[IndexState] = None
    version: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("document_id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not DOCUMENT_ID_PATTERN.match(value):
            raise ValueError("document_id must match ^[a-z0-9][a-z0-9_.:-]{2,95}$")
        return value

    @field_validator("content_hash")
    @classmethod
    def _hash_shape(cls, value: str) -> str:
        if value and not CONTENT_HASH_PATTERN.match(value):
            raise ValueError("content_hash must be 'sha256:<64 hex>'")
        return value

    @field_validator("mime_type")
    @classmethod
    def _mime_canonical(cls, value: str) -> str:
        return normalize_mime(value)

    @property
    def family(self) -> FormatFamily:
        return format_family(self.mime_type)

    @property
    def canonical_id(self) -> str:
        return f"{self.namespace.value}:{self.document_id}@{self.revision}"

    @property
    def document_key(self) -> str:
        """Identity independent of revision (for de-duplication and re-index decisions)."""

        return f"{self.namespace.value}:{self.document_id}"


# --------------------------------------------------------------------------------------
# Locators and citations
# --------------------------------------------------------------------------------------


class LocatorKind(str, Enum):
    DOCUMENT = "document"
    PAGE = "page"
    SLIDE = "slide"
    SHEET = "sheet"
    CELL = "cell"
    RANGE = "range"
    SECTION = "section"
    PARAGRAPH = "paragraph"
    ROW = "row"
    IMAGE = "image"
    NOTE = "note"
    CHAR = "char"


class Locator(_Strict):
    """A location inside one document. Coordinates are 1-based where the format is 1-based.

    * PDF: ``page`` (1-based), optional ``char_start``/``char_end``
    * DOCX: ``section`` (heading path), ``paragraph`` (1-based body index)
    * XLSX: ``sheet``, ``cell`` (A1) or ``range`` (A1:B10), ``row``
    * PPTX: ``slide`` (1-based), ``shape`` (shape name), ``image``
    """

    kind: LocatorKind = LocatorKind.DOCUMENT
    page: Optional[int] = Field(default=None, ge=1)
    slide: Optional[int] = Field(default=None, ge=1)
    sheet: Optional[str] = Field(default=None, max_length=128)
    cell: Optional[str] = Field(default=None, max_length=32)
    range: Optional[str] = Field(default=None, max_length=64)
    section: Optional[str] = Field(default=None, max_length=512)
    paragraph: Optional[int] = Field(default=None, ge=0)
    row: Optional[int] = Field(default=None, ge=1)
    shape: Optional[str] = Field(default=None, max_length=256)
    image: Optional[str] = Field(default=None, max_length=128)
    char_start: Optional[int] = Field(default=None, ge=0)
    char_end: Optional[int] = Field(default=None, ge=0)
    path: Optional[str] = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _has_coordinate(self) -> "Locator":
        coordinates = (
            self.page, self.slide, self.sheet, self.section, self.paragraph,
            self.row, self.shape, self.image, self.path,
        )
        if self.kind is not LocatorKind.DOCUMENT and not any(
            value is not None for value in coordinates
        ) and self.char_start is None:
            raise ValueError(f"locator kind '{self.kind.value}' needs a coordinate")
        if self.kind is LocatorKind.CELL and not (self.sheet and self.cell):
            raise ValueError("cell locator needs sheet and cell")
        if self.kind is LocatorKind.RANGE and not (self.sheet and self.range):
            raise ValueError("range locator needs sheet and range")
        return self

    def label(self) -> str:
        """Human-readable short label used in context blocks."""

        if self.kind is LocatorKind.PAGE and self.page:
            return f"p.{self.page}"
        if self.kind is LocatorKind.SLIDE and self.slide:
            return f"slide {self.slide}"
        if self.kind is LocatorKind.CELL and self.cell:
            return f"{self.sheet}!{self.cell}"
        if self.kind is LocatorKind.RANGE and self.range:
            return f"{self.sheet}!{self.range}"
        if self.kind is LocatorKind.SECTION and self.section:
            return self.section
        if self.kind is LocatorKind.PARAGRAPH and self.paragraph is not None:
            return f"¶{self.paragraph}"
        return self.kind.value


class Citation(_Strict):
    citation_id: str
    document: DocumentRef
    locator: Locator = Field(default_factory=Locator)
    quote: str = ""
    content_hash: str = ""
    revision_status: RevisionStatus = RevisionStatus.CURRENT

    @field_validator("content_hash")
    @classmethod
    def _hash_shape(cls, value: str) -> str:
        if value and not CONTENT_HASH_PATTERN.match(value):
            raise ValueError("content_hash must be 'sha256:<64 hex>'")
        return value


def make_citation(
    document: DocumentRef,
    locator: Locator,
    quote: str,
    *,
    content_hash: Optional[str] = None,
) -> Citation:
    """Deterministic citation id: same document/locator/quote -> same id."""

    seed = f"{document.canonical_id}|{locator.model_dump_json()}|{quote[:512]}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return Citation(
        citation_id=f"cit_{digest}",
        document=document,
        locator=locator,
        quote=quote[:2000],
        content_hash=content_hash or document.content_hash,
    )


# --------------------------------------------------------------------------------------
# Content nodes
# --------------------------------------------------------------------------------------


class NodeKind(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    IMAGE = "image"
    CHART = "chart"
    FORMULA = "formula"
    NOTE = "note"
    METADATA = "metadata"


class TableData(_Strict):
    rows: List[List[str]] = Field(default_factory=list)
    header_rows: int = Field(default=0, ge=0, le=5)
    caption: str = ""
    source_range: str = ""
    truncated: bool = False


class ImageData(_Strict):
    asset_id: str
    mime_type: str = "image/png"
    width: Optional[int] = Field(default=None, ge=0)
    height: Optional[int] = Field(default=None, ge=0)
    caption: str = ""
    alt_text: str = ""
    size_bytes: int = Field(default=0, ge=0)


class ChartData(_Strict):
    chart_type: str = "unknown"
    title: str = ""
    categories: List[str] = Field(default_factory=list)
    series: List[Dict[str, Any]] = Field(default_factory=list)


class DocNode(_Strict):
    node_id: str
    kind: NodeKind
    text: str = ""
    level: int = Field(default=0, ge=0, le=9)
    locator: Locator = Field(default_factory=Locator)
    style: str = ""
    table: Optional[TableData] = None
    image: Optional[ImageData] = None
    chart: Optional[ChartData] = None
    meta: Dict[str, Any] = Field(default_factory=dict)
    children: List["DocNode"] = Field(default_factory=list)

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> "DocNode":
        if self.kind is NodeKind.TABLE and self.table is None:
            raise ValueError("table node needs table data")
        if self.kind is NodeKind.IMAGE and self.image is None:
            raise ValueError("image node needs image data")
        if self.kind is NodeKind.CHART and self.chart is None:
            raise ValueError("chart node needs chart data")
        return self


DocNode.model_rebuild()


# --------------------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------------------


class ReadMode(str, Enum):
    OUTLINE = "outline"
    FULL = "full"
    PAGES = "pages"
    SECTION = "section"
    SHEET = "sheet"
    SLIDE = "slide"
    EXCERPT = "excerpt"


class ReadRequest(_Strict):
    document_id: str
    revision: Optional[int] = Field(default=None, ge=0)
    mode: ReadMode = ReadMode.FULL
    pages: Optional[List[int]] = None
    slide: Optional[int] = Field(default=None, ge=1)
    sheet: Optional[str] = None
    cell_range: Optional[str] = Field(default=None, alias="range")
    section: Optional[str] = None
    max_chars: int = Field(default=40000, ge=256, le=400000)
    max_rows: int = Field(default=500, ge=1, le=20000)
    include_tables: bool = True
    include_images: bool = False
    include_notes: bool = True
    cursor: Optional[str] = None

    @model_validator(mode="after")
    def _mode_coordinates(self) -> "ReadRequest":
        if self.mode is ReadMode.PAGES and not self.pages:
            raise ValueError("mode 'pages' requires pages")
        if self.mode is ReadMode.SLIDE and not self.slide:
            raise ValueError("mode 'slide' requires slide")
        if self.mode is ReadMode.SHEET and not self.sheet:
            raise ValueError("mode 'sheet' requires sheet")
        if self.mode is ReadMode.SECTION and not self.section:
            raise ValueError("mode 'section' requires section")
        return self


class ReadInlineRequest(_Strict):
    """Read caller-supplied bytes without storing a copy (ACore workspace -> AGTools engine).

    AGCore keeps workspace bytes in its own blob store; this call lets the PDF/DOCX/XLSX/PPTX
    engines read them without uploading a second copy or creating an AGTools document.
    """

    name: str = Field(min_length=1, max_length=512)
    mime_type: str
    content_base64: str = Field(min_length=1)
    request: ReadRequest


class ReadStats(_Strict):
    chars: int = 0
    nodes: int = 0
    pages: int = 0
    slides: int = 0
    sheets: int = 0
    tables: int = 0
    images: int = 0
    elapsed_ms: int = 0


class ReadResponse(_Strict):
    contract_version: str = CONTRACT_VERSION
    document: DocumentRef
    mode: ReadMode = ReadMode.FULL
    nodes: List[DocNode] = Field(default_factory=list)
    citations: List[Citation] = Field(default_factory=list)
    truncated: bool = False
    next_cursor: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    stats: ReadStats = Field(default_factory=ReadStats)


# --------------------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------------------


class SearchMode(str, Enum):
    KEYWORD = "keyword"
    VECTOR = "vector"
    HYBRID = "hybrid"


class SearchFilters(_Strict):
    mime_types: List[str] = Field(default_factory=list)
    families: List[FormatFamily] = Field(default_factory=list)
    document_ids: List[str] = Field(default_factory=list)
    names: List[str] = Field(default_factory=list)
    path_prefix: Optional[str] = None
    updated_after: Optional[datetime] = None
    metadata: Dict[str, str] = Field(default_factory=dict)

    @field_validator("mime_types")
    @classmethod
    def _canonical(cls, value: List[str]) -> List[str]:
        return [normalize_mime(item) for item in value]


class SearchRequest(_Strict):
    query: str = Field(min_length=1, max_length=4000)
    mode: SearchMode = SearchMode.HYBRID
    top_k: int = Field(default=8, ge=1, le=50)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    document_ids: List[str] = Field(default_factory=list)
    allow_degraded: bool = True
    rerank: bool = True


class SearchHit(_Strict):
    hit_id: str
    document: DocumentRef
    locator: Locator = Field(default_factory=Locator)
    excerpt: str
    score: float = 0.0
    keyword_score: Optional[float] = None
    vector_score: Optional[float] = None
    matched_by: List[str] = Field(default_factory=list)
    citation: Optional[Citation] = None
    chunk_id: Optional[str] = None


class ProviderStatus(_Strict):
    keyword: str = "ok"
    embedding: str = "ok"
    vision: str = "ok"


class SearchResponse(_Strict):
    contract_version: str = CONTRACT_VERSION
    query: str
    mode_requested: SearchMode = SearchMode.HYBRID
    mode_effective: SearchMode = SearchMode.KEYWORD
    degraded: bool = False
    degradation_reason: Optional[str] = None
    provider_status: ProviderStatus = Field(default_factory=ProviderStatus)
    total_matches: int = 0
    duplicates_removed: int = 0
    results: List[SearchHit] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Create / Update / Validate
# --------------------------------------------------------------------------------------


class BlockKind(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    BULLETS = "bullets"
    TABLE = "table"
    IMAGE = "image"
    PAGE_BREAK = "page_break"


class ContentBlock(_Strict):
    kind: BlockKind
    text: str = ""
    level: int = Field(default=1, ge=1, le=9)
    bullets: List[str] = Field(default_factory=list)
    # Cell values keep their native type so a spreadsheet number stays numeric.
    rows: List[List[Any]] = Field(default_factory=list)
    header_rows: int = Field(default=0, ge=0, le=5)
    asset_id: Optional[str] = None
    image_base64: Optional[str] = None
    width_cm: Optional[float] = Field(default=None, gt=0, le=60)
    caption: str = ""


class CreateRequest(_Strict):
    name: str = Field(min_length=1, max_length=256)
    mime_type: str
    blocks: List[ContentBlock] = Field(default_factory=list)
    template_document_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    overwrite_document_id: Optional[str] = None

    @field_validator("mime_type")
    @classmethod
    def _canonical(cls, value: str) -> str:
        return normalize_mime(value)


class CreateResponse(_Strict):
    contract_version: str = CONTRACT_VERSION
    document: DocumentRef
    created: bool = True
    warnings: List[str] = Field(default_factory=list)


class OpKind(str, Enum):
    REPLACE_TEXT = "replace_text"
    INSERT_PARAGRAPH = "insert_paragraph"
    DELETE_PARAGRAPH = "delete_paragraph"
    SET_CELL = "set_cell"
    SET_RANGE = "set_range"
    ADD_TABLE = "add_table"
    ADD_CHART = "add_chart"
    ADD_SHEET = "add_sheet"
    ADD_SLIDE = "add_slide"
    DELETE_SLIDE = "delete_slide"
    MOVE_SLIDE = "move_slide"
    INSERT_IMAGE = "insert_image"
    REPLACE_IMAGE = "replace_image"
    SET_METADATA = "set_metadata"


class UpdateOperation(_Strict):
    op: OpKind
    # text edits
    find: Optional[str] = Field(default=None, max_length=4000)
    replace: Optional[str] = Field(default=None, max_length=20000)
    all_occurrences: bool = True
    # paragraph / slide coordinates
    paragraph: Optional[int] = Field(default=None, ge=0)
    slide: Optional[int] = Field(default=None, ge=1)
    from_index: Optional[int] = Field(default=None, ge=0)
    to_index: Optional[int] = Field(default=None, ge=0)
    # spreadsheet
    sheet: Optional[str] = None
    cell: Optional[str] = None
    cell_range: Optional[str] = Field(default=None, alias="range")
    value: Optional[Any] = None
    formula: Optional[str] = None
    rows: List[List[Any]] = Field(default_factory=list)
    chart_type: Optional[str] = None
    title: Optional[str] = None
    anchor: Optional[str] = None
    # document structure
    text: Optional[str] = None
    level: int = Field(default=1, ge=1, le=9)
    layout: Optional[Any] = None
    bullets: List[str] = Field(default_factory=list)
    image_base64: Optional[str] = None
    asset_id: Optional[str] = None
    width_cm: Optional[float] = Field(default=None, gt=0, le=60)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _required_fields(self) -> "UpdateOperation":
        required: Dict[OpKind, Tuple[str, ...]] = {
            OpKind.REPLACE_TEXT: ("find", "replace"),
            OpKind.INSERT_PARAGRAPH: ("text",),
            OpKind.DELETE_PARAGRAPH: ("paragraph",),
            OpKind.SET_CELL: ("sheet", "cell"),
            OpKind.SET_RANGE: ("sheet", "range"),
            OpKind.ADD_TABLE: ("rows",),
            OpKind.ADD_CHART: ("sheet",),
            OpKind.ADD_SHEET: ("sheet",),
            OpKind.ADD_SLIDE: (),
            OpKind.DELETE_SLIDE: ("slide",),
            OpKind.MOVE_SLIDE: ("from_index", "to_index"),
            OpKind.INSERT_IMAGE: (),
            OpKind.REPLACE_IMAGE: (),
            OpKind.SET_METADATA: ("metadata",),
        }
        aliases = {"range": "cell_range"}
        missing = [
            name for name in required[self.op]
            if getattr(self, aliases.get(name, name)) in (None, "", [])
        ]
        if missing:
            raise ValueError(f"op '{self.op.value}' requires: {', '.join(missing)}")
        return self


class UpdateRequest(_Strict):
    document_id: str
    expected_revision: Optional[int] = Field(default=None, ge=0)
    operations: List[UpdateOperation] = Field(min_length=1, max_length=200)
    dry_run: bool = False
    output_name: Optional[str] = Field(default=None, max_length=256)


class UpdateResponse(_Strict):
    contract_version: str = CONTRACT_VERSION
    document: DocumentRef
    previous_revision: int = 0
    applied: List[str] = Field(default_factory=list)
    skipped: List[str] = Field(default_factory=list)
    dry_run: bool = False
    warnings: List[str] = Field(default_factory=list)
    diff_summary: str = ""


class IntegrityCheck(_Strict):
    name: str
    ok: bool
    detail: str = ""


class ValidateRequest(_Strict):
    document_id: str
    revision: Optional[int] = Field(default=None, ge=0)
    deep: bool = True


class ValidateResponse(_Strict):
    contract_version: str = CONTRACT_VERSION
    document: DocumentRef
    ok: bool = True
    checks: List[IntegrityCheck] = Field(default_factory=list)
    errors: List["KnowledgeErrorBody"] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Capability manifest
# --------------------------------------------------------------------------------------


class ReadCapability(_Strict):
    body: bool = True
    structure: bool = True
    tables: bool = True
    images: bool = True
    charts: bool = False
    formulas: bool = False
    notes: bool = False
    styles: bool = False
    merged_cells: bool = False
    page_range: bool = False
    partial: bool = True
    outline: bool = True


class WriteCapability(_Strict):
    create: bool = False
    edit: bool = False
    insert_image: bool = False
    replace_image: bool = False
    tables: bool = False
    charts: bool = False
    preserve_styles: bool = False
    template_based: bool = False


class FormatCapability(_Strict):
    mime_type: str
    family: FormatFamily
    extensions: List[str] = Field(default_factory=list)
    read: ReadCapability = Field(default_factory=ReadCapability)
    write: WriteCapability = Field(default_factory=WriteCapability)
    citation_kinds: List[LocatorKind] = Field(default_factory=list)
    notes: str = ""
    available: bool = True
    unavailable_reason: Optional[str] = None


class SearchCapability(_Strict):
    modes: List[SearchMode] = Field(default_factory=lambda: [SearchMode.KEYWORD])
    metadata_filters: List[str] = Field(default_factory=list)
    incremental_index: bool = False
    ready_reuse: bool = False
    keyword_fallback: bool = True


class VisionCapability(_Strict):
    available: bool = False
    engine: str = "none"
    model: Optional[str] = None
    capability_source: str = "none"
    ocr_fallback: bool = False
    ocr_engine: Optional[str] = None
    reason: Optional[str] = None


class ArtifactCapability(_Strict):
    atomic_write: bool = True
    revisions: bool = True
    rollback: bool = True
    optimistic_concurrency: bool = True
    integrity_check: bool = True


class LimitCapability(_Strict):
    max_file_bytes: int = 0
    max_pages: int = 0
    max_slides: int = 0
    max_sheets: int = 0
    max_chars_read: int = 0
    max_search_top_k: int = 50
    max_cells_read: int = 0


class CapabilityManifest(_Strict):
    contract_version: str = CONTRACT_VERSION
    contracts_accepted: List[str] = Field(default_factory=lambda: [CONTRACT_VERSION])
    extensions: List[str] = Field(default_factory=lambda: list(CONTRACT_EXTENSIONS))
    service: str = "agcore"
    build: str = ""
    generated_at: str = ""
    formats: List[FormatCapability] = Field(default_factory=list)
    search: SearchCapability = Field(default_factory=SearchCapability)
    vision: VisionCapability = Field(default_factory=VisionCapability)
    artifacts: ArtifactCapability = Field(default_factory=ArtifactCapability)
    limits: LimitCapability = Field(default_factory=LimitCapability)
    error_codes: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class KnowledgeErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    DOCUMENT_NOT_FOUND = "document_not_found"
    REVISION_NOT_FOUND = "revision_not_found"
    REVISION_CONFLICT = "revision_conflict"
    DOCUMENT_LOCKED = "document_locked"
    ENCRYPTED_DOCUMENT = "encrypted_document"
    CORRUPT_CONTENT = "corrupt_content"
    UNSUPPORTED_FORMAT = "unsupported_format"
    SIZE_LIMIT_EXCEEDED = "size_limit_exceeded"
    PAGE_LIMIT_EXCEEDED = "page_limit_exceeded"
    INDEX_NOT_READY = "index_not_ready"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    DEGRADED_SEARCH = "degraded_search"
    WRITE_NOT_SUPPORTED = "write_not_supported"
    READ_NOT_SUPPORTED = "read_not_supported"
    VISION_UNAVAILABLE = "vision_unavailable"
    INTEGRITY_MISMATCH = "integrity_mismatch"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    CONTRACT_MISMATCH = "contract_mismatch"
    INTERNAL_ERROR = "internal_error"


class KnowledgeErrorBody(_Strict):
    code: KnowledgeErrorCode
    message: str = ""
    detail: Dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class KnowledgeError(Exception):
    """Raised inside the engine; serialized to ``KnowledgeErrorBody`` on the wire."""

    HTTP_STATUS: Dict[KnowledgeErrorCode, int] = {
        KnowledgeErrorCode.INVALID_REQUEST: 400,
        KnowledgeErrorCode.DOCUMENT_NOT_FOUND: 404,
        KnowledgeErrorCode.REVISION_NOT_FOUND: 404,
        KnowledgeErrorCode.REVISION_CONFLICT: 409,
        KnowledgeErrorCode.DOCUMENT_LOCKED: 423,
        KnowledgeErrorCode.ENCRYPTED_DOCUMENT: 422,
        KnowledgeErrorCode.CORRUPT_CONTENT: 422,
        KnowledgeErrorCode.UNSUPPORTED_FORMAT: 415,
        KnowledgeErrorCode.SIZE_LIMIT_EXCEEDED: 413,
        KnowledgeErrorCode.PAGE_LIMIT_EXCEEDED: 422,
        KnowledgeErrorCode.INDEX_NOT_READY: 409,
        KnowledgeErrorCode.PROVIDER_UNAVAILABLE: 503,
        KnowledgeErrorCode.DEGRADED_SEARCH: 200,
        KnowledgeErrorCode.WRITE_NOT_SUPPORTED: 405,
        KnowledgeErrorCode.READ_NOT_SUPPORTED: 405,
        KnowledgeErrorCode.VISION_UNAVAILABLE: 422,
        KnowledgeErrorCode.INTEGRITY_MISMATCH: 422,
        KnowledgeErrorCode.PERMISSION_DENIED: 403,
        KnowledgeErrorCode.RATE_LIMITED: 429,
        KnowledgeErrorCode.CONTRACT_MISMATCH: 400,
        KnowledgeErrorCode.INTERNAL_ERROR: 500,
    }

    def __init__(
        self,
        code: KnowledgeErrorCode,
        message: str = "",
        *,
        detail: Optional[Dict[str, Any]] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message or code.value)
        self.body = KnowledgeErrorBody(
            code=code, message=message or code.value, detail=detail or {}, retryable=retryable
        )

    @property
    def status_code(self) -> int:
        return self.HTTP_STATUS.get(self.body.code, 500)


ValidateResponse.model_rebuild()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------------------
# AGCore federation extensions (additive; extension id `provider-federation`)
# --------------------------------------------------------------------------------------

WORKSPACE_CONTRACT_VERSION = "r2-w/v1+a02"


class ContextStatus(str, Enum):
    OK = "ok"
    INSUFFICIENT_DOCUMENTS = "insufficient_documents"
    EMPTY_WORKSPACE = "empty_workspace"


class ProviderKind(str, Enum):
    WORKSPACE = "workspace"
    SESSION_ATTACHMENT = "session_attachment"
    AGTOOLS = "agtools"


class ProviderCapability(_Strict):
    """What one engine behind AGCore can do, and whether it answered just now."""

    provider: ProviderKind
    available: bool = True
    contract_version: str = CONTRACT_VERSION
    formats: List[FormatCapability] = Field(default_factory=list)
    search_modes: List[SearchMode] = Field(default_factory=list)
    vision: VisionCapability = Field(default_factory=VisionCapability)
    degraded_reason: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)


class KnowledgeManifest(CapabilityManifest):
    """Merged manifest: the AGCore contract plus every provider's own capability table."""

    service: str = "agcore"
    workspace_contract: str = WORKSPACE_CONTRACT_VERSION
    providers: List[ProviderCapability] = Field(default_factory=list)


class FederatedSearchRequest(SearchRequest):
    workspace_id: Optional[str] = None
    conversation_id: Optional[str] = None
    sources: List[ProviderKind] = Field(
        default_factory=lambda: [ProviderKind.WORKSPACE, ProviderKind.SESSION_ATTACHMENT]
    )
    file_ids: List[str] = Field(default_factory=list)


class KnowledgeContextRequest(_Strict):
    query: str = Field(min_length=1, max_length=4000)
    workspace_id: Optional[str] = None
    conversation_id: Optional[str] = None
    top_k: int = Field(default=8, ge=1, le=50)
    token_budget: Optional[int] = Field(default=None, ge=64, le=131072)
    mode: SearchMode = SearchMode.HYBRID
    sources: List[ProviderKind] = Field(
        default_factory=lambda: [ProviderKind.WORKSPACE, ProviderKind.SESSION_ATTACHMENT]
    )
    file_ids: List[str] = Field(default_factory=list)
    allow_degraded: bool = True


class KnowledgeContextResponse(_Strict):
    contract_version: str = CONTRACT_VERSION
    query: str
    status: ContextStatus = ContextStatus.OK
    context_text: str = ""
    used_tokens: int = 0
    budget_tokens: int = 0
    truncated: bool = False
    sources: List[Citation] = Field(default_factory=list)
    retrieved: List[SearchHit] = Field(default_factory=list)
    degraded: bool = False
    warnings: List[str] = Field(default_factory=list)
    injection_suspected: int = 0


class ArtifactCreateRequest(_Strict):
    workspace_id: str
    path: str = Field(min_length=1, max_length=512)
    mime_type: str = "application/octet-stream"
    content_base64: Optional[str] = None
    text: Optional[str] = None
    overwrite: bool = False
    expected_content_hash: Optional[str] = None
    artifact_id: Optional[str] = None
    conversation_id: Optional[str] = None


class ArtifactUpdateRequest(_Strict):
    workspace_id: str
    file_id: str
    mime_type: str = "application/octet-stream"
    content_base64: Optional[str] = None
    text: Optional[str] = None
    expected_content_hash: Optional[str] = None
    expected_revision: Optional[int] = Field(default=None, ge=1)


class ArtifactRollbackRequest(_Strict):
    workspace_id: str
    file_id: str
    to_revision: int = Field(ge=1)
    expected_content_hash: Optional[str] = None


class ArtifactResponse(_Strict):
    contract_version: str = CONTRACT_VERSION
    document: DocumentRef
    workspace_id: str
    path: str
    revision: int
    content_hash: str
    created: bool = True
    warnings: List[str] = Field(default_factory=list)


class ArtifactValidateRequest(_Strict):
    workspace_id: str
    file_id: str


class ArtifactValidateResponse(_Strict):
    contract_version: str = CONTRACT_VERSION
    document: DocumentRef
    ok: bool = True
    checks: List[IntegrityCheck] = Field(default_factory=list)
    errors: List[KnowledgeErrorBody] = Field(default_factory=list)


class VisionAnalyzeRequest(_Strict):
    image_base64: str
    mime_type: str = "image/png"
    prompt: str = Field(default="", max_length=2000)


class VisionAnalyzeResponse(_Strict):
    contract_version: str = CONTRACT_VERSION
    available: bool = False
    text: str = ""
    engine: str = "none"
    model: Optional[str] = None
    capability_source: str = "none"
    error: Optional[str] = None
