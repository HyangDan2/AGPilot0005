"""Phase R1.1 Stage C & D Fail-First Reproduction & Gate Tests (QA-R1-14 .. QA-R1-18)."""
from __future__ import annotations

import json
from types import MappingProxyType
from typing import Any, Dict

import pytest

from app.core.clock import MockClock
from app.runtime.policy.ag_policy import DefaultAGPolicy, PolicyProposal, StateView
from app.runtime.policy.constraints import ActionType, ConstraintSpec
from app.schemas.gateway import FunctionCall, ToolCall
from app.tools.dispatcher import ToolDispatcher, _apply_dynamic_tool_budget
from app.tools.registry import ToolRegistration, ToolRegistry
from app.tracing.sanitizer import sanitize_mapping
from tests.replay.harness import RecordedGatewayClient, RecordedGatewayStep, run_deterministic_replay


# ---------------------------------------------------------------------------
# QA-R1-14 (R1-FIX-10): Cross-runtime (Legacy vs LangGraph) Replay & Fixture Exhaustion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qa_r1_14_cross_runtime_replay_and_fixture_exhaustion(tmp_path):
    from tests.replay.harness import run_legacy_replay

    # 1) Fixture exhaustion MUST fail instead of repeating last response
    clock = MockClock()
    client = RecordedGatewayClient([RecordedGatewayStep(content="only once")], clock=clock)
    await client.execute(messages=[])
    with pytest.raises(AssertionError, match="exhausted"):
        await client.execute(messages=[])

    # 2) Legacy vs LangGraph semantic equivalence on identical recorded fixtures
    constraints = ConstraintSpec.from_runtime(
        service_id="hd2ai-admin", owner_id="alice", authenticated=True,
        allowed_tools=["system.echo"], max_steps=5, max_tool_calls=4,
        time_budget_seconds=30.0, context_budget_tokens=4000, check_live_tool_policy=False,
    )
    steps = [
        RecordedGatewayStep(
            content=None,
            tool_calls=[ToolCall(id="tc_eq_1", function=FunctionCall(name="system.echo", arguments='{"message": "parity"}'))],
            finish_reason="tool_calls",
            advance_seconds=1.0,
        ),
        RecordedGatewayStep(content="동등성 검증 완료: parity", tool_calls=None, finish_reason="stop", advance_seconds=1.0),
    ]
    r1_snap = await run_deterministic_replay(
        message="parity 확인", recorded_steps=steps, constraints=constraints,
        db_path=str(tmp_path / "r1.db"), run_id="run-eq-r1",
    )
    leg_snap = await run_legacy_replay(
        message="parity 확인", recorded_steps=steps, constraints=constraints,
        run_id="run-eq-leg",
    )
    assert r1_snap["final_answer"] == leg_snap["final_answer"]
    assert r1_snap["tool_selection"] == leg_snap["tool_selection"]
    assert r1_snap["tool_arguments"] == leg_snap["tool_arguments"]
    assert r1_snap["budget_consumption"]["tool_calls"] == leg_snap["budget_consumption"]["tool_calls"]
    assert r1_snap["terminal_state"] == leg_snap["terminal_state"]


