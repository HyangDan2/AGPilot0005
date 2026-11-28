"""WP-08 Context & Budget Management.

One request never gets the whole catalogue.  :class:`ContextBudgetManager` owns the
finite context window and the finite execution budget:

* **Priority allocation** across five segment kinds — ``rules``, ``skills``,
  ``workspace_attachments``, ``tool_results``, ``intermediate_results`` — with per-kind
  quotas and a guaranteed reserve for rules.
* **Compaction** — high-priority content is kept, low-priority content is truncated
  head+tail (never silently dropped without a record) when the budget is exceeded.
* **Runtime scope enforcement** — ``max_step_budget`` and ``max_tool_calls`` raise
  :class:`BudgetExceeded` so a looping planner cannot spend forever.
* **Lazy schema exposure** — :meth:`ContextBudgetManager.select_tool_schemas` returns
  only the tools a step actually needs, plus the token cost of that selection.

Token counts are estimates from an injectable estimator (default: ~4 characters per
token).  That keeps the module dependency-free while still being deterministic in tests.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ContextKind",
    "CONTEXT_PRIORITY",
    "DEFAULT_SHARES",
    "BudgetExceeded",
    "ContextSegment",
    "SegmentAllocation",
    "BudgetPlan",
    "CompactionResult",
    "ToolSchemaSelection",
    "ContextBudgetManager",
    "estimate_tokens",
]


class ContextKind(str, Enum):
    RULES = "rules"
    SKILLS = "skills"
    WORKSPACE_ATTACHMENTS = "workspace_attachments"
    TOOL_RESULTS = "tool_results"
    INTERMEDIATE_RESULTS = "intermediate_results"


#: Higher number = kept first.
CONTEXT_PRIORITY: Dict[ContextKind, int] = {
    ContextKind.RULES: 1000,
    ContextKind.SKILLS: 400,
    ContextKind.WORKSPACE_ATTACHMENTS: 300,
    ContextKind.TOOL_RESULTS: 200,
    ContextKind.INTERMEDIATE_RESULTS: 100,
}

#: Per-kind quota shares of the usable window (sums to 1.0).
DEFAULT_SHARES: Dict[ContextKind, float] = {
    ContextKind.RULES: 0.20,
    ContextKind.SKILLS: 0.20,
    ContextKind.WORKSPACE_ATTACHMENTS: 0.25,
    ContextKind.TOOL_RESULTS: 0.25,
    ContextKind.INTERMEDIATE_RESULTS: 0.10,
}

TRUNCATION_MARKER = "\n…[truncated {tokens} tokens]…\n"


def estimate_tokens(text: str) -> int:
    """Deterministic approximation: 4 characters per token, minimum 1."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


class BudgetExceeded(RuntimeError):
    def __init__(self, code: str, limit: int, used: int) -> None:
        self.code = code
        self.limit = limit
        self.used = used
        super().__init__(f"{code}: used={used} limit={limit}")


class ContextSegment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ContextKind
    content: str = ""
    ref: Optional[str] = None
    tokens: int = 0
    required: bool = False

    @field_validator("tokens")
    @classmethod
    def _tokens(cls, value: int) -> int:
        if value < 0:
            raise ValueError("negative_token_count")
        return int(value)

    def with_tokens(self, tokens: int) -> "ContextSegment":
        return self.model_copy(update={"tokens": int(tokens)})


class SegmentAllocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ContextKind
    ref: Optional[str] = None
    original_tokens: int = 0
    allocated_tokens: int = 0
    status: str = "kept"  # kept | truncated | dropped
    content: str = ""


class BudgetPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    budget_tokens: int
    used_tokens: int = 0
    remaining_tokens: int = 0
    allocations: List[SegmentAllocation] = Field(default_factory=list)
    dropped_refs: List[str] = Field(default_factory=list)
    truncated_refs: List[str] = Field(default_factory=list)
    over_budget: bool = False

    @property
    def kept(self) -> List[SegmentAllocation]:
        return [item for item in self.allocations if item.status != "dropped"]

    def text(self) -> str:
        return "\n\n".join(item.content for item in self.kept if item.content)


class CompactionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    tokens: int
    plan: BudgetPlan
    dropped_count: int = 0
    truncated_count: int = 0


class ToolSchemaSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schemas: List[Dict[str, Any]] = Field(default_factory=list)
    tokens: int = 0
    selected: Tuple[str, ...] = ()
    omitted: Tuple[str, ...] = ()


