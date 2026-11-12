import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.runtime.runner import runner
from app.schemas.agent import AgentRunRequest

@pytest.mark.asyncio
async def test_health_api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/health")
        assert r.status_code == 200
        data = r.json()
        assert data["service"] == "AGCore"
        assert data["status"] == "healthy"

@pytest.mark.asyncio
async def test_agent_run_general_conversation():
    # General greeting, should not call tools
    req = AgentRunRequest(
        message="안녕하세요",
        stream=False,
    )
    session = await runner.create_run(req)
    await runner.start_run(session)
    await session.task
    
    assert session.state.status == "completed"
    assert session.state.step_count == 1
    assert session.state.tool_call_count == 0
    assert "안녕하세요" in (session.state.final_answer or "")

@pytest.mark.asyncio
async def test_agent_run_datetime_tool():
    # User asks for date/time, triggers system.get_datetime
    req = AgentRunRequest(
        message="오늘 현재 날짜와 시간을 알려줘",
        stream=False,
    )
    session = await runner.create_run(req)
    await runner.start_run(session)
    await session.task

    assert session.state.status == "completed"
    assert session.state.tool_call_count >= 1
    assert any(t.tool_name == "system.get_datetime" for t in session.state.traces)

@pytest.mark.asyncio
async def test_agent_run_math_tool():
    # User asks for calculation, triggers math.calculate
    req = AgentRunRequest(
        message="25 * 40 계산해줘",
        stream=False,
    )
    session = await runner.create_run(req)
    await runner.start_run(session)
    await session.task

    assert session.state.status == "completed"
    assert session.state.tool_call_count >= 1
    assert any(t.tool_name == "math.calculate" for t in session.state.traces)

@pytest.mark.asyncio
async def test_agent_run_cancel():
    req = AgentRunRequest(
        message="취소 테스트",
        stream=False,
    )
    session = await runner.create_run(req)
    # Start and cancel immediately
    await runner.start_run(session)
    await runner.cancel_run(session.state.run_id)
    try:
        await session.task
    except asyncio.CancelledError:
        pass
    assert session.state.status == "cancelled"

@pytest.mark.asyncio
async def test_rest_api_agent_run_and_status():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Create run
        r = await client.post("/api/v1/agent/runs", json={"message": "오늘 날짜 알려줘", "stream": False})
        assert r.status_code == 202
        run_data = r.json()
        run_id = run_data["run_id"]
        
        # Wait a bit for execution
        await asyncio.sleep(0.3)
        
        # 2. Get status
        r2 = await client.get(f"/api/v1/agent/runs/{run_id}")
        assert r2.status_code == 200
        status_data = r2.json()
        assert status_data["run_id"] == run_id
        assert status_data["status"] in ("completed", "running")

@pytest.mark.asyncio
async def test_swagger_and_favicon_endpoints():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Default direct docs
        r_docs = await client.get("/docs")
        assert r_docs.status_code == 200
        assert "HD2Lab Agent Core Platform (AGCore)" in r_docs.text
        assert "openapi.json" in r_docs.text

        # Reverse proxy prefix docs
        r_proxy_docs = await client.get("/docs", headers={"x-original-uri": "/admin/agcore/docs"})
        assert r_proxy_docs.status_code == 200
        assert "/admin/agcore/openapi.json" in r_proxy_docs.text

        # Favicons
        r_ico = await client.get("/favicon.ico")
        assert r_ico.status_code in (200, 404)
        r_svg = await client.get("/favicon.svg")
        assert r_svg.status_code == 200
        assert "svg" in r_svg.headers.get("content-type", "")
