"""ADR-0018: Domain-agnostic Tool Context, Post-Call Quality Inspector, and Escalation Gate."""
from datetime import datetime, timezone, timedelta
import json
from typing import Any, Dict, List, Optional, Set, Tuple

KST = timezone(timedelta(hours=9))
WEEKDAYS_KO = ("월", "화", "수", "목", "금", "토", "일")


class ContextAdapter:
    """Injects current KST timestamp and active tool guidance without hardcoding domain logic."""

    @staticmethod
    def build_system_message(active_tool_names: List[str]) -> str:
        now = datetime.now(KST)
        date_str = f"{now.strftime('%Y-%m-%d %H:%M')} KST ({WEEKDAYS_KO[now.weekday()]}요일)"
        lines = [
            f"[실행 컨텍스트] 현재 시각은 {date_str}입니다.",
        ]
        if active_tool_names:
            lines.append(f"사용 가능한 도구: {', '.join(active_tool_names)}")
            lines.append(
                "[도구 우선 원칙] 질문이 현재 시점의 사실·외부 정보·문서 처리 등 활성화된 도구로 확인할 수 있는 영역이라면, "
                "'도구가 없다'고 먼저 단정하지 말고 활성 도구 스키마를 확인해 적합한 도구를 호출한 뒤 근거에 기반해 답변하세요."
            )
            lines.append(
                "[도구 결과 품질 처리 원칙] 도구 응답의 meta.quality가 'shallow' 또는 'empty'이면 "
                "단순 링크 안내나 '직접 확인하세요'로 끝내지 말고, meta.next_hints를 참고해 파라미터를 보완해 재조회하거나 "
                "관련 상세 조회 도구로 근거를 확보한 뒤 구체적으로 답변하세요."
            )
        return "\n".join(lines)


class PostCallAdapter:
    """Normalizes tool outputs into deterministic quality signals (<1ms, zero extra LLM calls)."""

    @staticmethod
    def inspect(tool_name: str, content: str, is_error: bool) -> Dict[str, Any]:
        if is_error:
            return {"tool": tool_name, "quality": "empty", "reason": "tool_error", "next_hints": []}
        try:
            parsed = json.loads(content) if isinstance(content, str) else content
        except Exception:
            parsed = content

        if isinstance(parsed, dict) and isinstance(parsed.get("meta"), dict) and parsed["meta"].get("quality"):
            meta = parsed["meta"]
            return {
                "tool": tool_name,
                "quality": str(meta.get("quality")),
                "reason": str(meta.get("reason") or "ok"),
                "next_hints": list(meta.get("next_hints") or []),
            }

        # Generic fallback heuristic for legacy/external tools without explicit meta
        if parsed is None or parsed == [] or parsed == {}:
            return {"tool": tool_name, "quality": "empty", "reason": "empty_payload", "next_hints": []}
        text_len = len(content or "")
        if text_len < 80:
            return {"tool": tool_name, "quality": "shallow", "reason": "short_payload", "next_hints": []}
        return {"tool": tool_name, "quality": "sufficient", "reason": "ok", "next_hints": []}


class EscalationGate:
    """Trace-based deterministic escalation gate (no LLM-as-a-Judge, bounded by escalation_budget)."""

    @staticmethod
    def evaluate(
        last_meta: Optional[Dict[str, Any]],
        attempted_tools: Set[str],
        allowed_tools: Set[str],
        escalations_used: int,
        budget: int = 1,
    ) -> Tuple[bool, Optional[str]]:
        if not last_meta or escalations_used >= budget:
            return False, None
        if last_meta.get("quality") not in ("shallow", "empty"):
            return False, None

        usable_hints = []
        for h in last_meta.get("next_hints") or []:
            t = h.get("tool")
            if t and t in allowed_tools:
                sig = f"{t}:{json.dumps(h.get('args') or {}, sort_keys=True, ensure_ascii=False)}"
                if sig not in attempted_tools:
                    usable_hints.append(h)

        if not usable_hints:
            return False, None

        hint_lines = []
        for h in usable_hints[:2]:
            args_txt = f" 인자={json.dumps(h['args'], ensure_ascii=False)}" if h.get("args") else ""
            hint_lines.append(f"- {h['tool']}{args_txt} ({h.get('why') or '보강 조회'})")

        prompt = (
            f"[품질 보강 요청] 직전 도구({last_meta.get('tool')}) 결과가 '{last_meta.get('quality')}' "
            f"(사유: {last_meta.get('reason')})로 충분한 근거가 확보되지 않았습니다. "
            "포털 링크 안내로 끝내지 말고 아래 보강 힌트 중 하나를 실행해 구체적인 사실·내용을 확인한 뒤 답변하세요:\n"
            + "\n".join(hint_lines)
        )
        return True, prompt
