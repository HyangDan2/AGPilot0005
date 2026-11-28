"""AGCore Configuration and Environment Settings."""
import logging
import os
from pathlib import Path
from typing import Optional, Set
from pydantic import BaseModel

logger = logging.getLogger(__name__)
_DEPRECATED_ENV_WARNED: Set[str] = set()
_SUPPORTED_RUNTIME_ENGINES = {"", "langgraph_r1", "langgraph"}
# AM-06: Legacy-only tuning env vars (currently none exist in AGCore; hook retained for one-time deprecation warning)
_DEPRECATED_LEGACY_ENV_VARS: tuple[str, ...] = ()

def gateway_token() -> str:
    token = os.environ.get("AGCORE_GATEWAY_TOKEN", "")
    if token:
        return token
    credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if credentials_dir:
        token_file = Path(credentials_dir) / "agcore_gateway_token"
        if token_file.is_file():
            return token_file.read_text(encoding="utf-8").strip()
    return ""

class Settings(BaseModel):
    HOST: str = os.environ.get("AGCORE_HOST", "127.0.0.1")
    PORT: int = int(os.environ.get("AGCORE_PORT", "8200"))
    
    # AI Gateway Settings
    GATEWAY_URL: str = os.environ.get("AGCORE_GATEWAY_URL", "http://127.0.0.1:8080")
    GATEWAY_SOCKET: str = os.environ.get("AGCORE_GATEWAY_SOCKET", "/run/hd2lab-ai-gateway/api.sock")
    GATEWAY_ROUTE: str = os.environ.get("AGCORE_GATEWAY_ROUTE", "hd2ai-admin")
    GATEWAY_TOKEN: str = gateway_token()
    
    # Execution & Context Limits (from Master Work Order)
    MAX_TOOL_CALLS: int = int(os.environ.get("AGCORE_MAX_TOOL_CALLS", "16"))
    MAX_AGENT_STEPS: int = int(os.environ.get("AGCORE_MAX_AGENT_STEPS", "20"))
    CONTEXT_BUDGET_TOKENS: int = int(os.environ.get("AGCORE_CONTEXT_BUDGET_TOKENS", "12000"))
    MAX_TOOL_RESULT_TOKENS: int = int(os.environ.get("AGCORE_MAX_TOOL_RESULT_TOKENS", "2000"))
    TOOL_TIMEOUT_SECONDS: float = float(os.environ.get("AGCORE_TOOL_TIMEOUT_SECONDS", "30.0"))
    RUN_TIMEOUT_SECONDS: float = float(os.environ.get("AGCORE_RUN_TIMEOUT_SECONDS", "180.0"))
    
    # Service Auth Tokens, Credential Scopes & Trusted Proxy (NG-01 / NG-03 Fail-closed)
    SERVICE_TOKENS: str = os.environ.get("AGCORE_SERVICE_TOKENS", "")
    ADMIN_SERVICE_TOKENS: str = os.environ.get("AGCORE_ADMIN_SERVICE_TOKENS", "")
    CREDENTIAL_SCOPES_JSON: str = os.environ.get("AGCORE_CREDENTIAL_SCOPES", "")
    CREDENTIAL_SECRETS_JSON: str = os.environ.get("AGCORE_CREDENTIAL_SECRETS", "")
    TRUSTED_PROXY_IPS: str = os.environ.get("AGCORE_TRUSTED_PROXY_IPS", "127.0.0.1,::1")
    TRUSTED_PROXY_SECRET: str = os.environ.get("AGCORE_TRUSTED_PROXY_SECRET", "")
    CORS_ALLOW_ORIGINS: str = os.environ.get(
        "AGCORE_CORS_ALLOW_ORIGINS",
        "https://hd2lab.duckdns.org" if os.environ.get("AGCORE_ENV", "development").lower() == "production" else "*",
    )
    MAX_REQUEST_BODY_BYTES: int = int(os.environ.get("AGCORE_MAX_REQUEST_BODY_BYTES", str(2 * 1024 * 1024)))

    # Environment & Mock Policy (production blocks mock fallback fail-closed)
    ENVIRONMENT: str = os.environ.get("AGCORE_ENV", "development")
    ALLOW_MOCK_FALLBACK: bool = (
        False
        if os.environ.get("AGCORE_ENV", "development").lower() == "production"
        else os.environ.get("AGCORE_ALLOW_MOCK_FALLBACK", "true").lower() in ("true", "1", "yes")
    )

    # AGPilot0006 (Document Intelligence) Service Integration — NO hardcoded default secret (FIX-203)
    AGTOOLS_ENABLED: bool = os.environ.get("AGCORE_AGTOOLS_ENABLED", "true").lower() in ("true", "1", "yes")
    AGTOOLS_URL: str = os.environ.get("AGCORE_AGTOOLS_URL", "http://127.0.0.1:8006/api/v1")
    AGTOOLS_TENANT: str = os.environ.get("AGCORE_AGTOOLS_TENANT", "default_tenant")
    AGTOOLS_TOKEN: str = os.environ.get("AGCORE_AGTOOLS_TOKEN", "")
    AGTOOLS_TIMEOUT_SECONDS: float = float(os.environ.get("AGCORE_AGTOOLS_TIMEOUT_SECONDS", "10.0"))
    SQLITE_BUSY_TIMEOUT_MS: int = int(os.environ.get("AGCORE_SQLITE_BUSY_TIMEOUT_MS", "5000"))

    # Persistent, bounded full-document analysis (separate from general Agent limits).
    DURABLE_ANALYSIS_SERVICES: str = os.environ.get("AGCORE_DURABLE_ANALYSIS_SERVICES", "")
    ANALYSIS_DIR: str = os.environ.get("AGCORE_ANALYSIS_DIR", "/var/lib/hd2lab-agcore/analysis")
    ANALYSIS_MAX_CHUNKS: int = int(os.environ.get("AGCORE_ANALYSIS_MAX_CHUNKS", "1000"))
    ANALYSIS_MAX_MODEL_CALLS: int = int(os.environ.get("AGCORE_ANALYSIS_MAX_MODEL_CALLS", "500"))
    ANALYSIS_MAX_SECONDS: int = int(os.environ.get("AGCORE_ANALYSIS_MAX_SECONDS", "7200"))
    ANALYSIS_MODEL_TIMEOUT_SECONDS: float = float(os.environ.get("AGCORE_ANALYSIS_MODEL_TIMEOUT_SECONDS", "310"))
    TASK_MAX_DOCUMENT_CALLS: int = int(os.environ.get("AGCORE_TASK_MAX_DOCUMENT_CALLS", "5000"))
    TASK_MAX_REPLANS: int = int(os.environ.get("AGCORE_TASK_MAX_REPLANS", "2"))
    TASK_MAX_ANSWER_REPAIRS: int = int(os.environ.get("AGCORE_TASK_MAX_ANSWER_REPAIRS", "2"))

    # Phase R1.3 (AM-06): Single LangGraph R1 runtime & Append-only Trajectory store
    RUNTIME_ENGINE: str = os.environ.get("AGCORE_RUNTIME_ENGINE", "langgraph_r1")  # "langgraph_r1" (legacy retired)
    TRAJECTORY_DB_PATH: str = os.environ.get(
        "AGCORE_TRAJECTORY_DB_PATH",
        "/var/lib/hd2lab-agcore/trajectory.db"
        if os.access("/var/lib/hd2lab-agcore", os.W_OK)
        else str(Path(__file__).resolve().parents[1] / "data" / "trajectory.db"),
    )
    TRAJECTORY_ENABLED: bool = os.environ.get("AGCORE_TRAJECTORY_ENABLED", "true").lower() in ("true", "1", "yes")

    # Phase R2 (CP-02 & R2-INT-FIX-02): Workspace & Canvas Feature Flags + Service Allowlists
    WORKSPACE_ENABLED: bool = os.environ.get("AGCORE_WORKSPACE_ENABLED", "false").lower() in ("true", "1", "yes")
    CANVAS_ENABLED: bool = os.environ.get("AGCORE_CANVAS_ENABLED", "false").lower() in ("true", "1", "yes")
    CANVAS_TOOLS_ENABLED: bool = os.environ.get("AGCORE_CANVAS_TOOLS_ENABLED", "false").lower() in ("true", "1", "yes")
    WORKSPACE_ALLOWED_SERVICES: str = os.environ.get("AGCORE_WORKSPACE_ALLOWED_SERVICES", "")
    CANVAS_ALLOWED_SERVICES: str = os.environ.get("AGCORE_CANVAS_ALLOWED_SERVICES", "")
    # R21 Document & Knowledge Engine (dk/v1 federation)
    KNOWLEDGE_ENABLED: bool = os.environ.get("AGCORE_KNOWLEDGE_ENABLED", "false").lower() in ("true", "1", "yes", "on")
    KNOWLEDGE_ALLOWED_SERVICES: str = os.environ.get("AGCORE_KNOWLEDGE_ALLOWED_SERVICES", "")

