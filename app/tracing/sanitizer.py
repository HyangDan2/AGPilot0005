"""REG-POL-20: Telemetry sanitizer — removes user raw text, secrets, and sensitive tool parameters."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Mapping

SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "cookie",
    "credential",
    "private_key",
    "bearer",
    "message",
    "prompt",
    "content",
    "raw_text",
    "user_input",
    "query",
    "body",
    "arguments",
    "text",
)

SAFE_EXACT_KEYS = frozenset(
    {
        "service_id",
        "authenticated",
        "tool_names",
        "tool_count",
        "reasoning_effort",
        "context_budget_tokens",
        "target_budget_tokens",
        "terminal_status",
        "disable_tools",
        "expose_data",
        "step_count",
        "tool_call_count",
        "remaining_time_budget_seconds",
        "estimated_context_tokens",
        "has_pending_tool_calls",
        "pending_tool_count",
        "has_final_answer",
        "context_compacted",
        "fallback_count",
        "last_tool_quality",
        "allowed_tools",
        "allowed_actions",
        "max_steps",
        "max_tool_calls",
        "time_budget_seconds",
        "max_fallback_depth",
        "default_reasoning_effort",
        "reason_code",
        "action",
        "accepted",
        "violated_constraints",
        "fallback_action",
        "feedback_type",
        "category",
        "time_range",
        "lang",
        "limit",
        "top_k",
        "doc_id",
        "expected_event_count",
    }
)

SECRET_VALUE_PATTERNS = (
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{10,}\b", re.IGNORECASE),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
)


def _redact_string(value: str) -> Dict[str, Any]:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    return {"redacted": True, "length": len(value), "sha256_prefix": digest}


def _looks_like_secret(value: str) -> bool:
    return any(pat.search(value) for pat in SECRET_VALUE_PATTERNS)


def _is_sensitive_key(key: str) -> bool:
    """R1-FIX-13 C: Strict explicit Field Allowlist (any key outside SAFE_EXACT_KEYS is redacted)."""
    lower = key.lower().strip()
    if lower in SAFE_EXACT_KEYS:
        return False
    return True


def sanitize_value(value: Any, *, key_hint: str = "") -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _is_sensitive_key(key_hint) or _looks_like_secret(value):
            return _redact_string(value)
        # Bound free-form strings not in safe keys to prevent raw text leakage
        if key_hint and key_hint.lower() not in SAFE_EXACT_KEYS and len(value) > 120:
            return _redact_string(value)
        return value
    if isinstance(value, Mapping):
        return sanitize_mapping(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_value(item, key_hint=key_hint) for item in value]
    return str(value)[:120]


def sanitize_mapping(data: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in data.items():
        ks = str(k)
        if _is_sensitive_key(ks):
            if isinstance(v, str):
                out[ks] = _redact_string(v)
            else:
                out[ks] = {"redacted": True, "type": type(v).__name__}
        else:
            out[ks] = sanitize_value(v, key_hint=ks)
    return out
