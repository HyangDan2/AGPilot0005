"""Shared Model and Tool Execution Nodes for AGCore LangGraph R1 Runtime."""
import json
from typing import Any, Dict, Optional
from app.gateway.client import AIGatewayClient, gateway_client, gateway_for_service
from app.runtime.context import context_manager, is_context_overflow_error
from app.runtime.state import AgentState
from app.runtime.public_grounding import public_outline_calls, public_section_calls, public_chunk_calls, public_grounded_messages
from app.schemas.gateway import GatewayMessage, ToolCall, ToolDefinition
from app.tools.dispatcher import dispatcher
from app.tools.registry import registry
from app.adapters.document import document_owner_id, tool_token_budget
from app.core.auth import current_run_workspace_binding
from app.core.session_attachments import is_session_attachment
from app.config import settings
from app.knowledge.references import EvidenceTracker, SynthesisBudget, enrichment_query, plan_enrichment, is_workspace_reference_id


def attachment_refs(state: AgentState) -> list[str]:
    """Only the conversation-scoped attachment ids (``att-...``)."""

    return [ref for ref in (state.context_refs or []) if is_session_attachment(ref)]


def document_tool_refs(state: AgentState) -> list[str]:
    """Ids that ``document.*`` tools may use: attachments and legacy AGTools document ids.

    A Workspace file id is a different namespace — its bytes are read with ``workspace_*`` tools,
    so it never becomes a ``document.*`` argument.
    """

    return [ref for ref in (state.context_refs or []) if not is_workspace_reference_id(ref)]


def _attachment_tool_definitions(
    tools: list[ToolDefinition], refs: list[str]
) -> tuple[list[ToolDefinition], dict[str, frozenset[str]]]:
    """Use the run's authorized attachment references in model-facing schemas."""

    definitions = []
    allowed_arguments: dict[str, frozenset[str]] = {}
    for tool in tools:
        function = tool.function
        properties = function.parameters.get("properties", {})
        if not function.name.startswith("document.") or "doc_id" not in properties:
            definitions.append(tool)
            continue
        allowed_arguments[function.name] = frozenset(properties)
        properties = dict(properties)
        parameters = {**function.parameters, "properties": properties, "additionalProperties": False}
        if len(refs) == 1:
            properties.pop("doc_id")
            parameters["required"] = [key for key in parameters.get("required", []) if key != "doc_id"]
        else:
            properties["doc_id"] = {**properties["doc_id"], "enum": refs}
        definitions.append(tool.model_copy(update={"function": function.model_copy(update={"parameters": parameters})}))
    return definitions, allowed_arguments


def _bind_attachment_arguments(
    call: ToolCall, allowed_arguments: frozenset[str], attachment_id: Optional[str]
) -> ToolCall:
    """Normalize registered arguments and keep explicit IDs for scope validation."""

    raw = call.function.arguments
    try:
        arguments = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return call
    if not isinstance(arguments, dict):
        return call
    arguments = {key: value for key, value in arguments.items() if key in allowed_arguments}
    if attachment_id is not None and "doc_id" not in arguments:
        arguments["doc_id"] = attachment_id
    function = call.function.model_copy(update={"arguments": json.dumps(arguments, ensure_ascii=False)})
    return call.model_copy(update={"function": function})


def _request_text(state: AgentState) -> str:
    if state.request_message:
        return state.request_message
    for message in reversed(state.messages):
        if message.role == "user" and (message.content or "").strip():
            return message.content or ""
    return ""


def _enrichment_tool_calls(state: AgentState) -> tuple[list[ToolCall], list[str]]:
    """Bounded reads for the requested references that still have no evidence."""

    if state.evidence is None or not state.evidence.incomplete:
        return [], []
    remaining_attempts = max(0, int(settings.EVIDENCE_ENRICHMENT_LIMIT) - int(state.enrichment_attempts or 0))
    if remaining_attempts <= 0:
        return [], []
    attempted = set(state.enrichment_attempted_keys or [])
    tracker = EvidenceTracker.from_coverage(state.evidence)
    candidates = [ref for ref in tracker.missing_references() if ref.document_key not in attempted]
    if not candidates:
        return [], []
    plan = plan_enrichment(candidates[:remaining_attempts], query=enrichment_query(_request_text(state)))
    calls: list[ToolCall] = []
    keys: list[str] = []
    for index, item in enumerate(plan):
        calls.append(
            ToolCall(
                id=f"enrich-{state.step_count}-{index}",
                function={"name": item.tool_name, "arguments": json.dumps(item.arguments, ensure_ascii=False)},
            )
        )
        keys.append(item.reference_key)
    return calls, keys