settings = Settings()


def _parse_allowed_services(env_name: str, fallback_raw: str) -> Optional[Set[str]]:
    """Parse comma-separated service allowlist; returns None when unset/empty (no extra service restriction)."""
    raw = os.environ.get(env_name)
    if raw is None:
        raw = fallback_raw
    tokens = {item.strip() for item in (raw or "").split(",") if item.strip()}
    return tokens or None


def is_durable_analysis_enabled_for_service(service_id: str) -> bool:
    allowed = _parse_allowed_services("AGCORE_DURABLE_ANALYSIS_SERVICES", settings.DURABLE_ANALYSIS_SERVICES)
    return bool(allowed and (service_id in allowed or "*" in allowed))


def is_workspace_enabled() -> bool:
    """Resolve AGCORE_WORKSPACE_ENABLED dynamically (default False)."""
    raw = os.environ.get("AGCORE_WORKSPACE_ENABLED")
    if raw is not None:
        return raw.strip().lower() in ("true", "1", "yes", "on")
    return bool(settings.WORKSPACE_ENABLED)


def is_workspace_service_allowed(service_id: Optional[str]) -> bool:
    """Check AGCORE_WORKSPACE_ALLOWED_SERVICES without checking the global enabled toggle."""
    allowed = _parse_allowed_services("AGCORE_WORKSPACE_ALLOWED_SERVICES", settings.WORKSPACE_ALLOWED_SERVICES)
    if allowed is None:
        return True
    return bool(service_id) and (service_id in allowed or "*" in allowed)


