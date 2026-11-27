"""Agentic Execution Preview Adapter (WO-R2-W Amendment 02 §1.3).

One turn runs the full agent loop against the real workspace engine:

    User Request → Intent Analysis → Workspace Inspection → Tool Selection
    → File Search / Direct Read / RAG Retrieval → Analysis
    → File Creation / Patch → Result Verification → Chat Response

Design boundaries
-----------------
* **R1.3 runtime untouched.** This adapter is independent: it drives `WorkspaceToolAdapter`
  (the 11 Amendment 02 tools) and never imports the R1.3 graph/runner.
* **Fixed RAG pipeline is not forced.** The adapter picks tools per turn: an exact filename
  goes to `workspace_read_file` (direct read), an exact string to `workspace_search_text`,
  a semantic question to `workspace_retrieve`. A structure question never reads file bodies.
* **Tool execution is real, answer text is mock.** Every listed tool call really touched the
  workspace (rows, revisions, patches). No LLM is connected in this repository, so the answer
  is composed by a deterministic extractive writer and is labelled `llm.mock = true`.
* **Approval boundary.** Full overwrite, move-over-existing and delete need the user's
  confirmation; without it the turn stops with `pending_approval` and performs no write.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.workspace.context import detect_injection, sanitize_document_text
from app.workspace.contracts import (
    ArtifactReferenceInput,
    FileKind,
    ScopeView,
    WorkspaceErrorCode,
    WorkspaceLifecycleState,
)
from app.workspace.manager import WorkspaceFailure, WorkspaceManager
from app.workspace.retrieval import WorkspaceRetriever
from app.workspace.tools import (
    READ_TOOLS,
    TOOL_MANIFEST,
    WRITE_TOOLS,
    ToolInvocationContext,
    WorkspaceToolAdapter,
    relative_path_of,
    resolve_file,
)

MOCK_LLM_NOTICE = (
    "실제 LLM이 연결되지 않은 미리보기 응답입니다. 도구 실행(파일 읽기/생성/수정)은 실제 "
    "워크스페이스에 반영되었고, 응답 문장은 실행 결과에서 결정적으로 조립했습니다."
)

INTENT_LABELS = {
    "structure": "폴더 구성 설명",
    "find_and_summarize": "문서 검색 후 요약",
    "compare": "문서 비교",
    "create": "분석 결과 파일 생성",
    "patch": "기존 파일 부분 수정",
    "delete": "파일 삭제(승인 필요)",
    "open_canvas": "파일 Canvas 열기",
    "answer": "문서 근거 답변",
}


# --------------------------------------------------------------------------- models
class AgentToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: int
    tool: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    status: str = "ok"
    summary: str = ""
    duration_ms: int = 0
    revision: Optional[int] = None
    error: Optional[Dict[str, Any]] = None


class AgentFileAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str  # read | created | patched | moved | deleted | opened
    file_id: str
    path: str
    name: str = ""
    revision: int = 0
    previous_revision: Optional[int] = None
    diff: Optional[str] = None
    content_hash: Optional[str] = None
    canvas: Optional[Dict[str, Any]] = None


class AgentTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=8000)
    conversation_id: Optional[str] = Field(default=None, max_length=128)
    context_budget_tokens: Optional[int] = Field(default=None, ge=64, le=131072)
    capability: Optional[Dict[str, Any]] = None
    #: Approved external action ids, e.g. "workspace_delete_file:archive/old.md".
    approvals: List[str] = Field(default_factory=list)
    llm_mode: str = "mock"


class AgentTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str
    conversation_id: str
    workspace_id: str
    intent: str
    intent_label: str = ""
    intent_reason: str = ""
    plan: List[str] = Field(default_factory=list)
    tool_calls: List[AgentToolCall] = Field(default_factory=list)
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    files: List[AgentFileAction] = Field(default_factory=list)
    answer: str = ""
    status: str = "completed"  # completed | needs_input | partial | failed
    llm: Dict[str, Any] = Field(default_factory=dict)
    verification: Dict[str, Any] = Field(default_factory=dict)
    retrieval_availability: Dict[str, Any] = Field(default_factory=dict)
    injection_suspected: int = 0
    pending_approval: Optional[Dict[str, Any]] = None
    needs_input: Optional[str] = None
    created_at: str = ""


class AgentStepError(RuntimeError):
    def __init__(self, tool: str, error: Dict[str, Any]) -> None:
        super().__init__(f"{tool}: {error.get('code')}")
        self.tool = tool
        self.error = error


# --------------------------------------------------------------------------- intent
@dataclass
class IntentAnalysis:
    intent: str
    reason: str
    targets: List[str] = field(default_factory=list)
    target_path: Optional[str] = None
    section: Optional[str] = None
    query: str = ""
    terms: List[str] = field(default_factory=list)


_EXT = r"(?:md|markdown|txt|json|csv|tsv|py|ts|tsx|js|jsx|yaml|yml|html|css|log|pdf|docx|pptx|xlsx)"
_FILE_TOKEN = re.compile(rf"[A-Za-z0-9_가-힣][A-Za-z0-9_./가-힣-]*\.{_EXT}(?![A-Za-z0-9])", re.I)
_BACKTICK = re.compile(r"`([^`]{1,200})`")
_QUOTED = re.compile(r"[\"'“”‘’]([^\"'“”‘’]{1,200})[\"'“”‘’]")

_STOPWORDS = {
    "이", "그", "저", "좀", "해줘", "해라", "해", "줘", "요", "것", "거", "수", "있는", "있어",
    "문서", "파일", "폴더", "자료", "내용", "관련", "대해", "대한", "그리고", "또는", "에서",
    "으로", "로", "를", "을", "이랑", "랑", "의", "에", "가", "은", "는", "도", "만", "좀더",
    "찾아서", "찾아", "요약해줘", "요약", "설명해줘", "설명", "비교해줘", "비교", "저장해줘",
    "저장", "수정해줘", "수정", "열어줘", "열어", "기존", "방금", "생성한", "만든", "정리해줘",
    "분석", "결과", "결과를", "해주세요", "해줄래", "보여줘", "알려줘", "the", "a", "an", "of",
    "to", "and", "in", "for", "please", "file", "files", "folder", "document", "documents",
    "summary", "compare", "open", "save", "edit", "update", "this", "that", "with",
    "삭제해줘", "삭제", "지워줘", "지워",
    "md", "markdown", "txt", "json", "csv", "tsv", "py", "ts", "tsx", "js", "jsx",
    "yaml", "yml", "html", "pdf", "docx", "pptx", "xlsx", "log", "css",
}

_SECTION_HINTS = ("결론", "요약", "개요", "배경", "목적", "범위", "다음 단계", "참고", "부록", "서론", "본문")


_PARTICLES = (
    "에서", "으로", "라고", "이라는", "이라는", "부터", "까지", "보다", "처럼", "한테",
    "에게", "께서", "이랑", "으로", "로서", "로써", "라도", "이나", "든지",
    "를", "을", "의", "에", "가", "이", "은", "는", "과", "와", "도", "만", "로", "야", "랑",
)


def _strip_particle(token: str) -> str:
    for particle in _PARTICLES:
        if len(token) > len(particle) + 1 and token.endswith(particle):
            return token[: -len(particle)]
    return token


def _terms(message: str) -> List[str]:
    tokens = re.split(r"[\s,.;:!?()\[\]{}<>/\\|`\"'“”‘’·…~]+", message.lower())
    terms: List[str] = []
    for token in tokens:
        cleaned = _strip_particle(token.strip("-_")).strip("- _")
        if len(cleaned) < 2 or cleaned in _STOPWORDS:
            continue
        if cleaned.isdigit():
            continue
        terms.append(cleaned)
    seen: List[str] = []
    for term in terms:
        if term not in seen:
            seen.append(term)
    return seen[:12]


def _extract_targets(message: str) -> List[str]:
    found: List[str] = []
    for match in _BACKTICK.findall(message) + _QUOTED.findall(message):
        candidate = match.strip()
        if re.search(rf"\.{_EXT}$", candidate, re.I):
            found.append(candidate)
    for match in _FILE_TOKEN.findall(message):
        found.append(match.strip(".,;:"))
    unique: List[str] = []
    for item in found:
        if item and item not in unique:
            unique.append(item)
    return unique


def analyse_intent(message: str) -> IntentAnalysis:
    """Deterministic intent analysis (the 'Intent Analysis' stage of the preview loop)."""

    text = (message or "").strip()
    lower = text.lower()
    targets = _extract_targets(text)
    terms = _terms(text)
    quoted = [item.strip() for item in _BACKTICK.findall(text) + _QUOTED.findall(text)]
    path_target = next((item for item in targets if "/" in item), targets[0] if targets else None)
    section = next((hint for hint in _SECTION_HINTS if hint in text), None)

    if ("canvas" in lower or "캔버스" in lower) and re.search(r"열|open", lower):
        return IntentAnalysis("open_canvas", "Canvas 열기 요청이 감지되었습니다.", targets, path_target, section, text, terms)
    if re.search(r"삭제|지워|제거|delete|remove", lower) and (targets or re.search(r"파일|문서|folder|file", lower)):
        return IntentAnalysis("delete", "파일 삭제 요청입니다(사용자 승인 필요).", targets, path_target, section, text, terms)
    if re.search(r"수정|고쳐|바꿔|변경|patch|edit|update", lower) and (
        targets or re.search(r"파일|문서|folder|file", lower)
    ):
        return IntentAnalysis("patch", "대상 파일 수정 요청입니다.", targets, path_target, section, text, terms)
    if re.search(r"저장|만들|작성|생성|save|create|write", lower) and (
        path_target or re.search(r"파일|문서|folder|file", lower)
    ):
        return IntentAnalysis("create", "파일을 생성/저장하는 요청입니다.", targets, path_target, section, text, terms)
    if re.search(r"비교|차이|대조|compare|diff", lower):
        return IntentAnalysis("compare", "두 개 이상 문서 비교 요청입니다.", targets, path_target, section, text, terms)
    if re.search(r"찾아|검색|요약|정리|find", lower):
        return IntentAnalysis("find_and_summarize", "문서를 찾아 요약하는 요청입니다.", targets, path_target, section, text, terms)
    if re.search(r"구성|구조|목록|뭐가|무엇이|어떤 파일|트리|list|structure|overview", lower) and re.search(
        r"폴더|파일|워크스페이스|folder|file|workspace", lower
    ):
        return IntentAnalysis("structure", "폴더/파일 구성 질문입니다.", targets, path_target, section, text, terms)
    return IntentAnalysis("answer", "파일 조작 없이 문서 근거로 답하는 질문입니다.", targets, path_target, section, text, terms)


# --------------------------------------------------------------------------- text helpers
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\n{2,}")
_INJECTION_LINE = re.compile(
    r"(ignore\s+(all\s+)?(previous|prior|above)|disregard\s+(all\s+)?(previous|prior|above)"
    r"|you\s+are\s+now|system\s*prompt|developer\s+mode|이전\s*(의)?\s*(지시|명령|프롬프트)\s*(를|을)?\s*(무시|버려|잊어)"
    r"|시스템\s*프롬프트)",
    re.I,
)


def _clean_excerpt(text: str, limit: int = 400) -> str:
    """Neutralise delimiters and drop instruction-like lines before quoting a document."""

    cleaned = sanitize_document_text(text or "")
    kept = [line for line in cleaned.splitlines() if not _INJECTION_LINE.search(line)]
    joined = " ".join(line.strip() for line in kept if line.strip())
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined[:limit]


def _clean_lines(text: str, limit: int = 20000) -> List[str]:
    """Line-preserving clean text (headings and instruction lines removed)."""

    cleaned = sanitize_document_text(text or "")
    kept: List[str] = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or _INJECTION_LINE.search(stripped):
            continue
        kept.append(stripped)
        if sum(len(item) for item in kept) > limit:
            break
    return kept


def _headings(text: str) -> List[str]:
    titles: List[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                titles.append(title)
    return titles


def _sentences(text: str) -> List[str]:
    body = _clean_lines(text)
    sentences: List[str] = []
    for line in body:
        for piece in _SENTENCE_SPLIT.split(line):
            cleaned = piece.strip(" -·•\t")
            if len(cleaned) >= 12:
                sentences.append(cleaned)
    return sentences


def _score_sentences(text: str, terms: Sequence[str], limit: int = 5) -> List[str]:
    sentences = _sentences(text)
    if not sentences:
        return []
    if not terms:
        return sentences[:limit]
    scored: List[Tuple[float, int, str]] = []
    for index, sentence in enumerate(sentences):
        lowered = sentence.lower()
        hits = sum(1 for term in terms if term in lowered)
        if hits:
            scored.append((hits / max(1.0, len(terms)), index, sentence))
    scored.sort(key=lambda item: (-item[0], item[1]))
    picked = sorted(scored[:limit], key=lambda item: item[1])
    result = [sentence for _score, _index, sentence in picked]
    if len(result) < limit:
        # Sparse keyword overlap must not produce an empty summary: fill with leading sentences.
        for sentence in sentences:
            if sentence not in result:
                result.append(sentence)
            if len(result) >= limit:
                break
    return result


def _keyword_set(text: str, terms: Sequence[str]) -> List[str]:
    lowered = _clean_excerpt(text, limit=40000).lower()
    found = [term for term in terms if term in lowered]
    return found


def _relative_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _arg_preview(arguments: Dict[str, Any]) -> Dict[str, Any]:
    preview: Dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > 160:
            preview[key] = value[:160] + "…"
        elif isinstance(value, list):
            preview[key] = value[:5]
        else:
            preview[key] = value
    return preview


# --------------------------------------------------------------------------- adapter
class WorkspaceAgentAdapter:
    """Runs one agent turn over the workspace tools (preview adapter, R1.3 untouched)."""

    CONVERSATION_CAP = 200
    MAX_READ_FILES = 3

    def __init__(
        self,
        manager: WorkspaceManager,
        retriever: WorkspaceRetriever,
        assembler: Any,
        tools: WorkspaceToolAdapter,
        settings: Any = None,
    ) -> None:
        self.manager = manager
        self.retriever = retriever
        self.assembler = assembler
        self.tools = tools
        self.settings = settings
        self._conversations: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    # -- public -----------------------------------------------------------------
    def run(
        self,
        workspace_id: str,
        scope: ScopeView,
        message: str,
        *,
        conversation_id: Optional[str] = None,
        capability: Optional[Dict[str, Any]] = None,
        context_budget: Optional[int] = None,
        approvals: Optional[Sequence[str]] = None,
    ) -> AgentTurnResponse:
        turn = _TurnRun(self, workspace_id, scope, message, conversation_id, capability, context_budget, list(approvals or []))
        return turn.execute()

    def conversation_state(self, conversation_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._conversations.get(conversation_id) or {})

    def remember(self, conversation_id: str, state: Dict[str, Any]) -> None:
        with self._lock:
            existing = self._conversations.get(conversation_id) or {}
            existing.update(state)
            existing["updated_at"] = _relative_utc()
            self._conversations[conversation_id] = existing
            self._conversations.move_to_end(conversation_id)
            while len(self._conversations) > self.CONVERSATION_CAP:
                self._conversations.popitem(last=False)


class _TurnRun:
    """Mutable state of one turn; keeps the tool trace and the file actions."""

    def __init__(
        self,
        adapter: WorkspaceAgentAdapter,
        workspace_id: str,
        scope: ScopeView,
        message: str,
        conversation_id: Optional[str],
        capability: Optional[Dict[str, Any]],
        context_budget: Optional[int],
        approvals: List[str],
    ) -> None:
        self.adapter = adapter
        self.manager = adapter.manager
        self.workspace_id = workspace_id
        self.scope = scope
        self.message = message.strip()
        self.conversation_id = conversation_id or f"conv_{uuid.uuid4().hex[:12]}"
        self.capability = capability
        self.context_budget = context_budget
        self.approvals = approvals
        self.calls: List[AgentToolCall] = []
        self.files: List[AgentFileAction] = []
        self.citations: Dict[str, Dict[str, Any]] = {}
        self.plan: List[str] = []
        self.injection = 0
        self.verification: Dict[str, Any] = {}
        self.pending_approval: Optional[Dict[str, Any]] = None
        self.needs_input: Optional[str] = None
        self.status = "completed"
        self.answer = ""
        self.intent = "answer"
        self.analysis: Optional[IntentAnalysis] = None
        self.inspected: Dict[str, Any] = {}

    # -- tool plumbing ----------------------------------------------------------
    def _context(self) -> ToolInvocationContext:
        return ToolInvocationContext(
            scope=self.scope,
            capability=self.capability,
            context_budget=self.context_budget,
            conversation_id=self.conversation_id,
        )

    def _call(self, tool: str, arguments: Dict[str, Any], summary: str = "") -> Dict[str, Any]:
        if tool not in self.plan:
            self.plan.append(tool)
        response = self.adapter.tools.invoke(tool, arguments, self._context())
        record = AgentToolCall(
            step=len(self.calls) + 1,
            tool=tool,
            arguments=_arg_preview(arguments),
            status=response.get("status", "ok"),
            duration_ms=int(response.get("duration_ms") or 0),
            summary=summary,
        )
        if response.get("status") == "ok":
            result = response.get("result") or {}
            record.revision = result.get("revision")
            record.summary = summary or _default_summary(tool, result)
            self.calls.append(record)
            return result
        record.error = response.get("error") or {"code": "unknown"}
        self.calls.append(record)
        if response.get("status") == "approval_required":
            self.pending_approval = response.get("approval") or {
                "tool": tool,
                "required": ["confirm"],
                "arguments": _arg_preview(arguments),
            }
            self.pending_approval.setdefault("tool", tool)
            self.pending_approval.setdefault("proposed_arguments", _arg_preview(arguments))
            raise _ApprovalStop(tool)
        raise AgentStepError(tool, record.error)

    def _approved(self, tool: str, path: str) -> bool:
        return f"{tool}:{path}" in self.approvals or f"{tool}:*" in self.approvals

    # -- inspection -------------------------------------------------------------
    def _inspect(self) -> Dict[str, Any]:
        result = self._call("workspace_list_files", {"workspace_id": self.workspace_id, "recursive": True}, "워크스페이스 구조 확인")
        entries: List[Dict[str, Any]] = result.get("entries") or []
        files = [item for item in entries if item.get("kind") == "file"]
        folders = [item for item in entries if item.get("kind") == "folder"]
        self.inspected = {
            "total": result.get("total", len(entries)),
            "files": files,
            "folders": folders,
        }
        return self.inspected

    def _resolve_explicit(self, targets: Sequence[str]) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        inspected_files = self.inspected.get("files") or []
        for target in targets:
            identity = None
            try:
                identity = resolve_file(self.manager, self.workspace_id, self.scope, path=target)
            except WorkspaceFailure:
                identity = None
            entry: Optional[Dict[str, Any]] = None
            if identity is not None and identity.kind is FileKind.FILE:
                entry = _entry_of(identity)
            if entry is None:
                # A bare file name ("summary.md") still matches reports/summary.md.
                lowered = target.lower().strip()
                for item in inspected_files:
                    rel = (item.get("relative_path") or "").lower()
                    name = (item.get("name") or "").lower()
                    if rel == lowered or name == lowered or rel.endswith("/" + lowered):
                        entry = item
                        break
            if entry is not None and all(item["file_id"] != entry["file_id"] for item in found):
                found.append(entry)
        return found

    def _find(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        if not query:
            return []
        result = self._call(
            "workspace_find_files",
            {"workspace_id": self.workspace_id, "query": query, "limit": limit},
            f"이름/경로 검색: {query}",
        )
        return [item for item in result.get("matches") or [] if item.get("kind") == "file"]

    def _read(self, entry: Dict[str, Any], max_tokens: int = 1200) -> Optional[Dict[str, Any]]:
        try:
            result = self._call(
                "workspace_read_file",
                {
                    "workspace_id": self.workspace_id,
                    "file_id": entry["file_id"],
                    "max_tokens": max_tokens,
                },
                f"{entry.get('relative_path') or entry.get('path')} 읽기",
            )
        except AgentStepError:
            return None
        if result.get("binary"):
            return None
        self.injection += int(result.get("injection_suspected") or 0)
        doc = {
            "file_id": entry["file_id"],
            "path": entry.get("relative_path") or entry.get("path", "").lstrip("/"),
            "name": result.get("name") or entry.get("name", ""),
            "revision": int(result.get("revision") or 0),
            "content_hash": result.get("content_hash"),
            "text": result.get("text") or "",
            "truncated": bool(result.get("truncated")),
        }
        self._add_file(
            AgentFileAction(
                action="read",
                file_id=doc["file_id"],
                path=doc["path"],
                name=doc["name"],
                revision=doc["revision"],
                content_hash=doc.get("content_hash"),
            )
        )
        return doc

    def _retrieve(
        self,
        query: str,
        *,
        file_ids: Optional[Sequence[str]] = None,
        mode: str = "hybrid",
        top_k: int = 6,
        record: bool = True,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "query": query,
            "mode": mode,
            "top_k": top_k,
        }
        if file_ids:
            arguments["file_ids"] = list(file_ids)
        try:
            if record:
                result = self._call("workspace_retrieve", arguments, f"RAG 검색({mode}): {query[:60]}")
            else:
                response = self.adapter.tools.invoke("workspace_retrieve", arguments, self._context())
                if response.get("status") != "ok":
                    return {}
                result = response.get("result") or {}
        except AgentStepError:
            return {}
        for citation in result.get("citations") or []:
            self.citations[citation["citation_id"]] = citation
        self.injection += int(result.get("injection_suspected") or 0)
        return result

    def _add_file(self, action: AgentFileAction) -> None:
        # One card per file and outcome: a verification re-read never duplicates a created/patched card.
        for existing in self.files:
            if existing.file_id != action.file_id:
                continue
            if (existing.action, existing.revision) == (action.action, action.revision):
                return
            if action.action == "read" and existing.action in {"created", "patched", "opened"}:
                return
            if action.action in {"created", "patched"} and existing.action == "read":
                self.files.remove(existing)
                break
        self.files.append(action)

    def _remember_files(self, actions: Sequence[AgentFileAction]) -> None:
        state = self.adapter.conversation_state(self.conversation_id)
        recent = list(state.get("recent_files") or [])
        for action in actions:
            recent = [item for item in recent if item.get("file_id") != action.file_id]
            recent.insert(
                0,
                {
                    "file_id": action.file_id,
                    "path": action.path,
                    "name": action.name,
                    "revision": action.revision,
                    "action": action.action,
                    "at": _relative_utc(),
                },
            )
        self.adapter.remember(
            self.conversation_id,
            {"recent_files": recent[:10], "last_intent": self.intent, "workspace_id": self.workspace_id},
        )

    # -- verification -----------------------------------------------------------
    def _verify_write(self, path: str, *, expected_hash: Optional[str], section: Optional[str] = None) -> Dict[str, Any]:
        try:
            identity = resolve_file(self.manager, self.workspace_id, self.scope, path=path)
        except WorkspaceFailure as failure:
            self.verification = {"write_verified": False, "error": failure.error.model_dump(mode="json")}
            return self.verification
        read = self._read(_entry_of(identity), max_tokens=800) if identity.kind is FileKind.FILE else None
        verification: Dict[str, Any] = {
            "write_verified": read is not None,
            "file_id": identity.file_id,
            "path": relative_path_of(identity),
            "revision": identity.revision,
            "content_hash": identity.content_hash,
            "hash_matches_intent": (expected_hash is None or expected_hash == identity.content_hash),
            "index_status": identity.index_status.value if identity.index_status else None,
        }
        if section and read is not None:
            verification["section_present"] = section.lower() in read["text"].lower()
        self.verification = verification
        return verification

    # -- pipeline ---------------------------------------------------------------
    def execute(self) -> AgentTurnResponse:
        try:
            self.manager.ensure_open(self.workspace_id, self.scope)
            self.manager.get_workspace(self.workspace_id, self.scope)  # access check
            self.manager.add_conversation_reference(self.workspace_id, self.scope, self.conversation_id)
            self.analysis = analyse_intent(self.message)
            self.intent = self.analysis.intent
            self._inspect()

            if not self.inspected["files"] and not self.inspected["folders"]:
                self.status = "needs_input"
                self.needs_input = "워크스페이스가 비어 있습니다. 폴더를 열거나 파일을 올린 뒤 다시 요청해 주세요."
                self.answer = self.needs_input
            elif self.intent == "structure":
                self._run_structure()
            elif self.intent == "find_and_summarize":
                self._run_find_and_summarize()
            elif self.intent == "compare":
                self._run_compare()
            elif self.intent == "create":
                self._run_create()
            elif self.intent == "patch":
                self._run_patch()
            elif self.intent == "delete":
                self._run_delete()
            elif self.intent == "open_canvas":
                self._run_open_canvas()
            else:
                self._run_answer()
        except _ApprovalStop:
            self.status = "needs_input"
            approval = self.pending_approval or {}
            action_label = {
                "delete": "삭제",
                "overwrite": "덮어쓰기",
                "move_overwrite": "기존 파일 대체 이동",
            }.get(str(approval.get("action") or ""), "변경")
            self.answer = (
                f"`{approval.get('path', '')}`에 대한 {action_label} 작업은 되돌리기 어려워 사용자 승인이 필요합니다. "
                "아래 승인 카드에서 승인하면 같은 요청을 이어서 실행합니다."
            )
            self.needs_input = "approval_required"
        except AgentStepError as error:
            self.status = "partial" if self.calls else "failed"
            self.answer = (
                f"도구 실행이 실패해 작업을 멈췄습니다 ({error.tool}, {error.error.get('code')}). "
                f"{error.error.get('message') or ''}".strip()
            )
        except WorkspaceFailure as failure:
            self.status = "failed"
            payload = failure.error.model_dump(mode="json")
            self.answer = f"워크스페이스 접근에 실패했습니다 ({payload.get('code')}). {payload.get('message') or ''}".strip()
        finally:
            self._remember_files(self.files)

        return AgentTurnResponse(
            turn_id=f"turn_{uuid.uuid4().hex[:12]}",
            conversation_id=self.conversation_id,
            workspace_id=self.workspace_id,
            intent=self.intent,
            intent_label=INTENT_LABELS.get(self.intent, self.intent),
            intent_reason=(self.analysis.reason if self.analysis else ""),
            plan=self.plan,
            tool_calls=self.calls,
            citations=list(self.citations.values()),
            files=self.files,
            answer=self.answer,
            status=self.status,
            llm={
                "provider": "mock",
                "model": "extractive-preview-writer",
                "mock": True,
                "notice": MOCK_LLM_NOTICE,
                "tools_executed": len(self.calls),
            },
            verification=self.verification,
            retrieval_availability=_availability(self.adapter, self.workspace_id),
            injection_suspected=self.injection,
            pending_approval=self.pending_approval,
            needs_input=self.needs_input,
            created_at=_relative_utc(),
        )

    # -- intents ----------------------------------------------------------------
    def _run_structure(self) -> None:
        files = self.inspected["files"]
        folders = self.inspected["folders"]
        lines: List[str] = []
        roots: Dict[str, int] = {}
        for item in files:
            rel = item.get("relative_path") or ""
            root = rel.split("/", 1)[0] if "/" in rel else "(루트)"
            roots[root] = roots.get(root, 0) + 1
        lines.append(f"이 workspace에는 폴더 {len(folders)}개, 파일 {len(files)}개가 있습니다.")
        if roots:
            lines.append("")
            lines.append("**최상위 구성**")
            for root, count in sorted(roots.items(), key=lambda pair: (-pair[1], pair[0])):
                lines.append(f"- `{root}` — 파일 {count}개")
        indexed = sum(1 for item in files if (item.get("index_status") or "").upper() == "READY")
        lines.append("")
        lines.append(f"인덱스 준비(READY) {indexed}건 / 전체 {len(files)}건")
        if files:
            lines.append("")
            lines.append("**파일 목록**")
            for item in files[:40]:
                size = item.get("size_bytes") or 0
                status = item.get("index_status") or "-"
                lines.append(f"- `{item.get('relative_path')}` ({_human_size(size)}, index {status})")
            if len(files) > 40:
                lines.append(f"- … 외 {len(files) - 40}건")
        lines.append("")
        lines.append("_파일 본문은 읽지 않고 메타데이터만 확인했습니다._")
        self.answer = "\n".join(lines)
        self.verification = {
            "files": len(files),
            "folders": len(folders),
            "matches_workspace": len(files) + len(folders) == int(self.inspected.get("total") or 0),
        }

    def _candidates_for(self, analysis: IntentAnalysis, *, wanted: int = 2) -> List[Dict[str, Any]]:
        explicit = self._resolve_explicit(analysis.targets)
        if len(explicit) >= wanted:
            return explicit
        candidates = list(explicit)
        for term in analysis.terms[:4]:
            for entry in self._find(term, limit=8):
                if all(entry["file_id"] != item["file_id"] for item in candidates):
                    candidates.append(entry)
            if len(candidates) >= wanted:
                break
        if len(candidates) < wanted:
            retrieved = self._retrieve(analysis.query, top_k=8)
            for item in retrieved.get("retrieved") or []:
                file_id = item.get("file_id")
                if file_id and all(entry["file_id"] != file_id for entry in candidates):
                    candidates.append(
                        {
                            "file_id": file_id,
                            "kind": "file",
                            "name": item.get("file_name", ""),
                            "relative_path": item.get("file_name", ""),
                            "index_status": None,
                        }
                    )
                if len(candidates) >= wanted:
                    break
        if len(candidates) < wanted:
            # Last resort: the most plausible files already inspected this turn.
            for entry in _rank_files(self.inspected.get("files") or [], analysis.terms):
                if all(entry["file_id"] != item["file_id"] for item in candidates):
                    candidates.append(entry)
                if len(candidates) >= wanted:
                    break
        return candidates

    def _run_find_and_summarize(self) -> None:
        analysis = self.analysis or analyse_intent(self.message)
        candidates = self._candidates_for(analysis, wanted=1)
        if not candidates:
            self.status = "needs_input"
            self.needs_input = "조건에 맞는 문서를 찾지 못했습니다."
            self.answer = "요청과 맞는 문서를 찾지 못했습니다. 다른 이름이나 키워드로 다시 요청해 주세요."
            return
        target = candidates[0]
        document = self._read(target, max_tokens=1600)
        if document is None:
            self.status = "partial"
            self.answer = f"`{target.get('relative_path')}`는 본문을 읽을 수 없습니다(바이너리이거나 접근 불가)."
            return
        query = " ".join(analysis.terms[:6]) or self.message
        retrieved = self._retrieve(query, file_ids=[document["file_id"]], top_k=4)
        headings = _headings(document["text"])
        bullets = _score_sentences(document["text"], analysis.terms, limit=6) or _sentences(document["text"])[:4]
        lines = [f"`{document['path']}` 요약입니다. (revision {document['revision']})", ""]
        if headings:
            lines.append("**문서 구조**: " + " › ".join(headings[:8]))
            lines.append("")
        if bullets:
            lines.append("**핵심 내용**")
            for sentence in bullets:
                lines.append(f"- {sentence}")
            lines.append("")
        if retrieved.get("degraded"):
            lines.append(f"_인덱스 상태로 인해 검색은 {retrieved.get('mode_effective')} 모드로 동작했습니다._")
        if document["truncated"]:
            lines.append("_문서가 길어 앞부분만 읽었습니다._")
        self.answer = "\n".join(lines)

    def _run_compare(self) -> None:
        analysis = self.analysis or analyse_intent(self.message)
        candidates = self._candidates_for(analysis, wanted=2)
        if len(candidates) < 2:
            self.status = "needs_input"
            self.needs_input = "비교할 문서가 2개 이상 필요합니다."
            self.answer = "비교할 문서를 2개 이상 특정하지 못했습니다. 파일 이름을 지정해 주세요."
            return
        docs = [doc for doc in (self._read(item, max_tokens=1200) for item in candidates[:2]) if doc]
        if len(docs) < 2:
            self.status = "partial"
            self.answer = "두 문서 중 하나의 본문을 읽지 못해 비교를 완료하지 못했습니다."
            return
        first, second = docs
        self._retrieve(" ".join(analysis.terms[:6]) or self.message, file_ids=[first["file_id"], second["file_id"]], top_k=6)
        terms = analysis.terms or _terms(first["text"] + " " + second["text"])
        first_keys = _keyword_set(first["text"], terms)
        second_keys = _keyword_set(second["text"], terms)
        lines = [f"`{first['path']}` ↔ `{second['path']}` 비교", ""]
        lines.append("| 항목 | A | B |")
        lines.append("| --- | --- | --- |")
        lines.append(f"| 경로 | `{first['path']}` | `{second['path']}` |")
        lines.append(f"| 리비전 | {first['revision']} | {second['revision']} |")
        lines.append(f"| 분량 | {len(first['text'])}자 | {len(second['text'])}자 |")
        lines.append(f"| 섹션 수 | {len(_headings(first['text']))} | {len(_headings(second['text']))} |")
        lines.append("")
        lines.append("**공통 주제**: " + (", ".join(sorted(set(first_keys) & set(second_keys))) or "공통 키워드 없음"))
        lines.append("")
        lines.append("**A에만 있는 주제**: " + (", ".join(sorted(set(first_keys) - set(second_keys))) or "없음"))
        lines.append("")
        lines.append("**B에만 있는 주제**: " + (", ".join(sorted(set(second_keys) - set(first_keys))) or "없음"))
        first_sentences = _score_sentences(first["text"], terms, limit=3)
        second_sentences = _score_sentences(second["text"], terms, limit=3)
        if first_sentences or second_sentences:
            lines.append("")
            lines.append("**A 핵심**")
            lines.extend(f"- {sentence}" for sentence in first_sentences)
            lines.append("")
            lines.append("**B 핵심**")
            lines.extend(f"- {sentence}" for sentence in second_sentences)
        self.answer = "\n".join(lines)

    def _analysis_evidence(self, analysis: IntentAnalysis) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Evidence for a written report: retrieved chunks first, direct reads as fallback."""

        retrieved = self._retrieve(analysis.query or self.message, top_k=6)
        docs: List[Dict[str, Any]] = []
        seen: set = set()
        for item in retrieved.get("retrieved") or []:
            file_id = item.get("file_id")
            if not file_id or file_id in seen:
                continue
            seen.add(file_id)
            if len(docs) >= self.adapter.MAX_READ_FILES:
                break
            try:
                identity = self.manager.get_file_metadata(self.workspace_id, file_id, self.scope)
            except WorkspaceFailure:
                continue
            document = self._read(_entry_of(identity), max_tokens=900)
            if document is not None:
                docs.append(document)
        if not docs:
            files = self.inspected["files"]
            ranked = _rank_files(files, analysis.terms)
            for entry in ranked[: self.adapter.MAX_READ_FILES]:
                document = self._read(entry, max_tokens=900)
                if document is not None:
                    docs.append(document)
        return docs, retrieved

    def _run_create(self) -> None:
        analysis = self.analysis or analyse_intent(self.message)
        target_path = analysis.target_path
        if not target_path:
            self.status = "needs_input"
            self.answer = "저장할 파일 경로를 지정해 주세요. 예: `reports/summary.md`"
            self.needs_input = "target_path"
            return
        existing = next((item for item in self.inspected["files"] if item.get("relative_path") == target_path), None)
        if existing is None:
            try:
                existing_identity = resolve_file(self.manager, self.workspace_id, self.scope, path=target_path)
            except WorkspaceFailure:
                existing_identity = None
            if existing_identity is not None:
                existing = _entry_of(existing_identity)
        approved = self._approved("workspace_create_file", target_path)
        if existing is not None and not approved:
            self.pending_approval = {
                "action": "overwrite",
                "tool": "workspace_create_file",
                "path": target_path,
                "file_id": existing.get("file_id"),
                "current_revision": existing.get("revision"),
                "required": ["overwrite", "confirm_overwrite"],
                "approve_with": f"workspace_create_file:{target_path}",
            }
            raise _ApprovalStop("workspace_create_file")
        if existing is not None:
            try:
                self._read(existing, max_tokens=600)
            except AgentStepError:
                pass
        docs, retrieved = self._analysis_evidence(analysis)
        content = self._compose_report(analysis, docs, retrieved)
        arguments: Dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "path": target_path,
            "content": content,
            "create_parents": True,
        }
        if existing is not None:
            arguments["overwrite"] = True
            arguments["confirm_overwrite"] = True
        result = self._call("workspace_create_file", arguments, f"파일 생성: {target_path}")
        file_info = result.get("file") or {}
        action = AgentFileAction(
            action="patched" if existing is not None else "created",
            file_id=file_info.get("file_id") or (existing.get("file_id") if existing else ""),
            path=target_path,
            name=target_path.rsplit("/", 1)[-1],
            revision=int(result.get("revision") or 0),
            previous_revision=existing.get("revision") if existing else None,
            content_hash=result.get("content_hash"),
        )
        self._add_file(action)
        verification = self._verify_write(target_path, expected_hash=result.get("content_hash"))
        lines = [
            f"분석 결과를 `{target_path}`에 저장했습니다. (revision {action.revision})",
            "",
            f"- 근거 문서: {len(docs)}건" + (f" · 인용 {len(self.citations)}건" if self.citations else ""),
            f"- 검증: 본문 재읽기 {'성공' if verification.get('write_verified') else '실패'}, "
            f"해시 {'일치' if verification.get('hash_matches_intent') else '불일치'}",
        ]
        if docs:
            lines.append("- 참조: " + ", ".join(f"`{doc['path']}`" for doc in docs))
        lines.append("")
        lines.append("생성한 파일은 채팅의 파일 카드에서 열거나 Canvas로 보낼 수 있습니다.")
        self.answer = "\n".join(lines)

    def _compose_report(self, analysis: IntentAnalysis, docs: Sequence[Dict[str, Any]], retrieved: Dict[str, Any]) -> str:
        title = _report_title(self.message)
        lines = [f"# {title}", "", f"> 생성 시각: {_relative_utc()} · 요청: {self.message.strip()[:200]}", ""]
        lines.append("## 개요")
        lines.append("")
        lines.append(
            f"워크스페이스 문서 {len(docs)}건을 읽고 요청과 관련된 내용을 정리했습니다. "
            f"인용 가능한 검색 결과 {len(self.citations)}건을 함께 확인했습니다."
        )
        lines.append("")
        lines.append("## 주요 내용")
        lines.append("")
        for document in docs:
            lines.append(f"### {document['path']}")
            bullets = _score_sentences(document["text"], analysis.terms, limit=4)
            if not bullets:
                bullets = _sentences(document["text"])[:3]
            if bullets:
                lines.extend(f"- {sentence}" for sentence in bullets)
            else:
                lines.append("- 요약할 수 있는 문장이 없습니다.")
            lines.append("")
        lines.append("## 출처")
        lines.append("")
        for document in docs:
            lines.append(f"- `{document['path']}` · revision {document['revision']}")
        for citation in list(self.citations.values())[:6]:
            lines.append(
                f"- {citation['citation_id']} · `{citation.get('file_name')}`"
                + (f" · {citation.get('section')}" if citation.get("section") else "")
            )
        lines.append("")
        lines.append("## 결론")
        lines.append("")
        conclusions = []
        for document in docs[:2]:
            conclusions.extend(_score_sentences(document["text"], analysis.terms, limit=2))
        if conclusions:
            for sentence in conclusions[:3]:
                lines.append(f"- {sentence}")
        else:
            lines.append("- 문서에서 결론으로 정리할 근거 문장을 찾지 못했습니다.")
        lines.append("")
        lines.append("## 비고")
        lines.append("")
        lines.append(f"- {MOCK_LLM_NOTICE}")
        if retrieved.get("degraded"):
            lines.append(f"- 검색 모드: {retrieved.get('mode_effective')} (degraded)")
        lines.append("")
        return "\n".join(lines)

    def _last_file(self, analysis: IntentAnalysis) -> Optional[Dict[str, Any]]:
        explicit = self._resolve_explicit(analysis.targets)
        if explicit:
            return explicit[0]
        state = self.adapter.conversation_state(self.conversation_id)
        for item in state.get("recent_files") or []:
            if item.get("action") in {"created", "patched"}:
                try:
                    identity = resolve_file(self.manager, self.workspace_id, self.scope, path=item["path"])
                except WorkspaceFailure:
                    continue
                return _entry_of(identity)
        for item in state.get("recent_files") or []:
            try:
                identity = resolve_file(self.manager, self.workspace_id, self.scope, path=item["path"])
            except WorkspaceFailure:
                continue
            return _entry_of(identity)
        return None

    def _conclusion_content(self, document: Dict[str, Any], analysis: IntentAnalysis) -> str:
        points = _score_sentences(document["text"], analysis.terms, limit=4) or _sentences(document["text"])[:3]
        lines: List[str] = []
        if points:
            for point in points:
                lines.append(f"- {point}")
        else:
            lines.append("- 문서에서 결론으로 정리할 문장을 찾지 못했습니다.")
        lines.append("")
        lines.append(f"_요청({analysis.query[:80]})에 맞춰 결론 섹션만 갱신했습니다._")
        return "\n".join(lines)

    def _run_patch(self) -> None:
        analysis = self.analysis or analyse_intent(self.message)
        target = self._last_file(analysis)
        if target is None:
            self.status = "needs_input"
            self.needs_input = "target_file"
            self.answer = "수정할 파일을 찾지 못했습니다. 파일 경로를 지정해 주세요."
            return
        document = self._read(target, max_tokens=2400)
        if document is None:
            self.status = "partial"
            self.answer = f"`{target.get('relative_path')}` 본문을 읽지 못해 수정할 수 없습니다."
            return
        section = analysis.section
        edit_pairs = _edit_pairs(self.message)
        arguments: Dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "file_id": document["file_id"],
            "expected_revision": document["revision"],
        }
        if section:
            arguments["section"] = section
            arguments["content"] = self._conclusion_content(document, analysis)
        elif edit_pairs:
            arguments["edits"] = [{"find": find, "replace": replace} for find, replace in edit_pairs]
        else:
            self.status = "needs_input"
            self.needs_input = "patch_scope"
            self.answer = (
                f"`{document['path']}`에서 수정할 범위를 특정하지 못했습니다. "
                "섹션 이름(예: 결론)이나 바꿀 문장을 지정해 주세요."
            )
            return
        try:
            result = self._call(
                "workspace_patch_file",
                arguments,
                f"{document['path']} 부분 수정" + (f" · {section} 섹션" if section else ""),
            )
        except AgentStepError as error:
            if error.error.get("code") == WorkspaceErrorCode.SECTION_NOT_FOUND.value:
                self.status = "needs_input"
                self.needs_input = "section"
                self.answer = f"`{document['path']}`에 '{section}' 섹션이 없습니다. 섹션을 추가할지 알려 주세요."
                return
            raise
        action = AgentFileAction(
            action="patched",
            file_id=document["file_id"],
            path=document["path"],
            name=document["name"],
            revision=int(result.get("revision") or document["revision"]),
            previous_revision=int(result.get("previous_revision") or document["revision"]),
            diff=result.get("diff"),
            content_hash=result.get("content_hash"),
        )
        self._add_file(action)
        verification = self._verify_write(document["path"], expected_hash=result.get("content_hash"), section=section)
        lines = [
            f"`{document['path']}`의 " + (f"'{section}' 섹션" if section else "지정 구간") + "만 수정했습니다.",
            "",
            f"- revision {action.previous_revision} → {action.revision}",
            f"- 교체 {result.get('replacements', 0)}건 · 해시 {'일치' if verification.get('hash_matches_intent') else '불일치'}",
        ]
        if section:
            lines.append(f"- 섹션 확인: {'있음' if verification.get('section_present') else '확인 실패'}")
        if result.get("diff"):
            lines.append("")
            lines.append("```diff")
            lines.append(str(result["diff"])[:2000])
            lines.append("```")
        self.answer = "\n".join(lines)

    def _run_delete(self) -> None:
        analysis = self.analysis or analyse_intent(self.message)
        target = self._last_file(analysis)
        if target is None:
            self.status = "needs_input"
            self.needs_input = "target_file"
            self.answer = "삭제할 파일을 찾지 못했습니다. 파일 경로를 지정해 주세요."
            return
        path = target.get("relative_path") or ""
        approved = self._approved("workspace_delete_file", path)
        arguments = {
            "workspace_id": self.workspace_id,
            "file_id": target["file_id"],
            "confirm": approved,
        }
        result = self._call(
            "workspace_delete_file",
            arguments,
            f"파일 삭제: {path}" + (" (승인됨)" if approved else " (승인 대기)"),
        )
        self._add_file(
            AgentFileAction(
                action="deleted",
                file_id=target["file_id"],
                path=path,
                name=target.get("name", ""),
                revision=int(target.get("revision") or 0),
            )
        )
        self.verification = {
            "deleted": bool(result.get("deleted")),
            "removed_files": result.get("removed_files"),
            "removed_source_files": result.get("removed_source_files"),
        }
        self.answer = f"`{path}`를 삭제했습니다. (파일 {result.get('removed_files', 0)}건)"

    def _run_open_canvas(self) -> None:
        analysis = self.analysis or analyse_intent(self.message)
        target = self._last_file(analysis)
        if target is None:
            self.status = "needs_input"
            self.needs_input = "target_file"
            self.answer = "Canvas에서 열 파일을 찾지 못했습니다. 파일 이름을 지정해 주세요."
            return
        info = self._call(
            "workspace_get_file_info",
            {"workspace_id": self.workspace_id, "file_id": target["file_id"]},
            f"{target.get('relative_path')} 정보 확인",
        )
        file_info = info.get("file") or target
        artifact_id = f"canvas:{target['file_id']}"
        reference = self.manager.register_artifact_reference(
            self.workspace_id,
            self.scope,
            ArtifactReferenceInput(
                artifact_id=artifact_id,
                kind="canvas",
                file_id=target["file_id"],
                metadata={
                    "path": file_info.get("relative_path"),
                    "revision": file_info.get("revision"),
                    "opened_by": "workspace_agent",
                    "conversation_id": self.conversation_id,
                    "content_origin": file_info.get("content_origin"),
                },
            ),
        )
        canvas = {
            "action": "open",
            "artifact_id": reference.artifact_id,
            "artifact_ref_id": reference.artifact_ref_id,
            "workspace_id": self.workspace_id,
            "file_id": target["file_id"],
            "path": file_info.get("relative_path"),
            "revision": file_info.get("revision"),
            "content_hash": file_info.get("content_hash"),
            "content_origin": file_info.get("content_origin"),
            "storage_provider": file_info.get("storage_provider"),
            # Canvas contract (WO-C18): the canvas side opens this as
            # POST /artifacts/from-workspace and keeps `content_hash` as last_saved_content_hash.
            "canvas_handoff": {
                "from_workspace": {
                    "workspace_id": self.workspace_id,
                    "file_id": target["file_id"],
                    "revision": file_info.get("revision"),
                    "content_hash": file_info.get("content_hash"),
                },
                "save_to_workspace": "POST /artifacts/{artifact_id}/save-to-workspace",
                "workspace_link": "GET /artifacts/{artifact_id}/workspace-link",
                "conflict_field": "last_saved_content_hash",
            },
        }
        self._add_file(
            AgentFileAction(
                action="opened",
                file_id=target["file_id"],
                path=file_info.get("relative_path") or "",
                name=file_info.get("name", ""),
                revision=int(file_info.get("revision") or 0),
                canvas=canvas,
            )
        )
        self.verification = {
            "canvas_reference_registered": True,
            "artifact_id": reference.artifact_id,
            "file_revision": file_info.get("revision"),
        }
        self.answer = (
            f"`{file_info.get('relative_path')}`(revision {file_info.get('revision')})를 Canvas 열기 대상으로 등록했습니다.\n\n"
            "- artifact reference가 워크스페이스에 기록되었습니다.\n"
            "- 채팅의 파일 카드에서 **Canvas에서 열기**를 누르면 미리보기/연결된 Canvas로 이동합니다."
        )

    def _run_answer(self) -> None:
        analysis = self.analysis or analyse_intent(self.message)
        retrieved = self._retrieve(analysis.query or self.message, top_k=6)
        results = retrieved.get("retrieved") or []
        if results:
            lines = ["요청과 관련된 문서 내용입니다.", ""]
            for index, item in enumerate(results[:4], start=1):
                citation = next(
                    (c for c in self.citations.values() if c.get("chunk_id") == item.get("chunk_id")),
                    None,
                )
                label = citation["citation_id"] if citation else f"result-{index}"
                location = f" · {item.get('section')}" if item.get("section") else ""
                page = f" · p.{item.get('page_number')}" if item.get("page_number") else ""
                lines.append(f"{index}. [{label}] `{item.get('file_name')}`{page}{location}")
                lines.append(f"   > {_clean_excerpt(item.get('excerpt') or '', 320)}")
                lines.append("")
            lines.append("더 깊게 보려면 파일 이름을 지정해 달라고 하면 직접 읽어서 정리합니다.")
            self.answer = "\n".join(lines)
            return
        files = _rank_files(self.inspected["files"], analysis.terms)
        if not files:
            self.status = "needs_input"
            self.answer = "인덱싱된 문서가 없어 근거 답변을 만들지 못했습니다. 파일을 올리거나 이름을 지정해 주세요."
            self.needs_input = "no_documents"
            return
        self.answer = (
            "인덱스 검색 결과가 없습니다. 파일 이름을 지정하면 직접 읽어서 답하겠습니다.\n\n"
            "**후보 파일**: " + ", ".join(f"`{item.get('relative_path')}`" for item in files[:5])
        )