async def model_node(
    state: AgentState,
    on_delta=None,
    on_reasoning_delta=None,
    *,
    context_budget_override=None,
    reasoning_effort_override=None,
    disable_tools: bool = False,
) -> Dict[str, Any]:
    """Invokes AI Gateway with current messages and tools."""
    state.step_count += 1

    if state.service_id == "hd2ai-public" and state.context_refs and state.tool_call_count == 0:
        calls = public_outline_calls(state)
        return {
            "messages": list(state.messages) + [GatewayMessage(role="assistant", content=None, tool_calls=calls)],
            "step_count": state.step_count,
            "pending_tool_calls": calls,
            "status": "running",
        }
    if state.service_id == "hd2ai-public" and state.context_refs and not any(
        message.role == "tool" and message.name in ("document.read_section", "document.search") for message in state.messages
    ):
        calls = public_section_calls(state)
        if calls:
            return {
                "messages": list(state.messages) + [GatewayMessage(role="assistant", content=None, tool_calls=calls)],
                "step_count": state.step_count,
                "pending_tool_calls": calls,
                "status": "running",
            }
    
    if (state.service_id == 'hd2ai-public' and state.context_refs
            and any(m.role == 'tool' and m.name == 'document.search' for m in state.messages)
            and not any(m.role == 'tool' and m.name == 'document.read_chunk' for m in state.messages)):
        calls = public_chunk_calls(state)
        if calls:
            return {'messages':list(state.messages)+[GatewayMessage(role='assistant',content=None,tool_calls=calls)],
                    'step_count':state.step_count,'pending_tool_calls':calls,'status':'running'}

    # 1. Dynamic budget resolution and 3-stage compaction
    from app.core.runtime_config import get_effective_route_budget
    client = state.custom_client or gateway_for_service(state.service_id, gateway_client)
    route_budget, has_contract = await get_effective_route_budget(client, state.service_id)
    effective_input_budget = route_budget.input_budget_tokens if has_contract else None
    if context_budget_override is not None:
        effective_input_budget = (
            min(effective_input_budget, int(context_budget_override))
            if effective_input_budget is not None
            else int(context_budget_override)
        )
    pruned_msgs, _report = context_manager.compact_with_report(
        state.messages,
        budget_tokens=effective_input_budget,
        route=state.service_id,
    )
    public_grounding = state.service_id == "hd2ai-public" and bool(state.context_refs)
    if public_grounding:
        max_body_chars = max(1200, int((route_budget.input_budget_tokens if has_contract else context_manager.budget_tokens) * 2))
        pruned_msgs = public_grounded_messages(state, max_body_chars=max_body_chars)
    
    # 2. Get tool definitions for this service
    tools = registry.get_definitions(service=state.service_id, binding=state.workspace_binding)
    attachment_ids = attachment_refs(state)
    first_document_step = bool(document_tool_refs(state)) and state.tool_call_count == 0
    if first_document_step:
        tools = [tool for tool in tools if tool.function.name == "document.get_outline"]
    attachment_arguments: dict[str, frozenset[str]] = {}
    if state.workspace_binding is not None and attachment_ids:
        tools, attachment_arguments = _attachment_tool_definitions(tools, attachment_ids)

    # WO-R21-ADD-01: a draft answer produced before the requested references were read is not
    # streamed — bounded enrichment reads the missing references and the grounded answer follows.
    pending_enrichment = False
    if (
        not disable_tools
        and not public_grounding
        and state.references is not None
        and state.references.has_requested
        and state.evidence is not None
        and state.evidence.incomplete
        and int(state.enrichment_attempts or 0) < int(settings.EVIDENCE_ENRICHMENT_LIMIT)
    ):
        pending_enrichment = True

    
    # 3. Call Client (with 1-shot reactive compaction recovery on context overflow)
    model_label = None

    def capture_label(label):
        nonlocal model_label
        model_label = label

    label_options = {'on_model_label': capture_label} if isinstance(client, AIGatewayClient) else {}
    allow_tools = (not disable_tools) and (not public_grounding) and state.tool_call_count < state.max_tool_calls
    call_tools = tools if allow_tools else None
    call_choice = ("required" if first_document_step else "auto") if allow_tools else "none"
    effective_effort = reasoning_effort_override if reasoning_effort_override is not None else state.reasoning_effort

    import inspect
    from app.schemas.gateway import ModelReply

    def _structured_kwarg_name(c) -> Optional[str]:
        try:
            params = inspect.signature(c.execute).parameters
            if "_structured" in params:
                return "_structured"
            if "return_reply" in params:
                return "return_reply"
            return None
        except Exception:
            return None

    structured_kw = _structured_kwarg_name(client)
    accumulated_usage: Dict[str, int] = {}
    streamed_any_delta = False

    async def _guarded_on_delta(chunk: str):
        nonlocal streamed_any_delta
        streamed_any_delta = True
        if on_delta is not None:
            await on_delta(chunk)

    def _merge_usage(raw_u: Any) -> None:
        if raw_u is None:
            return
        if hasattr(raw_u, "model_dump"):
            raw_u = raw_u.model_dump()
        if not isinstance(raw_u, dict):
            return
        in_tok = raw_u.get("input_tokens") if raw_u.get("input_tokens") is not None else raw_u.get("prompt_tokens")
        out_tok = raw_u.get("output_tokens") if raw_u.get("output_tokens") is not None else raw_u.get("completion_tokens")
        tot_tok = raw_u.get("total_tokens")
        for key, val in (("input_tokens", in_tok), ("output_tokens", out_tok), ("total_tokens", tot_tok)):
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                accumulated_usage[key] = accumulated_usage.get(key, 0) + val

    async def _invoke_client(msgs, *, tools_arg, choice_arg, delta_cb):
        kw = dict(label_options)
        if structured_kw:
            kw[structured_kw] = True
        raw = await client.execute(
            messages=msgs,
            tools=tools_arg,
            tool_choice=choice_arg,
            stream=state.stream,
            reasoning_effort=effective_effort,
            on_reasoning_delta=on_reasoning_delta,
            on_content_delta=delta_cb,
            **kw,
        )
        if isinstance(raw, ModelReply):
            _merge_usage(raw.usage)
            if raw.model_label:
                capture_label(raw.model_label)
            return raw.content, raw.tool_calls, raw.finish_reason
        return raw

    try:
        content, tool_calls, finish_reason = await _invoke_client(
            pruned_msgs,
            tools_arg=call_tools,
            choice_arg=call_choice,
            delta_cb=(_guarded_on_delta if on_delta is not None else None) if not pending_enrichment else None,
        )
    except Exception as exc:
        if not is_context_overflow_error(exc):
            raise
        context_manager.bump_calibration_on_overflow(state.service_id)
        pruned_msgs, _ = context_manager.compact_with_report(
            pruned_msgs,
            budget_tokens=effective_input_budget,
            route=state.service_id,
            force_ratio=0.72,
        )
        model_label = None
        # FIX-207: Preserve effective_effort on retry and prevent duplicate streaming deltas
        retry_delta_cb = None if streamed_any_delta else (_guarded_on_delta if on_delta is not None else None)
        content, tool_calls, finish_reason = await _invoke_client(
            pruned_msgs,
            tools_arg=call_tools,
            choice_arg=call_choice,
            delta_cb=retry_delta_cb,
        )
    if public_grounding and not content and not tool_calls:
        model_label = None
        retry_delta_cb = None if streamed_any_delta else (_guarded_on_delta if on_delta is not None else None)
        content, tool_calls, finish_reason = await _invoke_client(
            pruned_msgs,
            tools_arg=None,
            choice_arg="none",
            delta_cb=retry_delta_cb,
        )
    if public_grounding and not content and not tool_calls:
        raise RuntimeError("Public document answer was empty")

    enrichment_keys: list[str] = []
    if allow_tools and not tool_calls and state.references is not None and state.references.has_requested:
        enrichment_calls, enrichment_keys = _enrichment_tool_calls(state)
        if enrichment_calls:
            tool_calls = enrichment_calls
            content = None
    
    new_messages = list(state.messages)
    assistant_msg = GatewayMessage(
        role="assistant",
        content=content,
        tool_calls=tool_calls if tool_calls else None,
    )
    new_messages.append(assistant_msg)
    
    updates: Dict[str, Any] = {
        "messages": new_messages,
        "step_count": state.step_count,
        "pending_tool_calls": tool_calls or [],
        "usage": accumulated_usage,
    }
    if enrichment_keys:
        updates["enrichment_attempts"] = int(state.enrichment_attempts or 0) + 1
        updates["enrichment_attempted_keys"] = list(dict.fromkeys(list(state.enrichment_attempted_keys or []) + enrichment_keys))
    
    if tool_calls:
        updates["status"] = "running"
    else:
        updates["status"] = "completed"
        updates["final_answer"] = content or ""
        updates["model_label"] = model_label

    if allow_tools and tool_calls and attachment_arguments:
        attachment_id = attachment_ids[0] if len(attachment_ids) == 1 else None
        tool_calls = [
            _bind_attachment_arguments(call, attachment_arguments[call.function.name], attachment_id)
            if call.function.name in attachment_arguments else call
            for call in tool_calls
        ]
        updates["pending_tool_calls"] = tool_calls
        if new_messages and new_messages[-1].role == "assistant" and new_messages[-1].tool_calls:
            new_messages[-1].tool_calls = tool_calls

    return updates