def is_workspace_enabled_for_service(service_id: Optional[str]) -> bool:
    """R2-INT-FIX-02: True iff Workspace is globally enabled AND allowed for service_id."""
    return is_workspace_enabled() and is_workspace_service_allowed(service_id)


def is_canvas_enabled() -> bool:
    """Resolve AGCORE_CANVAS_ENABLED dynamically (default False)."""
    raw = os.environ.get("AGCORE_CANVAS_ENABLED")
    if raw is not None:
        return raw.strip().lower() in ("true", "1", "yes", "on")
    return bool(settings.CANVAS_ENABLED)


def is_canvas_service_allowed(service_id: Optional[str]) -> bool:
    """Check AGCORE_CANVAS_ALLOWED_SERVICES without checking the global enabled toggle."""
    allowed = _parse_allowed_services("AGCORE_CANVAS_ALLOWED_SERVICES", settings.CANVAS_ALLOWED_SERVICES)
    if allowed is None:
        return True
    return bool(service_id) and (service_id in allowed or "*" in allowed)


def is_canvas_enabled_for_service(service_id: Optional[str]) -> bool:
    """R2-INT-FIX-02: True iff Canvas is globally enabled AND allowed for service_id."""
    return is_canvas_enabled() and is_canvas_service_allowed(service_id)


def is_canvas_tools_enabled() -> bool:
    """Resolve AGCORE_CANVAS_TOOLS_ENABLED dynamically (default False)."""
    raw = os.environ.get("AGCORE_CANVAS_TOOLS_ENABLED")
    if raw is not None:
        return raw.strip().lower() in ("true", "1", "yes", "on")
    return bool(settings.CANVAS_TOOLS_ENABLED)


def is_canvas_tools_enabled_for_service(service_id: Optional[str]) -> bool:
    """R2-INT-FIX-02: True iff Canvas tools are globally enabled AND allowed for service_id."""
    return is_canvas_tools_enabled() and is_canvas_service_allowed(service_id)


