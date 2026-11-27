"""ADR-0017: Dynamic Tool Provider Sync, Tool Profiles, and Service Bindings."""
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from app.config import settings

POLICY_PATH = Path(os.environ.get("AGCORE_TOOL_POLICY_FILE", "/var/lib/hd2lab-agcore/tool_policy.json"))
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
VALID_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

BUILTIN_TOOLS = [
    "system.echo",
    "system.get_datetime",
    "math.calculate",
    "document.get_outline",
    "document.read_section",
    "document.search",
    "document.read_chunk",
    "document.get_figure_info",
    # R2 Canvas artifact tools (registered additively; add to a profile to expose them)
    "canvas.create_artifact",
    "canvas.read_artifact",
    "canvas.list_artifacts",
    "canvas.propose_edit",
    "canvas.apply_edit",
    "canvas.list_revisions",
    "canvas.restore_revision",
    "canvas.export_artifact",
    # WO-C18 workspace-aware tools (their own opt-in switch: AGCORE_CANVAS_WORKSPACE_TOOLS_ENABLED)
    "canvas.save_to_workspace",
    "canvas.load_from_workspace",
]

DEFAULT_POLICY: Dict[str, Any] = {
    "schema": 1,
    "providers": {
        "builtin": {
            "id": "builtin",
            "label": "AGCore 내장 도구",
            "kind": "builtin",
            "enabled": True,
            "base_url": None,
            "manifest_path": None,
            "auth_preset": "none",
            "last_sync_at": None,
            "last_sync_ok": True,
            "last_error": None,
            "tools": [],
        },
        "agtools": {
            "id": "agtools",
            "label": "AGTools (문서 · 웹 검색)",
            "kind": "http",
            "enabled": True,
            "base_url": "http://127.0.0.1:8006",
            "manifest_path": "/api/v1/tools/manifest",
            "auth_preset": "agtools_service_token",
            "last_sync_at": None,
            "last_sync_ok": None,
            "last_error": None,
            "tools": [],
        },
    },
    "profiles": {
        "admin-full": {
            "id": "admin-full",
            "name": "관리자 전체 (문서 + 웹 검색)",
            "description": "관리자·베타용 전체 도구 프로파일",
            "tools": [
                "document.get_outline",
                "document.read_section",
                "document.search",
                "document.read_chunk",
                "document.get_figure_info",
                "web.search",
                "math.calculate",
                "system.get_datetime",
            ],
            "max_tool_calls": 16,
        },
        "public-safe": {
            "id": "public-safe",
            "name": "공개 기본 (웹 검색 OFF)",
            "description": "공개 /HD2AI/용 분리 프로파일 (콘솔에서 권한 조정 가능)",
            "tools": [
                "document.get_outline",
                "document.read_section",
                "document.search",
                "document.read_chunk",
                "document.get_figure_info",
                "math.calculate",
                "system.get_datetime",
            ],
            "max_tool_calls": 10,
        },
        "chat-only": {
            "id": "chat-only",
            "name": "도구 없음 (순수 대화)",
            "description": "모든 도구를 비활성화하는 경량 프로파일",
            "tools": [],
            "max_tool_calls": 1,
        },
    },
    "service_bindings": {
        "hd2ai-admin": "admin-full",
        "hd2ai-beta": "admin-full",
        "hd2ai-public": "public-safe",
    },
}


