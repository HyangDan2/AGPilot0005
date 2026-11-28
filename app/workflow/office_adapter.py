"""R2.1 / AGTools integration strategy — capability interface, Mock and Runtime adapters.

R3 workflows must be able to run acceptance scenarios long before the real AGTools
runtime is reachable.  The contract therefore has three parts:

* :class:`OfficeCapabilityAdapter` — the five capabilities R3 depends on:
  ``workspace.rag.search``, ``office.xlsx.analyze``, ``office.docx.compare``,
  ``office.pptx.generate``, ``workspace.file.write``.
* :class:`MockOfficeAdapter` — deterministic, in-memory implementation used by
  pre-development and Acceptance Scenario A.  It never touches the network.
* :class:`RuntimeOfficeAdapter` — HTTP client for the real AGTools/R2.1 endpoints.  It
  validates every response against the same contract and refuses to be used with a
  missing base URL, so the switch from mock to runtime is a configuration change only.

The adapters exchange plain dictionaries at the boundary (JSON-serialisable), which is
also what the MCP/AGTools cross-repository contract uses.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "OFFICE_CAPABILITIES",
    "OfficeAdapterError",
    "OfficeAdapterUnavailable",
    "OfficeHealth",
    "OfficeResult",
    "OfficeCapabilityAdapter",
    "MockOfficeAdapter",
    "RuntimeOfficeAdapter",
    "HttpTransport",
    "get_office_adapter",
    "assert_adapter_allowed",
]

#: Capability ids are the contract between the planner and the adapters.
OFFICE_CAPABILITIES: Tuple[str, ...] = (
    "workspace.rag.search",
    "office.xlsx.analyze",
    "office.docx.compare",
    "office.pptx.generate",
    "workspace.file.write",
)

_PRODUCTION_ENVS = frozenset({"production", "prod", "live"})
_SAFE_PATH = re.compile(r"^(?!/)(?!.*\.\.)[^\x00]{1,512}$")


class OfficeAdapterError(RuntimeError):
    """Contract or validation failure inside an office adapter."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


class OfficeAdapterUnavailable(OfficeAdapterError):
    """The runtime adapter cannot reach AGTools/R2.1."""


class OfficeHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter: str
    status: str
    capabilities: Tuple[str, ...] = ()
    detail: Optional[str] = None