class _ApprovalStop(RuntimeError):
    pass


# --------------------------------------------------------------------------- helpers
def _availability(adapter: WorkspaceAgentAdapter, workspace_id: str) -> Dict[str, Any]:
    from app.workspace.chat import retrieval_availability

    return retrieval_availability(adapter.manager, adapter.retriever, workspace_id).model_dump(mode="json")


def _entry_of(identity: Any) -> Dict[str, Any]:
    return {
        "file_id": identity.file_id,
        "kind": identity.kind.value if hasattr(identity.kind, "value") else str(identity.kind),
        "name": identity.name,
        "path": identity.path,
        "relative_path": relative_path_of(identity),
        "size_bytes": identity.size_bytes,
        "revision": identity.revision,
        "content_hash": identity.content_hash,
        "index_status": identity.index_status.value if identity.index_status else None,
        "updated_at": identity.updated_at,
    }


def _rank_files(files: Sequence[Dict[str, Any]], terms: Sequence[str]) -> List[Dict[str, Any]]:
    def score(item: Dict[str, Any]) -> Tuple[int, str]:
        name = (item.get("name") or "").lower()
        rel = (item.get("relative_path") or "").lower()
        hits = sum(2 for term in terms if term in name) + sum(1 for term in terms if term in rel)
        return (-hits, rel)

    return sorted(files, key=score)


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def _report_title(message: str) -> str:
    cleaned = re.sub(rf"[A-Za-z0-9_./가-힣-]*\.{_EXT}", " ", message)
    cleaned = re.sub(r"(저장|작성|생성|해줘|해주세요|해라|결과를|결과|분석|을|를|에|로)", " ", cleaned)
    cleaned = re.sub(r"\S*/\S*", " ", cleaned)
    cleaned = re.sub(r"[`\"'“”‘’]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .:-/")
    return cleaned[:60] or "워크스페이스 분석 보고서"