def validate_provider_url(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("SSRF 방지 정책상 등록 가능한 호스트(127.0.0.1, localhost)만 허용됩니다.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("유효하지 않은 공급자 URL 형식입니다.")
    return base_url.strip().rstrip("/")


def auth_headers(auth_preset: str, service_id: Optional[str] = None, owner_id: Optional[str] = None) -> Dict[str, str]:
    if auth_preset == "agtools_service_token":
        from app.core.auth import current_execution_scope, is_agtools_token_valid

        if not settings.AGTOOLS_ENABLED:
            raise RuntimeError("agtools_disabled")
        if not is_agtools_token_valid(settings.AGTOOLS_TOKEN):
            raise RuntimeError("agtools_token_missing_or_invalid")
        scope = current_execution_scope.get()
        if scope is not None:
            resolved_tenant = scope.tenant_id
            resolved_owner = scope.owner_id
        else:
            resolved_tenant = "public_tenant" if service_id == "hd2ai-public" else settings.AGTOOLS_TENANT
            resolved_owner = owner_id or "agcore_sync"
        return {
            "X-Service-Token": settings.AGTOOLS_TOKEN.strip(),
            "X-Tenant-Id": resolved_tenant,
            "X-Owner-Id": resolved_owner,
            "X-Access-Scope": "private",
        }
    return {}


class ToolPolicyStore:
    def __init__(self, path: Path = POLICY_PATH):
        self.path = path
        self._cache: Optional[Dict[str, Any]] = None
        self._mtime: float = -1.0

    def load(self) -> Dict[str, Any]:
        if self.path.exists():
            mtime = self.path.stat().st_mtime
            if self._cache is not None and mtime == self._mtime:
                return self._cache
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "providers" in data and "profiles" in data:
                    self._cache = data
                    self._mtime = mtime
                    return data
            except Exception:
                pass
        data = json.loads(json.dumps(DEFAULT_POLICY, ensure_ascii=False))
        try:
            self.save(data)
        except OSError:
            self._cache = data
        return data

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False, mode="w", encoding="utf-8") as tf:
            json.dump(data, tf, ensure_ascii=False, indent=2)
            tf.flush()
            os.fsync(tf.fileno())
            tmp = Path(tf.name)
        os.chmod(tmp, 0o644)
        tmp.replace(self.path)
        self._cache = data
        self._mtime = self.path.stat().st_mtime
        return data

    def is_tool_allowed(self, tool_name: str, service_id: Optional[str]) -> Tuple[bool, Optional[str]]:
        policy = self.load()
        # Check provider-level enabled state for dynamic/builtin tools
        providers = policy.get("providers", {})
        if tool_name in BUILTIN_TOOLS:
            if not providers.get("builtin", {}).get("enabled", True):
                return False, "builtin_provider_disabled"
            # NG-01: Respect global AGTOOLS_ENABLED switch across all document.* tools
            if tool_name.startswith("document.") and not settings.AGTOOLS_ENABLED:
                return False, "agtools_disabled"
            # If document.* is globally disabled in agtools manifest (when synced), respect it
            agtools = providers.get("agtools", {})
            if tool_name.startswith("document.") and agtools.get("enabled") is False:
                return False, "agtools_provider_disabled"
            if tool_name.startswith("document.") and agtools.get("last_sync_ok") is True:
                synced_names = {t.get("name") for t in agtools.get("tools", [])}
                if tool_name in ("document.get_outline", "document.search", "document.read_chunk") and tool_name not in synced_names:
                    return False, "tool_globally_disabled"
        else:
            # CR-01.5: Only block tools belonging to agtools when AGTOOLS_ENABLED=False (never block independent providers)
            agtools_names = {t.get("name") for t in (providers.get("agtools", {}).get("tools") or [])}
            if not settings.AGTOOLS_ENABLED and tool_name in agtools_names:
                return False, "agtools_disabled"
        if not service_id:
            return True, None
        profile_id = policy.get("service_bindings", {}).get(service_id)
        if not profile_id:
            # NG-03 C: Unregistered service MUST fail-closed (no implicit public-safe fallback!)
            return False, "unregistered_service"
        profile = policy.get("profiles", {}).get(profile_id)
        if not profile:
            return False, "profile_not_found"
        allowed = set(profile.get("tools") or [])
        if tool_name not in allowed:
            return False, f"disallowed_by_profile:{profile_id}"
        return True, None

    def is_registered_service(self, service_id: Optional[str]) -> bool:
        """HF-01: Returns True iff service_id is explicitly registered in service_bindings."""
        if not service_id:
            return False
        policy = self.load()
        return bool((policy.get("service_bindings") or {}).get(service_id))

    def allowed_tools_for_service(self, service_id: Optional[str]) -> List[str]:
        policy = self.load()
        if not service_id:
            return []
        profile_id = policy.get("service_bindings", {}).get(service_id)
        if not profile_id:
            # HF-01: Never fall back to public-safe for unregistered services
            return []
        profile = policy.get("profiles", {}).get(profile_id, {})
        return list(profile.get("tools") or [])

    async def sync_provider(self, provider_id: str, tool_registry: Any) -> Dict[str, Any]:
        policy = self.load()
        prov = policy.get("providers", {}).get(provider_id)
        if not prov:
            raise KeyError(f"Unknown provider: {provider_id}")
        if prov.get("kind") != "http":
            prov["last_sync_at"] = time.time()
            prov["last_sync_ok"] = True
            prov["last_error"] = None
            self.save(policy)
            return prov

        # Remove previously synced dynamic http tools from this provider
        for prev in list(prov.get("tools") or []):
            if prev.get("kind") == "http":
                tool_registry.unregister(prev["name"])

        if provider_id == "agtools" and not settings.AGTOOLS_ENABLED:
            prov["tools"] = []
            prov["last_sync_at"] = time.time()
            prov["last_sync_ok"] = True
            prov["last_error"] = "agtools_disabled"
            try:
                self.save(policy)
            except OSError:
                pass
            return prov

        if not prov.get("enabled", True):
            prov["tools"] = []
            prov["last_sync_at"] = time.time()
            prov["last_sync_ok"] = True
            prov["last_error"] = "disabled"
            self.save(policy)
            return prov

        base_url = validate_provider_url(prov["base_url"])
        manifest_path = prov.get("manifest_path") or "/api/v1/tools/manifest"
        url = base_url + manifest_path
        headers = auth_headers(prov.get("auth_preset") or "none")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                manifest = resp.json()
            tools = manifest.get("tools") or []
            prov["tools"] = tools
            prov["last_sync_at"] = time.time()
            prov["last_sync_ok"] = True
            prov["last_error"] = None
            self._register_http_tools(prov, tool_registry)
            try:
                self.save(policy)
            except OSError:
                self._cache = policy
            return prov
        except Exception as exc:
            prov["last_sync_at"] = time.time()
            prov["last_sync_ok"] = False
            prov["last_error"] = str(exc)
            try:
                self.save(policy)
            except OSError:
                self._cache = policy
            return prov

    def _register_http_tools(self, prov: Dict[str, Any], tool_registry: Any) -> None:
        if prov.get("id") == "agtools" and not settings.AGTOOLS_ENABLED:
            return
        if not prov.get("enabled", True):
            return
        base_url = prov.get("base_url") or ""
        preset = prov.get("auth_preset") or "none"
        for tool in prov.get("tools") or []:
            if tool.get("kind") != "http" or not tool.get("enabled", True):
                continue
            ep = tool.get("endpoint") or {}
            method = str(ep.get("method") or "POST").upper()
            path = str(ep.get("path") or "")
            timeout = float(ep.get("timeout_seconds") or 15.0)
            target_url = base_url.rstrip("/") + path

            async def _make_handler(
                _method=method,
                _url=target_url,
                _preset=preset,
                _timeout=timeout,
                **kwargs,
            ):
                hdrs = auth_headers(_preset)
                async with httpx.AsyncClient(timeout=_timeout) as client:
                    if _method == "GET":
                        r = await client.get(_url, params=kwargs, headers=hdrs)
                    else:
                        r = await client.post(_url, json=kwargs, headers=hdrs)
                    if r.status_code >= 400:
                        return {"error": True, "code": f"http_{r.status_code}", "message": r.text[:300]}
                    return r.json()

            tool_registry.register(
                name=tool["name"],
                description=tool.get("description") or tool["name"],
                parameters=tool.get("parameters") or {"type": "object", "properties": {}},
                handler=_make_handler,
                is_read_only=bool(tool.get("is_read_only", True)),
            )

    async def sync_all(self, tool_registry: Any) -> Dict[str, Any]:
        policy = self.load()
        for pid in list(policy.get("providers", {}).keys()):
            await self.sync_provider(pid, tool_registry)
        return self.load()


tool_policy = ToolPolicyStore()
