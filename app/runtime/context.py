"""Dynamic Token Allocation, Auto-Calibrating Context Budget Manager, and 3-Stage Compactor.

Design principles:
- Zero hardcoded model context limits or truncation sizes: all pools and thresholds
  are computed dynamically as ratios of the active route's AI Gateway input budget.
- Elastic pool borrowing: unused summary/evidence pool tokens roll over to active pools.
- Atomic turn-group integrity: assistant(tool_calls) and matching tool(tool_call_id)
  messages are always kept or compacted together so provider APIs never fail with HTTP 400.
- Structured stub preservation: older tool payloads keep document/section/figure IDs
  so orchestration can re-fetch details or figures on demand.
"""
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.core.runtime_config import CompactionPolicy, get_runtime_config
from app.schemas.gateway import GatewayMessage

CONTEXT_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "too many tokens",
    "prompt is too long",
    "input is too long",
    "exceeds the context",
)


def is_context_overflow_error(exc: Exception) -> bool:
    """Detect provider/gateway context-window overflow errors for reactive recovery."""
    msg = str(exc).lower()
    return any(marker in msg for marker in CONTEXT_OVERFLOW_MARKERS)


@dataclass
class BudgetAllocation:
    input_budget: int
    system_tokens: int
    available_tokens: int
    recent_budget: int
    evidence_budget: int
    summary_budget: int
    soft_threshold: int
    compact_threshold: int
    hard_cap: int
    tool_stub_budget: int


@dataclass
class CompactionReport:
    before_tokens: int
    after_tokens: int
    input_budget: int
    stage_reached: int  # 0=none, 1=stub, 2=rolling_summary, 3=hard_prune
    dropped_groups: int
    trimmed: bool