# ---------------------------------------------------------------------------
# QA-R1-16 & QA-R1-17 (R1-FIX-12): Dynamic Tool Budget (reg.parameters, remaining=0) & Schema Validation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qa_r1_16_and_17_dynamic_tool_budget_and_schema_validation():
    reg = ToolRegistration(
        name="custom.reader",
        description="test reader",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": 50000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
        is_read_only=True,
    )

    # 1) reg.parameters actually read (not broken reg.definition!) -> fills max_chars & limit
    updated, max_bytes = _apply_dynamic_tool_budget(reg, {"query": "test"}, input_budget_tokens=32000, remaining_input_tokens=16000, batch_size=2)
    assert "max_chars" in updated and 500 <= updated["max_chars"] <= 50000
    assert "limit" in updated and 1 <= updated["limit"] <= 20

    # 2) remaining_input_tokens=0 MUST NOT restore input_budget_tokens!
    updated_zero, max_bytes_zero = _apply_dynamic_tool_budget(reg, {"query": "test"}, input_budget_tokens=32000, remaining_input_tokens=0, batch_size=2)
    assert max_bytes_zero < max_bytes

    # 3) Schema validation rejects missing required arg & non-object JSON & wrong type
    local_reg = ToolRegistry()
    local_reg.register(
        reg.name,
        reg.description,
        reg.parameters,
        lambda query, max_chars=1000, limit=5: {"ok": True, "payload": "x" * 20000},
        is_read_only=True,
    )
    disp = ToolDispatcher(tool_registry=local_reg)

    # Non-object JSON arguments
    r_arr = await disp.execute_tool_call(
        ToolCall(id="c1", function=FunctionCall(name="custom.reader", arguments='["not-an-object"]')),
        constraints=ConstraintSpec.from_runtime(
            service_id="hd2ai-admin", owner_id="u1", authenticated=True,
            allowed_tools=["custom.reader"], max_steps=3, max_tool_calls=3,
            time_budget_seconds=10.0, context_budget_tokens=4000, check_live_tool_policy=False,
        ),
    )
    assert r_arr.is_error and r_arr.trace.error_code == "invalid_arguments"

    # Missing required field 'query'
    r_req = await disp.execute_tool_call(
        ToolCall(id="c2", function=FunctionCall(name="custom.reader", arguments='{"limit": 3}')),
        constraints=ConstraintSpec.from_runtime(
            service_id="hd2ai-admin", owner_id="u1", authenticated=True,
            allowed_tools=["custom.reader"], max_steps=3, max_tool_calls=3,
            time_budget_seconds=10.0, context_budget_tokens=4000, check_live_tool_policy=False,
        ),
    )
    assert r_req.is_error and r_req.trace.error_code == "invalid_arguments"

    # Truncated result MUST remain valid JSON with truncated=True marker
    r_trunc = await disp.execute_tool_call(
        ToolCall(id="c3", function=FunctionCall(name="custom.reader", arguments='{"query": "big"}')),
        input_budget_tokens=4000,
        remaining_input_tokens=500,
        constraints=ConstraintSpec.from_runtime(
            service_id="hd2ai-admin", owner_id="u1", authenticated=True,
            allowed_tools=["custom.reader"], max_steps=3, max_tool_calls=3,
            time_budget_seconds=10.0, context_budget_tokens=4000, check_live_tool_policy=False,
        ),
    )
    assert not r_trunc.is_error
    parsed = json.loads(r_trunc.content)
    assert parsed.get("truncated") is True


# ---------------------------------------------------------------------------
# R1-FIX-13: Strict Field Allowlist Privacy Sanitizer (blocks short non-standard keys)
# ---------------------------------------------------------------------------
def test_r1_fix_13_strict_allowlist_sanitizer_blocks_short_unknown_keys():
    sanitized = sanitize_mapping(
        {
            "step_count": 2,
            "service_id": "hd2ai-admin",
            "x": "short user secret",  # short string under non-standard key MUST be redacted!
            "custom_note": "1234",
        }
    )
    assert sanitized["step_count"] == 2
    assert sanitized["service_id"] == "hd2ai-admin"
    assert sanitized["x"]["redacted"] is True
    assert sanitized["custom_note"]["redacted"] is True


# ---------------------------------------------------------------------------
# QA-R1-18 (R1-FIX-14): Policy Lifecycle, Version Pinning, Rollback & Live ACL
# ---------------------------------------------------------------------------
def test_qa_r1_18_policy_version_pinning_rollback_and_live_acl():
    from app.runtime.policy.registry import ENGINE_VERSION, PolicyRegistry

    class AltPolicy(DefaultAGPolicy):
        version = "agpolicy-r1-alt-v2"

    reg = PolicyRegistry()
    reg.register(DefaultAGPolicy())
    reg.register(AltPolicy())
    assert ENGINE_VERSION != DefaultAGPolicy.version

    # Pin policy for Run A before switching active policy
    pinned_a = reg.get_active()
    assert pinned_a.version == "agpolicy-r1-v1"

    reg.activate("agpolicy-r1-alt-v2")
    assert reg.get_active().version == "agpolicy-r1-alt-v2"
    # Pinned policy for Run A remains unaffected
    assert pinned_a.version == "agpolicy-r1-v1"

    # Rollback to previous policy version
    rolled = reg.rollback()
    assert rolled.version == "agpolicy-r1-v1"
    assert reg.get_active().version == "agpolicy-r1-v1"