def validate_runtime_engine_config(engine: Optional[str] = None) -> str:
    """AM-06: Boot fail-fast on retired 'legacy' engine; accept unset, 'langgraph_r1', or 'langgraph'."""
    raw = settings.RUNTIME_ENGINE if engine is None else engine
    normalized = (raw or "").strip().lower()
    if normalized == "legacy":
        raise RuntimeError(
            "Boot fail-fast (AM-06): AGCORE_RUNTIME_ENGINE=legacy has been retired in R1.3. "
            "Unset AGCORE_RUNTIME_ENGINE or set it to 'langgraph_r1'."
        )
    if normalized not in _SUPPORTED_RUNTIME_ENGINES:
        raise RuntimeError(
            f"Boot fail-fast (AM-06): unsupported AGCORE_RUNTIME_ENGINE='{raw}'. "
            "Allowed values: unset, 'langgraph_r1', 'langgraph'."
        )
    for dep_var in _DEPRECATED_LEGACY_ENV_VARS:
        if os.environ.get(dep_var) and dep_var not in _DEPRECATED_ENV_WARNED:
            _DEPRECATED_ENV_WARNED.add(dep_var)
            logger.warning(
                "Deprecated legacy runtime environment variable '%s' is ignored in R1.3.",
                dep_var,
            )
    return "langgraph_r1"


def validate_boot_configuration() -> None:
    """FIX-203 & AM-06: Fail-fast boot validation. Logs only missing env var names, never secrets."""
    # AM-06: Enforce retired legacy runtime fail-fast across ALL environments
    validate_runtime_engine_config()
    if settings.ENVIRONMENT.lower() != "production":
        return
    from app.core.auth import is_agtools_token_valid

    missing = []
    if settings.AGTOOLS_ENABLED and not is_agtools_token_valid(settings.AGTOOLS_TOKEN):
        missing.append("AGCORE_AGTOOLS_TOKEN")
    has_service_auth = bool((settings.CREDENTIAL_SCOPES_JSON or "").strip())
    has_proxy_auth = bool((settings.TRUSTED_PROXY_SECRET or "").strip())
    if not has_service_auth and not has_proxy_auth:
        missing.append("AGCORE_CREDENTIAL_SCOPES_OR_AGCORE_TRUSTED_PROXY_SECRET")
    if (settings.CREDENTIAL_SCOPES_JSON or "").strip():
        import json as _json
        try:
            scopes = _json.loads(settings.CREDENTIAL_SCOPES_JSON)
            secrets = _json.loads(settings.CREDENTIAL_SECRETS_JSON or "{}")
            if not isinstance(scopes, dict) or not isinstance(secrets, dict):
                missing.append("AGCORE_CREDENTIAL_SCOPES_INVALID_TYPE")
            else:
                seen_secrets: dict[str, str] = {}
                for cid in scopes:
                    vals = secrets.get(cid)
                    if not isinstance(vals, list) or not vals or not all(is_agtools_token_valid(v) for v in vals):
                        missing.append(f"AGCORE_CREDENTIAL_SECRETS[{cid}]")
                        continue
                    for v in vals:
                        norm_v = v.strip()
                        if norm_v in seen_secrets and seen_secrets[norm_v] != cid:
                            missing.append(f"duplicate_credential_secret:{seen_secrets[norm_v]}:{cid}")
                        seen_secrets[norm_v] = cid
                    cfg = scopes.get(cid) or {}
                    if isinstance(cfg, dict):
                        fixed_owner = str(cfg.get("fixed_owner") or "")
                        if ":" in fixed_owner:
                            missing.append(f"invalid_fixed_owner_delimiter:{cid}")
                        for t in (cfg.get("allowed_tenants") or []):
                            if ":" in str(t):
                                missing.append(f"invalid_tenant_delimiter:{cid}")
        except Exception:
            missing.append("AGCORE_CREDENTIAL_SCOPES_INVALID_JSON")
    if missing:
        raise RuntimeError(
            "Production boot fail-fast: missing or invalid required credentials: " + ", ".join(missing)
        )


def is_knowledge_enabled() -> bool:
    """Resolve AGCORE_KNOWLEDGE_ENABLED dynamically (default False: additive feature)."""

    raw = os.environ.get("AGCORE_KNOWLEDGE_ENABLED")
    if raw is not None:
        return raw.strip().lower() in ("true", "1", "yes", "on")
    return bool(settings.KNOWLEDGE_ENABLED)


def is_knowledge_service_allowed(service_id: Optional[str]) -> bool:
    allowed = _parse_allowed_services("AGCORE_KNOWLEDGE_ALLOWED_SERVICES", settings.KNOWLEDGE_ALLOWED_SERVICES)
    if allowed is None:
        return True
    return bool(service_id) and (service_id in allowed or "*" in allowed)


def is_knowledge_enabled_for_service(service_id: Optional[str]) -> bool:
    return is_knowledge_enabled() and is_knowledge_service_allowed(service_id)
