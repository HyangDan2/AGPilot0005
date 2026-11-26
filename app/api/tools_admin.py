"""ADR-0017: Dynamic Tool Providers, Tool Profiles, and Service Binding Admin API."""
from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StrictBool

from app.tools.policy import VALID_ID, tool_policy, validate_provider_url
from app.tools.registry import registry

router = APIRouter(prefix="/config/tools", tags=["Tools Policy"])


class ProviderUpsert(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    kind: str = Field("http")
    enabled: StrictBool = True
    base_url: Optional[str] = None
    manifest_path: Optional[str] = "/api/v1/tools/manifest"
    auth_preset: str = Field("none")


class ProfileUpsert(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=300)
    tools: List[str] = Field(default_factory=list)
    max_tool_calls: int = Field(16, ge=1, le=32)


class ServiceBindingUpsert(BaseModel):
    profile_id: str = Field(..., min_length=1, max_length=64)


def _snapshot():
    policy = tool_policy.load()
    catalog = [
        {
            "name": reg.name,
            "description": reg.description,
            "is_read_only": reg.is_read_only,
        }
        for reg in registry._tools.values()
    ]
    bindings = policy.get("service_bindings", {})
    profile_usage = {}
    for svc, pid in bindings.items():
        profile_usage.setdefault(pid, []).append(svc)
    return {
        "schema": policy.get("schema", 1),
        "providers": policy.get("providers", {}),
        "profiles": policy.get("profiles", {}),
        "service_bindings": bindings,
        "profile_usage": profile_usage,
        "catalog": catalog,
    }


@router.get("/snapshot")
async def get_tools_snapshot():
    return _snapshot()


@router.post("/sync")
async def sync_all_providers(provider_id: Optional[str] = None):
    if provider_id:
        try:
            await tool_policy.sync_provider(provider_id, registry)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
    else:
        await tool_policy.sync_all(registry)
    return _snapshot()


@router.put("/providers/{provider_id}")
async def upsert_provider(provider_id: str, body: ProviderUpsert):
    if not VALID_ID.fullmatch(provider_id):
        raise HTTPException(422, "유효하지 않은 공급자 ID입니다.")
    policy = tool_policy.load()
    existing = policy.get("providers", {}).get(provider_id, {})
    kind = existing.get("kind", body.kind)
    base_url = None
    if kind == "http":
        if not body.base_url:
            raise HTTPException(422, "HTTP 공급자는 base_url이 필요합니다.")
        try:
            base_url = validate_provider_url(body.base_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if body.auth_preset not in ("none", "agtools_service_token"):
        raise HTTPException(422, "허용되지 않은 auth_preset입니다.")
    policy.setdefault("providers", {})[provider_id] = {
        **existing,
        "id": provider_id,
        "label": body.label,
        "kind": kind,
        "enabled": bool(body.enabled),
        "base_url": base_url,
        "manifest_path": body.manifest_path if kind == "http" else None,
        "auth_preset": body.auth_preset if kind == "http" else "none",
    }
    tool_policy.save(policy)
    await tool_policy.sync_provider(provider_id, registry)
    return _snapshot()


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(provider_id: str):
    if provider_id == "builtin":
        raise HTTPException(409, "내장 공급자(builtin)는 삭제할 수 없습니다.")
    policy = tool_policy.load()
    prov = policy.get("providers", {}).get(provider_id)
    if not prov:
        raise HTTPException(404, "공급자를 찾을 수 없습니다.")
    prov_tools = {t.get("name") for t in prov.get("tools") or []}
    refs = [pid for pid, pf in policy.get("profiles", {}).items() if prov_tools & set(pf.get("tools") or [])]
    if refs:
        raise HTTPException(409, f"프로파일에서 참조 중인 공급자입니다: {', '.join(refs)}")
    for t in prov.get("tools") or []:
        if t.get("kind") == "http":
            registry.unregister(t["name"])
    del policy["providers"][provider_id]
    tool_policy.save(policy)


@router.put("/profiles/{profile_id}")
async def upsert_profile(profile_id: str, body: ProfileUpsert):
    if not VALID_ID.fullmatch(profile_id):
        raise HTTPException(422, "유효하지 않은 프로파일 ID입니다.")
    policy = tool_policy.load()
    policy.setdefault("profiles", {})[profile_id] = {
        "id": profile_id,
        "name": body.name,
        "description": body.description,
        "tools": sorted(set(body.tools)),
        "max_tool_calls": body.max_tool_calls,
    }
    tool_policy.save(policy)
    return _snapshot()


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: str):
    policy = tool_policy.load()
    if profile_id not in policy.get("profiles", {}):
        raise HTTPException(404, "프로파일을 찾을 수 없습니다.")
    used_by = [svc for svc, pid in policy.get("service_bindings", {}).items() if pid == profile_id]
    if used_by:
        raise HTTPException(409, f"서비스에서 사용 중인 프로파일은 삭제할 수 없습니다: {', '.join(used_by)}")
    del policy["profiles"][profile_id]
    tool_policy.save(policy)


@router.put("/services/{service_id}")
async def bind_service(service_id: str, body: ServiceBindingUpsert):
    if not VALID_ID.fullmatch(service_id):
        raise HTTPException(422, "유효하지 않은 서비스 ID입니다.")
    policy = tool_policy.load()
    if body.profile_id not in policy.get("profiles", {}):
        raise HTTPException(404, f"프로파일 '{body.profile_id}'을(를) 찾을 수 없습니다.")
    policy.setdefault("service_bindings", {})[service_id] = body.profile_id
    tool_policy.save(policy)
    return _snapshot()


@router.delete("/services/{service_id}", status_code=204)
async def unbind_service(service_id: str):
    policy = tool_policy.load()
    if service_id not in policy.get("service_bindings", {}):
        raise HTTPException(404, "연결된 서비스를 찾을 수 없습니다.")
    del policy["service_bindings"][service_id]
    tool_policy.save(policy)