async def tool_node(
    state: AgentState,
    on_tool_event=None,
    *,
    validated_calls=None,
    constraints=None,
    timeout_override=None,
    approved_context_budget=None,
) -> Dict[str, Any]:
    """Executes pending (or strictly ValidatedAction-approved) tool calls and updates message state."""
    calls = list(validated_calls) if validated_calls is not None else list(state.pending_tool_calls)
    approved_ids = {c.id for c in calls}
    unapproved_calls = [c for c in state.pending_tool_calls if c.id not in approved_ids] if validated_calls is not None else []
    new_traces = list(state.traces)
    new_messages = list(state.messages)
    citations = list(state.citations)
    attachment_ids = attachment_refs(state)
    document_ids = document_tool_refs(state)
    if validated_calls is None and document_ids and state.tool_call_count == 0:
        for index, call in enumerate(calls):
            if call.function.name == "document.get_outline":
                call.function.arguments = json.dumps({"doc_id": document_ids[min(index, len(document_ids) - 1)]})
        if new_messages and new_messages[-1].role == "assistant":
            new_messages[-1].tool_calls = calls
    new_tool_count = state.tool_call_count + len(calls)
    tracker = EvidenceTracker.from_coverage(state.evidence) if state.evidence is not None else None
    
    if on_tool_event:
        for call in calls:
            await on_tool_event("started", call, state.step_count)

    # Execute batch of calls concurrently with dynamic per-tool token_budget
    from app.core.runtime_config import resolve_route_budget
    active_budget = resolve_route_budget(state.service_id)
    route_input_budget = active_budget.input_budget_tokens if active_budget else context_manager.budget_tokens
    if approved_context_budget is not None:
        route_input_budget = min(route_input_budget, int(approved_context_budget))
    elif constraints is not None:
        route_input_budget = min(route_input_budget, int(constraints.context_budget_tokens))
    est_used_tokens = max(0, sum(len((m.content or "")) for m in state.messages) // 4)
    # WO-R21-ADD-01: reserve the final synthesis budget before handing tokens to tool results.
    synthesis_reserve = max(0, int(state.synthesis_reserve_tokens or 0))
    evidence_budget = route_input_budget
    if synthesis_reserve:
        evidence_budget = max(512, route_input_budget - min(synthesis_reserve, max(0, route_input_budget - 512)))
    if tracker is not None:
        tracker.set_budget(
            SynthesisBudget(
                reserved_tokens=synthesis_reserve,
                evidence_tokens=max(0, evidence_budget),
                total_input_budget_tokens=max(0, route_input_budget),
            )
        )
    remaining_input_tokens = max(0, evidence_budget - est_used_tokens)
    # HF-03: Compute Document Tool's own tool_token_budget from the clamped route_input_budget!
    per_tool_budget = context_manager.compute_tool_token_budget(
        state.messages,
        budget_tokens=evidence_budget,
        route=state.service_id,
        tool_count=max(1, len(calls)),
    )
    if per_tool_budget is not None:
        per_tool_budget = min(int(per_tool_budget), remaining_input_tokens // max(1, len(calls)))
    from app.core.auth import current_execution_scope, current_run_approvals, current_run_conversation_id

    owner_token = document_owner_id.set(state.owner_id)
    budget_token = tool_token_budget.set(per_tool_budget)
    # CR-02, R2-INT-FIX-03-BE & R2-BETA-HOTFIX-02-BE: Propagate verified scope, approvals & conversation_id
    scope_token = current_execution_scope.set(state.execution_scope) if state.execution_scope is not None else None
    approvals_token = current_run_approvals.set(frozenset(state.approvals or ()))
    conv_token = current_run_conversation_id.set(state.conversation_id)
    workspace_token = current_run_workspace_binding.set(state.workspace_binding)
    try:
        results = await dispatcher.execute_batch(
            calls=calls,
            step=state.step_count,
            service=state.service_id,
            timeout=timeout_override,
            allowed_document_ids=document_ids,
            input_budget_tokens=evidence_budget,
            remaining_input_tokens=remaining_input_tokens,
            constraints=constraints,
        )
    finally:
        current_run_workspace_binding.reset(workspace_token)
        current_run_conversation_id.reset(conv_token)
        current_run_approvals.reset(approvals_token)
        if scope_token is not None:
            current_execution_scope.reset(scope_token)
        tool_token_budget.reset(budget_token)
        document_owner_id.reset(owner_token)
    
    for res in results:
        new_traces.append(res.trace)
        if tracker is not None:
            try:
                tracker.record_tool_result(
                    res.name, dict(res.trace.arguments or {}), res.content, is_error=res.is_error
                )
            except (ValueError, TypeError, AttributeError):
                pass
        if res.name in ('document.read_section','document.read_chunk') and not res.is_error:
            try:
                data = json.loads(res.content);doc_id=data.get('doc_id') or data.get('document_id')
                chunk_id=data.get('chunk_id') or data.get('section_id')
                if doc_id in state.context_refs and chunk_id:
                    key=doc_id+'/'+chunk_id
                    if not any(c['key']==key for c in citations):
                        citations.append({'key':key,'doc_id':doc_id,'chunk_id':chunk_id,
                                          'title':state.document_titles.get(doc_id,'첨부 문서'),
                                          'page':(data.get('source') or {}).get('page')})
            except (ValueError,TypeError,AttributeError):
                pass
        # Append tool message response
        new_messages.append(
            GatewayMessage(
                role="tool",
                content=res.content,
                tool_call_id=res.tool_call_id,
                name=res.name,
            )
        )
        if on_tool_event:
            await on_tool_event("completed", res, state.step_count)

    # R1-FIX-01.12: Record explicit rejected tool messages for any pending calls excluded from ValidatedAction
    for rej in unapproved_calls:
        new_messages.append(
            GatewayMessage(
                role="tool",
                content=json.dumps({"error": True, "code": "tool_call_not_approved"}, ensure_ascii=False),
                tool_call_id=rej.id,
                name=rej.function.name,
            )
        )

    updates = {
        "messages": new_messages,
        "tool_call_count": new_tool_count,
        'citations': citations,
        "pending_tool_calls": [],
        "traces": new_traces,
        "status": "running",
    }
    if tracker is not None:
        updates["evidence"] = tracker.coverage()
    return updates

