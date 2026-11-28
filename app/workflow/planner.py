"""WP-01 R3 Planner — routing decision, execution plan and DAG contract.

The planner is the only component that decides *whether* a request becomes a workflow.
Two routes exist and they are not interchangeable:

``direct_chat``
    The request is answered by the ordinary chat path.  Nothing else happens: no model
    call, no skill discovery/loading, no workflow graph execution (Acceptance Scenario H).
    The planner returns before touching any of those components, so the zero-call
    guarantee is structural, not a matter of the caller remembering to skip work.

``workflow``
    A deterministic :class:`ExecutionPlan` is produced from the capabilities the caller
    may use, validated as a DAG (no cycles, no dangling dependencies), bounded by the
    step/token budget, and handed to :mod:`app.workflow.runtime`.

Design notes
------------
* Planning is *model-free* by default.  ``Planner(model=...)`` accepts an optional
  callable for future model-assisted planning, and ``model_calls`` counts its use so the
  fast-path tests can prove it stays at zero.
* ``input_bindings`` is a small, explicit binding language (see
  :func:`resolve_bindings`): ``$request.*``, ``$context.*``, ``$scope.*``,
  ``$outputs.<key>[.path]``, ``$steps.<step_id>[.path]``, ``$rules.<key>`` and the
  ``{"$ref": ...}`` / ``{"$template": ..., "vars": ...}`` operators.  Nothing is
  evaluated as code, so a plan can be stored, replayed and audited safely.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "PlannerError",
    "BindingResolutionError",
    "PlanStep",
    "ExecutionPlan",
    "PlannerRequest",
    "PlannerDecision",
    "Planner",
    "resolve_bindings",
    "validate_plan_dag",
]

_PLAN_ID_RE = re.compile(r"^plan_[A-Za-z0-9_\-]{4,80}$")
_STEP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
_REF_RE = re.compile(r"^\$(request|context|scope|outputs|steps|rules)(\.[A-Za-z0-9_\-.\[\]]+)?$")


class PlannerError(RuntimeError):
    """A request cannot be planned inside the declared contract limits."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


class BindingResolutionError(PlannerError):
    """An ``input_bindings`` reference could not be resolved."""

    def __init__(self, reference: str, detail: str = "") -> None:
        self.reference = reference
        super().__init__("binding_unresolved", f"{reference}{': ' + detail if detail else ''}")