class ContextBudgetManager:
    """Allocates a finite context window and enforces the execution budget."""

    def __init__(
        self,
        max_context_tokens: int = 12_000,
        max_step_budget: int = 10,
        max_tool_calls: int = 6,
        *,
        reserved_output_tokens: int = 0,
        shares: Optional[Mapping[ContextKind, float]] = None,
        token_estimator: Optional[Callable[[str], int]] = None,
    ) -> None:
        if max_context_tokens <= 0:
            raise ValueError("max_context_tokens_must_be_positive")
        self.max_context_tokens = int(max_context_tokens)
        self.max_step_budget = max(1, int(max_step_budget))
        self.max_tool_calls = max(0, int(max_tool_calls))
        self.reserved_output_tokens = max(0, int(reserved_output_tokens))
        self._estimate = token_estimator or estimate_tokens
        self.shares: Dict[ContextKind, float] = dict(DEFAULT_SHARES)
        if shares:
            for raw_kind, share in shares.items():
                self.shares[ContextKind(raw_kind)] = max(0.0, float(share))
        total = sum(self.shares.values()) or 1.0
        self.shares = {kind: value / total for kind, value in self.shares.items()}
        self.steps_used = 0
        self.tool_calls_used = 0

    # -- measurement -------------------------------------------------------------------
    @property
    def usable_tokens(self) -> int:
        return max(1, self.max_context_tokens - self.reserved_output_tokens)

    def count(self, text: str) -> int:
        return self._estimate(text or "")

    def _prepare(self, segments: Iterable[ContextSegment]) -> List[ContextSegment]:
        prepared: List[ContextSegment] = []
        for segment in segments:
            tokens = segment.tokens or self.count(segment.content)
            prepared.append(segment.with_tokens(tokens))
        return prepared

    def total_tokens(self, segments: Iterable[ContextSegment]) -> int:
        return sum(segment.tokens or self.count(segment.content) for segment in segments)

    # -- allocation --------------------------------------------------------------------
    def allocate(self, segments: Sequence[ContextSegment], *, limit_tokens: Optional[int] = None) -> BudgetPlan:
        """Allocate the window: rules reserved first, then per-kind quotas, then leftover.

        ``limit_tokens`` temporarily tightens the window for this call only, so a shared
        manager can serve concurrent requests without mutating its own limits.
        """
        prepared = self._prepare(segments)
        budget = self.usable_tokens
        if limit_tokens is not None:
            if limit_tokens <= 0:
                raise ValueError("limit_tokens_must_be_positive")
            budget = min(budget, int(limit_tokens))
        allocations: List[Optional[SegmentAllocation]] = [None] * len(prepared)
        remaining = budget

        # Pass 1: rules are the system's own instruction layer — never dropped.
        for index, segment in enumerate(prepared):
            if segment.kind is not ContextKind.RULES:
                continue
            take = min(segment.tokens, remaining)
            status = "kept" if take == segment.tokens else "truncated"
            content = segment.content if status == "kept" else _truncate(segment.content, take)
            allocations[index] = SegmentAllocation(
                kind=segment.kind, ref=segment.ref, original_tokens=segment.tokens,
                allocated_tokens=take, status=status, content=content,
            )
            remaining -= take

        # Pass 2: every other kind gets at most its quota.
        quotas = {kind: int(budget * share) for kind, share in self.shares.items()}
        for kind in sorted(
            (kind for kind in ContextKind if kind is not ContextKind.RULES),
            key=lambda kind: -CONTEXT_PRIORITY[kind],
        ):
            quota = min(quotas.get(kind, 0), remaining)
            used_for_kind = 0
            for index, segment in enumerate(prepared):
                if allocations[index] is not None or segment.kind is not kind:
                    continue
                take = min(segment.tokens, max(0, quota - used_for_kind), remaining)
                if take <= 0:
                    continue
                status = "kept" if take == segment.tokens else "truncated"
                allocations[index] = SegmentAllocation(
                    kind=segment.kind, ref=segment.ref, original_tokens=segment.tokens,
                    allocated_tokens=take, status=status,
                    content=segment.content if status == "kept" else _truncate(segment.content, take),
                )
                used_for_kind += take
                remaining -= take

        # Pass 3: leftover goes to whatever is still unmet, in priority order.
        for kind in sorted(
            (kind for kind in ContextKind if kind is not ContextKind.RULES),
            key=lambda kind: -CONTEXT_PRIORITY[kind],
        ):
            for index, segment in enumerate(prepared):
                if allocations[index] is not None or segment.kind is not kind:
                    continue
                if remaining <= 0:
                    break
                take = min(segment.tokens, remaining)
                status = "kept" if take == segment.tokens else "truncated"
                allocations[index] = SegmentAllocation(
                    kind=segment.kind, ref=segment.ref, original_tokens=segment.tokens,
                    allocated_tokens=take, status=status,
                    content=segment.content if status == "kept" else _truncate(segment.content, take),
                )
                remaining -= take

        over_budget = False
        dropped_refs: List[str] = []
        truncated_refs: List[str] = []
        used = budget - remaining
        for index, segment in enumerate(prepared):
            if allocations[index] is not None:
                if allocations[index].status == "truncated":
                    truncated_refs.append(segment.ref or f"{segment.kind.value}:{index}")
                    over_budget = over_budget or segment.required
                continue
            allocations[index] = SegmentAllocation(
                kind=segment.kind, ref=segment.ref, original_tokens=segment.tokens,
                allocated_tokens=0, status="dropped", content="",
            )
            dropped_refs.append(segment.ref or f"{segment.kind.value}:{index}")
            over_budget = over_budget or segment.required

        return BudgetPlan(
            budget_tokens=budget,
            used_tokens=used,
            remaining_tokens=max(0, remaining),
            allocations=[item for item in allocations if item is not None],
            dropped_refs=dropped_refs,
            truncated_refs=truncated_refs,
            over_budget=over_budget,
        )

    def fits(self, segments: Sequence[ContextSegment]) -> bool:
        return self.total_tokens(segments) <= self.usable_tokens

    # -- compaction --------------------------------------------------------------------
    def compact_context(
        self,
        segments: Sequence[ContextSegment],
        *,
        limit_tokens: Optional[int] = None,
    ) -> CompactionResult:
        """Truncate/drop lowest-priority content until the window fits."""
        plan = self.allocate(segments, limit_tokens=limit_tokens)
        blocks: List[str] = []
        for allocation in plan.kept:
            header = f"## {allocation.kind.value}" + (f" ({allocation.ref})" if allocation.ref else "")
            blocks.append(f"{header}\n{allocation.content}".rstrip())
        return CompactionResult(
            text="\n\n".join(blocks),
            tokens=plan.used_tokens,
            plan=plan,
            dropped_count=len(plan.dropped_refs),
            truncated_count=len(plan.truncated_refs),
        )

    # -- lazy tool schema exposure -----------------------------------------------------
    def skill_segments(self, engine: Any, names: Sequence[str]) -> List[ContextSegment]:
        """Materialise only the selected skills as context segments.

        Discovery never loads a body; this is the point where the chosen skills (and only
        those) become prompt content.
        """
        segments: List[ContextSegment] = []
        for name in names:
            skill = engine.load_skill(name)
            content = f"# {skill.name}@{skill.manifest.version}\n{skill.instructions}"
            segments.append(ContextSegment(
                kind=ContextKind.SKILLS, content=content, ref=skill.name, tokens=self.count(content),
            ))
        return segments

    def select_tool_schemas(
        self,
        registry: Any,
        selected: Sequence[str],
        *,
        limit_tokens: Optional[int] = None,
    ) -> ToolSchemaSelection:
        """Expose only the schemas the current step needs."""
        specs = registry.select(selected) if hasattr(registry, "select") else []
        schemas = [spec.schema_payload() for spec in specs]
        omitted = tuple(name for name in selected if name not in {spec.qualified_name for spec in specs})
        if limit_tokens is not None:
            kept: List[Dict[str, Any]] = []
            used = 0
            for schema in schemas:
                cost = self.count(json.dumps(schema, ensure_ascii=False))
                if used + cost > limit_tokens:
                    omitted = omitted + (schema["function"]["name"],)
                    continue
                kept.append(schema)
                used += cost
            schemas = kept
        tokens = sum(self.count(json.dumps(schema, ensure_ascii=False)) for schema in schemas)
        return ToolSchemaSelection(
            schemas=schemas,
            tokens=tokens,
            selected=tuple(schema["function"]["name"] for schema in schemas),
            omitted=omitted,
        )

    # -- runtime scope -----------------------------------------------------------------
    def guard_step(self, step_index: Optional[int] = None) -> int:
        """Register one planning step; raises when ``max_step_budget`` is exceeded."""
        if step_index is not None:
            if step_index > self.max_step_budget:
                raise BudgetExceeded("max_step_budget_exceeded", self.max_step_budget, step_index)
            self.steps_used = max(self.steps_used, int(step_index))
            return self.steps_used
        if self.steps_used >= self.max_step_budget:
            raise BudgetExceeded("max_step_budget_exceeded", self.max_step_budget, self.steps_used + 1)
        self.steps_used += 1
        return self.steps_used

    def guard_tool_call(self, count: int = 1) -> int:
        """Register tool calls; raises when ``max_tool_calls`` would be exceeded."""
        if count <= 0:
            raise ValueError("count_must_be_positive")
        if self.tool_calls_used + count > self.max_tool_calls:
            raise BudgetExceeded(
                "max_tool_calls_exceeded", self.max_tool_calls, self.tool_calls_used + count,
            )
        self.tool_calls_used += count
        return self.tool_calls_used

    def snapshot(self) -> Dict[str, Any]:
        return {
            "max_context_tokens": self.max_context_tokens,
            "usable_tokens": self.usable_tokens,
            "steps_used": self.steps_used,
            "max_step_budget": self.max_step_budget,
            "tool_calls_used": self.tool_calls_used,
            "max_tool_calls": self.max_tool_calls,
        }

    def reset(self) -> None:
        self.steps_used = 0
        self.tool_calls_used = 0


def _truncate(text: str, tokens: int) -> str:
    """Keep the head and tail of a block so the model still sees its boundaries."""
    if tokens <= 0:
        return ""
    chars = tokens * 4
    if len(text) <= chars:
        return text
    head = text[: int(chars * 0.7)]
    tail = text[-int(chars * 0.2):] if chars > 20 else ""
    removed_tokens = max(1, estimate_tokens(text) - tokens)
    return f"{head}{TRUNCATION_MARKER.format(tokens=removed_tokens)}{tail}"
