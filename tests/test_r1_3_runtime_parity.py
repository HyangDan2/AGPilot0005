"""WO-05-B1 (AM-05): Legacy vs LangGraph (langgraph_r1) 14-Scenario Runtime Parity Suite.

Verifies functional equivalence across API contracts, state transitions, scope/permission
enforcement, tool execution, SSE ordering & replay, trajectory/feedback, and admission control.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.core.auth import ExecutionScope
from app.core.clock import MockClock
from app.core.runtime_config import AgentRuntimeConfig, get_runtime_config
from app.gateway.client import GatewayStreamError
from app.main import app
from app.runtime.context import context_manager
from app.runtime.runner import AgentRunner, runner as global_runner
from app.schemas.agent import AgentRunRequest
from app.schemas.gateway import FunctionCall, GatewayMessage, ModelReply, ModelUsage, ToolCall
from app.tools.policy import tool_policy
from app.tools.registry import registry
from app.tracing.store import TrajectoryEventStore, TrajectoryLogger


ENGINES = ["legacy", "langgraph_r1"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_sse(raw_text: str) -> List[Dict[str, Any]]:
    """Parse raw text/event-stream body into list of {id, event, data}."""
    events: List[Dict[str, Any]] = []
    for block in raw_text.strip().split("\n\n"):
        if not block.strip():
            continue
        ev_id: Optional[str] = None
        ev_name: Optional[str] = None
        data_lines: List[str] = []
        for line in block.splitlines():
            if line.startswith("id:"):
                ev_id = line[3:].strip()
            elif line.startswith("event:"):
                ev_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if ev_name or data_lines:
            raw_data = "\n".join(data_lines)
            try:
                parsed = json.loads(raw_data) if raw_data else {}
            except Exception:
                parsed = {"raw": raw_data}
            events.append({"id": ev_id, "event": ev_name, "data": parsed})
    return events


async def _wait_for_terminal(client: AsyncClient, run_id: str, headers: Dict[str, str], timeout: float = 5.0) -> Dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        resp = await client.get(f"/api/v1/agent/runs/{run_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Run {run_id} did not reach terminal state within {timeout}s: {body}")
        await asyncio.sleep(0.02)


class ScriptedGateway:
    """Deterministic mock AI Gateway client supporting streaming deltas, tool calls, and usage."""

    def __init__(self, script: List[Dict[str, Any]]):
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []

    async def execute(
        self,
        messages,
        tools=None,
        tool_choice=None,
        stream=False,
        reasoning_effort=None,
        on_reasoning_delta=None,
        on_content_delta=None,
        _structured=False,
        **kwargs,
    ):
        idx = len(self.calls)
        self.calls.append(
            {
                "messages": list(messages),
                "tools": [t.function.name for t in (tools or [])],
                "tool_choice": tool_choice,
                "stream": stream,
                "reasoning_effort": reasoning_effort,
            }
        )
        step = self.script[min(idx, len(self.script) - 1)]
        if "wait_event" in step:
            await step["wait_event"].wait()
        if "sleep" in step:
            await asyncio.sleep(step["sleep"])
        if "raise" in step:
            raise step["raise"]

        content = step.get("content")
        tool_calls = step.get("tool_calls")
        finish_reason = step.get("finish_reason", "tool_calls" if tool_calls else "stop")
        model_label = step.get("model_label", "parity-mock-model")
        raw_usage = step.get("usage", {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20})
        usage = ModelUsage(**raw_usage) if isinstance(raw_usage, dict) else raw_usage

        if stream and on_content_delta and content and step.get("emit_deltas", True):
            mid = max(1, len(content) // 2)
            for chunk in (content[:mid], content[mid:]):
                if chunk:
                    await on_content_delta(chunk)

        if _structured:
            return ModelReply(
                content=content,
                tool_calls=tool_calls or [],
                finish_reason=finish_reason,
                model_label=model_label,
                usage=usage,
            )
        return content, tool_calls, finish_reason


def _configure_proxy_auth(monkeypatch) -> Dict[str, str]:
    monkeypatch.setattr(settings, "TRUSTED_PROXY_SECRET", "parity-proxy-secret-01")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "127.0.0.1,::1,testclient")
    monkeypatch.setattr(settings, "CREDENTIAL_SCOPES_JSON", "")
    monkeypatch.setattr(settings, "CREDENTIAL_SECRETS_JSON", "")
    return {
        "X-Trusted-Proxy-Secret": "parity-proxy-secret-01",
        "X-HD2Lab-User": "alice",
        "X-HD2Lab-Service": "hd2ai-admin",
        "X-HD2Lab-Tenant": "tenant-a",
        "X-HD2Lab-Role": "user",
    }


def _setup_parity_env(monkeypatch, tmp_path, engine: str, gw: Any) -> Tuple[TrajectoryLogger, Dict[str, str]]:
    monkeypatch.setattr(settings, "RUNTIME_ENGINE", engine)
    monkeypatch.setattr(settings, "TRAJECTORY_ENABLED", True)
    tstore = TrajectoryEventStore(tmp_path / f"traj_{engine}.db")
    tlogger = TrajectoryLogger(tstore, clock=MockClock())
    monkeypatch.setattr("app.tracing.store.get_trajectory_logger", lambda: tlogger)
    monkeypatch.setattr("app.api.agent.get_trajectory_logger", lambda: tlogger)
    monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, client: gw)
    monkeypatch.setattr("app.runtime.graph.gateway_client", gw)

    async def _no_contract(client, svc):
        return None, False

    monkeypatch.setattr("app.core.runtime_config.get_effective_route_budget", _no_contract)
    headers = _configure_proxy_auth(monkeypatch)
    return tlogger, headers


# ---------------------------------------------------------------------------
# Scenario 1: Single-turn Chat
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_01_single_turn_chat(engine, monkeypatch, tmp_path):
    gw = ScriptedGateway([{"content": "안녕하세요, 단일 턴 응답입니다.", "model_label": "mock-v1"}])
    _, headers = _setup_parity_env(monkeypatch, tmp_path, engine, gw)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1a. Non-streaming POST -> 202 + GET completed
        r = await client.post(
            "/api/v1/agent/runs",
            json={"message": "안녕", "stream": False},
            headers=headers,
        )
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        final = await _wait_for_terminal(client, run_id, headers)
        assert final["status"] == "completed"
        assert final["answer"] == "안녕하세요, 단일 턴 응답입니다."
        assert final["model_label"] == "mock-v1"
        assert final["usage"]["steps"] == 1
        assert final["usage"]["tool_calls"] == 0
        assert final["error"] is None

        # 1b. Streaming POST -> SSE lifecycle & answer.delta + run.completed
        gw2 = ScriptedGateway([{"content": "스트리밍 답변입니다."}])
        monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw2)
        rs = await client.post(
            "/api/v1/agent/runs",
            json={"message": "스트림 테스트", "stream": True},
            headers=headers,
        )
        assert rs.status_code == 200
        events = _parse_sse(rs.text)
        names = [e["event"] for e in events]
        assert names[0] == "run.started"
        assert names[-1] == "run.completed"
        assert "step.started" in names
        assert "model.started" in names
        assert "answer.delta" in names
        assert "model.completed" in names
        assert "step.completed" in names
        deltas = "".join(e["data"]["delta"] for e in events if e["event"] == "answer.delta")
        assert deltas == "스트리밍 답변입니다."
        assert events[-1]["data"]["answer"] == "스트리밍 답변입니다."
        assert events[-1]["data"]["total_steps"] == 1
        assert events[-1]["data"]["total_tool_calls"] == 0

        # 1c. Streaming endpoint when gateway returns full content without invoking delta callback
        gw3 = ScriptedGateway([{"content": "비델타 폴백 답변입니다.", "emit_deltas": False}])
        monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw3)
        rs2 = await client.post(
            "/api/v1/agent/runs",
            json={"message": "비델타 스트림", "stream": True},
            headers=headers,
        )
        assert rs2.status_code == 200
        events2 = _parse_sse(rs2.text)
        deltas2 = "".join(e["data"]["delta"] for e in events2 if e["event"] == "answer.delta")
        assert deltas2 == "비델타 폴백 답변입니다.", f"[{engine}] Missing synthetic answer.delta on non-delta reply"
        assert events2[-1]["event"] == "run.completed"


# ---------------------------------------------------------------------------
# Scenario 2: Multi-turn Chat
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_02_multi_turn_chat(engine, monkeypatch, tmp_path):
    gw = ScriptedGateway([{"content": "이전 대화의 프로젝트명은 Orion입니다."}])
    _, headers = _setup_parity_env(monkeypatch, tmp_path, engine, gw)

    history = [
        {"role": "user", "content": "우리 프로젝트명은 Orion이야."},
        {"role": "assistant", "content": "네, 프로젝트명 Orion을 기억하겠습니다."},
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/agent/runs",
            json={"message": "프로젝트명이 뭐였지?", "history": history, "stream": False},
            headers=headers,
        )
        assert r.status_code == 202
        final = await _wait_for_terminal(client, r.json()["run_id"], headers)
        assert final["status"] == "completed"
        assert "Orion" in final["answer"]
        assert final["usage"]["steps"] == 1
        assert final["usage"]["tool_calls"] == 0

    sent_msgs = gw.calls[0]["messages"]
    roles_and_contents = [(m.role, m.content) for m in sent_msgs if m.role != "system"]
    assert roles_and_contents == [
        ("user", "우리 프로젝트명은 Orion이야."),
        ("assistant", "네, 프로젝트명 Orion을 기억하겠습니다."),
        ("user", "프로젝트명이 뭐였지?"),
    ]


# ---------------------------------------------------------------------------
# Scenario 3: Single Tool Calling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_03_single_tool_calling(engine, monkeypatch, tmp_path):
    gw = ScriptedGateway(
        [
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_math_1",
                        function=FunctionCall(name="math.calculate", arguments='{"expression": "6 * 7"}'),
                    )
                ],
            },
            {"content": "계산 결과는 42입니다."},
        ]
    )
    _, headers = _setup_parity_env(monkeypatch, tmp_path, engine, gw)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rs = await client.post(
            "/api/v1/agent/runs",
            json={"message": "6 * 7 계산해줘", "stream": True},
            headers=headers,
        )
        assert rs.status_code == 200
        events = _parse_sse(rs.text)
        names = [e["event"] for e in events]
        assert names[0] == "run.started"
        assert names[-1] == "run.completed"
        assert names.count("tool.started") == 1
        assert names.count("tool.completed") == 1
        t_start = next(e for e in events if e["event"] == "tool.started")
        t_done = next(e for e in events if e["event"] == "tool.completed")
        assert t_start["data"]["tool_name"] == "math.calculate"
        assert t_start["data"]["tool_call_id"] == "tc_math_1"
        assert t_done["data"]["tool_name"] == "math.calculate"
        assert t_done["data"]["tool_call_id"] == "tc_math_1"
        assert t_done["data"]["is_error"] is False
        assert t_done["data"]["status"] == "completed"
        assert events[-1]["data"]["total_steps"] == 2
        assert events[-1]["data"]["total_tool_calls"] == 1
        assert "42" in events[-1]["data"]["answer"]

    # Second model call received tool output containing 42
    tool_msgs = [m for m in gw.calls[1]["messages"] if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "42" in (tool_msgs[0].content or "")


# ---------------------------------------------------------------------------
# Scenario 4: Sequential Tool Calling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_04_sequential_tool_calling(engine, monkeypatch, tmp_path):
    gw = ScriptedGateway(
        [
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_seq_1",
                        function=FunctionCall(name="system.get_datetime", arguments='{"timezone": "Asia/Seoul"}'),
                    )
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_seq_2",
                        function=FunctionCall(name="math.calculate", arguments='{"expression": "10 + 20"}'),
                    )
                ],
            },
            {"content": "순차 도구 실행 완료: Asia/Seoul 및 30"},
        ]
    )
    _, headers = _setup_parity_env(monkeypatch, tmp_path, engine, gw)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/agent/runs",
            json={"message": "시각 조회 후 계산", "stream": False},
            headers=headers,
        )
        assert r.status_code == 202
        final = await _wait_for_terminal(client, r.json()["run_id"], headers)
        assert final["status"] == "completed"
        assert final["usage"]["steps"] == 3
        assert final["usage"]["tool_calls"] == 2
        assert [t["tool_name"] for t in final["traces"]] == ["system.get_datetime", "math.calculate"]
        assert all(t["status"] == "completed" for t in final["traces"])
        assert "Asia/Seoul" in final["answer"] and "30" in final["answer"]

    # Step 3 model input has both tool messages in order
    tool_msgs = [m for m in gw.calls[2]["messages"] if m.role == "tool"]
    assert [m.name for m in tool_msgs] == ["system.get_datetime", "math.calculate"]


# ---------------------------------------------------------------------------
# Scenario 5: Parallel / Multiple Tool Execution
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_05_parallel_multiple_tool_execution(engine, monkeypatch, tmp_path):
    gw = ScriptedGateway(
        [
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_par_1",
                        function=FunctionCall(name="math.calculate", arguments='{"expression": "2 + 3"}'),
                    ),
                    ToolCall(
                        id="tc_par_2",
                        function=FunctionCall(name="system.get_datetime", arguments='{"timezone": "UTC"}'),
                    ),
                ],
            },
            {"content": "병렬 실행 결과: 5, UTC"},
        ]
    )
    _, headers = _setup_parity_env(monkeypatch, tmp_path, engine, gw)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rs = await client.post(
            "/api/v1/agent/runs",
            json={"message": "두 도구를 동시에 실행", "stream": True},
            headers=headers,
        )
        assert rs.status_code == 200
        events = _parse_sse(rs.text)
        started_ids = [e["data"]["tool_call_id"] for e in events if e["event"] == "tool.started"]
        completed_ids = [e["data"]["tool_call_id"] for e in events if e["event"] == "tool.completed"]
        assert set(started_ids) == {"tc_par_1", "tc_par_2"}
        assert set(completed_ids) == {"tc_par_1", "tc_par_2"}
        assert events[-1]["event"] == "run.completed"
        assert events[-1]["data"]["total_steps"] == 2
        assert events[-1]["data"]["total_tool_calls"] == 2

    tool_msgs = [m for m in gw.calls[1]["messages"] if m.role == "tool"]
    assert {m.tool_call_id for m in tool_msgs} == {"tc_par_1", "tc_par_2"}


# ---------------------------------------------------------------------------
# Scenario 6: Dynamic Tool Discovery
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_06_dynamic_tool_discovery(engine, monkeypatch, tmp_path):
    async def _probe_handler(query: str) -> Dict[str, Any]:
        return {"discovered": True, "echo_query": query}

    dyn_name = "dynamic.parity_probe"
    registry.register(
        name=dyn_name,
        description="동적으로 등록된 패리티 검증 도구",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=_probe_handler,
        is_read_only=True,
    )
    orig_is_allowed = tool_policy.is_tool_allowed

    def _allow_dyn(tool_name: str, service: Optional[str] = None):
        if tool_name == dyn_name:
            return True, None
        return orig_is_allowed(tool_name, service)

    monkeypatch.setattr(tool_policy, "is_tool_allowed", _allow_dyn)
    try:
        gw = ScriptedGateway(
            [
                {
                    "content": None,
                    "tool_calls": [
                        ToolCall(
                            id="tc_dyn_1",
                            function=FunctionCall(name=dyn_name, arguments='{"query": "hello-dyn"}'),
                        )
                    ],
                },
                {"content": "동적 도구 호출 성공: hello-dyn"},
            ]
        )
        _, headers = _setup_parity_env(monkeypatch, tmp_path, engine, gw)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post(
                "/api/v1/agent/runs",
                json={"message": "동적 도구 실행", "stream": False},
                headers=headers,
            )
            assert r.status_code == 202
            final = await _wait_for_terminal(client, r.json()["run_id"], headers)
            assert final["status"] == "completed"
            assert final["usage"]["tool_calls"] == 1
            assert final["traces"][0]["tool_name"] == dyn_name
            assert final["traces"][0]["status"] == "completed"
            assert dyn_name in gw.calls[0]["tools"]
    finally:
        registry.unregister(dyn_name)


# ---------------------------------------------------------------------------
# Scenario 7: Document Analysis & Durable TaskOrchestrator Boundary
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_07_document_analysis_and_durable_orchestrator(engine, monkeypatch, tmp_path):
    gw = ScriptedGateway([{"content": "unused"}])
    _, headers = _setup_parity_env(monkeypatch, tmp_path, engine, gw)

    # Part A: Durable TaskOrchestrator boundary via /api/v1/agent/runs with context_refs
    created_calls: List[Dict[str, Any]] = []
    fake_job_id = "ana_" + ("a1b2c3d4" * 4)

    class StubAnalysisServices:
        async def create(self, owner, service, message, refs, history=None, analysis_refs=None, **kwargs):
            created_calls.append({"owner": owner, "service": service, "refs": list(refs), "tenant_id": kwargs.get("tenant_id")})
            return {
                "job_id": fake_job_id,
                "status": "queued",
                "answer": None,
                "model_calls": 0,
                "document_calls": 0,
                "created_at": 1000.0,
                "error_code": None,
            }

        def get(self, job_id, owner, service, tenant_id=None):
            if job_id != fake_job_id:
                return None
            return {
                "job_id": fake_job_id,
                "status": "completed",
                "answer": "Durable TaskOrchestrator 분석 완료",
                "model_label": "durable-v1",
                "model_calls": 3,
                "document_calls": 5,
                "created_at": 1000.0,
                "error_code": None,
            }

        def cancel(self, job_id, owner, service, tenant_id=None):
            return {"job_id": job_id, "status": "cancelled"}

    stub_svc = StubAnalysisServices()
    monkeypatch.setattr("app.api.agent.services", lambda _app: stub_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 7a. Deferred SSE handoff to Durable TaskOrchestrator (identical regardless of RUNTIME_ENGINE)
        rs = await client.post(
            "/api/v1/agent/runs",
            json={"message": "문서 전체 분석", "context_refs": ["doc_001"], "stream": True},
            headers=headers,
        )
        assert rs.status_code == 200
        events = _parse_sse(rs.text)
        assert [e["event"] for e in events] == ["run.started", "run.deferred"]
        assert events[0]["data"]["executor"] == "document"
        assert events[1]["data"]["job_id"] == fake_job_id
        assert created_calls[0]["refs"] == ["doc_001"]
        assert created_calls[0]["tenant_id"] == "tenant-a"

        # 7b. GET durable run via /api/v1/agent/runs/run-<hex32>
        durable_run_id = "run-" + fake_job_id[4:]
        rg = await client.get(f"/api/v1/agent/runs/{durable_run_id}", headers=headers)
        assert rg.status_code == 200
        assert rg.json()["status"] == "completed"
        assert rg.json()["answer"] == "Durable TaskOrchestrator 분석 완료"

    # Part B: Direct AgentRunner document tool path with context_refs & citations
    async def _fake_outline(doc_id: str):
        return {"status": "completed", "document_name": "Parity Spec.pdf", "sections": [{"section_id": "sec-1", "title": "Intro"}]}

    async def _fake_read_section(doc_id: str, section_id: str, **kwargs):
        return {"doc_id": doc_id, "section_id": section_id, "chunk_id": "chk-1", "content": "핵심 조항 내용", "source": {"page": 2}}

    monkeypatch.setattr("app.adapters.document.document_adapter.get_outline", _fake_outline)
    monkeypatch.setattr("app.adapters.document.document_adapter.read_section", _fake_read_section)

    doc_gw = ScriptedGateway(
        [
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_doc_1",
                        function=FunctionCall(name="document.get_outline", arguments='{"doc_id": "doc_001"}'),
                    )
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_doc_2",
                        function=FunctionCall(
                            name="document.read_section",
                            arguments='{"doc_id": "doc_001", "section_id": "sec-1"}',
                        ),
                    )
                ],
            },
            {"content": "문서 근거에 따르면 핵심 조항이 포함되어 있습니다."},
        ]
    )
    local_runner = AgentRunner()
    scope = ExecutionScope(
        authenticated=True,
        auth_source="trusted_proxy",
        tenant_id="tenant-a",
        owner_id="alice",
        service_id="hd2ai-admin",
        role="user",
    )
    sess = await local_runner.create_run(
        AgentRunRequest(message="문서 핵심 요약", context_refs=["doc_001"], stream=False),
        service_id="hd2ai-admin",
        owner_id="alice",
        caller_scope=scope,
        custom_client=doc_gw,
    )
    await local_runner.start_run(sess)
    await asyncio.wait_for(sess.task, timeout=5.0)
    assert sess.state.status == "completed"
    assert sess.state.tool_call_count == 2
    assert len(sess.state.citations) == 1
    assert sess.state.citations[0]["doc_id"] == "doc_001"
    assert "핵심 조항" in (sess.state.final_answer or "")


# ---------------------------------------------------------------------------
# Scenario 8: Context Budget (Proactive Compaction + Reactive Overflow Recovery)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_08_context_budget(engine, monkeypatch, tmp_path):
    # 8a. Proactive compaction when messages exceed CONTEXT_BUDGET_TOKENS
    gw = ScriptedGateway([{"content": "컨텍스트 압축 후 정상 응답"}])
    _, headers = _setup_parity_env(monkeypatch, tmp_path, engine, gw)
    monkeypatch.setattr(settings, "CONTEXT_BUDGET_TOKENS", 350)
    monkeypatch.setattr(context_manager, "budget_tokens", 350)

    long_history = [
        {"role": "user", "content": f"이전 질문 {i}: " + ("A" * 280)}
        for i in range(6)
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/agent/runs",
            json={"message": "현재 질문입니다.", "history": long_history, "stream": False},
            headers=headers,
        )
        assert r.status_code == 202
        final = await _wait_for_terminal(client, r.json()["run_id"], headers)
        assert final["status"] == "completed", f"[{engine}] Context budget compaction failed: {final}"
        assert final["answer"] == "컨텍스트 압축 후 정상 응답"

    # 8b. Reactive 1-shot compaction recovery on provider context_length_exceeded
    gw_overflow = ScriptedGateway(
        [
            {"raise": RuntimeError("context_length_exceeded: maximum context length reached")},
            {"content": "오버플로 재시도 복구 성공"},
        ]
    )
    monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw_overflow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r2 = await client.post(
            "/api/v1/agent/runs",
            json={"message": "오버플로 복구 테스트", "stream": False},
            headers=headers,
        )
        assert r2.status_code == 202
        final2 = await _wait_for_terminal(client, r2.json()["run_id"], headers)
        assert final2["status"] == "completed"
        assert final2["answer"] == "오버플로 재시도 복구 성공"
        assert len(gw_overflow.calls) == 2


# ---------------------------------------------------------------------------
# Scenario 9: Session Isolation (Tenant / Owner / Service / Admin Scope)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_09_session_isolation(engine, monkeypatch, tmp_path):
    gw = ScriptedGateway([{"content": "격리 테스트 응답"}])
    _, _ = _setup_parity_env(monkeypatch, tmp_path, engine, gw)

    def _hdr(user: str, svc: str, tenant: str, role: str = "user") -> Dict[str, str]:
        return {
            "X-Trusted-Proxy-Secret": "parity-proxy-secret-01",
            "X-HD2Lab-User": user,
            "X-HD2Lab-Service": svc,
            "X-HD2Lab-Tenant": tenant,
            "X-HD2Lab-Role": role,
        }

    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SCOPES_JSON",
        json.dumps(
            {
                "admin-tenant-a": {
                    "allowed_services": ["hd2ai-beta"],
                    "allowed_roles": ["admin"],
                    "allowed_tenants": ["tenant-a"],
                    "owner_delegation": False,
                    "fixed_owner": "admin-a",
                },
                "admin-tenant-b": {
                    "allowed_services": ["hd2ai-beta"],
                    "allowed_roles": ["admin"],
                    "allowed_tenants": ["tenant-b"],
                    "owner_delegation": False,
                    "fixed_owner": "admin-b",
                },
            }
        ),
    )
    monkeypatch.setattr(
        settings,
        "CREDENTIAL_SECRETS_JSON",
        json.dumps(
            {
                "admin-tenant-a": ["tok-admin-a-parity-01"],
                "admin-tenant-b": ["tok-admin-b-parity-01"],
            }
        ),
    )

    alice_hdr = _hdr("alice", "hd2ai-beta", "tenant-a", "user")
    bob_hdr = _hdr("bob", "hd2ai-beta", "tenant-a", "user")
    cross_tenant_hdr = _hdr("alice", "hd2ai-beta", "tenant-b", "user")
    cross_service_hdr = _hdr("alice", "hd2ai-admin", "tenant-a", "user")
    same_tenant_admin_hdr = {
        "X-Service-Token": "tok-admin-a-parity-01",
        "X-HD2Lab-Service": "hd2ai-beta",
        "X-HD2Lab-Role": "admin",
        "X-HD2Lab-Tenant": "tenant-a",
    }
    other_tenant_admin_hdr = {
        "X-Service-Token": "tok-admin-b-parity-01",
        "X-HD2Lab-Service": "hd2ai-beta",
        "X-HD2Lab-Role": "admin",
        "X-HD2Lab-Tenant": "tenant-b",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/agent/runs", json={"message": "hello", "stream": False}, headers=alice_hdr)
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        final = await _wait_for_terminal(client, run_id, alice_hdr)
        assert final["status"] == "completed"

        # Different owner -> 404
        assert (await client.get(f"/api/v1/agent/runs/{run_id}", headers=bob_hdr)).status_code == 404
        # Different tenant -> 404
        assert (await client.get(f"/api/v1/agent/runs/{run_id}", headers=cross_tenant_hdr)).status_code == 404
        # Different service (non-admin) -> 404
        assert (await client.get(f"/api/v1/agent/runs/{run_id}", headers=cross_service_hdr)).status_code == 404
        # Same-tenant admin -> 200
        assert (await client.get(f"/api/v1/agent/runs/{run_id}", headers=same_tenant_admin_hdr)).status_code == 200
        # Cross-tenant admin -> 404 (explicit admin tenant scope!)
        assert (await client.get(f"/api/v1/agent/runs/{run_id}", headers=other_tenant_admin_hdr)).status_code == 404


# ---------------------------------------------------------------------------
# Scenario 10: SSE Streaming / Replay (Last-Event-ID & 410 event_replay_expired)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_10_sse_streaming_and_replay(engine, monkeypatch, tmp_path):
    gw = ScriptedGateway([{"content": "SSE 리플레이 테스트 응답"}])
    _, headers = _setup_parity_env(monkeypatch, tmp_path, engine, gw)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/agent/runs", json={"message": "리플레이", "stream": False}, headers=headers)
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        await _wait_for_terminal(client, run_id, headers)

        # Full stream
        r_full = await client.get(f"/api/v1/agent/runs/{run_id}/events", headers=headers)
        assert r_full.status_code == 200
        all_events = _parse_sse(r_full.text)
        assert len(all_events) >= 3
        assert all_events[0]["event"] == "run.started"
        assert all_events[-1]["event"] == "run.completed"
        first_id = all_events[0]["data"].get("event_id")
        assert first_id is not None

        # Replay from Last-Event-ID = first_id -> strictly events after first_id
        r_replay = await client.get(
            f"/api/v1/agent/runs/{run_id}/events",
            headers={**headers, "Last-Event-ID": first_id},
        )
        assert r_replay.status_code == 200
        replayed = _parse_sse(r_replay.text)
        assert len(replayed) == len(all_events) - 1
        assert replayed[0]["data"].get("event_id") == all_events[1]["data"].get("event_id")
        assert replayed[-1]["event"] == "run.completed"

        # Evict older events from ring buffer -> 410 event_replay_expired
        sess = global_runner.get_session(run_id)
        assert sess is not None
        sess.event_bus._buffer.popleft()
        sess.event_bus._buffer.popleft()
        r_expired = await client.get(
            f"/api/v1/agent/runs/{run_id}/events",
            headers={**headers, "Last-Event-ID": first_id},
        )
        assert r_expired.status_code == 410
        assert r_expired.json()["error_code"] == "event_replay_expired"


# ---------------------------------------------------------------------------
# Scenario 11: Cancellation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_11_cancellation(engine, monkeypatch, tmp_path):
    blocker = asyncio.Event()
    gw = ScriptedGateway([{"wait_event": blocker, "content": "never reached"}])
    _, headers = _setup_parity_env(monkeypatch, tmp_path, engine, gw)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/agent/runs", json={"message": "긴 작업", "stream": False}, headers=headers)
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        await asyncio.sleep(0.05)

        rc = await client.post(f"/api/v1/agent/runs/{run_id}/cancel", headers=headers)
        assert rc.status_code == 200
        assert rc.json()["status"] == "cancelled"

        # Let background task unwind cleanly
        sess = global_runner.get_session(run_id)
        if sess and sess.task:
            try:
                await asyncio.wait_for(sess.task, timeout=2.0)
            except (asyncio.CancelledError, Exception):
                pass

        final = await _wait_for_terminal(client, run_id, headers)
        assert final["status"] == "cancelled"
        assert final["error"] is None, f"[{engine}] Cancelled run must not have error: {final['error']}"

        # SSE terminates with run.cancelled and NO run.completed/run.failed
        rev = await client.get(f"/api/v1/agent/runs/{run_id}/events", headers=headers)
        assert rev.status_code == 200
        ev_names = [e["event"] for e in _parse_sse(rev.text)]
        assert ev_names[-1] == "run.cancelled"
        assert "run.completed" not in ev_names
        assert "run.failed" not in ev_names

        # Second cancel returns 400
        rc2 = await client.post(f"/api/v1/agent/runs/{run_id}/cancel", headers=headers)
        assert rc2.status_code == 400


# ---------------------------------------------------------------------------
# Scenario 12: Timeout / Error Recovery
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_12_timeout_and_error_recovery(engine, monkeypatch, tmp_path):
    # 12a. Run timeout
    gw_slow = ScriptedGateway([{"sleep": 1.0, "content": "too late"}])
    _, headers = _setup_parity_env(monkeypatch, tmp_path, engine, gw_slow)
    short_cfg = AgentRuntimeConfig(max_tool_calls=8, max_agent_steps=8, run_timeout_seconds=30.0)
    object.__setattr__(short_cfg, "run_timeout_seconds", 0.08)
    orig_cfg = get_runtime_config()
    monkeypatch.setattr("app.core.runtime_config._ACTIVE_CONFIG", short_cfg)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/agent/runs", json={"message": "타임아웃", "stream": False}, headers=headers)
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        final = await _wait_for_terminal(client, run_id, headers)
        assert final["status"] == "failed"
        assert final["error"]["code"] == "run_timeout"

        rev = await client.get(f"/api/v1/agent/runs/{run_id}/events", headers=headers)
        ev_names = [e["event"] for e in _parse_sse(rev.text)]
        assert ev_names[-1] == "run.failed"

    # Restore normal timeout for 12b & 12c
    monkeypatch.setattr("app.core.runtime_config._ACTIVE_CONFIG", orig_cfg)

    # 12b. GatewayStreamError handling
    gw_err = ScriptedGateway([{"raise": GatewayStreamError("stream_broken")}])
    monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw_err)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r2 = await client.post("/api/v1/agent/runs", json={"message": "게이트웨이 오류", "stream": False}, headers=headers)
        assert r2.status_code == 202
        final2 = await _wait_for_terminal(client, r2.json()["run_id"], headers)
        assert final2["status"] == "failed"
        assert final2["error"]["code"] == "gateway_stream_broken"

    # 12c. Tool error recovery: invalid math expression -> tool fails -> model recovers on next step
    gw_rec = ScriptedGateway(
        [
            {
                "content": None,
                "tool_calls": [
                    ToolCall(
                        id="tc_bad_math",
                        function=FunctionCall(name="math.calculate", arguments='{"expression": "1 / 0"}'),
                    )
                ],
            },
            {"content": "0으로 나눌 수 없어 오류를 복구해 안내합니다."},
        ]
    )
    monkeypatch.setattr("app.runtime.graph.gateway_for_service", lambda svc, c: gw_rec)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r3 = await client.post("/api/v1/agent/runs", json={"message": "1/0 계산", "stream": False}, headers=headers)
        assert r3.status_code == 202
        final3 = await _wait_for_terminal(client, r3.json()["run_id"], headers)
        assert final3["status"] == "completed"
        assert final3["usage"]["tool_calls"] == 1
        assert final3["traces"][0]["status"] == "failed"
        assert "복구" in final3["answer"]


# ---------------------------------------------------------------------------
# Scenario 13: Trajectory / Feedback (201, 409 duplicate, 503 fail-closed)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_13_trajectory_and_feedback(engine, monkeypatch, tmp_path):
    gw = ScriptedGateway([{"content": "피드백 대상 응답"}])
    tlogger, headers = _setup_parity_env(monkeypatch, tmp_path, engine, gw)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/agent/runs", json={"message": "피드백 테스트", "stream": False}, headers=headers)
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        final = await _wait_for_terminal(client, run_id, headers)
        assert final["status"] == "completed"

        # 13a. First feedback -> 201 Created
        fb1 = await client.post(
            f"/api/v1/agent/runs/{run_id}/feedback",
            json={"feedback_type": "thumbs_up"},
            headers=headers,
        )
        assert fb1.status_code == 201
        assert fb1.json()["reward_signal"] == 1.0
        assert fb1.json()["run_id"] == run_id

        # 13b. Duplicate feedback -> 409 Conflict
        fb2 = await client.post(
            f"/api/v1/agent/runs/{run_id}/feedback",
            json={"feedback_type": "thumbs_up"},
            headers=headers,
        )
        assert fb2.status_code == 409
        assert fb2.json()["detail"] == "duplicate_feedback"

        # 13c. Trajectory reconstruction
        tr = await client.get(f"/api/v1/agent/runs/{run_id}/trajectory", headers=headers)
        assert tr.status_code == 200
        traj_body = tr.json()
        assert traj_body["run_id"] == run_id
        assert len(traj_body["feedback"]) == 1
        if engine == "langgraph_r1":
            assert len(traj_body["steps"]) >= 1
            assert traj_body["terminal_status"] == "completed"

        # 13d. Persistence unavailable -> 503 fail-closed
        def _boom(*args, **kwargs):
            raise RuntimeError("sqlite_io_error")

        monkeypatch.setattr(tlogger.store, "append_feedback_atomic", _boom)
        fb3 = await client.post(
            f"/api/v1/agent/runs/{run_id}/feedback",
            json={"feedback_type": "thumbs_down"},
            headers=headers,
        )
        assert fb3.status_code == 503
        assert fb3.json()["detail"] == "feedback_persistence_unavailable"


# ---------------------------------------------------------------------------
# Scenario 14: Concurrent Run Admission (429 active_run_limit_exceeded)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ENGINES)
async def test_parity_14_concurrent_run_admission(engine, monkeypatch, tmp_path):
    gate = asyncio.Event()
    gw = ScriptedGateway([{"wait_event": gate, "content": "동시성 완료"}])
    _, headers = _setup_parity_env(monkeypatch, tmp_path, engine, gw)

    isolated_runner = AgentRunner(max_active_runs=2)
    monkeypatch.setattr("app.api.agent.runner", isolated_runner)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post("/api/v1/agent/runs", json={"message": "run 1", "stream": False}, headers=headers)
        r2 = await client.post("/api/v1/agent/runs", json={"message": "run 2", "stream": False}, headers=headers)
        assert r1.status_code == 202
        assert r2.status_code == 202

        # 3rd concurrent run exceeds max_active_runs=2 -> 429
        r3 = await client.post("/api/v1/agent/runs", json={"message": "run 3", "stream": False}, headers=headers)
        assert r3.status_code == 429
        assert r3.json()["detail"] == "active_run_limit_exceeded"

        # Unblock active runs -> both complete -> 4th run admitted
        gate.set()
        for rid in (r1.json()["run_id"], r2.json()["run_id"]):
            fin = await _wait_for_terminal(client, rid, headers)
            assert fin["status"] == "completed"

        r4 = await client.post("/api/v1/agent/runs", json={"message": "run 4", "stream": False}, headers=headers)
        assert r4.status_code == 202
        fin4 = await _wait_for_terminal(client, r4.json()["run_id"], headers)
        assert fin4["status"] == "completed"