# --------------------------------------------------------------------------------------
# Plan model
# --------------------------------------------------------------------------------------
class PlanStep(BaseModel):
    """One executable unit of a plan.

    The field set is the WP-01 contract.  ``condition`` is the only additive field: it
    enables conditional branch/skip semantics without inventing a second plan format.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    name: str = ""
    capability: str
    tool_name: Optional[str] = None
    skill_name: Optional[str] = None
    depends_on: Tuple[str, ...] = ()
    parallel_group: Optional[str] = None
    input_bindings: Dict[str, Any] = Field(default_factory=dict)
    output_key: Optional[str] = None
    approval_required: bool = False
    max_retries: int = 0
    timeout_seconds: float = 30.0
    condition: Optional[str] = None

    @field_validator("step_id")
    @classmethod
    def _step_id(cls, value: str) -> str:
        if not _STEP_ID_RE.match(value or ""):
            raise ValueError("invalid_step_id")
        return value

    @field_validator("capability")
    @classmethod
    def _capability(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned or len(cleaned) > 128:
            raise ValueError("invalid_capability")
        return cleaned

    @field_validator("depends_on", mode="before")
    @classmethod
    def _depends_on(cls, value: Any) -> Tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(str(item) for item in value)

    @field_validator("max_retries")
    @classmethod
    def _retries(cls, value: int) -> int:
        if int(value) < 0 or int(value) > 10:
            raise ValueError("invalid_step_retries")
        return int(value)

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout(cls, value: float) -> float:
        if value is None or not (0 < float(value) <= 600):
            raise ValueError("invalid_step_timeout")
        return float(value)

    @model_validator(mode="after")
    def _no_self_dependency(self) -> "PlanStep":
        if self.step_id in self.depends_on:
            raise ValueError(f"self_dependency: {self.step_id}")
        return self

    @property
    def key(self) -> str:
        """The key an output is stored under (explicit ``output_key`` wins)."""
        return self.output_key or self.step_id


class ExecutionPlan(BaseModel):
    """A validated, bounded DAG of :class:`PlanStep` objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str
    goal: str = ""
    steps: Tuple[PlanStep, ...] = ()
    max_steps: int = 10
    max_retries: int = 2
    estimated_tokens: int = 0
    replan_on_failure: bool = True
    version: int = 1
    parent_plan_id: Optional[str] = None
    notes: Tuple[str, ...] = ()

    @field_validator("plan_id")
    @classmethod
    def _plan_id(cls, value: str) -> str:
        if not _PLAN_ID_RE.match(value or ""):
            raise ValueError("invalid_plan_id")
        return value

    @field_validator("steps", mode="before")
    @classmethod
    def _steps(cls, value: Any) -> Tuple[Any, ...]:
        return tuple(value or ())

    @field_validator("max_steps")
    @classmethod
    def _max_steps(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError("invalid_max_steps")
        return int(value)

    @model_validator(mode="after")
    def _validate_shape(self) -> "ExecutionPlan":
        validate_plan_dag(self.steps, max_steps=self.max_steps)
        return self

    # -- helpers ----------------------------------------------------------------------
    def step_map(self) -> Dict[str, PlanStep]:
        return {step.step_id: step for step in self.steps}

    def step(self, step_id: str) -> PlanStep:
        found = self.step_map().get(step_id)
        if found is None:
            raise PlannerError("unknown_step", step_id)
        return found

    def output_keys(self) -> Dict[str, str]:
        """``output_key -> step_id``."""
        return {step.key: step.step_id for step in self.steps}

    def roots(self) -> List[PlanStep]:
        return [step for step in self.steps if not step.depends_on]

    def topological_order(self) -> List[PlanStep]:
        return [self.step_map()[step_id] for step_id in _topological_ids(self.steps)]

    def parallel_groups(self) -> Dict[str, List[str]]:
        groups: Dict[str, List[str]] = {}
        for step in self.steps:
            if step.parallel_group:
                groups.setdefault(step.parallel_group, []).append(step.step_id)
        return groups

    def capabilities(self) -> List[str]:
        seen: List[str] = []
        for step in self.steps:
            if step.capability not in seen:
                seen.append(step.capability)
        return seen

    def summary(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "parent_plan_id": self.parent_plan_id,
            "goal": self.goal,
            "step_count": len(self.steps),
            "steps": [step.step_id for step in self.steps],
            "capabilities": self.capabilities(),
            "parallel_groups": self.parallel_groups(),
            "max_steps": self.max_steps,
            "max_retries": self.max_retries,
            "estimated_tokens": self.estimated_tokens,
            "replan_on_failure": self.replan_on_failure,
        }

    def remaining(self, completed: Iterable[str]) -> List[PlanStep]:
        done = set(completed)
        return [step for step in self.steps if step.step_id not in done]


def _topological_ids(steps: Sequence[PlanStep]) -> List[str]:
    """Kahn's algorithm over the step graph; raises on a cycle or dangling dependency."""
    order_index = {step.step_id: index for index, step in enumerate(steps)}
    indegree = {step.step_id: 0 for step in steps}
    children: Dict[str, List[str]] = {step.step_id: [] for step in steps}
    for step in steps:
        for dependency in step.depends_on:
            if dependency not in indegree:
                raise PlannerError("dangling_dependency", f"{step.step_id}->{dependency}")
            indegree[step.step_id] += 1
            children[dependency].append(step.step_id)
    ready = sorted((sid for sid, deg in indegree.items() if deg == 0), key=lambda sid: order_index[sid])
    ordered: List[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort(key=lambda sid: order_index[sid])
    if len(ordered) != len(steps):
        cyclic = sorted(sid for sid, deg in indegree.items() if deg > 0)
        raise PlannerError("cyclic_dependency", ",".join(cyclic))
    return ordered


def validate_plan_dag(steps: Sequence[PlanStep], *, max_steps: Optional[int] = None) -> bool:
    """Validate ids, dependencies, cycles, parallel groups and the step budget."""
    if max_steps is not None and len(steps) > int(max_steps):
        raise PlannerError("max_steps_exceeded", f"steps={len(steps)} max_steps={max_steps}")
    ids = [step.step_id for step in steps]
    duplicates = sorted({sid for sid in ids if ids.count(sid) > 1})
    if duplicates:
        raise PlannerError("duplicate_step_id", ",".join(duplicates))
    _topological_ids(steps)
    # Steps that are explicitly parallel must not depend on each other.
    groups: Dict[str, set] = {}
    for step in steps:
        if step.parallel_group:
            groups.setdefault(step.parallel_group, set()).add(step.step_id)
    for group, members in groups.items():
        for step in steps:
            if step.step_id in members and any(dep in members for dep in step.depends_on):
                raise PlannerError("parallel_group_has_dependency", f"{group}:{step.step_id}")
    return True


# --------------------------------------------------------------------------------------
# Binding language
# --------------------------------------------------------------------------------------
_OPERATORS = ("$ref", "$template")


def _dig(value: Any, path: Sequence[str]) -> Any:
    current = value
    for part in path:
        if isinstance(current, Mapping):
            if part not in current:
                raise KeyError(part)
            current = current[part]
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise KeyError(part) from exc
        else:
            raise KeyError(part)
    return current


def _split_reference(reference: str) -> Tuple[str, List[str]]:
    match = _REF_RE.match(reference)
    if not match:
        raise BindingResolutionError(reference, "malformed_reference")
    root, rest = match.group(1), match.group(2) or ""
    parts: List[str] = []
    for chunk in rest.lstrip(".").split("."):
        if not chunk:
            continue
        # ``hits[0]`` style indexes are accepted; nothing else is.
        if chunk.endswith("]") and "[" in chunk:
            name, _, index = chunk.partition("[")
            if name:
                parts.append(name)
            parts.append(index[:-1])
        else:
            parts.append(chunk)
    return root, parts


def _lookup(root: str, parts: Sequence[str], resolver: "BindingResolver") -> Any:
    if root == "request":
        return _dig(resolver.request, parts)
    if root == "context":
        return _dig(resolver.context, parts)
    if root == "scope":
        return _dig(resolver.scope, parts)
    if root == "outputs":
        return _dig(resolver.outputs, parts)
    if root == "steps":
        return _dig(resolver.step_outputs, parts)
    if root == "rules":
        # Rule keys are flat dotted identifiers (``report.output_path``), not nested maps.
        flat = ".".join(parts)
        if flat in resolver.rules:
            return resolver.rules[flat]
        return _dig(resolver.rules, parts)
    raise BindingResolutionError(f"${root}", "unknown_root")


class BindingResolver:
    """Read-only view a plan binds against (never exposes callables)."""

    def __init__(
        self,
        *,
        request: Optional[Mapping[str, Any]] = None,
        context: Optional[Mapping[str, Any]] = None,
        scope: Optional[Mapping[str, Any]] = None,
        outputs: Optional[Mapping[str, Any]] = None,
        step_outputs: Optional[Mapping[str, Any]] = None,
        rules: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.request = dict(request or {})
        self.context = dict(context or {})
        self.scope = dict(scope or {})
        self.outputs = dict(outputs or {})
        self.step_outputs = dict(step_outputs or {})
        self.rules = dict(rules or {})

    def resolve(self, reference: str) -> Any:
        root, parts = _split_reference(reference)
        try:
            return _lookup(root, parts, self)
        except KeyError as exc:
            raise BindingResolutionError(reference, f"missing:{exc.args[0] if exc.args else 'path'}") from exc
        except (TypeError, ValueError) as exc:
            raise BindingResolutionError(reference, str(exc)) from exc


def resolve_bindings(bindings: Optional[Mapping[str, Any]], resolver: BindingResolver) -> Dict[str, Any]:
    """Resolve every value of an ``input_bindings`` mapping (recursively)."""

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            if value.startswith("$"):
                return resolver.resolve(value)
            return value
        if isinstance(value, Mapping):
            keys = set(value)
            if keys == {"$ref"}:
                return resolver.resolve(str(value["$ref"]))
            if keys == {"$template"} or keys == {"$template", "vars"}:
                template = str(value["$template"])
                raw_vars = value.get("vars") or {}
                if not isinstance(raw_vars, Mapping):
                    raise BindingResolutionError("$template", "vars_must_be_object")
                rendered = {str(name): walk(item) for name, item in raw_vars.items()}
                try:
                    return template.format(**rendered)
                except (KeyError, IndexError, ValueError) as exc:
                    raise BindingResolutionError("$template", str(exc)) from exc
            if "$ref" in keys or "$template" in keys:
                raise BindingResolutionError("$operator", f"unsupported_operator_combo:{sorted(keys)}")
            return {str(key): walk(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(item) for item in value]
        return value

    return {str(key): walk(value) for key, value in (bindings or {}).items()}


# --------------------------------------------------------------------------------------
# Request / decision
# --------------------------------------------------------------------------------------
class PlannerRequest(BaseModel):
    """Everything the planner is allowed to look at."""

    model_config = ConfigDict(extra="ignore")

    query: str = ""
    context: Dict[str, Any] = Field(default_factory=dict)
    budget: Dict[str, Any] = Field(default_factory=dict)
    execution_mode: Literal["auto", "direct", "workflow"] = "auto"
    available_capabilities: Tuple[str, ...] = ()

    @field_validator("available_capabilities", mode="before")
    @classmethod
    def _caps(cls, value: Any) -> Tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(dict.fromkeys(str(item) for item in value))


class PlannerDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    route: Literal["direct_chat", "workflow"]
    reason: str
    plan: Optional[ExecutionPlan] = None

    def explain(self) -> Dict[str, Any]:
        return {
            "route": self.route,
            "reason": self.reason,
            "plan": self.plan.summary() if self.plan else None,
        }


# --------------------------------------------------------------------------------------
# Capability recipes
# --------------------------------------------------------------------------------------
#: Canonical execution order used when a plan is derived from a capability set.
CAPABILITY_ORDER: Tuple[str, ...] = (
    "workspace.rag.search",
    "office.xlsx.analyze",
    "office.docx.compare",
    "office.pptx.generate",
    "workspace.file.write",
)

#: The five-capability document production pipeline (Acceptance Scenario A).
DOCUMENT_PIPELINE: Tuple[str, ...] = CAPABILITY_ORDER

#: Capabilities replaced when replanning after a non-retryable failure.
REPLAN_FALLBACKS: Dict[str, str] = {
    "office.pptx.generate": "workspace.file.write",
}

#: Error codes that a single bounded retry cannot fix.
NON_RETRYABLE_CODES = frozenset(
    {
        "file_not_found",
        "invalid_table_payload",
        "path_escape",
        "workspace_required",
        "missing_parameter",
        "invalid_parameters",
        "title_required",
        "unknown_capability",
        "tool_not_found",
        "mcp_tool_denied",
        "mcp_tool_not_allowlisted",
        "permission_denied",
        "scope_mismatch",
        "unscoped_resource",
        "prompt_injection_detected",
    }
)

_WORKFLOW_HINTS: Tuple[str, ...] = (
    "작성", "만들", "생성", "분석", "비교", "정리", "보고서", "슬라이드", "문서",
    "workflow", "report", "analy", "compar", "generate", "build", "draft", "summar",
)


def _step_key(capability: str) -> str:
    tail = capability.split(".")[-1]
    return re.sub(r"[^a-z0-9_]", "_", tail.lower())


def _params(request: PlannerRequest, capability: str) -> Dict[str, Any]:
    params = request.context.get("params") or {}
    if not isinstance(params, Mapping):
        return {}
    value = params.get(capability) or {}
    return dict(value) if isinstance(value, Mapping) else {}


def _document_pipeline(request: PlannerRequest) -> List[PlanStep]:
    rag = _params(request, "workspace.rag.search")
    xlsx = _params(request, "office.xlsx.analyze")
    docx = _params(request, "office.docx.compare")
    pptx = _params(request, "office.pptx.generate")
    write = _params(request, "workspace.file.write")

    return [
        PlanStep(
            step_id="s1_research",
            name="Search the workspace",
            capability="workspace.rag.search",
            skill_name="workspace-research",
            input_bindings={
                "workspace_id": rag.get("workspace_id", "$scope.workspace_id"),
                "query": rag.get("query", "$request.query"),
                "limit": rag.get("limit", 5),
            },
            output_key="research",
            approval_required=bool(rag.get("approval_required", False)),
            timeout_seconds=float(rag.get("timeout_seconds", 30)),
        ),
        PlanStep(
            step_id="s2_table",
            name="Analyse the spreadsheet",
            capability="office.xlsx.analyze",
            skill_name="spreadsheet-analysis",
            depends_on=("s1_research",),
            parallel_group="analysis",
            input_bindings={
                "file_ref": xlsx.get("file_ref", "$context.inputs.table_ref"),
                "operations": xlsx.get("operations", ["summary"]),
            },
            output_key="table",
            approval_required=bool(xlsx.get("approval_required", False)),
            max_retries=int(xlsx.get("max_retries", 1)),
            timeout_seconds=float(xlsx.get("timeout_seconds", 30)),
        ),
        PlanStep(
            step_id="s3_diff",
            name="Compare the documents",
            capability="office.docx.compare",
            skill_name="document-comparison",
            depends_on=("s1_research",),
            parallel_group="analysis",
            input_bindings={
                "base_ref": docx.get("base_ref", "$context.inputs.base_doc"),
                "target_ref": docx.get("target_ref", "$context.inputs.target_doc"),
                "granularity": docx.get("granularity", "paragraph"),
            },
            output_key="diff",
            approval_required=bool(docx.get("approval_required", False)),
            max_retries=int(docx.get("max_retries", 1)),
            timeout_seconds=float(docx.get("timeout_seconds", 30)),
        ),
        PlanStep(
            step_id="s4_deck",
            name="Generate the deck",
            capability="office.pptx.generate",
            skill_name="presentation-generation",
            depends_on=("s2_table", "s3_diff"),
            input_bindings={
                "title": pptx.get("title", "$request.query"),
                "slides": pptx.get("slides", {"$ref": "$outputs.research.hits"}),
                "template_ref": pptx.get("template_ref"),
            },
            output_key="deck",
            approval_required=bool(pptx.get("approval_required", False)),
            max_retries=int(pptx.get("max_retries", 1)),
            timeout_seconds=float(pptx.get("timeout_seconds", 30)),
        ),
        PlanStep(
            step_id="s5_report",
            name="Write the report",
            capability="workspace.file.write",
            skill_name="report-generation",
            depends_on=("s4_deck",),
            input_bindings={
                "workspace_id": write.get("workspace_id", "$scope.workspace_id"),
                "path": write.get("path", "reports/report.md"),
                "content": write.get(
                    "content",
                    {
                        "$template": (
                            "# {title}\n\n"
                            "- research hits: {hits}\n"
                            "- spreadsheet rows: {rows}\n"
                            "- document changes: {changes}\n"
                            "- deck: {deck}\n"
                        ),
                        "vars": {
                            "title": "$request.query",
                            "hits": "$outputs.research.hit_count",
                            "rows": "$outputs.table.row_count",
                            "changes": "$outputs.diff.added_count",
                            "deck": "$outputs.deck.artifact_id",
                        },
                    },
                ),
                "overwrite": write.get("overwrite", True),
            },
            output_key="report",
            approval_required=bool(write.get("approval_required", False)),
            timeout_seconds=float(write.get("timeout_seconds", 30)),
        ),
    ]


def _capability_chain(request: PlannerRequest, capabilities: Sequence[str]) -> List[PlanStep]:
    steps: List[PlanStep] = []
    previous: Optional[str] = None
    # Planner-only keys never reach the capability adapter as a parameter.
    reserved = ("tool_name", "skill_name", "output_key", "approval_required", "max_retries", "timeout_seconds")
    for index, capability in enumerate(capabilities, start=1):
        params = _params(request, capability)
        bindings = {key: value for key, value in params.items() if key not in reserved}
        # A workspace-scoped capability always runs inside the caller's workspace; the
        # binding is explicit so the runtime's scope check still sees the value.
        if capability.startswith("workspace.") and "workspace_id" not in bindings:
            bindings["workspace_id"] = "$scope.workspace_id"
        step = PlanStep(
            step_id=f"s{index}_{_step_key(capability)}",
            name=f"Run {capability}",
            capability=capability,
            tool_name=params.get("tool_name"),
            skill_name=params.get("skill_name"),
            depends_on=(previous,) if previous else (),
            input_bindings=bindings,
            output_key=params.get("output_key", _step_key(capability)),
            approval_required=bool(params.get("approval_required", False)),
            max_retries=int(params.get("max_retries", 1)),
            timeout_seconds=float(params.get("timeout_seconds", 30)),
        )
        steps.append(step)
        previous = step.step_id
    return steps


# --------------------------------------------------------------------------------------
# Planner
# --------------------------------------------------------------------------------------
class Planner:
    """Route selection and deterministic plan construction."""

    def __init__(
        self,
        *,
        model: Optional[Any] = None,
        skills: Optional[Any] = None,
        registry: Optional[Any] = None,
        rules: Optional[Any] = None,
        max_steps: int = 10,
        max_retries: int = 2,
        max_replans: int = 1,
        max_estimated_tokens: int = 12_000,
        token_estimator: Optional[Any] = None,
    ) -> None:
        self.model = model
        self.skills = skills
        self.registry = registry
        self.rules = rules
        self.max_steps = max(1, int(max_steps))
        self.max_retries = max(0, int(max_retries))
        self.max_replans = max(0, int(max_replans))
        self.max_estimated_tokens = max(1, int(max_estimated_tokens))
        self._count_tokens = token_estimator or (lambda text: max(1, (len(text) + 3) // 4))
        #: Observability counters — the fast path must leave every one of them at zero.
        self.model_calls = 0
        self.skill_loads = 0
        self.graph_runs = 0
        self.plans_built = 0
        self.replans_built = 0
        self.last_reason = ""

    # -- routing ----------------------------------------------------------------------
    def plan(self, request: PlannerRequest) -> PlannerDecision:
        mode = request.execution_mode
        capabilities = tuple(request.available_capabilities)

        if mode == "direct":
            return self._direct("explicit_direct_mode")

        if mode == "auto":
            if not capabilities:
                return self._direct("fast_path_no_capability_required")
            if not self._looks_like_work(request):
                return self._direct("fast_path_simple_query")

        if not capabilities:
            raise PlannerError("no_available_capability", "workflow mode requires at least one capability")

        plan = self.build_plan(request)
        self.last_reason = "workflow_planned" if mode == "workflow" else "workflow_capability_signal"
        return PlannerDecision(route="workflow", reason=self.last_reason, plan=plan)

    def _direct(self, reason: str) -> PlannerDecision:
        self.last_reason = reason
        return PlannerDecision(route="direct_chat", reason=reason, plan=None)

    @staticmethod
    def _looks_like_work(request: PlannerRequest) -> bool:
        context = request.context or {}
        if context.get("force_workflow") is True or context.get("requires_tools") is True:
            return True
        if context.get("params"):
            return True
        query = (request.query or "").lower()
        return any(hint in query for hint in _WORKFLOW_HINTS)

    # -- planning ---------------------------------------------------------------------
    def build_plan(self, request: PlannerRequest) -> ExecutionPlan:
        capabilities = list(request.available_capabilities)
        budget = request.budget or {}
        max_steps = int(budget.get("max_steps", self.max_steps))
        max_retries = int(budget.get("max_retries", self.max_retries))
        token_limit = int(budget.get("max_estimated_tokens", self.max_estimated_tokens))

        if set(DOCUMENT_PIPELINE) <= set(capabilities):
            steps = _document_pipeline(request)
        else:
            ordered = [capability for capability in CAPABILITY_ORDER if capability in capabilities]
            ordered += [capability for capability in capabilities if capability not in ordered]
            steps = _capability_chain(request, ordered)

        validate_plan_dag(steps, max_steps=max_steps)
        estimated = self._estimate(request, steps)
        if estimated > token_limit:
            raise PlannerError("token_budget_exceeded", f"estimated={estimated} limit={token_limit}")

        plan = ExecutionPlan(
            plan_id=f"plan_{uuid.uuid4().hex[:16]}",
            goal=(request.query or "workflow")[:280],
            steps=tuple(steps),
            max_steps=max_steps,
            max_retries=max_retries,
            estimated_tokens=estimated,
            replan_on_failure=bool(budget.get("replan_on_failure", True)),
        )
        self.plans_built += 1
        return plan

    def _estimate(self, request: PlannerRequest, steps: Sequence[PlanStep]) -> int:
        payload = json.dumps(
            {
                "query": request.query,
                "context": request.context,
                "steps": [step.model_dump(mode="json") for step in steps],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return int(self._count_tokens(payload))

    # -- replanning -------------------------------------------------------------------
    @staticmethod
    def is_retryable(error_code: str) -> bool:
        return (error_code or "").strip().lower() not in NON_RETRYABLE_CODES

    def replan(
        self,
        plan: ExecutionPlan,
        *,
        failed_step_id: str,
        error_code: str,
        completed_steps: Iterable[str] = (),
        replans_used: int = 0,
    ) -> ExecutionPlan:
        """Build the next plan version after ``failed_step_id`` exhausted its retries.

        Completed steps are dropped (they are never re-executed), the failed step is
        replaced by its declared fallback capability when one exists, and the dependent
        steps keep pointing at the same ``output_key`` so downstream bindings still
        resolve.
        """
        if not plan.replan_on_failure:
            raise PlannerError("replanning_disabled", plan.plan_id)
        if replans_used >= self.max_replans:
            raise PlannerError("max_replans_exceeded", f"replans={replans_used} limit={self.max_replans}")

        done = set(completed_steps) | {failed_step_id}
        failed = plan.step(failed_step_id)
        fallback = REPLAN_FALLBACKS.get(failed.capability)

        replacement: Optional[PlanStep] = None
        if fallback:
            replacement = PlanStep(
                step_id=f"{failed.step_id}_fallback",
                name=f"Fallback for {failed.capability}: {fallback}",
                capability=fallback,
                tool_name=None,
                skill_name=failed.skill_name,
                depends_on=failed.depends_on,
                input_bindings={
                    "workspace_id": "$scope.workspace_id",
                    "path": "reports/fallback_report.md",
                    "content": {
                        "$template": "Fallback report for {goal}\n\nfailed capability: {capability}\nerror: {error}\n",
                        "vars": {"goal": plan.goal, "capability": failed.capability, "error": error_code},
                    },
                    "overwrite": True,
                },
                output_key=failed.key,
                approval_required=failed.approval_required,
                timeout_seconds=failed.timeout_seconds,
            )
            finished = set(done)
            pending = [step for step in plan.steps if step.step_id not in finished]
            renumbered: List[PlanStep] = []
            for step in pending:
                depends = tuple(
                    replacement.step_id if dep == failed.step_id else dep for dep in step.depends_on
                )
                renumbered.append(step.model_copy(update={"depends_on": depends}))
            steps = [replacement] + renumbered
        else:
            # No declared fallback: the failed output can never be produced, so the step
            # and everything transitively depending on it is dropped from the next plan.
            dropped = {failed_step_id}
            changed = True
            while changed:
                changed = False
                for step in plan.steps:
                    if step.step_id in dropped:
                        continue
                    if any(dep in dropped for dep in step.depends_on):
                        dropped.add(step.step_id)
                        changed = True
            steps = [step for step in plan.steps if step.step_id not in dropped]
            if not steps:
                raise PlannerError("nothing_left_to_replan", failed_step_id)

        validate_plan_dag(steps, max_steps=plan.max_steps)
        replanned = ExecutionPlan(
            plan_id=f"plan_{uuid.uuid4().hex[:16]}",
            goal=plan.goal,
            steps=tuple(steps),
            max_steps=plan.max_steps,
            max_retries=plan.max_retries,
            estimated_tokens=self._count_tokens(json.dumps([step.model_dump(mode="json") for step in steps], ensure_ascii=False)),
            replan_on_failure=plan.replan_on_failure,
            version=plan.version + 1,
            parent_plan_id=plan.plan_id,
            notes=(
                f"replan after {failed_step_id} ({failed.capability}) failed with {error_code}",
                f"fallback={fallback or 'none'}",
            ),
        )
        self.replans_built += 1
        return replanned