class ContextManager:
    def __init__(
        self,
        budget_tokens: int = settings.CONTEXT_BUDGET_TOKENS,
        max_tool_result_tokens: int = settings.MAX_TOOL_RESULT_TOKENS,
    ):
        self.budget_tokens = budget_tokens
        self.max_tool_result_tokens = max_tool_result_tokens
        self._cache: Dict[str, str] = {}
        # Route-level EMA calibration factor (actual_prompt_tokens / estimated_tokens)
        self._calibration: Dict[str, float] = {}

    def _policy(self) -> CompactionPolicy:
        try:
            return get_runtime_config().compaction_policy
        except Exception:
            return CompactionPolicy()

    def get_calibration_factor(self, route: Optional[str] = None) -> float:
        if not route:
            return 1.0
        return self._calibration.get(route, 1.0)

    def record_observed_usage(self, route: Optional[str], estimated_tokens: int, actual_prompt_tokens: int) -> float:
        """Update route EMA calibration factor from observed provider prompt_tokens."""
        if not route or estimated_tokens <= 0 or actual_prompt_tokens <= 0:
            return self.get_calibration_factor(route)
        observed_ratio = max(0.5, min(2.5, actual_prompt_tokens / float(estimated_tokens)))
        prev = self._calibration.get(route, 1.0)
        updated = round((0.7 * prev) + (0.3 * observed_ratio), 4)
        self._calibration[route] = max(0.5, min(2.5, updated))
        return self._calibration[route]

    def bump_calibration_on_overflow(self, route: Optional[str]) -> float:
        """Immediately raise token estimate factor when provider reports context overflow."""
        key = route or "__default__"
        prev = self._calibration.get(key, 1.0)
        updated = min(2.5, round(prev * 1.25, 4))
        self._calibration[key] = updated
        return updated

    def estimate_tokens(self, text: str, route: Optional[str] = None) -> int:
        """Byte-based estimation dynamically scaled by route EMA calibration factor."""
        if not text:
            return 0
        raw = max(1, len(text.encode("utf-8")) // 3)
        factor = self.get_calibration_factor(route)
        return max(1, int(round(raw * factor)))

    def estimate_message_tokens(self, m: GatewayMessage, route: Optional[str] = None) -> int:
        total = 4
        if m.content:
            total += self.estimate_tokens(m.content, route=route)
        if m.tool_calls:
            for tc in m.tool_calls:
                total += self.estimate_tokens(tc.function.name, route=route)
                total += self.estimate_tokens(tc.function.arguments, route=route)
        return total

    def estimate_messages_tokens(self, messages: List[GatewayMessage], route: Optional[str] = None) -> int:
        return sum(self.estimate_message_tokens(m, route=route) for m in messages)

    def allocate_budget(
        self,
        messages: List[GatewayMessage],
        budget_tokens: Optional[int] = None,
        route: Optional[str] = None,
        has_evidence: bool = True,
        has_history_summary: bool = False,
    ) -> BudgetAllocation:
        """Dynamically compute token pools and thresholds from active input budget ratios."""
        policy = self._policy()
        effective = max(64, int(budget_tokens if budget_tokens is not None else self.budget_tokens))
        sys_tokens = sum(self.estimate_message_tokens(m, route=route) for m in messages if m.role == "system")
        available = max(32, effective - sys_tokens)

        r_recent = policy.recent_pool_ratio
        r_evidence = policy.evidence_pool_ratio
        r_summary = policy.summary_pool_ratio

        # Elastic borrowing: redistribute unused summary/evidence share to active pools
        if not has_history_summary:
            if has_evidence:
                r_evidence += r_summary
            else:
                r_recent += r_summary
            r_summary = 0.0
        if not has_evidence:
            r_recent += r_evidence
            r_evidence = 0.0

        ratio_sum = max(0.01, r_recent + r_evidence + r_summary)
        recent_budget = max(16, int(available * (r_recent / ratio_sum)))
        evidence_budget = max(0, int(available * (r_evidence / ratio_sum)))
        summary_budget = max(0, int(available * (r_summary / ratio_sum)))

        soft_th = max(32, int(effective * policy.soft_threshold_ratio))
        compact_th = max(soft_th, int(effective * policy.compact_threshold_ratio))
        hard_cap = max(compact_th, int(effective * policy.hard_cap_ratio))
        tool_stub_budget = max(12, int(available * policy.tool_stub_share_ratio))

        return BudgetAllocation(
            input_budget=effective,
            system_tokens=sys_tokens,
            available_tokens=available,
            recent_budget=recent_budget,
            evidence_budget=evidence_budget,
            summary_budget=summary_budget,
            soft_threshold=soft_th,
            compact_threshold=compact_th,
            hard_cap=hard_cap,
            tool_stub_budget=tool_stub_budget,
        )

    def compute_tool_token_budget(
        self,
        messages: List[GatewayMessage],
        budget_tokens: Optional[int] = None,
        route: Optional[str] = None,
        tool_count: int = 1,
    ) -> int:
        """Compute per-tool token_budget dynamically from remaining Active Evidence Pool."""
        alloc = self.allocate_budget(messages, budget_tokens=budget_tokens, route=route, has_evidence=True)
        used_tokens = self.estimate_messages_tokens(messages, route=route)
        remaining_total = max(64, alloc.hard_cap - used_tokens)
        evidence_cap = min(alloc.evidence_budget or remaining_total, remaining_total)
        divisor = max(1, int(tool_count))
        return max(128, evidence_cap // divisor)

    def cache_key(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        serialized = json.dumps({"t": tool_name, "a": arguments}, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def get_cached_result(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        key = self.cache_key(tool_name, arguments)
        return self._cache.get(key)

    def cache_result(self, tool_name: str, arguments: Dict[str, Any], result: str):
        key = self.cache_key(tool_name, arguments)
        self._cache[key] = result

    @staticmethod
    def _group_atomic_turns(messages: List[GatewayMessage]) -> Tuple[List[GatewayMessage], List[List[GatewayMessage]]]:
        """Separate system messages and group remaining messages into atomic turn units.
        An assistant message with tool_calls is always grouped with its subsequent tool messages.
        """
        system_msgs: List[GatewayMessage] = []
        groups: List[List[GatewayMessage]] = []
        i = 0
        n = len(messages)
        while i < n:
            m = messages[i]
            if m.role == "system":
                system_msgs.append(m)
                i += 1
                continue
            if m.role == "assistant" and m.tool_calls:
                group = [m]
                call_ids = {tc.id for tc in m.tool_calls if getattr(tc, "id", None)}
                i += 1
                while i < n and messages[i].role == "tool":
                    if not call_ids or (messages[i].tool_call_id in call_ids) or messages[i].tool_call_id is None:
                        group.append(messages[i])
                        i += 1
                    else:
                        break
                groups.append(group)
                continue
            groups.append([m])
            i += 1
        return system_msgs, groups

    def _build_structured_tool_stub(self, m: GatewayMessage, max_tokens: int, route: Optional[str]) -> str:
        """Deterministically compress an older tool message while keeping doc/section/figure IDs."""
        raw = m.content or ""
        char_cap = max(48, int((max_tokens * 3) / max(0.5, self.get_calibration_factor(route))))
        marker = "...[이전 도구 실행 결과 요약됨]"
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                keep: Dict[str, Any] = {}
                for k in (
                    "doc_id", "document_id", "section_id", "chunk_id", "figure_id",
                    "figure_ids", "query", "provider", "total_matches", "total_results", "title",
                ):
                    if k in parsed and parsed[k] not in (None, "", []):
                        keep[k] = parsed[k]
                if "results" in parsed and isinstance(parsed["results"], list):
                    keep["results_count"] = len(parsed["results"])
                    top_refs = []
                    for r in parsed["results"][:3]:
                        if isinstance(r, dict):
                            ref = r.get("chunk_id") or r.get("section_id") or r.get("url") or r.get("title")
                            if ref:
                                top_refs.append(str(ref)[:60])
                    if top_refs:
                        keep["top_refs"] = top_refs
                if keep:
                    stub = json.dumps(keep, ensure_ascii=False)
                    if len(stub) > char_cap:
                        stub = stub[:char_cap]
                    return f"{stub} {marker}"
        except Exception:
            pass
        snippet_len = max(32, char_cap - len(marker) - 1)
        return raw[:snippet_len] + marker

    def _build_rolling_summary(self, dropped_groups: List[List[GatewayMessage]], max_tokens: int, route: Optional[str]) -> GatewayMessage:
        """Deterministically roll older turn groups into a bounded structured summary message."""
        char_cap = max(80, int((max_tokens * 3) / max(0.5, self.get_calibration_factor(route))))
        bullets: List[str] = []
        for grp in dropped_groups:
            for m in grp:
                text = (m.content or "").strip().replace("\n", " ")
                if not text:
                    continue
                if m.role == "user":
                    bullets.append(f"- 사용자: {text[:120]}")
                elif m.role == "assistant":
                    bullets.append(f"- 응답: {text[:120]}")
                elif m.role == "tool":
                    bullets.append(f"- 도구({m.name or 'tool'}): {text[:100]}")
        body = "\n".join(bullets)
        if len(body) > char_cap:
            body = body[: max(40, char_cap - 3)] + "..."
        return GatewayMessage(
            role="system",
            content=f"[컨텍스트 압축 요약]\n{body}" if body else "[컨텍스트 압축 요약] 이전 대화 생략됨",
        )

    def _guard_oversized_single_message(
        self,
        messages: List[GatewayMessage],
        hard_cap: int,
        route: Optional[str],
    ) -> Tuple[List[GatewayMessage], bool]:
        """Protect against a single gigantic user/tool message that alone exceeds hard_cap."""
        if self.estimate_messages_tokens(messages, route=route) <= hard_cap:
            return messages, False
        if not messages:
            return messages, False
        trimmed = False
        out = list(messages)
        # Trim the largest non-system message proportionally (preserve head + tail)
        non_sys_indices = [idx for idx, m in enumerate(out) if m.role != "system" and (m.content or "")]
        if not non_sys_indices:
            return out, False
        target_idx = max(non_sys_indices, key=lambda idx: self.estimate_message_tokens(out[idx], route=route))
        other_tokens = self.estimate_messages_tokens(out, route=route) - self.estimate_message_tokens(out[target_idx], route=route)
        allowed_tokens = max(16, hard_cap - other_tokens - 4)
        allowed_bytes = max(48, int((allowed_tokens * 3) / max(0.5, self.get_calibration_factor(route))))
        marker = "\n...[길이 초과로 중간 생략됨]...\n"
        marker_bytes = len(marker.encode("utf-8"))
        content = out[target_idx].content or ""
        if len(content.encode("utf-8")) > allowed_bytes:
            body_bytes = max(24, allowed_bytes - marker_bytes)
            half_bytes = max(12, body_bytes // 2)
            raw_b = content.encode("utf-8")
            head = raw_b[:half_bytes].decode("utf-8", errors="ignore")
            tail = raw_b[-half_bytes:].decode("utf-8", errors="ignore")
            clipped = head + marker + tail
            out[target_idx] = out[target_idx].model_copy(update={"content": clipped})
            trimmed = True
        return out, trimmed

    def compact_with_report(
        self,
        messages: List[GatewayMessage],
        budget_tokens: Optional[int] = None,
        route: Optional[str] = None,
        force_ratio: Optional[float] = None,
    ) -> Tuple[List[GatewayMessage], CompactionReport]:
        """Execute 3-stage dynamic compaction and return (messages, CompactionReport)."""
        effective_budget = max(64, int(budget_tokens if budget_tokens is not None else self.budget_tokens))
        if force_ratio is not None:
            effective_budget = max(64, int(effective_budget * max(0.3, min(1.0, force_ratio))))

        alloc = self.allocate_budget(
            messages,
            budget_tokens=effective_budget,
            route=route,
            has_evidence=any(m.role == "tool" for m in messages),
            has_history_summary=any(m.role == "system" and (m.content or "").startswith("[컨텍스트 압축 요약]") for m in messages),
        )
        before_tokens = self.estimate_messages_tokens(messages, route=route)
        if before_tokens <= alloc.soft_threshold:
            return messages, CompactionReport(
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                input_budget=alloc.input_budget,
                stage_reached=0,
                dropped_groups=0,
                trimmed=False,
            )

        trimmed = False
        stage_reached = 0
        dropped_groups_count = 0

        # Stage 1: Deterministic structured stubbing of older tool results
        working: List[GatewayMessage] = []
        last_idx = len(messages) - 1
        for i, m in enumerate(messages):
            if m.role == "tool" and i < last_idx:
                tok = self.estimate_tokens(m.content or "", route=route)
                if tok > alloc.tool_stub_budget:
                    stub = self._build_structured_tool_stub(m, alloc.tool_stub_budget, route=route)
                    working.append(
                        GatewayMessage(
                            role="tool",
                            content=stub,
                            tool_call_id=m.tool_call_id,
                            name=m.name,
                        )
                    )
                    trimmed = True
                    stage_reached = max(stage_reached, 1)
                    continue
            working.append(m)

        # Stage 2: Atomic turn-group rolling compaction if still above compact_threshold
        if self.estimate_messages_tokens(working, route=route) > alloc.compact_threshold:
            system_msgs, groups = self._group_atomic_turns(working)
            compacted_groups: List[List[GatewayMessage]] = []
            # Always keep at least the latest atomic group (e.g. current user question / latest tool turn)
            while (
                len(groups) > 1
                and self.estimate_messages_tokens(
                    system_msgs + [m for g in groups for m in g], route=route
                ) > alloc.compact_threshold
            ):
                compacted_groups.append(groups.pop(0))
                dropped_groups_count += 1
                trimmed = True
                stage_reached = max(stage_reached, 2)

            if compacted_groups:
                summary_cap = max(32, alloc.summary_budget or int(alloc.available_tokens * 0.2))
                summary_msg = self._build_rolling_summary(compacted_groups, summary_cap, route=route)
                working = system_msgs + [summary_msg] + [m for g in groups for m in g]
            else:
                working = system_msgs + [m for g in groups for m in g]

        # Stage 3: Emergency hard-cap guard (drop oldest non-system groups, then clip oversized single message)
        if self.estimate_messages_tokens(working, route=route) > alloc.hard_cap:
            system_msgs, groups = self._group_atomic_turns(working)
            while (
                len(groups) > 1
                and self.estimate_messages_tokens(
                    system_msgs + [m for g in groups for m in g], route=route
                ) > alloc.hard_cap
            ):
                groups.pop(0)
                dropped_groups_count += 1
                trimmed = True
                stage_reached = 3
            working = system_msgs + [m for g in groups for m in g]

            # If still above hard_cap (e.g. single gigantic prompt), clip proportionally
            working, clipped = self._guard_oversized_single_message(working, alloc.hard_cap, route=route)
            if clipped:
                trimmed = True
                stage_reached = 3

        after_tokens = self.estimate_messages_tokens(working, route=route)
        return working, CompactionReport(
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            input_budget=alloc.input_budget,
            stage_reached=stage_reached,
            dropped_groups=dropped_groups_count,
            trimmed=trimmed,
        )

    def prune_and_compress(
        self,
        messages: List[GatewayMessage],
        budget_tokens: Optional[int] = None,
        route: Optional[str] = None,
    ) -> Tuple[List[GatewayMessage], bool]:
        """Backward-compatible entry point returning (compacted_messages, trimmed)."""
        compacted, report = self.compact_with_report(messages, budget_tokens=budget_tokens, route=route)
        return compacted, report.trimmed


context_manager = ContextManager()
