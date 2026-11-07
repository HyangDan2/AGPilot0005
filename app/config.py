"""AGCore Configuration and Environment Settings."""
import os
from pydantic import BaseModel

class Settings(BaseModel):
    HOST: str = os.environ.get("AGCORE_HOST", "127.0.0.1")
    PORT: int = int(os.environ.get("AGCORE_PORT", "8200"))
    
    # AI Gateway Settings
    GATEWAY_URL: str = os.environ.get("AGCORE_GATEWAY_URL", "http://127.0.0.1:8080")
    GATEWAY_SOCKET: str = os.environ.get("AGCORE_GATEWAY_SOCKET", "/run/hd2lab-ai-gateway/api.sock")
    GATEWAY_ROUTE: str = os.environ.get("AGCORE_GATEWAY_ROUTE", "hd2ai-admin")
    GATEWAY_TOKEN: str = os.environ.get("AGCORE_GATEWAY_TOKEN", "")
    
    # Execution & Context Limits (from Master Work Order)
    MAX_TOOL_CALLS: int = int(os.environ.get("AGCORE_MAX_TOOL_CALLS", "6"))
    MAX_AGENT_STEPS: int = int(os.environ.get("AGCORE_MAX_AGENT_STEPS", "10"))
    CONTEXT_BUDGET_TOKENS: int = int(os.environ.get("AGCORE_CONTEXT_BUDGET_TOKENS", "12000"))
    MAX_TOOL_RESULT_TOKENS: int = int(os.environ.get("AGCORE_MAX_TOOL_RESULT_TOKENS", "2000"))
    TOOL_TIMEOUT_SECONDS: float = float(os.environ.get("AGCORE_TOOL_TIMEOUT_SECONDS", "30.0"))
    RUN_TIMEOUT_SECONDS: float = float(os.environ.get("AGCORE_RUN_TIMEOUT_SECONDS", "120.0"))
    
    # Service Auth Tokens (Callers allowed to invoke AGCore)
    SERVICE_TOKENS: str = os.environ.get("AGCORE_SERVICE_TOKENS", "")

settings = Settings()