class OfficeResult(BaseModel):
    """Uniform envelope for every capability call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: str
    ok: bool = True
    data: Dict[str, Any] = Field(default_factory=dict)
    adapter: str = "mock"
    untrusted: bool = False


class OfficeCapabilityAdapter(ABC):
    """The five-capability interface shared by Mock and Runtime adapters."""

    name: ClassVar[str] = "abstract"
    capabilities: ClassVar[Tuple[str, ...]] = OFFICE_CAPABILITIES
    is_mock: ClassVar[bool] = False

    # -- individual capabilities -------------------------------------------------------
    @abstractmethod
    async def workspace_rag_search(
        self, *, workspace_id: str, query: str, limit: int = 5, filters: Optional[Dict[str, Any]] = None
    ) -> OfficeResult: ...

    @abstractmethod
    async def xlsx_analyze(
        self, *, file_ref: str, operations: Sequence[str] = ("summary",), sheet: Optional[str] = None
    ) -> OfficeResult: ...

    @abstractmethod
    async def docx_compare(self, *, base_ref: str, target_ref: str, granularity: str = "paragraph") -> OfficeResult: ...

    @abstractmethod
    async def pptx_generate(
        self, *, title: str, slides: Sequence[Mapping[str, Any]], template_ref: Optional[str] = None
    ) -> OfficeResult: ...

    @abstractmethod
    async def workspace_file_write(
        self, *, workspace_id: str, path: str, content: str, overwrite: bool = False
    ) -> OfficeResult: ...

    # -- generic dispatch --------------------------------------------------------------
    async def invoke(self, capability: str, params: Mapping[str, Any]) -> OfficeResult:
        """Call a capability by id — the form the workflow planner uses."""
        if capability not in self.capabilities:
            raise OfficeAdapterError("unknown_capability", capability)
        payload = dict(params or {})
        try:
            if capability == "workspace.rag.search":
                return await self.workspace_rag_search(
                    workspace_id=payload.pop("workspace_id"),
                    query=payload.pop("query"),
                    **payload,
                )
            if capability == "office.xlsx.analyze":
                return await self.xlsx_analyze(**payload)
            if capability == "office.docx.compare":
                return await self.docx_compare(**payload)
            if capability == "office.pptx.generate":
                return await self.pptx_generate(**payload)
            if capability == "workspace.file.write":
                return await self.workspace_file_write(**payload)
        except KeyError as exc:
            raise OfficeAdapterError("missing_parameter", str(exc)) from exc
        except TypeError as exc:
            raise OfficeAdapterError("invalid_parameters", str(exc)) from exc
        raise OfficeAdapterError("unknown_capability", capability)

    async def health(self) -> OfficeHealth:
        return OfficeHealth(adapter=self.name, status="ok", capabilities=self.capabilities)


class MockOfficeAdapter(OfficeCapabilityAdapter):
    """Deterministic in-memory adapter for R3 pre-development and Scenario A.

    Every result is a pure function of the inputs (plus, for file writes, the current
    in-memory workspace), so assertions never depend on timing or hashing randomness.
    """

    name: ClassVar[str] = "mock"
    is_mock: ClassVar[bool] = True

    def __init__(self, *, documents: Optional[Mapping[str, str]] = None) -> None:
        self.files: Dict[str, str] = dict(documents or {})
        self.calls: List[Dict[str, Any]] = []
        self.side_effects: List[str] = []

    # -- helpers -----------------------------------------------------------------------
    def _record(self, capability: str, params: Mapping[str, Any]) -> None:
        self.calls.append({"capability": capability, "params": dict(params)})

    def _result(self, capability: str, data: Dict[str, Any]) -> OfficeResult:
        return OfficeResult(capability=capability, ok=True, data=data, adapter=self.name)

    @staticmethod
    def _tokens(text: str) -> List[str]:
        return [token for token in re.split(r"[^0-9a-zA-Z가-힣]+", (text or "").lower()) if token]

    # -- capabilities ------------------------------------------------------------------
    async def workspace_rag_search(
        self, *, workspace_id: str, query: str, limit: int = 5, filters: Optional[Dict[str, Any]] = None
    ) -> OfficeResult:
        self._record("workspace.rag.search", {"workspace_id": workspace_id, "query": query, "limit": limit})
        if not workspace_id:
            raise OfficeAdapterError("workspace_required")
        terms = self._tokens(query)
        hits: List[Dict[str, Any]] = []
        for path, content in sorted(self.files.items()):
            if filters and filters.get("path_prefix") and not path.startswith(str(filters["path_prefix"])):
                continue
            haystack = content.lower()
            matched = [term for term in terms if term in haystack]
            if not matched:
                continue
            overlap = len(set(matched)) / max(1, len(set(terms)))
            first = next((line for line in content.splitlines() if any(term in line.lower() for term in terms)), "")
            hits.append({
                "path": path,
                "chunk_id": f"{path}#0",
                "score": round(0.4 + 0.6 * overlap, 4),
                "snippet": first.strip()[:280],
                "matched_terms": sorted(set(matched)),
            })
        hits.sort(key=lambda hit: (-hit["score"], hit["path"]))
        capped = hits[: max(1, int(limit))]
        return self._result("workspace.rag.search", {
            "workspace_id": workspace_id,
            "query": query,
            "hits": capped,
            "hit_count": len(capped),
            "total_candidates": len(hits),
            "truncated": len(hits) > len(capped),
        })

    async def xlsx_analyze(
        self, *, file_ref: str, operations: Sequence[str] = ("summary",), sheet: Optional[str] = None
    ) -> OfficeResult:
        self._record("office.xlsx.analyze", {"file_ref": file_ref, "operations": list(operations), "sheet": sheet})
        rows = self._json_rows(file_ref)
        columns: List[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        stats: Dict[str, Dict[str, float]] = {}
        for column in columns:
            values = [row[column] for row in rows if isinstance(row.get(column), (int, float)) and not isinstance(row.get(column), bool)]
            if values:
                stats[column] = {
                    "count": len(values),
                    "sum": round(float(sum(values)), 6),
                    "avg": round(float(sum(values)) / len(values), 6),
                    "min": min(values),
                    "max": max(values),
                }
        return self._result("office.xlsx.analyze", {
            "file_ref": file_ref,
            "sheet": sheet or "Sheet1",
            "row_count": len(rows),
            "columns": columns,
            "operations": list(operations),
            "stats": stats,
            "empty": not rows,
        })

    def _json_rows(self, file_ref: str) -> List[Dict[str, Any]]:
        raw = self.files.get(file_ref)
        if raw is None:
            raise OfficeAdapterError("file_not_found", file_ref)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OfficeAdapterError("invalid_table_payload", file_ref) from exc
        if isinstance(parsed, dict):
            parsed = parsed.get("rows", [])
        if not isinstance(parsed, list) or not all(isinstance(row, dict) for row in parsed):
            raise OfficeAdapterError("invalid_table_payload", file_ref)
        return parsed

    async def docx_compare(self, *, base_ref: str, target_ref: str, granularity: str = "paragraph") -> OfficeResult:
        self._record("office.docx.compare", {"base_ref": base_ref, "target_ref": target_ref, "granularity": granularity})
        base = self._lines(base_ref)
        target = self._lines(target_ref)
        base_set, target_set = set(base), set(target)
        added = [line for line in target if line not in base_set]
        removed = [line for line in base if line not in target_set]
        return self._result("office.docx.compare", {
            "base_ref": base_ref,
            "target_ref": target_ref,
            "granularity": granularity,
            "added": added,
            "removed": removed,
            "added_count": len(added),
            "removed_count": len(removed),
            "changed": bool(added or removed),
            "similarity": round(len(base_set & target_set) / max(1, len(base_set | target_set)), 4),
        })

    def _lines(self, file_ref: str) -> List[str]:
        if file_ref not in self.files:
            raise OfficeAdapterError("file_not_found", file_ref)
        return [line.strip() for line in self.files[file_ref].splitlines() if line.strip()]

    async def pptx_generate(
        self, *, title: str, slides: Sequence[Mapping[str, Any]], template_ref: Optional[str] = None
    ) -> OfficeResult:
        self._record("office.pptx.generate", {"title": title, "slide_count": len(slides), "template_ref": template_ref})
        if not title:
            raise OfficeAdapterError("title_required")
        canonical = json.dumps(
            {"title": title, "slides": [dict(slide) for slide in slides], "template_ref": template_ref},
            sort_keys=True,
            ensure_ascii=False,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return self._result("office.pptx.generate", {
            "artifact_id": f"pptx_{digest}",
            "title": title,
            "slide_count": len(slides),
            "template_ref": template_ref,
            "checksum": f"sha256:{digest}",
            "estimated_bytes": 1024 + 512 * len(slides),
        })

    async def workspace_file_write(
        self, *, workspace_id: str, path: str, content: str, overwrite: bool = False
    ) -> OfficeResult:
        self._record("workspace.file.write", {"workspace_id": workspace_id, "path": path, "overwrite": overwrite})
        if not workspace_id:
            raise OfficeAdapterError("workspace_required")
        if not _SAFE_PATH.match(path or ""):
            raise OfficeAdapterError("path_escape", path)
        if path in self.files and not overwrite:
            raise OfficeAdapterError("file_exists", path)
        existed = path in self.files
        payload = content.encode("utf-8")
        self.files[path] = content
        self.side_effects.append(f"write:{workspace_id}:{path}")
        return self._result("workspace.file.write", {
            "workspace_id": workspace_id,
            "path": path,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "created": not existed,
            "overwritten": existed,
        })


HttpTransport = Callable[[str, str, Dict[str, str], Dict[str, Any], float], Awaitable[Tuple[int, Any]]]


class RuntimeOfficeAdapter(OfficeCapabilityAdapter):
    """HTTP adapter for the real AGTools (R2.1) runtime.

    Endpoint paths mirror the AGTools routes described by the WP-07 capability manifest
    (``app/r3_capabilities.py`` in AGPilot0006); confirm them against that manifest
    before enabling the adapter in a live environment.  The transport is injectable so
    contract tests can run without a server.
    """

    name: ClassVar[str] = "runtime"
    is_mock: ClassVar[bool] = False

    ENDPOINTS: ClassVar[Dict[str, str]] = {
        "workspace.rag.search": "/api/v1/search",
        "office.xlsx.analyze": "/api/v1/xlsx/analyze",
        "office.docx.compare": "/api/v1/docx/compare",
        "office.pptx.generate": "/api/v1/pptx/generate",
        "workspace.file.write": "/api/v1/workspace/files",
    }

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        transport: Optional[HttpTransport] = None,
        auth_header: Optional[Callable[[], Mapping[str, str]]] = None,
    ) -> None:
        base = (base_url or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise OfficeAdapterError("invalid_base_url")
        self.base_url = base
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = max(0, int(max_retries))
        self._transport = transport or self._http_transport
        self._auth_header = auth_header
        self.calls: List[Dict[str, Any]] = []

    # -- transport ---------------------------------------------------------------------
    @staticmethod
    async def _http_transport(
        method: str, url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: float
    ) -> Tuple[int, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, url, headers=headers, json=payload)
            try:
                body = response.json()
            except Exception:  # noqa: BLE001 - non-JSON error page
                body = {"raw_status": response.status_code}
            return response.status_code, body

    def _headers(self) -> Dict[str, str]:
        headers = {"content-type": "application/json", "accept": "application/json"}
        if self._auth_header is not None:
            headers.update({str(k): str(v) for k, v in (self._auth_header() or {}).items()})
        return headers

    async def _call(self, capability: str, payload: Mapping[str, Any]) -> OfficeResult:
        endpoint = self.ENDPOINTS.get(capability)
        if endpoint is None:
            raise OfficeAdapterError("unknown_capability", capability)
        url = f"{self.base_url}{endpoint}"
        body = {"capability": capability, "params": dict(payload)}
        self.calls.append({"capability": capability, "url": url})
        last_error_code = "unavailable"
        last_error_detail = ""
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                status, response = await asyncio.wait_for(
                    self._transport("POST", url, self._headers(), body, self.timeout_seconds),
                    timeout=self.timeout_seconds + 1,
                )
            except asyncio.TimeoutError:
                last_error_code, last_error_detail = "timeout", capability
            except Exception as exc:  # noqa: BLE001 - transport failures degrade, never crash the workflow
                last_error_code = "transport_error"
                last_error_detail = f"{capability} ({type(exc).__name__})"
            else:
                if status >= 500:
                    last_error_code, last_error_detail = "upstream_unavailable", f"{capability} status={status}"
                elif status >= 400:
                    raise OfficeAdapterError("upstream_rejected", f"{capability} status={status}")
                else:
                    return self._unwrap(capability, response)
            if attempt + 1 < attempts and self._retry_delay > 0:
                await asyncio.sleep(self._retry_delay)
        raise OfficeAdapterUnavailable(last_error_code, last_error_detail)

    _retry_delay: float = 0.0

    def _unwrap(self, capability: str, response: Any) -> OfficeResult:
        if not isinstance(response, Mapping):
            raise OfficeAdapterError("invalid_response", "not_an_object")
        if response.get("ok") is False:
            raise OfficeAdapterError("upstream_error", str(response.get("error") or "unknown"))
        echoed = response.get("capability")
        if echoed is not None and echoed != capability:
            raise OfficeAdapterError("capability_mismatch", f"expected={capability} got={echoed}")
        data = response.get("data", response)
        if not isinstance(data, Mapping):
            raise OfficeAdapterError("invalid_response", "data_not_an_object")
        return OfficeResult(capability=capability, ok=True, data=dict(data), adapter=self.name, untrusted=True)

    # -- capabilities ------------------------------------------------------------------
    async def workspace_rag_search(
        self, *, workspace_id: str, query: str, limit: int = 5, filters: Optional[Dict[str, Any]] = None
    ) -> OfficeResult:
        return await self._call("workspace.rag.search", {
            "workspace_id": workspace_id, "query": query, "limit": limit, "filters": filters or {},
        })

    async def xlsx_analyze(
        self, *, file_ref: str, operations: Sequence[str] = ("summary",), sheet: Optional[str] = None
    ) -> OfficeResult:
        return await self._call("office.xlsx.analyze", {
            "file_ref": file_ref, "operations": list(operations), "sheet": sheet,
        })

    async def docx_compare(self, *, base_ref: str, target_ref: str, granularity: str = "paragraph") -> OfficeResult:
        return await self._call("office.docx.compare", {
            "base_ref": base_ref, "target_ref": target_ref, "granularity": granularity,
        })

    async def pptx_generate(
        self, *, title: str, slides: Sequence[Mapping[str, Any]], template_ref: Optional[str] = None
    ) -> OfficeResult:
        return await self._call("office.pptx.generate", {
            "title": title, "slides": [dict(slide) for slide in slides], "template_ref": template_ref,
        })

    async def workspace_file_write(
        self, *, workspace_id: str, path: str, content: str, overwrite: bool = False
    ) -> OfficeResult:
        if not _SAFE_PATH.match(path or ""):
            raise OfficeAdapterError("path_escape", path)
        return await self._call("workspace.file.write", {
            "workspace_id": workspace_id, "path": path, "content": content, "overwrite": overwrite,
        })

    async def health(self) -> OfficeHealth:
        try:
            status, response = await asyncio.wait_for(
                self._transport("GET", f"{self.base_url}/health", self._headers(), {}, self.timeout_seconds),
                timeout=self.timeout_seconds + 1,
            )
        except Exception as exc:  # noqa: BLE001
            return OfficeHealth(adapter=self.name, status="degraded", capabilities=self.capabilities,
                                detail=type(exc).__name__)
        state = "ok" if status < 400 else "degraded"
        return OfficeHealth(adapter=self.name, status=state, capabilities=self.capabilities, detail=f"http_{status}")


def assert_adapter_allowed(adapter: OfficeCapabilityAdapter, *, environment: Optional[str]) -> None:
    """Production refuses the Mock adapter: real runtime only."""
    env = (environment or "").strip().lower()
    if adapter.is_mock and env in _PRODUCTION_ENVS:
        raise OfficeAdapterError("mock_adapter_forbidden_in_production")


def get_office_adapter(
    kind: str = "mock",
    *,
    environment: Optional[str] = None,
    documents: Optional[Mapping[str, str]] = None,
    base_url: Optional[str] = None,
    transport: Optional[HttpTransport] = None,
) -> OfficeCapabilityAdapter:
    """Factory used by the runtime wiring; mock is rejected in production."""
    normalized = (kind or "mock").strip().lower()
    if normalized == "mock":
        adapter: OfficeCapabilityAdapter = MockOfficeAdapter(documents=documents)
    elif normalized == "runtime":
        if not base_url:
            raise OfficeAdapterError("base_url_required")
        adapter = RuntimeOfficeAdapter(base_url, transport=transport)
    else:
        raise OfficeAdapterError("unknown_adapter", kind)
    assert_adapter_allowed(adapter, environment=environment)
    return adapter
