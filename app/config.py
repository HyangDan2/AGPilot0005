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
    
    # Service Auth Tokens (Callers allowed to invoke AGCore)
    SERVICE_TOKENS: str = os.environ.get("AGCORE_SERVICE_TOKENS", "")

    # Environment & Mock Policy
    ENVIRONMENT: str = os.environ.get("AGCORE_ENV", "development")
    ALLOW_MOCK_FALLBACK: bool = os.environ.get("AGCORE_ALLOW_MOCK_FALLBACK", "true").lower() in ("true", "1", "yes")

    # AGPilot0006 (Document Intelligence) Service Integration
    AGTOOLS_URL: str = os.environ.get("AGCORE_AGTOOLS_URL", "http://127.0.0.1:8006/api/v1")
    AGTOOLS_TENANT: str = os.environ.get("AGCORE_AGTOOLS_TENANT", "default_tenant")
    AGTOOLS_TOKEN: str = os.environ.get("AGCORE_AGTOOLS_TOKEN", "hd2-doc-secret-token-key-2026")
    AGTOOLS_TIMEOUT_SECONDS: float = float(os.environ.get("AGCORE_AGTOOLS_TIMEOUT_SECONDS", "10.0"))

    # Persistent, bounded full-document analysis (separate from general Agent limits).
    ANALYSIS_DIR: str = os.environ.get("AGCORE_ANALYSIS_DIR", "/var/lib/hd2lab-agcore/analysis")
    ANALYSIS_MAX_CHUNKS: int = int(os.environ.get("AGCORE_ANALYSIS_MAX_CHUNKS", "1000"))
    ANALYSIS_MAX_MODEL_CALLS: int = int(os.environ.get("AGCORE_ANALYSIS_MAX_MODEL_CALLS", "500"))
    ANALYSIS_MAX_SECONDS: int = int(os.environ.get("AGCORE_ANALYSIS_MAX_SECONDS", "7200"))
    ANALYSIS_MODEL_TIMEOUT_SECONDS: float = float(os.environ.get("AGCORE_ANALYSIS_MODEL_TIMEOUT_SECONDS", "310"))
    TASK_MAX_DOCUMENT_CALLS: int = int(os.environ.get("AGCORE_TASK_MAX_DOCUMENT_CALLS", "5000"))
    TASK_MAX_REPLANS: int = int(os.environ.get("AGCORE_TASK_MAX_REPLANS", "2"))
    TASK_MAX_ANSWER_REPAIRS: int = int(os.environ.get("AGCORE_TASK_MAX_ANSWER_REPAIRS", "2"))

settings = Settings()