def _edit_pairs(message: str) -> List[Tuple[str, str]]:
    """Parsed `A` -> `B` / "A"를 "B"로 pairs from the request."""

    patterns = [
        re.compile(r"[`\"'“”‘’]([^`\"'“”‘’]{1,200})[`\"'“”‘’]\s*(?:을|를|→|->|에서|to)\s*[`\"'“”‘’]([^`\"'“”‘’]{1,200})[`\"'“”‘’]"),
    ]
    pairs: List[Tuple[str, str]] = []
    for pattern in patterns:
        for find, replace in pattern.findall(message):
            if find.strip() and find.strip() != replace.strip():
                pairs.append((find.strip(), replace.strip()))
    return pairs


def _default_summary(tool: str, result: Dict[str, Any]) -> str:
    if tool == "workspace_list_files":
        return f"항목 {result.get('total', 0)}개"
    if tool == "workspace_find_files":
        return f"일치 {result.get('total', 0)}건"
    if tool == "workspace_read_file":
        return f"revision {result.get('revision')} · {result.get('total_chars', len(result.get('text') or ''))}자"
    if tool == "workspace_search_text":
        return f"일치 {result.get('total_matches', 0)}건"
    if tool == "workspace_retrieve":
        return f"결과 {len(result.get('retrieved') or [])}건 · 인용 {len(result.get('citations') or [])}건"
    if tool == "workspace_create_file":
        return f"revision {result.get('revision')}"
    if tool == "workspace_patch_file":
        return f"revision {result.get('previous_revision')} → {result.get('revision')}"
    if tool == "workspace_get_file_info":
        file_info = result.get("file") or {}
        return f"revision {file_info.get('revision')} · 청크 {result.get('chunks', 0)}"
    return ""


def assert_manifest_is_agent_only() -> None:
    """Guard test helper: no terminal/OS capability is exposed as a workspace tool."""

    names = {tool["name"] for tool in TOOL_MANIFEST["tools"]}
    forbidden = {"shell", "exec", "terminal", "os_command", "run_command", "workspace_run_command"}
    assert not (names & forbidden), names & forbidden
    assert set(READ_TOOLS + WRITE_TOOLS) == names
