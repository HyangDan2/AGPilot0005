"""AGCore Configuration and Environment Settings."""
import os
from pathlib import Path
from pydantic import BaseModel

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
    ANALYSIS_DIR: str = os.environ.get("AGCORE_ANALYSIS_DIR", "/var/lib/hd2lab-agcore/analysis")
    ANALYSIS_MAX_CHUNKS: int = int(os.environ.get("AGCORE_ANALYSIS_MAX_CHUNKS", "1000"))
    ANALYSIS_MAX_MODEL_CALLS: int = int(os.environ.get("AGCORE_ANALYSIS_MAX_MODEL_CALLS", "500"))
    ANALYSIS_MAX_SECONDS: int = int(os.environ.get("AGCORE_ANALYSIS_MAX_SECONDS", "7200"))
    ANALYSIS_MODEL_TIMEOUT_SECONDS: float = float(os.environ.get("AGCORE_ANALYSIS_MODEL_TIMEOUT_SECONDS", "310"))
    TASK_MAX_DOCUMENT_CALLS: int = int(os.environ.get("AGCORE_TASK_MAX_DOCUMENT_CALLS", "5000"))
    TASK_MAX_REPLANS: int = int(os.environ.get("AGCORE_TASK_MAX_REPLANS", "2"))
    TASK_MAX_ANSWER_REPAIRS: int = int(os.environ.get("AGCORE_TASK_MAX_ANSWER_REPAIRS", "2"))

    # Phase R1 Amendment 02: Feature-flagged LangGraph R1 runtime & Append-only Trajectory store
    RUNTIME_ENGINE: str = os.environ.get("AGCORE_RUNTIME_ENGINE", "legacy")  # "legacy" | "langgraph_r1"
    TRAJECTORY_DB_PATH: str = os.environ.get(
        "AGCORE_TRAJECTORY_DB_PATH",
        "/var/lib/hd2lab-agcore/trajectory.db"
        if os.access("/var/lib/hd2lab-agcore", os.W_OK)
        else str(Path(__file__).resolve().parents[1] / "data" / "trajectory.db"),
    )
    TRAJECTORY_ENABLED: bool = os.environ.get("AGCORE_TRAJECTORY_ENABLED", "true").lower() in ("true", "1", "yes")

settings = Settings()


def validate_boot_configuration() -> None:
    """FIX-203: Fail-fast production boot validation. Logs only missing env var names, never secrets."""
    if settings.ENVIRONMENT.lower() != "production":
        return
    from app.core.auth import is_agtools_token_valid

    missing = []
    if settings.AGTOOLS_ENABLED and not is_agtools_token_valid(settings.AGTOOLS_TOKEN):
        missing.append("AGCORE_AGTOOLS_TOKEN")
    has_service_auth = bool(
        (settings.SERVICE_TOKENS or "").strip()
        or (settings.ADMIN_SERVICE_TOKENS or "").strip()
        or (settings.CREDENTIAL_SCOPES_JSON or "").strip()
    )
    has_proxy_auth = bool((settings.TRUSTED_PROXY_SECRET or "").strip())
    if not has_service_auth and not has_proxy_auth:
        missing.append("AGCORE_SERVICE_TOKENS_OR_AGCORE_CREDENTIAL_SCOPES_OR_AGCORE_TRUSTED_PROXY_SECRET")
    if (settings.CREDENTIAL_SCOPES_JSON or "").strip():
        import json as _json
        try:
            scopes = _json.loads(settings.CREDENTIAL_SCOPES_JSON)
            secrets = _json.loads(settings.CREDENTIAL_SECRETS_JSON or "{}")
            for cid in scopes:
                vals = secrets.get(cid)
                if not isinstance(vals, list) or not vals or not all(is_agtools_token_valid(v) for v in vals):
                    missing.append(f"AGCORE_CREDENTIAL_SECRETS[{cid}]")
        except Exception:
            missing.append("AGCORE_CREDENTIAL_SCOPES_INVALID_JSON")
    if missing:
        raise RuntimeError(
            "Production boot fail-fast: missing or invalid required credentials: " + ", ".join(missing)
        )
